# Sandbox Network Architecture

This document explains how E2B assigns network addresses to Firecracker microVMs, why every sandbox reports the same IP (`169.254.0.21`) when you run `hostname -I` inside it, and how the unique per-sandbox addresses are actually structured.

---

## Quick answer

`169.254.0.21` is a **hardcoded constant** in the orchestrator source:

```go
// packages/orchestrator/pkg/sandbox/network/slot.go:185
func (s *Slot) NamespaceIP() string {
    return "169.254.0.21"
}
```

It is the guest-side IP of the TAP device that connects the Firecracker VM to its host network namespace. Every sandbox uses the same value because each sandbox lives in its own **isolated Linux network namespace** — there is no conflict, just like two computers can both have `192.168.1.1` on their private LANs.

---

## The three layers of network isolation

Each sandbox slot (index `N`) creates three separate address spaces on the worker host.

```
┌──────────────────────────────────────────────────────────────────┐
│  WORKER HOST  143.198.25.149                                     │
│                                                                  │
│  Layer 1 — Default namespace (host-global, unique per sandbox)   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  HostIP   = 10.11.0.N          /32  (unique per sandbox)  │  │
│  │  vpeer-N  = 10.12.0.(N×2+1)   /31  (unique per sandbox)  │  │
│  └────────────────────────────────────────────────────────────┘  │
│           │ veth pair (vpeer ↔ veth)                             │
│  Layer 2 — Network namespace  ns-N  (isolated per sandbox)       │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  veth-N   = 10.12.0.(N×2)     /31  (unique per sandbox)  │  │
│  │                                                            │  │
│  │  tap0 host-end = 169.254.0.22  /30  (same every sandbox)  │  │
│  └────────────────────────────────────────────────────────────┘  │
│           │ TAP device (kernel ↔ Firecracker VMM)               │
│  Layer 3 — Firecracker guest (what the VM sees)                  │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  eth0 / tap0 = 169.254.0.21   /30  ← hostname -I output   │  │
│  │  default gw  = 169.254.0.22                                │  │
│  └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### Layer 1 — Host default namespace

The orchestrator allocates IPs from two `/16` blocks, one per address role:

| Pool | CIDR | Env override |
|------|------|--------------|
| Host IPs | `10.11.0.0/16` | `SANDBOXES_HOST_NETWORK_CIDR` |
| Vrt (veth/vpeer) IPs | `10.12.0.0/16` | `SANDBOXES_VRT_NETWORK_CIDR` |

For slot index `N`:

```
HostIP  = 10.11.0.N          (one address, /32)
vEthIP  = 10.12.0.(N×2)      (namespace side of veth pair)
vPeerIP = 10.12.0.(N×2+1)    (host side of veth pair)
```

These are the **unique** IPs. The orchestrator and client-proxy use `HostIP` to route traffic to a specific sandbox.

### Layer 2 — Network namespace `ns-N`

For each sandbox the orchestrator creates a dedicated Linux network namespace named `ns-N`. Inside it:

- The `veth-N` device gets `10.12.0.(N×2)` — the inner end of the veth pair that bridges namespace ↔ host.
- A TAP device (`tap0`) is created. The **host side** of the tap gets `169.254.0.22`.
- The TAP connects the network namespace to the Firecracker process for that VM.

Because this namespace is isolated, its `169.254.0.22` does not conflict with any other sandbox's namespace, even though they all use the same address.

### Layer 3 — Inside the Firecracker VM (guest)

Firecracker configures the guest's network interface using values from the slot:

```
Interface:   tap0 (appears as eth0 inside the guest)
Guest IP:    169.254.0.21 / 30
Gateway:     169.254.0.22
Subnet:      169.254.0.20/30
```

The `/30` block `169.254.0.20/30` has exactly four addresses:

```
169.254.0.20  — network address
169.254.0.21  — guest (the VM itself)
169.254.0.22  — host-side tap (the gateway)
169.254.0.23  — broadcast
```

This is why `hostname -I` always returns `169.254.0.21`. It is the VM's view of its own side of the "cable" that connects it to the host. Because every VM sits in its own namespace, the same address can be reused infinitely without collision.

---

## IP address table for sandboxes 1–5

| Slot | HostIP (10.11…) | vEthIP (10.12…) | vPeerIP (10.12…) | Guest IP (inside VM) | Gateway (inside VM) |
|------|-----------------|-----------------|------------------|----------------------|---------------------|
| 1 | 10.11.0.1 | 10.12.0.2 | 10.12.0.3 | 169.254.0.21 | 169.254.0.22 |
| 2 | 10.11.0.2 | 10.12.0.4 | 10.12.0.5 | 169.254.0.21 | 169.254.0.22 |
| 3 | 10.11.0.3 | 10.12.0.6 | 10.12.0.7 | 169.254.0.21 | 169.254.0.22 |
| 4 | 10.11.0.4 | 10.12.0.8 | 10.12.0.9 | 169.254.0.21 | 169.254.0.22 |
| 5 | 10.11.0.5 | 10.12.0.10 | 10.12.0.11 | 169.254.0.21 | 169.254.0.22 |

The guest-side column is always the same. The unique identity of a sandbox, from the host's perspective, is `HostIP = 10.11.0.N`.

---

## How traffic reaches a sandbox (full request path)

```
Browser or SDK
    │
    │  HTTPS  →  api.shammirbaig.online  (Caddy, port 443)
    ↓
Control plane — e2b-do-control-01
    │  API process (:3000) validates key, resolves sandbox → slot index N
    │  Looks up HostIP = 10.11.0.N for sandbox N
    ↓
Worker — e2b-do-worker-01  (143.198.25.149)
    │  Client-proxy (:3002) receives wildcard domain request
    │    e.g.  sbx_abc123-3000.sandbox.shammirbaig.online
    │  Extracts sandbox ID → looks up HostIP in orchestrator catalog
    │
    │  TCP connection to  10.11.0.N:49983  (envd port)
    ↓
Host default namespace on worker
    │  Packet arrives at veth/vpeer pair → forwarded into ns-N
    ↓
Network namespace ns-N
    │  Packet crosses from veth-N into tap0 host-end (169.254.0.22)
    ↓
Firecracker VMM process
    │  Delivers packet to VM guest via TAP device
    ↓
Guest (sandbox VM)
    │  Received on eth0 (169.254.0.21)
    │  envd daemon listening on :49983 handles the request
    │  envd forwards to port 3000 inside the guest
    ↓
Your running process inside the sandbox
```

---

## Why `169.254.0.x`?

`169.254.0.0/16` is the IANA **link-local** range (RFC 3927). It has two properties that make it ideal here:

1. **Not routable** — no router will forward a packet with a `169.254.x.x` source or destination. It physically cannot leak out of the host.
2. **No DHCP required** — link-local addresses are self-assigned, so the orchestrator can hard-code them without any IPAM infrastructure.

The `/30` point-to-point link between the host-side tap (`169.254.0.22`) and the guest (`169.254.0.21`) is just a private cable. It exists once per network namespace, and namespaces are fully isolated from each other at the kernel level.

---

## Slot pool capacity

With the default `10.12.0.0/16` vrt CIDR:

```
Total IPs in /16       = 65 536
Addresses per slot     = 2  (veth + vpeer, one /31 block)
Reserved (overflow)    = 2
Max slots              = (65 536 / 2) − 2  =  32 766 sandboxes
```

The practical ceiling is much lower — NBD device slots (`nbds_max`, default 64 on this setup) and available RAM are the binding constraints.

---

## Checking real per-sandbox IPs from the host

Because `169.254.0.21` is always the same inside the VM, to identify which physical slot a sandbox occupies you query from the **host side**:

```bash
# SSH into e2b-do-worker-01

# List all sandbox network namespaces
ls /var/run/netns/          # or ip netns list

# Show the veth pair for slot 3
ip addr show veth-3
# → inet 10.12.0.7/31  (vPeerIP for slot 3)

# Show the HostIP route for slot 3
ip route show 10.11.0.3
# → 10.11.0.3 dev veth-3

# Enter namespace ns-3 and inspect the tap
sudo ip netns exec ns-3 ip addr
# → inet 169.254.0.22/30 dev tap0  (host-end of tap, unique namespace)
# → inet 10.12.0.6/31 dev veth-3

# Enter the guest via envd to confirm guest-side IP
# (inside the VM)
# → eth0: inet 169.254.0.21/30
```

---

## Source code references

| File | What it defines |
|------|----------------|
| [packages/orchestrator/pkg/sandbox/network/slot.go](packages/orchestrator/pkg/sandbox/network/slot.go) | `Slot` struct, all IP allocation math, `NamespaceIP() = "169.254.0.21"` |
| [packages/orchestrator/pkg/sandbox/network/pool.go](packages/orchestrator/pkg/sandbox/network/pool.go) | Network slot pool (`NewSlotsPoolSize=32`, `ReusedSlotsPoolSize=100`) |
| [packages/orchestrator/pkg/sandbox/network/storage_local.go](packages/orchestrator/pkg/sandbox/network/storage_local.go) | In-process slot storage for local dev |
| [packages/orchestrator/pkg/sandbox/network/storage_kv.go](packages/orchestrator/pkg/sandbox/network/storage_kv.go) | Consul-backed slot storage for production |
| [packages/orchestrator/pkg/sandbox/network/firewall.go](packages/orchestrator/pkg/sandbox/network/firewall.go) | nftables rules per namespace (egress filtering) |
| [packages/orchestrator/pkg/server/sandboxes.go](packages/orchestrator/pkg/server/sandboxes.go) | Orchestrator gRPC server, `maxStartingInstancesPerNode=3` |
