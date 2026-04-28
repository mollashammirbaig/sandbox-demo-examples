#!/usr/bin/env python3
"""Build something inside an E2B sandbox and download the output artifacts.

Demonstrates:
  • writing Python source files into the sandbox
  • compiling them to .pyc bytecode and packing a .tar.gz
  • listing the files produced
  • downloading them to a local directory via sandbox.files.read()

Python is always available in the base image; no extra installs needed.
The downloaded artifacts land in ./artifacts/<sandbox_id>/ so you can inspect
them on your laptop after the sandbox is gone.

Usage:
  python3 scripts/e2b_download_artifacts.py
  python3 scripts/e2b_download_artifacts.py --out /tmp/my-artifacts

Env (all optional):
  E2B_API_KEY   required (see .env)
  E2B_API_URL   required (see .env)
  E2B_DOMAIN    required (see .env)
"""

from __future__ import annotations

import argparse
import os
import pathlib
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
    from e2b.sandbox.commands.command_handle import CommandExitException
except ImportError:
    print("Install:  python3 -m pip install e2b", file=sys.stderr)
    raise SystemExit(1)

DEFAULT_OUT_DIR = "./artifacts"

# ── source files written into the sandbox ────────────────────────────────────

MATHLIB_PY = """\
\"\"\"Tiny math library — compiled to .pyc as the build artifact.\"\"\"

def add(a: float, b: float) -> float:
    return a + b

def multiply(a: float, b: float) -> float:
    return a * b

def factorial(n: int) -> int:
    if n <= 1:
        return 1
    return n * factorial(n - 1)
"""

MAIN_PY = """\
from mathlib import add, multiply, factorial

if __name__ == "__main__":
    print(f"add(2, 3)        = {add(2, 3)}")
    print(f"multiply(4, 5)   = {multiply(4, 5)}")
    print(f"factorial(10)    = {factorial(10)}")
"""

# Shell script run inside the sandbox to produce all artifacts
BUILD_SCRIPT = """\
set -e
cd /tmp/pybuild

# compile both modules to .pyc (written into __pycache__)
python3 -m py_compile mathlib.py main.py

# also run the program so we can capture its output as a report
python3 main.py > build_report.txt

# pack everything (sources + bytecode) into a tarball
tar -czf /tmp/pybuild_dist.tar.gz -C /tmp pybuild

echo "=== build_report.txt ==="
cat build_report.txt
echo "========================"
find /tmp/pybuild -type f | sort
echo "/tmp/pybuild_dist.tar.gz"
"""


def create_kwargs() -> dict:
    return {
        "api_key": os.getenv("E2B_API_KEY"),
        "api_url": os.getenv("E2B_API_URL"),
        "domain":  os.getenv("E2B_DOMAIN"),
    }


def run(sb: Sandbox, cmd: str, timeout: int = 60) -> str:
    """Run a command; return stdout. Raises on non-zero exit."""
    try:
        r = sb.commands.run(cmd, timeout=timeout)
        return r.stdout or ""
    except CommandExitException as e:
        raise RuntimeError(
            f"Command failed (exit {e.exit_code}):\n{e.stderr or e.stdout}"
        ) from e


def download_file(sb: Sandbox, remote_path: str, local_path: pathlib.Path) -> int:
    """Download a single file from the sandbox. Returns bytes written."""
    content: bytes = sb.files.read(remote_path, format="bytes")
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(content)
    return len(content)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=DEFAULT_OUT_DIR,
                    help=f"Local directory for artifacts (default: {DEFAULT_OUT_DIR})")
    args = ap.parse_args()

    opts    = create_kwargs()
    out_dir = pathlib.Path(args.out)

    print(f"\n{'═'*62}")
    print("  E2B Download Artifacts Demo")
    print(f"{'═'*62}")
    print(f"  API  : {opts['api_url']}")
    print(f"  Local: {out_dir.resolve()}")
    print(f"{'═'*62}\n")

    # ── 1. create sandbox ────────────────────────────────────────────────────
    print("  [1/4] Creating sandbox...", flush=True)
    t0 = time.perf_counter()
    try:
        sb = Sandbox.create("base", timeout=180, **opts)
    except httpx.ConnectError as e:
        print(f"Cannot reach E2B API: {e}", file=sys.stderr)
        return 1
    print(f"  sandbox_id = {sb.sandbox_id}  ({(time.perf_counter()-t0)*1000:.0f} ms)\n")

    artifact_dir = out_dir / sb.sandbox_id

    try:
        # ── 2. write sources + build ──────────────────────────────────────────
        print("  [2/4] Writing source files and compiling...")
        sb.files.write("/tmp/pybuild/mathlib.py", MATHLIB_PY)
        sb.files.write("/tmp/pybuild/main.py", MAIN_PY)
        sb.files.write("/tmp/pybuild/build.sh", BUILD_SCRIPT)

        try:
            output = run(sb, "bash /tmp/pybuild/build.sh", timeout=60)
        except RuntimeError as e:
            print(f"\n  Build failed: {e}", file=sys.stderr)
            return 1

        for line in output.strip().splitlines():
            print(f"    {line}")
        print()

        # ── 3. discover artifacts to download ────────────────────────────────
        print("  [3/4] Collecting artifact paths...")
        remote_files: list[str] = []
        for line in output.strip().splitlines():
            p = line.strip()
            if p.startswith("/tmp/") and not p.startswith("==="):
                remote_files.append(p)

        if not remote_files:
            print("  No artifact paths found in build output.", file=sys.stderr)
            return 1

        for p in remote_files:
            try:
                size_out = run(sb, f"stat -c %s {p}", timeout=5).strip()
            except RuntimeError:
                size_out = "?"
            print(f"    {p}  ({size_out} bytes)")
        print()

        # ── 4. download ───────────────────────────────────────────────────────
        print(f"  [4/4] Downloading {len(remote_files)} file(s) → {artifact_dir}/")
        total_bytes = 0
        for remote_path in remote_files:
            rel   = remote_path.lstrip("/")
            local = artifact_dir / rel
            try:
                n = download_file(sb, remote_path, local)
                total_bytes += n
                print(f"    ✓  {local}  ({n:,} bytes)")
            except Exception as e:
                print(f"    ✗  {remote_path}: {e}", file=sys.stderr)

        print(f"\n  Downloaded {total_bytes:,} bytes total → {artifact_dir.resolve()}/")

    finally:
        print("\n  Killing sandbox...", end=" ", flush=True)
        sb.kill()
        print("done.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
