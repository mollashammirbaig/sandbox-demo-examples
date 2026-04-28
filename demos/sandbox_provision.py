#!/usr/bin/env python3
"""Provision N sandboxes on the self-hosted Droplet, print their internal IPs
and port-3000 preview URLs, then explain the request flow.

Usage:
  python3 scripts/e2b_sandbox_provision.py           # 10 sandboxes
  python3 scripts/e2b_sandbox_provision.py --count 20
  python3 scripts/e2b_sandbox_provision.py --count 5 --keep   # don't kill at end

Env (required – targets the DigitalOcean self-hosted stack, see .env):
  E2B_API_KEY
  E2B_API_URL
  E2B_DOMAIN
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # install python-dotenv or set env vars manually

try:
    import httpx
    from e2b import Sandbox
except ImportError:
    print("Install:  python3 -m pip install e2b httpx", file=sys.stderr)
    raise SystemExit(1)

# ── config ────────────────────────────────────────────────────────────────────

PREVIEW_PORT    = 3000


def api_kwargs() -> dict:
    return {
        "api_key": os.getenv("E2B_API_KEY"),
        "api_url": os.getenv("E2B_API_URL"),
    }


def create_kwargs() -> dict:
    return {
        **api_kwargs(),
        "domain": os.getenv("E2B_DOMAIN"),
    }


# ── per-sandbox result ────────────────────────────────────────────────────────

@dataclass
class SandboxRecord:
    index:       int
    sandbox_id:  str = ""
    internal_ip: str = ""
    preview_url: str = ""
    create_ms:   float = 0.0
    error:       str = ""
    sandbox:     object = field(default=None, repr=False)


# ── worker ───────────────────────────────────────────────────────────────────

def provision_one(index: int) -> SandboxRecord:
    rec = SandboxRecord(index=index)
    t0 = time.perf_counter()
    try:
        sb = Sandbox.create("base", timeout=120, **create_kwargs())
        rec.create_ms = (time.perf_counter() - t0) * 1000
        rec.sandbox_id = sb.sandbox_id
        rec.sandbox = sb

        # ── internal IP: run hostname -I inside the guest ─────────────────
        # The orchestrator assigns a tap-device IP (e.g. 192.168.0.x) to the
        # Firecracker VM.  hostname -I prints all interface IPs; we take the
        # first non-loopback one.
        try:
            r = sb.commands.run("hostname -I", timeout=15)
            ips = [ip.strip() for ip in r.stdout.split() if ip.strip() and not ip.startswith("127.")]
            rec.internal_ip = ips[0] if ips else "(none)"
        except Exception as e:
            rec.internal_ip = f"(error: {e})"

        # ── preview URL for port 3000 ─────────────────────────────────────
        # get_host(port) returns the hostname the client-proxy exposes for
        # that sandbox+port combo.  The SDK builds:
        #   <sandbox_id>-<port>.<domain>
        # Traffic path:  your browser → client-proxy :3002 → envd :49983 → port 3000 inside VM
        try:
            host = sb.get_host(PREVIEW_PORT)
            rec.preview_url = f"https://{host}"
        except Exception as e:
            rec.preview_url = f"(error: {e})"

    except Exception as e:
        rec.create_ms = (time.perf_counter() - t0) * 1000
        rec.error = str(e)

    return rec


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=10,
                    help="Number of sandboxes to provision (default 10, max 20)")
    ap.add_argument("--keep", action="store_true",
                    help="Leave sandboxes running instead of killing them")
    ap.add_argument("--workers", type=int, default=5,
                    help="Parallel provisioning workers (default 5)")
    args = ap.parse_args()

    count   = max(1, min(20, args.count))
    workers = max(1, min(args.workers, count))

    print(f"\n{'═'*62}")
    print(f"  E2B Sandbox Provision — {count} sandboxes on self-hosted Droplet")
    print(f"{'═'*62}")
    print(f"  API  : {os.getenv('E2B_API_URL')}")
    print(f"  Domain: {os.getenv('E2B_DOMAIN')}")
    print(f"  Workers: {workers}  |  Keep running: {args.keep}")
    print(f"{'═'*62}\n")

    records: list[SandboxRecord] = [None] * count
    t_wall = time.perf_counter()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(provision_one, i): i for i in range(count)}
        for fut in as_completed(futures):
            rec = fut.result()
            records[rec.index] = rec
            status = "✓" if not rec.error else "✗"
            print(f"  [{status}] #{rec.index+1:02d}  id={rec.sandbox_id or 'FAILED':<36}"
                  f"  {rec.create_ms:7.0f} ms")

    wall_ms = (time.perf_counter() - t_wall) * 1000
    ok = [r for r in records if not r.error]
    fail = [r for r in records if r.error]

    # ── result table ─────────────────────────────────────────────────────────
    print(f"\n{'─'*100}")
    print(f"  {'#':<4}  {'Sandbox ID':<38}  {'Internal IP':<18}  Preview URL (port {PREVIEW_PORT})")
    print(f"{'─'*100}")
    for r in records:
        if r.error:
            print(f"  {r.index+1:<4}  {'ERROR':<38}  {'':<18}  {r.error[:40]}")
        else:
            print(f"  {r.index+1:<4}  {r.sandbox_id:<38}  {r.internal_ip:<18}  {r.preview_url}")
    print(f"{'─'*100}")
    print(f"\n  Provisioned: {len(ok)}/{count}  |  Failed: {len(fail)}")
    print(f"  Wall time (parallel): {wall_ms/1000:.1f}s  |"
          f"  Avg create: {sum(r.create_ms for r in ok)/len(ok):.0f} ms" if ok else "")

    # ── flow explanation ──────────────────────────────────────────────────────
    print(f"""
{'═'*62}
  HOW EACH REQUEST FLOWS THROUGH THE CONTROL PLANE
{'═'*62}

  ┌──────────────────────────────────────────────────────────┐
  │  1. SDK / curl → POST https://api.<YOUR_DOMAIN>          │
  │     /sandboxes  (port 443, TLS termination at Caddy)     │
  │                                                          │
  │  2. API process (packages/api, :3000 inside Droplet)     │
  │     • Validates API key against Postgres                 │
  │     • Records sandbox row in Postgres                    │
  │     • Calls Orchestrator gRPC :5008                      │
  │                                                          │
  │  3. Orchestrator (packages/orchestrator, sudo, :5008)    │
  │     • Pulls template rootfs / kernel from GCS/local      │
  │     • Creates Linux tap device, assigns IP from pool     │
  │       (e.g. 192.168.0.x — that is the "internal IP")     │
  │     • Starts Firecracker microVM with that tap           │
  │     • envd (daemon) boots inside the VM on :49983        │
  │     • Registers sandbox in the in-memory catalog         │
  │                                                          │
  │  4. API returns {{ sandboxID, … }} to SDK                  │
  │                                                          │
  │  ── EVERY subsequent SDK call (commands.run, files, …) ──│
  │                                                          │
  │  5. SDK → HTTPS → API → gRPC → Orchestrator              │
  │     (control plane touched on every operation)           │
  │                                                          │
  │  ── Preview URL (port {PREVIEW_PORT}) ───────────────────────────│
  │                                                          │
  │  6. Browser → https://<id>-{PREVIEW_PORT}.<YOUR_DOMAIN>              │
  │     Caddy wildcard → client-proxy :3002                  │
  │     client-proxy reads sandbox ID from hostname          │
  │     → looks up orchestrator via Consul / static config   │
  │     → TCP-proxies to envd :49983 inside the VM           │
  │     → envd forwards to port {PREVIEW_PORT} inside the guest     │
  │                                                          │
  │  Key insight: the INTERNAL IP (step 3) is a private      │
  │  network address on the Droplet host — never routable    │
  │  from the internet.  All external access goes through    │
  │  the client-proxy wildcard path (step 6).                │
  └──────────────────────────────────────────────────────────┘
""")

    # ── cleanup ───────────────────────────────────────────────────────────────
    if not args.keep and ok:
        print(f"  Killing {len(ok)} sandboxes...", end=" ", flush=True)
        def _kill(r: SandboxRecord):
            try:
                r.sandbox.kill()
            except Exception:
                pass
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_kill, ok))
        print("done.")
    elif args.keep:
        print(f"  --keep: {len(ok)} sandboxes left running (kill manually or wait for timeout).")

    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
