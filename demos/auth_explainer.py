#!/usr/bin/env python3
"""
E2B Authentication Explainer
=============================

This script does NOT create sandboxes or run code.  Its sole purpose is to
walk through *exactly* what happens when your API key is presented to the
self-hosted E2B stack — tracing the call from your laptop all the way to the
Postgres row check.

Run:
  python3 scripts/e2b_auth_explainer.py

Env (read-only — the script never talks to the network):
  E2B_API_KEY   required (see .env)
  E2B_API_URL   required (see .env)
"""

from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # install python-dotenv or set env vars manually

def section(title: str) -> None:
    print(f"\n{'═'*68}")
    print(f"  {title}")
    print(f"{'═'*68}\n")


def code_block(lines: list[str]) -> None:
    print("  ┌" + "─" * 64 + "┐")
    for line in lines:
        print(f"  │  {line:<62}│")
    print("  └" + "─" * 64 + "┘")


def main() -> int:
    api_key = os.getenv("E2B_API_KEY")
    api_url = os.getenv("E2B_API_URL")

    prefix  = api_key[:8] if len(api_key) >= 8 else api_key
    masked  = prefix + "…" + ("*" * 8)

    print(f"""
  ╔══════════════════════════════════════════════════════════════════╗
  ║           E2B Authentication — How It Really Works              ║
  ╚══════════════════════════════════════════════════════════════════╝

  API endpoint : {api_url}
  API key      : {masked}
""")

    # ─── 1. What the key looks like ───────────────────────────────────────────
    section("1. The API Key Format")
    print("""\
  Every E2B API key begins with the prefix  e2b_  followed by a 32-character
  random hex string (128 bits of entropy).

  Example:
    <YOUR_E2B_API_KEY>
    ├─┤  └──────────────────────────────┘
    prefix        random token (hex, 128 bit)

  The prefix is a human-readable marker; the entire string is the credential.
  It is generated once by the API when you create a new team/user and stored
  (hashed) in Postgres.
""")

    # ─── 2. How the client sends the key ──────────────────────────────────────
    section("2. How the Key Is Sent — HTTP Bearer Token")
    print("""\
  The E2B SDK (and raw curl) sends the key in the standard Authorization
  header as a Bearer token:

    Authorization: Bearer <YOUR_E2B_API_KEY>

  Every HTTP request to the API carries this header.  No cookies, no sessions,
  no OAuth redirects — a single stateless header per request.

  curl equivalent:
""")
    code_block([
        f"curl -s {api_url}/sandboxes \\",
        "  -H 'Authorization: Bearer e2b_<your-key>' \\",
        "  -H 'Content-Type: application/json'",
    ])
    print()

    # ─── 3. TLS / transport layer ─────────────────────────────────────────────
    section("3. Transport Security (TLS)")
    print("""\
  The self-hosted stack terminates TLS at Caddy (reverse proxy) using a
  wildcard certificate from Let's Encrypt for *.<YOUR_DOMAIN>.

  Flow from the client's perspective:

    Your laptop
      │  HTTPS (TLS 1.3, port 443)
      ▼
    Caddy (edge)           ← wildcard cert for *.<YOUR_DOMAIN>
      │  HTTP/1.1 plain-text (loopback / internal network)
      ▼
    API process (:3000)    ← never sees a raw TCP connection from the internet

  Because TLS terminates at Caddy, the API process itself operates on
  plain HTTP.  The Bearer token is protected in transit by the TLS layer;
  it is never logged or stored by Caddy.
""")

    # ─── 4. Inside the API — auth middleware ──────────────────────────────────
    section("4. Inside the API — Authentication Middleware")
    print("""\
  Source: packages/api/internal/auth/

  When a request arrives, the Gin router passes it through the auth middleware
  before it reaches any handler.  The middleware does four things:

  ┌─────────────────────────────────────────────────────────────────┐
  │  Step A — Extract the token                                     │
  │                                                                 │
  │  strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")   │
  │  → rawToken  (e.g. "<YOUR_E2B_API_KEY>")     │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │  Step B — Validate format                                       │
  │                                                                 │
  │  • Must be non-empty                                            │
  │  • Must start with  "e2b_"                                      │
  │  Fails fast with HTTP 401 if either check fails.               │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │  Step C — Database lookup (with Redis cache)                    │
  │                                                                 │
  │  1. Hash the raw token:  sha256(rawToken)                       │
  │  2. Check Redis:  GET auth:<hash>                               │
  │     • HIT  → deserialize cached Team row, skip DB              │
  │     • MISS → SELECT * FROM teams WHERE api_key_hash = $1        │
  │              Cache result in Redis with short TTL (~60 s)       │
  │  Fails with HTTP 401 if no matching row is found.              │
  └─────────────────────────────────────────────────────────────────┘

  ┌─────────────────────────────────────────────────────────────────┐
  │  Step D — Inject into request context                           │
  │                                                                 │
  │  c.Set("team", team)   // Gin context key                      │
  │  Downstream handlers call  auth.GetTeam(c)  to read it.        │
  └─────────────────────────────────────────────────────────────────┘

  The token is NEVER stored in plaintext.  Only the sha256 hash lives in
  Postgres and Redis.  The raw token travels only over TLS and exists in
  memory for the duration of the request.
""")

    # ─── 5. What the DB row looks like ────────────────────────────────────────
    section("5. Postgres Schema (simplified)")
    print("""\
  Table: teams
  ┌──────────────────┬────────────────────────────────────────────┐
  │ column           │ notes                                      │
  ├──────────────────┼────────────────────────────────────────────┤
  │ id               │ UUID primary key                           │
  │ name             │ human-readable team name                   │
  │ api_key_hash     │ sha256 of the raw e2b_… token (hex)        │
  │ tier             │ free | pro | enterprise                    │
  │ is_blocked       │ boolean — 401 if true even on hash match   │
  │ created_at       │ timestamp                                  │
  └──────────────────┴────────────────────────────────────────────┘

  The lookup query executed by sqlc-generated code:

    SELECT id, name, tier, is_blocked
    FROM teams
    WHERE api_key_hash = encode(sha256($1::bytea), 'hex')
    LIMIT 1;

  $1 is the raw token bytes.  The hash is computed inside Postgres so the
  plaintext never touches disk or WAL.
""")

    # ─── 6. What happens on failure ───────────────────────────────────────────
    section("6. Auth Failure Scenarios")
    print("""\
  Scenario                           HTTP status   Body
  ─────────────────────────────────  ─────────────  ────────────────────────
  Missing Authorization header       401            {"message":"missing key"}
  Header present but no "e2b_" prefix 401           {"message":"invalid key"}
  Token not found in DB              401            {"message":"invalid key"}
  Team row has is_blocked = true     401            {"message":"team blocked"}
  Redis down (DB fallback works)     200            (transparent fallback)
  DB down (Redis still warm)         200 or 503     depends on cache TTL

  Note: the API returns the same generic message for "not found" and "bad
  format" — this is intentional to prevent key enumeration.
""")

    # ─── 7. Sandbox-level auth ────────────────────────────────────────────────
    section("7. Per-Operation Authorization (beyond authentication)")
    print("""\
  Authentication answers "who are you?".  The API also enforces:

  • Sandbox ownership  — sandbox.team_id must match the authenticated team.
    Any attempt to read/kill another team's sandbox → HTTP 403.

  • Tier limits        — free-tier teams are capped on concurrent sandboxes
    and timeout maximums.  Enforcement is in packages/api/internal/handlers/.

  • Template access    — public templates are open to all; private templates
    check template.team_id == authenticated team.id.

  These checks happen inside each handler AFTER the auth middleware has
  already verified the key and loaded the Team into the Gin context.
""")

    # ─── 8. Orchestrator / internal services ─────────────────────────────────
    section("8. Internal Services — No Public Auth Required")
    print("""\
  The Orchestrator (gRPC :5008) and Envd (Connect RPC :49983) are never
  reachable from the internet.  They listen only on internal interfaces:

    Orchestrator  → binds to 0.0.0.0:5008 but blocked by iptables;
                    only the API process (same host) can dial it.

    Envd          → binds inside the Firecracker VM.  The only path from the
                    internet is through the client-proxy, which authenticates
                    the sandbox ID before proxying — no separate credential
                    is needed inside the VM.

  Internal service calls from API → Orchestrator carry no credential; trust
  is enforced by network topology (firewall rules + Nomad task groups on the
  same host), not by token-based auth.
""")

    # ─── 9. Quick reference ───────────────────────────────────────────────────
    section("9. Quick Reference")
    code_block([
        "# Test your key against the live API",
        f"curl -s {api_url}/health",
        "",
        "# Authenticated request — list running sandboxes",
        f"curl -s {api_url}/sandboxes \\",
        f"  -H 'Authorization: Bearer {api_key}'",
        "",
        "# Python SDK — key is passed once at Sandbox.create()",
        "from e2b import Sandbox",
        f"sb = Sandbox.create('base', api_key='{masked}',",
        f"                    api_url='{api_url}')",
    ])
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
