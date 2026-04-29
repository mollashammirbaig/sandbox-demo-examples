#!/usr/bin/env python3
"""Very small E2B self-hosted example for running from your laptop.

This creates a sandbox via the public API, runs a Python command inside it,
and kills it. Uses a real domain with wildcard TLS for production-grade routing.

Optional overrides via environment variables:
  E2B_API_KEY
  E2B_API_URL       required (see .env)
  E2B_DOMAIN        required (see .env)
"""

from __future__ import annotations

import os
import shlex
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # install python-dotenv or set env vars manually

import httpx

try:
    from e2b import Sandbox
except ImportError:
    print("Install dependency first: python3 -m pip install e2b", file=sys.stderr)
    raise SystemExit(1)

def main() -> int:
    api_key = os.getenv("E2B_API_KEY")
    api_url = os.getenv("E2B_API_URL")
    domain = os.getenv("E2B_DOMAIN")

    sandbox = None
    try:
        sandbox = Sandbox.create(
            "base",
            api_key=api_key,
            api_url=api_url,
            domain=domain,
            timeout=120,
        )
    except httpx.ConnectError as exc:
        print(f"Could not reach E2B API at {api_url}: {exc}", file=sys.stderr)
        return 1

    try:
        result = sandbox.commands.run(
            "python3 -c " + shlex.quote('print("Hello from Python in the sandbox")')
        )
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        return int(result.exit_code or 0)
    except httpx.ConnectError as exc:
        print(f"Could not reach sandbox at {domain}: {exc}", file=sys.stderr)
        return 1
    finally:
        if sandbox is not None:
            sandbox.kill()


if __name__ == "__main__":
    raise SystemExit(main())
