#!/usr/bin/env python3
"""Wall-clock benchmark (ms) for self-hosted E2B: create → get_info → 2× python → kill.

  pip install e2b
  ./scripts/e2b_benchmark.py
  ./scripts/e2b_benchmark.py --runs 5

Env: E2B_DROPLET_HOST (default 165.227.194.36), E2B_API_URL, E2B_SANDBOX_URL, E2B_API_KEY
"""

from __future__ import annotations

import argparse
import os
import shlex
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # install python-dotenv or set env vars manually

try:
    import httpx
    from e2b import Sandbox
except ImportError:
    print("Install:  python3 -m pip install e2b", file=sys.stderr)
    raise SystemExit(1)


def api_kwargs() -> dict:
    api_url = os.environ.get("E2B_API_URL")
    return {
        "api_key": os.environ.get("E2B_API_KEY"),
        "api_url": api_url,
    }


def create_kwargs() -> dict:
    o = api_kwargs()
    domain = os.environ.get("E2B_DOMAIN")
    return {**o, "domain": domain}


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000.0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--runs", type=int, default=1, help="Serial full lifecycles to average")
    args = p.parse_args()
    runs = max(1, args.runs)

    api = api_kwargs()
    opts = create_kwargs()
    py_cmd = "python3 -c " + shlex.quote("print(1)")
    acc = {"create": 0.0, "get_info": 0.0, "python_1": 0.0, "python_2": 0.0, "kill": 0.0}
    all_ok = True

    for n in range(1, runs + 1):
        if runs > 1:
            print(f"\n=== run {n}/{runs} ===")

        t0 = time.perf_counter()
        try:
            sb = Sandbox.create("base", timeout=120, **opts)
        except httpx.ConnectError as e:
            print(e, file=sys.stderr)
            return 1
        t_create = _ms(t0)


        t0 = time.perf_counter()
        Sandbox.get_info(sb.sandbox_id, **api)
        t_info = _ms(t0)

        t0 = time.perf_counter()
        r1 = sb.commands.run(py_cmd)
        t_py1 = _ms(t0)

        t0 = time.perf_counter()
        r2 = sb.commands.run(py_cmd)
        t_py2 = _ms(t0)

        t0 = time.perf_counter()
        sb.kill()
        t_kill = _ms(t0)

        acc["create"] += t_create
        acc["get_info"] += t_info
        acc["python_1"] += t_py1
        acc["python_2"] += t_py2
        acc["kill"] += t_kill

        ok1 = r1.exit_code in (0, None)
        ok2 = r2.exit_code in (0, None)
        all_ok = all_ok and ok1 and ok2
        total = t_create + t_info + t_py1 + t_py2 + t_kill

        print(f"{'stage':<22} {'ms':>10}")
        print("-" * 34)
        print(f"{'create':<22} {t_create:10.1f}")
        print(f"{'get_info':<22} {t_info:10.1f}")
        print(f"{'python_run (1st)':<22} {t_py1:10.1f}  exit={r1.exit_code}")
        print(f"{'python_run (2nd)':<22} {t_py2:10.1f}  exit={r2.exit_code}")
        print(f"{'kill':<22} {t_kill:10.1f}")
        print("-" * 34)
        print(f"{'total (this run)':<22} {total:10.1f}")

    if runs > 1:
        print(f"\n=== mean over {runs} runs (ms) ===")
        for k, v in acc.items():
            print(f"{k:<22} {v / runs:10.1f}")

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
