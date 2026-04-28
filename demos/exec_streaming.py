#!/usr/bin/env python3
"""Stream stdout/stderr in real-time while running tests or compilation inside an E2B sandbox.

The SDK's commands.run() buffers and returns output only after the process
exits.  For long-running tasks (pytest, cargo build, go test) you want to see
output as it arrives.  Use on_stdout / on_stderr callbacks instead.

Usage:
  python3 scripts/e2b_exec_streaming.py
  python3 scripts/e2b_exec_streaming.py --task compile   # runs: go build ./...
  python3 scripts/e2b_exec_streaming.py --task test      # runs: pytest -v (default)
  python3 scripts/e2b_exec_streaming.py --cmd "make all"

Env (all optional):
  E2B_API_KEY   required (see .env)
  E2B_API_URL   required (see .env)
  E2B_DOMAIN    required (see .env)
"""

from __future__ import annotations

import argparse
import os
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

# Sample programs written into the sandbox before running the chosen task.
# These give us something real to compile / test without needing the user's
# actual source tree present inside the VM.
SAMPLE_PYTHON_TESTS = """\
import time

def test_addition():
    assert 1 + 1 == 2

def test_string_format():
    msg = "hello {}".format("world")
    assert msg == "hello world"

def test_slow_pass():
    time.sleep(0.3)
    assert True

def test_slow_fail():
    time.sleep(0.2)
    assert 1 == 2, "intentional failure to show stderr streaming"
"""

SAMPLE_GO_MAIN = """\
package main

import "fmt"

func add(a, b int) int { return a + b }

func main() {
    fmt.Println("build ok — add(2,3) =", add(2, 3))
}
"""

SAMPLE_GO_TEST = """\
package main

import "testing"

func TestAdd(t *testing.T) {
    if got := add(2, 3); got != 5 {
        t.Fatalf("add(2,3) = %d, want 5", got)
    }
}
"""

TASKS: dict[str, tuple[str, str]] = {
    # task name → (setup shell script, streaming command)
    "test": (
        "pip install pytest -q && cat > /tmp/test_sample.py << 'EOF'\n"
        + SAMPLE_PYTHON_TESTS
        + "EOF",
        "pytest /tmp/test_sample.py -v --tb=short",
    ),
    "compile": (
        "apt-get install -y golang-go -qq 2>/dev/null; "
        "mkdir -p /tmp/goapp && "
        "cat > /tmp/goapp/main.go << 'EOF'\n"
        + SAMPLE_GO_MAIN
        + "EOF\n"
        + "cat > /tmp/goapp/main_test.go << 'EOF'\n"
        + SAMPLE_GO_TEST
        + "EOF",
        "cd /tmp/goapp && go mod init example.com/app 2>&1 && go build ./... && go test ./... -v",
    ),
}


def create_kwargs() -> dict:
    return {
        "api_key": os.getenv("E2B_API_KEY"),
        "api_url": os.getenv("E2B_API_URL"),
        "domain":  os.getenv("E2B_DOMAIN"),
    }


def stream_command(sb: Sandbox, cmd: str) -> int:
    """Run cmd in the sandbox, printing each line as it arrives. Returns exit code."""

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def on_stdout(data: str) -> None:
        # data may contain newlines; print each line immediately
        for line in data.splitlines():
            print(f"  \033[32mOUT\033[0m  {line}", flush=True)
        stdout_lines.append(data)

    def on_stderr(data: str) -> None:
        for line in data.splitlines():
            print(f"  \033[31mERR\033[0m  {line}", file=sys.stderr, flush=True)
        stderr_lines.append(data)

    try:
        result = sb.commands.run(
            cmd,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
            timeout=120,
        )
        return int(result.exit_code or 0)
    except CommandExitException as e:
        return int(e.exit_code or 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--task", choices=list(TASKS), default="test",
                       help="Pre-built task: 'test' (pytest) or 'compile' (go build+test)")
    group.add_argument("--cmd", help="Arbitrary shell command to stream")
    args = ap.parse_args()

    opts = create_kwargs()

    print(f"\n{'═'*62}")
    print("  E2B Exec Streaming Demo")
    print(f"{'═'*62}")
    print(f"  API : {opts['api_url']}")

    if args.cmd:
        setup_cmd   = None
        stream_cmd  = args.cmd
        label       = f"custom: {args.cmd}"
    else:
        setup_cmd, stream_cmd = TASKS[args.task]
        label = args.task

    print(f"  Task: {label}")
    print(f"{'═'*62}\n")

    print("  [1/3] Creating sandbox...", flush=True)
    t0 = time.perf_counter()
    try:
        sb = Sandbox.create("base", timeout=180, **opts)
    except httpx.ConnectError as e:
        print(f"Cannot reach E2B API: {e}", file=sys.stderr)
        return 1
    print(f"  sandbox_id = {sb.sandbox_id}  ({(time.perf_counter()-t0)*1000:.0f} ms)\n")

    exit_code = 0
    try:
        if setup_cmd:
            print("  [2/3] Running setup (buffered, not streamed)...")
            r = sb.commands.run(setup_cmd, timeout=120)
            if r.exit_code not in (0, None):
                print(f"  Setup failed (exit {r.exit_code}):\n{r.stderr}", file=sys.stderr)
                return 1
            print("  Setup done.\n")

        print(f"  [3/3] Streaming: {stream_cmd}\n")
        print(f"  {'─'*58}")
        t1 = time.perf_counter()
        exit_code = stream_command(sb, stream_cmd)
        elapsed   = (time.perf_counter() - t1) * 1000
        print(f"  {'─'*58}")
        print(f"\n  exit_code = {exit_code}  |  elapsed = {elapsed:.0f} ms")

    finally:
        print("\n  Killing sandbox...", end=" ", flush=True)
        sb.kill()
        print("done.")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
