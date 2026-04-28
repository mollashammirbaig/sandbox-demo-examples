#!/usr/bin/env python3
"""LLM Agent + E2B Sandbox: Groq LLM executes code in an isolated Firecracker VM.

WHY LLM AGENTS NEED SANDBOXES
──────────────────────────────
LLMs generate code you can't fully trust: it may use wrong libraries, read
sensitive files, or produce side-effects.  Running that code directly on the
host is dangerous.  The E2B sandbox isolates every execution in a Firecracker
microVM — AI-generated code never touches the host filesystem, env vars, or
production services.

WHY SANDBOXES NEED THE AGENT LOOP
───────────────────────────────────
Code often fails on the first attempt (missing library, wrong assumption).
The agent loop closes that gap automatically:
  LLM reasons → emits code_exec → sandbox runs it → error returned as context
  → LLM self-corrects → repeat until success (or MAX_TURNS reached).
No human in the loop. No unsafe code on the host.

Install:
  pip install groq e2b

Usage:
  python3 demos/e2b_llm_agent.py                  # default: data
  python3 demos/e2b_llm_agent.py --task selfheal   # showcases self-correction
  python3 demos/e2b_llm_agent.py --task sandboxed  # showcases isolation
  python3 demos/e2b_llm_agent.py --task data
  python3 demos/e2b_llm_agent.py --task files
  python3 demos/e2b_llm_agent.py --task multiStep

Env (all optional except GROQ_API_KEY):
  GROQ_API_KEY        required (free at console.groq.com)
  E2B_API_KEY         required (see .env)
  E2B_API_URL         required (see .env)
  E2B_DOMAIN          required (see .env)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # install python-dotenv or set env vars manually

try:
    from groq import Groq
except ImportError:
    print("Install:  pip install groq", file=sys.stderr)
    raise SystemExit(1)

try:
    from e2b import Sandbox
    from e2b.sandbox.commands.command_handle import CommandExitException
except ImportError:
    print("Install:  pip install e2b", file=sys.stderr)
    raise SystemExit(1)

# ── Config ────────────────────────────────────────────────────────────────────

MODEL           = "llama-3.3-70b-versatile"
MAX_TOKENS      = 4096
MAX_TURNS       = 10   # safety cap on the agent loop

# ── Pre-defined tasks ─────────────────────────────────────────────────────────

TASKS: dict[str, str] = {
    "data": """\
Generate a synthetic sales dataset (50 rows: product, region, units_sold, revenue).
Then compute:
  1. Total revenue by product (top 5).
  2. Average units sold per region.
  3. The single best-performing product+region combination.
Print everything as nicely formatted tables using only the Python standard library.
""",

    "files": """\
Inside the sandbox:
  1. Write a JSON config file at /tmp/agent_config.json with keys:
       version, environment, features (list of 3 strings), max_sandboxes.
  2. Read it back and pretty-print it.
  3. Append a timestamp field to the JSON and write it again.
  4. Confirm the final file contents.
Use only the Python standard library.
""",

    "multiStep": """\
Solve this step by step, using a separate code execution for each step:

Step 1 — Generate 200 random integers between 1 and 1000, save to /tmp/numbers.txt
Step 2 — Read /tmp/numbers.txt and compute: mean, median, std-dev, min, max
Step 3 — Find all prime numbers in the list
Step 4 — Write a summary report to /tmp/report.txt and print its contents

Use only the Python standard library. Show your reasoning between steps.
""",

    # ── Showcase tasks ────────────────────────────────────────────────────────

    "selfheal": """\
SHOWCASE: Self-correcting agent loop
─────────────────────────────────────
This task is designed to produce an error on the first attempt so you can
watch the agent detect it and fix it autonomously — no human in the loop.

Task: Analyze the following sales data and produce a bar chart saved to
/tmp/sales_chart.png, then print per-product revenue sorted descending.

Data (CSV):
product,units,revenue
Widget,150,4500
Gadget,89,2670
Doohickey,210,6300
Thingamajig,45,1350
Whatsit,178,5340

Instructions:
- First attempt: use matplotlib to draw the chart.
- If matplotlib is unavailable, fall back to an ASCII bar chart printed to
  stdout and skip the PNG.
- Always print the sorted revenue table regardless.
Use only what is actually available in the sandbox.
""",

    "sandboxed": """\
SHOWCASE: Sandbox isolation
────────────────────────────
This task proves that AI-generated code runs inside an isolated VM, not on
the host machine. Execute all of the following and report what you find:

1. Print the hostname and current user — confirm it is NOT the developer's laptop.
2. Try to read /etc/shadow — show the sandbox safely contains sensitive-file access.
3. Print every environment variable — confirm no host secrets (AWS keys, tokens) leak in.
4. Write a file to /tmp/sandbox_proof.txt with the text:
       "Code executed safely inside E2B sandbox — host filesystem untouched."
   Then read it back to prove the sandbox has a real, writable filesystem.
5. Try to write to /host-not-mounted/test.txt — confirm the host fs is not mounted.

Summarise: what does each result tell us about sandbox isolation?
""",
}

# ── Tool definition (OpenAI/Groq format) ──────────────────────────────────────

CODE_EXEC_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "code_exec",
        "description": (
            "Execute Python code inside a secure, isolated E2B Firecracker sandbox. "
            "The sandbox persists for the duration of the conversation so files written "
            "in one call are readable in the next. "
            "Always capture output with print(). "
            "Return value: stdout + stderr combined."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Valid Python 3 source code to execute.",
                },
                "description": {
                    "type": "string",
                    "description": "One-line description of what this code does (shown in logs).",
                },
            },
            "required": ["code"],
        },
    },
}


# ── Sandbox execution ─────────────────────────────────────────────────────────

def exec_in_sandbox(sb: Sandbox, code: str) -> str:
    """Run Python code in the sandbox; return stdout+stderr as a single string."""
    # Unique path per call avoids permission errors from re-writing the same file.
    script_path = f"/tmp/_agent_exec_{uuid.uuid4().hex[:8]}.py"
    try:
        sb.files.write(script_path, code)
    except Exception as e:
        return f"[sandbox file write error] {e}"

    try:
        result = sb.commands.run(f"python3 {script_path}", timeout=60)
        parts = []
        if result.stdout:
            parts.append(result.stdout.rstrip())
        if result.stderr:
            parts.append(f"[stderr]\n{result.stderr.rstrip()}")
        if result.exit_code not in (0, None):
            parts.append(f"[exit code {result.exit_code}]")
        return "\n".join(parts) if parts else "(no output)"
    except CommandExitException as e:
        out = []
        if e.stdout:
            out.append(e.stdout.rstrip())
        if e.stderr:
            out.append(f"[stderr]\n{e.stderr.rstrip()}")
        out.append(f"[exit code {e.exit_code}]")
        return "\n".join(out)
    except Exception as e:
        return f"[sandbox exec error] {e}"


# ── Agent loop ────────────────────────────────────────────────────────────────

def run_agent(task: str, sb: Sandbox) -> str:
    """
    Run the Groq agent loop against the given task.

    The loop:
      1. Send messages to Groq with the code_exec tool available.
      2. For every tool_call in the response, execute the code in the
         sandbox and collect results.
      3. Append assistant turn + tool results and repeat.
      4. Stop when the model responds with no tool calls or MAX_TURNS is reached.
    Returns the model's final text response.
    """
    client = Groq()
    messages: list[dict] = [{"role": "user", "content": task}]

    print(f"\n{'─'*64}")
    print(f"  TASK\n{'─'*64}")
    print(f"  {task.strip().splitlines()[0]}…")
    print(f"{'─'*64}\n")

    final_text = ""

    for turn in range(1, MAX_TURNS + 1):
        print(f"  [turn {turn}] Calling {MODEL} via Groq…", flush=True)

        response = client.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            tools=[CODE_EXEC_TOOL],
            tool_choice="auto",
            messages=messages,
        )

        message = response.choices[0].message
        stop_reason = response.choices[0].finish_reason

        # Collect any text the model emitted this turn
        if message.content:
            print(f"\n  [model reasoning]\n  {message.content[:400].replace(chr(10), chr(10)+'  ')}")
            final_text = message.content

        # If no tool calls, model is done
        tool_calls = message.tool_calls or []
        if stop_reason == "stop" or not tool_calls:
            print(f"\n  [done — finish_reason={stop_reason}]")
            break

        # Append assistant message (with tool_calls) to history
        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })

        # Execute each tool call in the sandbox
        for tc in tool_calls:
            args = json.loads(tc.function.arguments)
            code = args.get("code", "")
            desc = args.get("description", "(no description)")
            print(f"\n  [code_exec] {desc}")
            print(f"  {'·'*56}")
            for line in code.strip().splitlines()[:8]:
                print(f"    {line}")
            if len(code.strip().splitlines()) > 8:
                print(f"    … ({len(code.strip().splitlines())} lines total)")
            print(f"  {'·'*56}", flush=True)

            t0 = time.perf_counter()
            output = exec_in_sandbox(sb, code)
            elapsed = (time.perf_counter() - t0) * 1000

            print(f"  [output]  ({elapsed:.0f} ms)")
            for line in output.splitlines()[:20]:
                print(f"    {line}")
            if len(output.splitlines()) > 20:
                print(f"    … ({len(output.splitlines())} lines total)")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": output,
            })

    else:
        print(f"  [warning] reached MAX_TURNS ({MAX_TURNS}) without stop")

    return final_text


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--task",
        choices=list(TASKS),
        default="selfheal",
        help="Pre-built task to run (default: selfheal)",
    )
    ap.add_argument(
        "--prompt",
        help="Custom task prompt (overrides --task)",
    )
    args = ap.parse_args()

    if not os.environ.get("GROQ_API_KEY"):
        print("Error: GROQ_API_KEY env var is required. Get a free key at console.groq.com", file=sys.stderr)
        return 1

    task = args.prompt if args.prompt else TASKS[args.task]

    e2b_opts = {
        "api_key": os.getenv("E2B_API_KEY"),
        "api_url": os.getenv("E2B_API_URL"),
        "domain":  os.getenv("E2B_DOMAIN"),
    }

    print(f"\n{'═'*64}")
    print(f"  E2B LLM Agent Demo")
    print(f"{'═'*64}")
    print(f"  Model  : {MODEL}")
    print(f"  Task   : {args.task if not args.prompt else 'custom'}")
    print(f"  API    : {e2b_opts['api_url']}")
    print(f"{'═'*64}\n")

    print("  Creating sandbox…", flush=True)
    t0 = time.perf_counter()
    try:
        sb = Sandbox.create("base", timeout=300, **e2b_opts)
    except Exception as e:
        print(f"  Failed to create sandbox: {e}", file=sys.stderr)
        return 1
    print(f"  sandbox_id = {sb.sandbox_id}  ({(time.perf_counter()-t0)*1000:.0f} ms)\n")

    try:
        final_answer = run_agent(task, sb)

        print(f"\n{'═'*64}")
        print(f"  FINAL ANSWER")
        print(f"{'═'*64}")
        print(final_answer)
        print(f"{'═'*64}\n")
    finally:
        print("  Killing sandbox…", end=" ", flush=True)
        sb.kill()
        print("done.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
