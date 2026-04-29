#!/usr/bin/env python3
"""Benchmark snapshot creation and restore latency.

Each run:
  1. Create a baseline sandbox          → measures create_ms
  2. Snapshot it                        → measures snapshot_ms
  3. Kill the original sandbox
  4. Restore a new sandbox from snapshot → measures restore_ms
  5. Kill the restored sandbox
  6. Delete the snapshot template

Usage:
  pip install e2b python-dotenv
  python3 snapshot_benchmark.py
  python3 snapshot_benchmark.py --runs 5

Env (or .env file):
  E2B_API_KEY, E2B_API_URL, E2B_DOMAIN
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from e2b import Sandbox
except ImportError:
    print("Install:  pip install e2b", file=sys.stderr)
    raise SystemExit(1)


def _sdk_kwargs() -> dict:
    return {
        "api_key": os.getenv("E2B_API_KEY"),
        "api_url": os.getenv("E2B_API_URL"),
        "domain":  os.getenv("E2B_DOMAIN"),
    }


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def _percentile(data: list[float], p: float) -> float:
    data = sorted(data)
    k = (len(data) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (k - lo)


def _print_stats(label: str, times: list[float]) -> None:
    if not times:
        print(f"  {label:<22}  (no successful samples)")
        return
    print(f"  {label:<22}  "
          f"min={min(times):7.0f}  "
          f"max={max(times):7.0f}  "
          f"mean={statistics.mean(times):7.0f}  "
          f"p50={_percentile(times, 50):7.0f}  "
          f"p95={_percentile(times, 95):7.0f}  ms")


def run_one(n: int, runs: int, kwargs: dict) -> tuple[float, float, float] | None:
    print(f"\n  ── run {n}/{runs} " + "─" * 36)

    # 1. create baseline sandbox
    t0 = time.perf_counter()
    sb = Sandbox.create("base", timeout=300, **kwargs)
    create_ms = _ms(t0)
    print(f"  [✓] create    {create_ms:8.1f} ms  {sb.sandbox_id}")

    snapshot_id = None
    try:
        # 2. snapshot
        t0 = time.perf_counter()
        snap = sb.create_snapshot(**kwargs)
        snapshot_ms = _ms(t0)
        snapshot_id = snap.snapshot_id
        print(f"  [✓] snapshot  {snapshot_ms:8.1f} ms  {snapshot_id}")

        # 3. kill original
        sb.kill()

        # 4. restore
        t0 = time.perf_counter()
        sb_r = Sandbox.create(snapshot_id, timeout=300, **kwargs)
        restore_ms = _ms(t0)
        print(f"  [✓] restore   {restore_ms:8.1f} ms  {sb_r.sandbox_id}")

        # 5. kill restored
        sb_r.kill()

        return create_ms, snapshot_ms, restore_ms

    except Exception as e:
        print(f"  [✗] {e}", file=sys.stderr)
        try:
            sb.kill()
        except Exception:
            pass
        return None

    finally:
        # 6. delete snapshot template
        if snapshot_id:
            try:
                Sandbox.delete_snapshot(snapshot_id, **kwargs)
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3, help="Number of full cycles (default 3)")
    args = ap.parse_args()

    runs = max(1, args.runs)
    kwargs = _sdk_kwargs()

    print()
    print("═" * 60)
    print("  Snapshot / Restore Latency Benchmark")
    print("═" * 60)
    print(f"  API    : {os.getenv('E2B_API_URL')}")
    print(f"  Domain : {os.getenv('E2B_DOMAIN')}")
    print(f"  Runs   : {runs}")
    print("═" * 60)

    create_times:   list[float] = []
    snapshot_times: list[float] = []
    restore_times:  list[float] = []

    for i in range(1, runs + 1):
        result = run_one(i, runs, kwargs)
        if result:
            c, s, r = result
            create_times.append(c)
            snapshot_times.append(s)
            restore_times.append(r)

    print()
    print("═" * 60)
    print(f"  Summary — {len(create_times)}/{runs} runs succeeded  (ms)")
    print("─" * 60)
    _print_stats("create (baseline)", create_times)
    _print_stats("snapshot",          snapshot_times)
    _print_stats("restore",           restore_times)
    print("═" * 60)
    print()

    return 0 if len(create_times) == runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
