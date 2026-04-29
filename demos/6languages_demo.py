#!/usr/bin/env python3
"""E2B self-hosted demo: run code in multiple languages inside one sandbox.

Creates a single sandbox, runs a hello-world snippet in each available
language, prints the output, then kills the sandbox.

Optional env overrides:
  E2B_API_KEY
  E2B_API_URL    required (see .env)
  E2B_DOMAIN     required (see .env)
"""

from __future__ import annotations

import os
import sys
import time

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # install python-dotenv or set env vars manually

try:
    from e2b import Sandbox
    from e2b.sandbox.commands.command_handle import CommandExitException
except ImportError:
    print("Install dependency first: python3 -m pip install e2b", file=sys.stderr)
    raise SystemExit(1)

# (label, binary, command to run inside sandbox)
LANGUAGES = [
    ("Bash",       "bash",   "bash -c 'echo Hello from Bash'"),
    ("Python",     "python3","python3 -c 'print(\"Hello from Python\")'"),
    ("Node.js",    "node",   "node -e 'console.log(\"Hello from Node.js\")'"),
    ("Ruby",       "ruby",   "ruby -e 'puts \"Hello from Ruby\"'"),
    ("Perl",       "perl",   "perl -e 'print \"Hello from Perl\\n\"'"),
    ("PHP",        "php",    "php -r 'echo \"Hello from PHP\\n\";'"),
]


def run_language(sb: "Sandbox", label: str, binary: str, cmd: str) -> None:
    try:
        sb.commands.run(f"which {binary}", timeout=10)
    except CommandExitException:
        print(f"  [{label}] not installed — skipping")
        return

    try:
        result = sb.commands.run(cmd, timeout=15)
        output = (result.stdout or "").strip()
        print(f"  [{label}] {output}")
    except CommandExitException as exc:
        print(f"  [{label}] error: {exc}")


def main() -> int:
    api_key = os.getenv("E2B_API_KEY")
    api_url = os.getenv("E2B_API_URL")
    domain  = os.getenv("E2B_DOMAIN")

    print("Creating sandbox...")
    try:
        sb = Sandbox.create(
            "base",
            api_key=api_key,
            api_url=api_url,
            domain=domain,
            timeout=120,
        )
    except Exception as exc:
        print(f"Failed to create sandbox: {exc}", file=sys.stderr)
        return 1

    print(f"Sandbox ready: {sb.sandbox_id}\n")
    time.sleep(3)  # wait for catalog registration

    try:
        print("Running languages:\n")
        for label, binary, cmd in LANGUAGES:
            run_language(sb, label, binary, cmd)
        print()
        return 0
    finally:
        sb.kill()
        print("Sandbox killed.")


if __name__ == "__main__":
    raise SystemExit(main())
