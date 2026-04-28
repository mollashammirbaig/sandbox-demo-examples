#!/usr/bin/env python3
"""Snapshot a sandbox, spawn a new one from the snapshot, verify disk + guest RAM.

  .venv-snapshot-test/bin/pip install e2b httpx   # or any venv
  ./scripts/e2b_snapshot_restore_test.py

Env (same as e2b_sdk_smoke_test.py): E2B_DROPLET_HOST, E2B_API_URL, E2B_SANDBOX_URL,
E2B_API_KEY. Optional: E2B_SNAPSHOT_REQUEST_TIMEOUT (seconds, default 600).

**Sandbox URL:** If ``E2B_SANDBOX_URL`` is unset, it defaults to ``http://<host>:3002`` where
``<host>`` is the hostname from ``E2B_API_URL`` when that host is public (not loopback), so
client-proxy is reached on the **same** address you use for the API (e.g. droplet public IP).
If ``E2B_API_URL`` uses ``127.0.0.1``/``localhost`` (API tunnel), ``<host>`` falls back to
``E2B_DROPLET_HOST`` so envd traffic still targets the **public** client-proxy on the droplet.

After each ``Sandbox.create``, the script sets the ``Host`` header to
``{49983}-{sandbox_id}.proxy.local`` (same idea as ``example.py``) on **commands** and
**pty** Connect clients, **filesystem** RPC clients, and the shared **httpx** envd
client (used by ``files.write`` and other HTTP routes) so client-proxy accepts routing
when ``sandbox_url`` uses a bare IP.

The in-guest "RAM" check: a Python HTTP server reads a one-time secret file, stores it
in memory, deletes the file, then serves the secret only from its heap. After
snapshot → new sandbox, we curl that server on the new VM; success implies guest
memory (including the process) was restored, not that VM2 reads VM1's RAM.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shlex
import sys
import textwrap
import time
import uuid
from urllib.parse import urlparse

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
    print("Install:  python3 -m pip install e2b httpx", file=sys.stderr)
    raise SystemExit(1)

ENVD_COMMAND_PORT = 49983
RAMTEST_PORT = 18765
DISK_MARKER_PATH = "/tmp/snapshot_disk_marker"
RAM_SRV_PATH = "/tmp/ramtest_srv.py"
RAM_SECRET_PATH = "/tmp/.ramsecret"
# Server: load secret from file into memory, delete file, serve from memory only.
_RAM_SERVER_SRC = textwrap.dedent(
    f"""
    import http.server, os, signal, socketserver
    PORT = {RAMTEST_PORT}
    path = {RAM_SECRET_PATH!r}
    with open(path) as f:
        SECRET = f.read().strip()
    os.remove(path)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(SECRET.encode())
        def log_message(self, *args, **kwargs):
            pass
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", PORT), H) as httpd:
        httpd.serve_forever()
    """
).strip()


def _loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return True
    if hostname in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _default_sandbox_url(*, api_url: str, droplet_host: str, proxy_port: int) -> str:
    """Public client-proxy base URL when E2B_SANDBOX_URL is not set explicitly."""
    p = urlparse(api_url)
    h = p.hostname
    if h and not _loopback_host(h):
        connect_host = h
    else:
        connect_host = droplet_host
    scheme = p.scheme if p.scheme in ("http", "https") else "http"
    return f"{scheme}://{connect_host}:{proxy_port}"


def api_kwargs() -> dict:
    droplet_host = os.environ.get("E2B_DROPLET_HOST", "157.245.127.4")
    api_url = os.environ.get("E2B_API_URL", f"http://{droplet_host}:3000")
    proxy_port = int(os.environ.get("E2B_CLIENT_PROXY_PORT", "3002"))
    raw_sandbox = os.environ.get("E2B_SANDBOX_URL")
    if raw_sandbox is not None and str(raw_sandbox).strip() != "":
        sandbox_url = raw_sandbox
    else:
        sandbox_url = _default_sandbox_url(
            api_url=api_url, droplet_host=droplet_host, proxy_port=proxy_port
        )
    req_timeout = float(os.environ.get("E2B_SNAPSHOT_REQUEST_TIMEOUT", "600"))
    return {
        "api_key": os.environ.get("E2B_API_KEY"),
        "api_url": api_url,
        "sandbox_url": sandbox_url,
        "request_timeout": req_timeout,
    }


def create_kwargs() -> dict:
    o = api_kwargs()
    api_url = o["api_url"] or ""
    return {**o, "secure": api_url.startswith("https")}


def _apply_host_to_connect_rpc(rpc: object, host: str) -> None:
    """Patch Host on every generated Connect client (``_*`` with ``_headers``)."""
    for name in dir(rpc):
        if not name.startswith("_") or name.startswith("__"):
            continue
        client = getattr(rpc, name, None)
        if client is None:
            continue
        hdrs = getattr(client, "_headers", None)
        if hdrs is not None:
            hdrs["Host"] = host


def _set_proxy_host_header(sandbox: Sandbox) -> None:
    """Force Host header client-proxy expects when connecting via bare IP (see example.py)."""
    host = f"{ENVD_COMMAND_PORT}-{sandbox.sandbox_id}.proxy.local"
    _apply_host_to_connect_rpc(sandbox.commands._rpc, host)
    _apply_host_to_connect_rpc(sandbox.pty._rpc, host)
    _apply_host_to_connect_rpc(sandbox.files._rpc, host)
    # files.write uses httpx against ENVD_API_FILES_ROUTE, not the filesystem RPC.
    sandbox._envd_api.headers["Host"] = host


def _guest_fetch_localhost(sb: Sandbox, port: int) -> tuple[str | None, int | None]:
    code = textwrap.dedent(
        f"""
        import urllib.request
        r = urllib.request.urlopen("http://127.0.0.1:{port}/", timeout=3)
        print(r.read().decode())
        """
    ).strip()
    try:
        r = sb.commands.run("python3 -c " + shlex.quote(code))
    except CommandExitException as e:
        return None, e.exit_code
    out = (r.stdout or "").strip()
    if r.exit_code not in (0, None):
        return None, r.exit_code
    return out, r.exit_code


def _wait_localhost_secret(sb: Sandbox, want: str, *, label: str, deadline_s: float = 120.0) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        got, _ = _guest_fetch_localhost(sb, RAMTEST_PORT)
        if got == want:
            return True
        time.sleep(0.5)
    print(f"[{label}] timeout waiting for http://127.0.0.1:{RAMTEST_PORT}/", file=sys.stderr)
    return False


def _setup_guest_a(sb: Sandbox, disk_marker: str, ram_secret: str) -> bool:
    # Disk marker
    w = sb.commands.run(
        "bash -lc " + shlex.quote(f"printf %s {shlex.quote(disk_marker)} > {DISK_MARKER_PATH}")
    )
    if w.exit_code not in (0, None):
        print("disk marker write failed:", w.stderr, file=sys.stderr)
        return False

    # RAM test server script (avoid shell heredocs / quoting)
    try:
        sb.files.write(RAM_SRV_PATH, _RAM_SERVER_SRC)
    except Exception as e:
        print("files.write server script failed:", e, file=sys.stderr)
        return False

    r2 = sb.commands.run(
        "bash -lc " + shlex.quote(f"printf %s {shlex.quote(ram_secret)} > {RAM_SECRET_PATH}")
    )
    if r2.exit_code not in (0, None):
        print("write ram secret file failed:", r2.stderr, file=sys.stderr)
        return False

    h = sb.commands.run(
        f"python3 -u {RAM_SRV_PATH}",
        background=True,
        timeout=0,
    )
    h.disconnect()

    if not _wait_localhost_secret(sb, ram_secret, label="sandbox_a"):
        return False

    chk = sb.commands.run("bash -lc " + shlex.quote(f"test ! -f {RAM_SECRET_PATH} && echo gone"))
    if "gone" not in (chk.stdout or ""):
        print("expected ram secret file removed after server start; got:", chk.stdout, chk.stderr, file=sys.stderr)
        return False

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--skip-delete-snapshot",
        action="store_true",
        help="Leave snapshot template on API (for debugging)",
    )
    args = parser.parse_args()

    api = api_kwargs()
    opts = create_kwargs()
    print(
        f"API {api['api_url']!r}  sandbox {api['sandbox_url']!r}  "
        f"request_timeout={api['request_timeout']}s"
    )

    disk_marker = f"disk-{uuid.uuid4().hex}"
    ram_secret = f"ram-{uuid.uuid4().hex}"
    print(f"disk_marker (written to {DISK_MARKER_PATH}): {disk_marker}")
    print(f"ram_secret (loaded into server process, file then removed): {ram_secret}")

    sb_a: Sandbox | None = None
    sb_b: Sandbox | None = None
    snapshot_id: str | None = None

    def cleanup() -> None:
        nonlocal sb_a, sb_b, snapshot_id
        for name, sb in (("B", sb_b), ("A", sb_a)):
            if sb is None:
                continue
            try:
                sb.kill()
                print(f"killed sandbox {name} {sb.sandbox_id!r}")
            except Exception as e:
                print(f"kill {name} failed: {e}", file=sys.stderr)
            if name == "B":
                sb_b = None
            else:
                sb_a = None
        if snapshot_id and not args.skip_delete_snapshot:
            try:
                deleted = Sandbox.delete_snapshot(snapshot_id, **api)
                print(f"delete_snapshot {snapshot_id!r} -> {deleted}")
            except Exception as e:
                print(f"delete_snapshot failed: {e}", file=sys.stderr)
        elif snapshot_id:
            print(f"kept snapshot {snapshot_id!r} (--skip-delete-snapshot)")

    results: dict[str, str] = {"disk_on_B": "not_run", "ram_http_on_B": "not_run"}

    try:
        try:
            sb_a = Sandbox.create("base", timeout=600, **opts)
        except httpx.ConnectError as e:
            print(f"{e}\nNo server at {opts['api_url']!r}.", file=sys.stderr)
            return 1

        print(f"sandbox A {sb_a.sandbox_id!r}")
        _set_proxy_host_header(sb_a)
        if not _setup_guest_a(sb_a, disk_marker, ram_secret):
            return 1

        print("creating snapshot (guest paused briefly; may take several minutes)…")
        snap = sb_a.create_snapshot(**api)
        snapshot_id = snap.snapshot_id
        print(f"snapshot_id {snapshot_id!r}")

        print("creating sandbox B from snapshot…")
        sb_b = Sandbox.create(snapshot_id, timeout=600, **opts)
        print(f"sandbox B {sb_b.sandbox_id!r} (new VM / new id)")
        _set_proxy_host_header(sb_b)

        cat = sb_b.commands.run("bash -lc " + shlex.quote(f"cat {DISK_MARKER_PATH}"))
        got_disk = (cat.stdout or "").strip()
        if cat.exit_code in (0, None) and got_disk == disk_marker:
            results["disk_on_B"] = "PASS"
        else:
            results["disk_on_B"] = f"FAIL (exit={cat.exit_code!r} stdout={got_disk!r})"

        got_ram, ex = _guest_fetch_localhost(sb_b, RAMTEST_PORT)
        if got_ram == ram_secret:
            results["ram_http_on_B"] = "PASS"
        else:
            results["ram_http_on_B"] = f"FAIL (exit={ex!r} got={got_ram!r})"
            dbg = sb_b.commands.run("bash -lc " + shlex.quote("ps aux | head -25; ss -lntp 2>/dev/null | head -15 || true"))
            print("debug (ps + listeners):", (dbg.stdout or "")[:2000], file=sys.stderr)

        print("")
        print(f"{'check':<20} {'result':<50}")
        print("-" * 70)
        for k, v in results.items():
            print(f"{k:<20} {v:<50}")
        print("-" * 70)
        print(f"{'ram_secret (expected)':<20} {ram_secret}")
        print(f"{'ram_secret (from B)':<20} {got_ram!r}")

        ok = results["disk_on_B"] == "PASS" and results["ram_http_on_B"] == "PASS"
        return 0 if ok else 1
    finally:
        cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
