#!/usr/bin/env python3
"""Capacity benchmark: find how many Firecracker sandboxes can run concurrently
on e2b-do-worker-01 (16 GB / 200 GB / NYC3, 143.198.25.149).

Strategy (additive ramp)
------------------------
  Each wave adds --step NEW sandboxes to the already-running pool.
  All sandboxes from earlier waves stay alive throughout.

  Wave 1: create 5  → 5  alive
  Wave 2: create 5  → 10 alive
  Wave 3: create 5  → 15 alive
  ...
  Stop when a new wave's batch success rate drops below --threshold.
  The total alive before that wave is the measured concurrent ceiling.

Theoretical ceiling (16 GB worker, base template)
--------------------------------------------------
  Usable RAM  : ~14 336 MB (16 GB − OS − API/Orchestrator overhead)
  Per VM      : ~250 MB (128 MB guest + kernel + envd + CoW page cache)
  RAM ceiling : ~57 VMs
  NBD limit   : 64 slots  (set via modprobe nbd nbds_max=64)
  Effective   : min(57, 64) ≈ 55 sandboxes

Usage
-----
  # default: step=5, push until failure (no --max needed)
  python3 scripts/e2b_droplet_capacity_benchmark.py

  # explicit ceiling
  python3 scripts/e2b_droplet_capacity_benchmark.py --max 40

  # faster ramp
  python3 scripts/e2b_droplet_capacity_benchmark.py --step 10 --workers 10

  # leave sandboxes alive for manual inspection
  python3 scripts/e2b_droplet_capacity_benchmark.py --no-cleanup

Env (all optional):
  E2B_API_KEY   required (see .env)
  E2B_API_URL   required (see .env)
  E2B_DOMAIN    required (see .env)
"""

from __future__ import annotations

import argparse
import os
import statistics
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

# ── defaults ──────────────────────────────────────────────────────────────────

WORKER_TOTAL_RAM_MB  = 16_384
WORKER_OS_OVERHEAD   = 2_048   # OS + orchestrator + API
VM_RAM_MB            = 250     # per Firecracker base sandbox
NBD_MAX              = 64      # modprobe nbd nbds_max=64
THEORETICAL_CEIL     = min((WORKER_TOTAL_RAM_MB - WORKER_OS_OVERHEAD) // VM_RAM_MB, NBD_MAX)


def api_kwargs() -> dict:
    return {
        "api_key": os.getenv("E2B_API_KEY"),
        "api_url": os.getenv("E2B_API_URL"),
    }


def create_kwargs() -> dict:
    return {**api_kwargs(), "domain": os.getenv("E2B_DOMAIN")}


# ── data types ────────────────────────────────────────────────────────────────

@dataclass
class SandboxResult:
    label:      str            # e.g. "W2-03"
    sandbox_id: str   = ""
    create_ms:  float = 0.0
    cmd_ms:     float = 0.0
    cmd_ok:     bool  = False
    error:      str   = ""
    sandbox:    object = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return not self.error and self.cmd_ok


@dataclass
class WaveResult:
    wave_num:  int
    new_batch: list[SandboxResult]   # sandboxes created this wave
    wall_ms:   float
    total_alive_before: int          # alive from all previous waves

    @property
    def n_ok(self) -> int:
        return sum(1 for r in self.new_batch if r.ok)

    @property
    def n_fail(self) -> int:
        return len(self.new_batch) - self.n_ok

    @property
    def success_pct(self) -> float:
        return 100.0 * self.n_ok / len(self.new_batch) if self.new_batch else 0.0

    @property
    def total_alive_after(self) -> int:
        return self.total_alive_before + self.n_ok

    @property
    def create_p50(self) -> float | None:
        vals = [r.create_ms for r in self.new_batch if r.ok]
        return statistics.median(vals) if vals else None

    @property
    def create_p95(self) -> float | None:
        vals = sorted(r.create_ms for r in self.new_batch if r.ok)
        if not vals:
            return None
        idx = max(0, int(len(vals) * 0.95) - 1)
        return vals[idx]

    @property
    def cmd_p50(self) -> float | None:
        vals = [r.cmd_ms for r in self.new_batch if r.ok]
        return statistics.median(vals) if vals else None


# ── worker ────────────────────────────────────────────────────────────────────

def create_and_probe(label: str) -> SandboxResult:
    rec = SandboxResult(label=label)
    t0 = time.perf_counter()
    try:
        sb = Sandbox.create("base", timeout=120, **create_kwargs())
        rec.create_ms = (time.perf_counter() - t0) * 1000
        rec.sandbox_id = sb.sandbox_id
        rec.sandbox = sb

        t1 = time.perf_counter()
        r = sb.commands.run("echo alive", timeout=20)
        rec.cmd_ms = (time.perf_counter() - t1) * 1000
        rec.cmd_ok = r.exit_code in (0, None) and "alive" in (r.stdout or "")

    except Exception as exc:
        rec.create_ms = (time.perf_counter() - t0) * 1000
        rec.error = str(exc)
    return rec


def kill_result(rec: SandboxResult) -> None:
    try:
        if rec.sandbox is not None:
            rec.sandbox.kill()
    except Exception:
        pass


# ── wave runner ───────────────────────────────────────────────────────────────

def run_wave(wave_num: int, step: int, total_alive_before: int, workers: int) -> WaveResult:
    t0 = time.perf_counter()
    batch: list[SandboxResult] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        labels = [f"W{wave_num}-{i+1:02d}" for i in range(step)]
        futures = {pool.submit(create_and_probe, lbl): lbl for lbl in labels}
        for fut in as_completed(futures):
            r = fut.result()
            batch.append(r)
            tick = "✓" if r.ok else "✗"
            sid  = r.sandbox_id[:26] if r.sandbox_id else "FAILED"
            err  = f"  ERR: {r.error[:44]}" if r.error else ""
            print(f"    {tick} {r.label:<8}  {sid:<28}"
                  f"  create {r.create_ms:6.0f} ms"
                  f"  cmd {r.cmd_ms:5.0f} ms{err}")

    batch.sort(key=lambda r: r.label)
    return WaveResult(
        wave_num=wave_num,
        new_batch=batch,
        wall_ms=(time.perf_counter() - t0) * 1000,
        total_alive_before=total_alive_before,
    )


# ── kill helpers ──────────────────────────────────────────────────────────────

def kill_all(all_waves: list[WaveResult], workers: int) -> None:
    live = [r for w in all_waves for r in w.new_batch if r.ok]
    if not live:
        return
    print(f"\n  Cleaning up {len(live)} sandboxes ...", end=" ", flush=True)
    with ThreadPoolExecutor(max_workers=max(workers, 10)) as pool:
        list(pool.map(kill_result, live))
    print("done.")


# ── summary helpers ───────────────────────────────────────────────────────────

def _ms(v: float | None) -> str:
    return f"{v:.0f}" if v is not None else "n/a"


def print_summary(all_waves: list[WaveResult], ceiling: int | None) -> None:
    total_created = sum(len(w.new_batch) for w in all_waves)
    total_ok      = sum(w.n_ok          for w in all_waves)

    print(f"\n{'═'*72}")
    print(f"  BENCHMARK SUMMARY")
    print(f"  Worker : e2b-do-worker-01  ·  16 GB RAM / 8 vCPU / 200 GB / NYC3")
    print(f"  Control: e2b-do-control-01 ·  8 GB RAM / 4 vCPU / 100 GB / NYC3")
    print(f"{'═'*72}")

    hdr = (f"  {'Wave':>4}  {'Added':>5}  {'OK':>4}  {'Fail':>4}  "
           f"{'Total alive':>11}  {'Success%':>9}  "
           f"{'Wall (s)':>8}  {'p50 create':>10}  {'p95 create':>10}  {'p50 cmd':>7}")
    print(f"\n{hdr}")
    print(f"  {'─'*4}  {'─'*5}  {'─'*4}  {'─'*4}  {'─'*11}  {'─'*9}  "
          f"{'─'*8}  {'─'*10}  {'─'*10}  {'─'*7}")

    for w in all_waves:
        print(f"  {w.wave_num:>4}  {len(w.new_batch):>5}  {w.n_ok:>4}  {w.n_fail:>4}  "
              f"{w.total_alive_after:>11}  {w.success_pct:>8.0f}%  "
              f"{w.wall_ms/1000:>8.1f}  "
              f"{_ms(w.create_p50):>10}  {_ms(w.create_p95):>10}  {_ms(w.cmd_p50):>7}")

    print(f"\n  Total sandboxes attempted : {total_created}")
    print(f"  Total succeeded           : {total_ok}")

    if ceiling is not None:
        print(f"\n  ► Measured concurrent ceiling : {ceiling} sandboxes")
    else:
        print(f"\n  ► All waves passed — raise --max to find the real ceiling.")

    pct_of_theory = f"({100*ceiling/THEORETICAL_CEIL:.0f}% of theoretical {THEORETICAL_CEIL})" \
        if ceiling else ""
    print(f"  ► Theoretical ceiling         : ~{THEORETICAL_CEIL} sandboxes  "
          f"(14 GB usable ÷ {VM_RAM_MB} MB/VM, capped by NBD={NBD_MAX})")
    if ceiling and pct_of_theory:
        print(f"                                  {pct_of_theory}")

    print(f"""
  ── Resource constraints on e2b-do-worker-01 ────────────────────────
  RAM    : 16 GB total · ~2 GB OS+services = 14 GB for VMs
           Each base sandbox ≈ {VM_RAM_MB} MB  →  ~{(WORKER_TOTAL_RAM_MB-WORKER_OS_OVERHEAD)//VM_RAM_MB} VMs by RAM alone
  CPU    : 8 vCPU (hyperthreaded) · idle VMs use < 0.5% CPU each
           Concurrent workloads degrade above ~8 active VMs
  Disk   : CoW overlay per VM on 200 GB; base template ~1 GB
           → ~190 VMs by disk (not the bottleneck)
  Network: tap device + /30 subnet per VM  ·  /24 pool → 64 addresses
  NBD    : nbds_max={NBD_MAX}  (modprobe nbd nbds_max=64)  ← hard cap
  FDs    : ulimit -n · default 1024 on Ubuntu → raise to 65535
           for > ~30 VMs (each VM needs ~20 file descriptors)
  ─────────────────────────────────────────────────────────────────────
  Control plane (e2b-do-control-01, 8 GB)
    API + Postgres + Redis + ClickHouse all share 8 GB.
    Under high sandbox counts the API gRPC call queue to the
    orchestrator may become the bottleneck before the worker's RAM.
  ─────────────────────────────────────────────────────────────────────
""")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max",       type=int,   default=0,
                    help="Stop after this many total sandboxes (0 = push until failure)")
    ap.add_argument("--step",      type=int,   default=5,
                    help="Sandboxes to add per wave (default 5)")
    ap.add_argument("--threshold", type=float, default=80.0,
                    help="Min success %% for a wave to be 'healthy' (default 80)")
    ap.add_argument("--workers",   type=int,   default=5,
                    help="Parallel create workers per wave (default 5)")
    ap.add_argument("--no-cleanup", action="store_true",
                    help="Leave sandboxes running (manual kill / timeout)")
    args = ap.parse_args()

    hard_max = args.max if args.max > 0 else THEORETICAL_CEIL + 20
    workers  = max(1, min(args.workers, args.step))

    print(f"\n{'═'*64}")
    print(f"  E2B Droplet Capacity Benchmark  (additive ramp)")
    print(f"{'═'*64}")
    print(f"  Worker  : e2b-do-worker-01  143.198.25.149  (16 GB / NYC3)")
    print(f"  Control : e2b-do-control-01                 ( 8 GB / NYC3)")
    print(f"  API     : {os.getenv('E2B_API_URL')}")
    print(f"  Step    : +{args.step} sandboxes/wave  |  Workers: {workers}")
    print(f"  Threshold: ≥ {args.threshold:.0f}% ok  |  Hard max: {hard_max}")
    print(f"  Theoretical ceiling: ~{THEORETICAL_CEIL} sandboxes")
    print(f"{'═'*64}\n")

    all_waves:    list[WaveResult] = []
    total_alive   = 0
    ceiling:      int | None = None
    wave_num      = 0

    while total_alive < hard_max:
        wave_num += 1
        step_now  = min(args.step, hard_max - total_alive)

        print(f"┌── Wave {wave_num}  +{step_now} sandboxes  (currently {total_alive} alive) {'─'*20}")
        wave = run_wave(wave_num, step_now, total_alive, workers)
        all_waves.append(wave)

        c50 = _ms(wave.create_p50)
        c95 = _ms(wave.create_p95)
        d50 = _ms(wave.cmd_p50)
        print(f"└── {wave.n_ok}/{step_now} ok ({wave.success_pct:.0f}%)  "
              f"total alive={wave.total_alive_after}  "
              f"wall={wave.wall_ms/1000:.1f}s  "
              f"create p50={c50}ms p95={c95}ms  cmd p50={d50}ms\n")

        if wave.success_pct < args.threshold:
            print(f"  ⚠ Wave {wave_num} below {args.threshold:.0f}% — ceiling reached.\n")
            ceiling = total_alive  # alive count BEFORE this failing wave
            break

        total_alive = wave.total_alive_after

    else:
        ceiling = total_alive
        print(f"  Hard max ({hard_max}) reached — raise --max to probe further.\n")

    print_summary(all_waves, ceiling)

    if not args.no_cleanup:
        kill_all(all_waves, workers=max(args.workers, 10))
    else:
        alive_count = sum(w.n_ok for w in all_waves)
        print(f"  --no-cleanup: {alive_count} sandboxes left running.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
