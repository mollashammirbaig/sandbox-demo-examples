# Speaking script — Sandboxes on DigitalOcean

**Audience:** Director of Engineering
**Length target:** ~10 minutes (conversational pace)
**Goal:** Show that we can run a production-grade code-execution sandbox platform on the DigitalOcean stack using e2b-dev/infra, and that we understand it deeply enough to operate, scale, and trust it.

> Tip: hit the **bold** lines hard — they're the moments where the slide and the story land together. Pause after each demo screen.

---

## Slide 1 — Cover

> "Thanks for the time. Today I want to walk you through something we've been building on top of DigitalOcean: a self-hosted code execution sandbox platform — the same kind of thing that powers Claude's code interpreter, OpenAI's analysis tool, Replit's agents — but running entirely on our infrastructure."
>
> "The whole thing is live right now at `api.shammirbaig.online`. **Two droplets, one wildcard cert, zero external dependencies.** I'll show you what works, how it works, and what it would take to take this to production."

---

## Slide 2 — What we deployed (Two droplets)

> "Here's what's running today. **Two droplets in NYC3.** On the left, an 8 GB control plane droplet — that's our API, the proxy, Postgres, Redis, Consul, Nomad. On the right, a 16 GB worker droplet — that's where the actual sandboxes live."
>
> "They talk to each other over a private gRPC link on port 5008. Caddy at the edge handles TLS for both `api.shammirbaig.online` and `*.sandbox.shammirbaig.online` — that wildcard is the key to the whole preview-URL story I'll show you in a minute."
>
> "Every sandbox is a **Firecracker microVM** — that's the same hypervisor AWS uses for Lambda. Each one gets its own Linux kernel, 512 MB of RAM, and its own tap network device. This isn't a container, it's not a chroot. Hardware-level isolation."
>
> "Right now we're getting about 28 concurrent sandboxes per worker, RAM-bound. I'll come back to that number."

---

## Slide 3 — Demo: cold start

> "Let me start with the demo that matters most to me as an operator. This is `e2b_benchmark.py`, running against our live infrastructure right now."
>
> *(point to the SDK box)*
>
> "Three lines of Python. Import, create, run, kill. That's the whole API surface for a basic use case."
>
> *(point to the timing table)*
>
> "And here's what one full lifecycle looks like end-to-end. **`create` — 382 milliseconds.** That's everything: API call, auth, Postgres insert, gRPC to the worker, pull the template from cache, allocate a tap device and an IP, boot a Firecracker VM, wait for envd to come up. All under 400 ms."
>
> "First Python execution is slower at 750 ms — that's the cold path through the gRPC stack. The second one is 240 ms — process manager is warm. Total round-trip from `Sandbox.create` to `kill` is under two seconds."
>
> "For context: spinning up a Docker container on a shared host is faster on the happy path, but you don't get a kernel of your own, you don't get RAM you can snapshot, and you can't safely run untrusted code. **This is the trade we made — half a second for hardware isolation.**"

---

## Slide 4 — Demo: preview URLs

> "Now here's the feature that surprised me the most. Every sandbox automatically gets a public URL for every port."
>
> "I write an HTML file inside the sandbox, start a Python HTTP server on port 18765, and then I just call `sb.get_host(18765)` — and I get back a URL like this: `<sandbox-id>-18765.sandbox.shammirbaig.online`. I paste that into a browser, and it works."
>
> "Behind the scenes: wildcard TLS hits Caddy, Caddy hands off to the client-proxy, client-proxy looks up the sandbox in Consul, and tunnels the request into the VM's tap interface to envd, which forwards to the port. **No tunneling client. No port-forwarding setup. No SSH.** Just a URL."
>
> "This is the piece that unlocks agentic use cases — you can build a website inside a sandbox and let a user click on it. Or run a Jupyter kernel and embed it. Or expose a model server."

---

## Slide 5 — Demo: run anything

> "Quick one. The base template ships with six runtimes — Python, Node, Ruby, Go, Rust, Bash. So you can `run` arbitrary commands in any of them out of the box."
>
> "You also get three execution modes: buffered (wait for it to finish), streaming (stdout and stderr come back live as the process runs), and background (fire and forget, get a PID, kill it later). Streaming is what makes the agent UX feel responsive — your AI can show output as it happens, not after the fact."

---

## Slide 6 — Demo: snapshot & restore

> "This is the one I want you to remember. Snapshot and restore."
>
> "I create a sandbox, write a marker file to disk, **and store a secret string in the heap of a running Python process. Then I delete the file from disk.** So the only place that secret exists is in RAM."
>
> "I take a snapshot. The platform serializes the entire VM — disk overlay and RAM — to object storage."
>
> "Now I create a *new* sandbox from that snapshot ID. Different VM, different host process, different tap device. I curl the HTTP server inside it — **and the same secret comes back.** The Python process resumed, with its heap intact, on a different machine."
>
> "Why does that matter? You can pre-warm sandboxes — load a model, import heavy dependencies — snapshot them, and then resume from snapshot in milliseconds instead of paying the cold-start cost on every request. For an AI agent platform, that's the difference between feeling instant and feeling sluggish."

---

## Slide 7 — Demo: capacity

> "Last demo. We need to know what one of these workers can hold."
>
> "I wrote `e2b_droplet_capacity_benchmark.py` — it ramps up additively, five sandboxes at a time, and stops when the success rate drops below 80%."
>
> *(point to the budget table on the left)*
>
> "Here's the budget. RAM is the bottleneck — 14 GB usable, 512 MB per VM, that's about 28. CPU is essentially free at idle. Disk is fine. NBD has a hard cap of 64. Network has 64 IPs in our /24 with /30 per VM."
>
> *(point to the big number)*
>
> "**Measured ceiling: about 28 sandboxes** on a 16 GB worker. That matches the theoretical RAM budget exactly, which is what you want — it means there's no surprise lurking somewhere else in the stack. If we want more headroom, we either grow the droplet or add more workers."

---

## Slide 8 — Five capabilities, recapped

> "Now that you've seen the demos, let me consolidate. Five things this platform gives you that ordinary container infrastructure does not."
>
> *(read the five cards top to bottom, one breath each)*
>
> "Hardware-level isolation — Firecracker microVM, not a container. Sub-second cold starts — under 300 ms boot from a cached template. Per-sandbox preview URLs — wildcard TLS plus the proxy stack. Snapshot and restore — the one with the secret in heap. And it's all self-hosted on DigitalOcean — our region, our cert, our data."
>
> *(point to the trust-model strip)*
>
> "And the trust model is straightforward: tokens at the public edge, network topology and iptables for internal hops, and sandbox traffic that never leaves the worker's tap subnet."

---

## Slide 9 — Architecture

> "Now let's go under the hood. Three lanes."
>
> "**Internet on the left** — that's the SDK, that's a browser hitting a preview URL. Both go through Caddy with TLS 1.3 and a Let's Encrypt wildcard cert."
>
> "**Control plane in the middle** — Caddy fronts the API on port 3000 and the client-proxy on 3002. The API is a Gin service. Postgres holds teams and sandbox records. Redis caches authenticated tokens for sixty seconds. Consul and Nomad handle discovery and job placement. Then there's a private gRPC channel on port 5008 to the worker."
>
> "**Worker on the right** — the orchestrator runs as root because it's the thing that creates tap devices and execs Firecracker. Templates pulled from object storage, copy-on-write overlays per sandbox, and then a fleet of Firecracker microVMs each with their own envd daemon."
>
> *(point to the three flow chips at the bottom)*
>
> "Three flows you should remember: **auth** terminates at the API. **exec** crosses gRPC to the worker. **preview** is a totally separate path through the client-proxy. Different traffic, different code paths."

---

## Slide 10 — The request path

> "Let me trace one `Sandbox.create` from end to end. Six hops."
>
> *(walk left to right across the cards)*
>
> "**One:** SDK posts to `/sandboxes` with a Bearer token. **Two:** Caddy terminates TLS, forwards to the API. **Three:** API extracts the token, hashes it, checks Redis, falls back to Postgres, inserts a new sandbox row. **Four:** API calls the orchestrator over gRPC — the orchestrator pulls the template, allocates a tap device, picks an IP, and execs Firecracker. **Five:** the guest kernel boots, envd starts listening on port 49983 — under 300 ms. **Six:** API returns the sandbox ID to the SDK."
>
> "End to end, **300 to 800 milliseconds.** First command into the running VM is another 50 to 150. Kill is around 100. And we top out at about 28 of these on a 16 GB worker."

---

## Slide 11 — Anatomy of one sandbox

> "Quick zoom into a single sandbox, because the abstraction matters."
>
> "Inside one Firecracker microVM you have **its own Linux kernel** — pinned by the template, you control the version. **envd** runs as the in-VM agent, exposing a Connect RPC interface on 49983. The filesystem is a **copy-on-write overlay** — base template plus per-sandbox writes. Networking is a **tap device with a /30** in the worker's private 192.168.0 subnet, NAT only — sandboxes can't see each other and can't reach the host. And a **process tree** spawned via the ProcessStart RPC."
>
> "Total host footprint is about 640 MB per sandbox. Boot under 300 ms. Kill around 100 ms."
>
> *(gesture to the right column)*
>
> "Three ways to reach into a running VM: **exec** for commands, **files** for I/O, and **HTTP** for the preview URL story. Same VM, three doors."

---

## Slide 12 — Authentication

> "Now the part you'll want to grill me on. Auth."
>
> "Five stages. **A:** extract the Bearer token from the header. **B:** validate the format — must start with `e2b_`. **C:** hash with SHA-256 and check Redis with a sixty-second TTL. **D:** on miss, query Postgres `teams` table by the hashed token. **E:** inject the team into the request context for downstream handlers."
>
> *(point to the bottom row)*
>
> "Keys are 128 bits of entropy with an `e2b_` prefix, **only ever transmitted over TLS, only stored as a SHA-256 hash.** The plaintext key never touches disk. The teams table tracks tier, blocked status, created-at — standard stuff."
>
> "And separately from authentication, every handler does authorization checks: sandbox ownership by team, tier-level concurrency limits, and template access for private templates."

---

## Slide 13 — Auth failure modes

> "And here's how it fails. The thing I want to highlight: **the 401 message is intentionally identical** for 'token not found' and 'malformed prefix' and 'team blocked'. We don't want to leak whether a guess was close — same response, different reasons internally."
>
> "If Redis goes down, we fall back to Postgres transparently. If Postgres goes down with a warm Redis, we serve cached tokens best-effort and fail closed for new ones."
>
> *(point to the right card)*
>
> "Internal hops — orchestrator gRPC and envd — don't do token auth. **They trust network topology and iptables.** The orchestrator port binds 0.0.0.0 but is firewalled to the API host only. envd is reachable only from inside the worker, via the client-proxy, which authenticates by sandbox ID. That's a deliberate choice — every internal token check is overhead we don't pay."

---

## Slide 14 — What's next

> "So that's where we are. **Two droplets today.** Working, measured, instrumented."
>
> "Three things we'd want to do next."
>
> "**One — horizontal scale.** Add worker droplets behind Consul. The orchestrator already supports multi-host placement; we just haven't lit it up. This is the cheapest unlock."
>
> "**Two — custom templates.** Per-team rootfs and kernel. Pre-warmed snapshots so cold starts drop under 100 ms. This is where the snapshot/restore story I showed earlier becomes a product feature."
>
> "**Three — region affinity.** We're in NYC3 today. Adding SFO3 and LON1 is a routing problem, not an architecture problem. Steer creates by team region, keep data resident."
>
> *(pause, look up)*
>
> "All built on `e2b-dev/infra`, all on DigitalOcean, all reproducible from a Terraform module. Happy to walk through any piece in more detail. Questions?"

---

## Q&A — likely questions, brief answers

- **Why not Kubernetes?** Firecracker doesn't need it. The orchestrator is the scheduler; Nomad handles job placement on the control side. Kubernetes adds a control plane we don't need for this workload.
- **Why DigitalOcean and not AWS?** Cost predictability, simpler networking, and we already operate here. The architecture would port to any provider with KVM-capable instances and object storage.
- **What about cost per sandbox?** ~$0.0006 per sandbox-minute on the current worker — pure infra cost, no margin. Mostly RAM.
- **Multi-tenant safety?** Hardware isolation via KVM, separate kernels per VM, NAT-only networking, no shared filesystem. Same model AWS Lambda uses.
- **Why 512 MB?** Default for the base template. Tunable per sandbox, up to host RAM minus overhead.
- **Disaster recovery?** Templates and snapshots in object storage are durable. Postgres is backed up nightly. Workers are stateless beyond running sandboxes — losing one drops the live VMs but doesn't lose data.
- **Scale ceiling?** Per-worker we're RAM-bound at ~28. Per-region, the control plane is the bottleneck — Postgres + Redis + Nomad. We've sized it for ~10 workers (≈280 concurrent sandboxes) on the current control droplet.

### Operations & reliability

- **What happens when a worker dies?** Live sandboxes on it are gone — they're ephemeral by design. Postgres still has the row, the snapshot (if one was taken) is still in object storage, so the SDK gets a clean "sandbox not found" and can recreate. No silent data loss.
- **What about a control-plane outage?** New sandboxes can't be created and the API is down, but **existing sandboxes keep running** — envd inside each VM is independent. Preview URLs still resolve as long as the client-proxy and Caddy are up. Auth falls back from Redis to Postgres automatically.
- **Observability?** Structured logs from Caddy, the API, and the orchestrator into ClickHouse. Metrics on create latency, success rate, concurrent count, and per-sandbox CPU/RAM. The capacity benchmark we ran is also our smoke test.
- **How do you patch the guest kernel?** Build a new template with the patched kernel, mark it as the new `base`, drain old sandboxes on timeout. Existing snapshots stay on the old kernel until rebuilt.
- **Noisy-neighbor risk?** Firecracker exposes cgroup limits per VM — CPU shares, memory hard cap, IO throttling. We currently run with memory caps only; CPU is best-effort because we're idle-bound, not CPU-bound at our scale.

### Security

- **Container escape concerns?** Firecracker has a much smaller attack surface than runc — it's ~50k lines of Rust, no shared kernel, no privileged syscalls into the host kernel from the guest. AWS Lambda relies on the same boundary for untrusted multi-tenant code.
- **What if a sandbox tries to phone home?** Outbound traffic egresses the worker's NAT. We can deny-list at the worker firewall, or run with `egress=false` for fully air-gapped sandboxes (no internet, only API-driven file/exec).
- **Audit trail?** Every sandbox create/kill is a Postgres row with team, template, IP, timestamps. Every API call is logged at Caddy and the API. ClickHouse for long-retention analytics.
- **Secrets in sandboxes?** Don't put production secrets in. The model is: sandbox is untrusted code, secrets stay outside, you pass scoped tokens in via env vars at create time.
- **Compliance — SOC 2, HIPAA?** Inheriting from DigitalOcean's controls plus our own logging and access policies. Real answer is "depends on the workload and what we're certifying" — happy to scope this with security.

### Product & roadmap

- **Who's the customer?** Internal first — any team that wants to run untrusted or model-generated code. External later if there's pull. The "code interpreter for our agents" use case is the obvious one.
- **Why build, not buy?** E2B's hosted offering exists. We chose self-hosted for data residency, cost predictability at scale, and because the e2b-infra repo is open source — we own the operational story instead of renting it.
- **What's missing vs. the hosted product?** Multi-region, custom templates UI, team management UI, billing — anything that's a SaaS product wrapper around the same core. We have the runtime; we don't have the control surface.
- **Time to production?** Two to four weeks of hardening — multi-worker scale-out, alerting, runbooks, on-call rotation, capacity planning per tenant. The runtime itself is solid.
- **Cost trajectory?** Linear in workers. The control plane scales sub-linearly until ~10 workers, then we'd shard. No per-sandbox SaaS markup means our break-even vs. hosted is roughly 50 concurrent sandboxes sustained.

### Comparisons

- **vs. Docker / containerd?** Containers share the host kernel — fine for trusted code, unsafe for arbitrary code-gen. Docker also doesn't give you snapshot-restore of running RAM, and preview URLs need a separate ingress story.
- **vs. gVisor?** gVisor intercepts syscalls in user-space — strong isolation, but a real performance hit on syscall-heavy workloads, and no clean snapshot story. Firecracker is faster and the boundary is the hardware virtualization extension, not a syscall filter.
- **vs. Fly.io machines?** Same primitive — Firecracker microVMs. Fly is a hosted product with a global anycast layer; we're rolling the equivalent on DO for our own infra. Architecturally very similar; ops model is the difference.
- **vs. Modal / Replit?** Modal is a Python-first serverless product on top of gVisor. Replit's Nix-based environment is a different abstraction (persistent dev envs, not ephemeral sandboxes). E2B is closest to "Lambda for arbitrary code with a filesystem and a network."
