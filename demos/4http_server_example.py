#!/usr/bin/env python3
"""E2B self-hosted example: run a Python HTTP server inside a sandbox.

Creates a sandbox, starts python3 -m http.server on port 8000, fetches the
index page from outside via the wildcard domain, prints the HTML, then kills
the sandbox.

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

import httpx

try:
    from e2b import Sandbox
except ImportError:
    print("Install dependency first: python3 -m pip install e2b httpx", file=sys.stderr)
    raise SystemExit(1)

HTTP_PORT       = 8000


def main() -> int:
    api_key = os.getenv("E2B_API_KEY")
    api_url = os.getenv("E2B_API_URL")
    domain  = os.getenv("E2B_DOMAIN")

    # ── 1. Create sandbox ────────────────────────────────────────────────────
    print("Creating sandbox...")
    sandbox = None
    try:
        sandbox = Sandbox.create(
            "base",
            api_key=api_key,
            api_url=api_url,
            domain=domain,
            timeout=300,
        )
    except httpx.ConnectError as exc:
        print(f"Could not reach E2B API at {api_url}: {exc}", file=sys.stderr)
        return 1

    print(f"Sandbox created: {sandbox.sandbox_id}")
    # Give orchestrator a moment to register the sandbox in the catalog
    time.sleep(3)

    try:
        # ── 2. Write a custom index page ─────────────────────────────────────
        html = (
            "<html><body>"
            "<h1>Hello from DigitalOcean sandbox!</h1>"
            f"<p>Sandbox ID: {sandbox.sandbox_id}</p>"
            "</body></html>"
        )
        sandbox.commands.run(
            f"mkdir -p /tmp/www && printf '%s' {html!r} > /tmp/www/index.html",
            timeout=10,
        )

        # ── 3. Start HTTP server in the background ────────────────────────────
        print(f"Starting HTTP server on port {HTTP_PORT}...")
        sandbox.commands.run(
            f"sh -c 'nohup python3 -m http.server {HTTP_PORT} --directory /tmp/www "
            f">/tmp/httpserver.log 2>&1 </dev/null &'",
            timeout=10,
        )

        # Give the server a moment to bind
        time.sleep(2)

        # ── 4. Get the public URL ─────────────────────────────────────────────
        host = sandbox.get_host(HTTP_PORT)
        url  = f"https://{host}"
        print(f"Sandbox HTTP server reachable at: {url}")

        # ── 5. Fetch the page from outside the sandbox ────────────────────────
        print("Fetching index page...")
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        print(f"Status: {resp.status_code}")
        print("─" * 50)
        print(resp.text.strip())
        print("─" * 50)

        return 0

    except httpx.ConnectError as exc:
        print(f"Could not reach sandbox HTTP server: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as exc:
        print(f"HTTP error {exc.response.status_code}: {exc}", file=sys.stderr)
        return 1
    finally:
        if sandbox is not None:
            print("Killing sandbox...")
            # sandbox.kill()
            print("Done.")


if __name__ == "__main__":
    raise SystemExit(main())
