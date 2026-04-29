#!/usr/bin/env bash
# Measure sandbox create latency by hitting the control plane API directly on
# localhost:3000 (bypasses Caddy/TLS). Run this ON the control plane droplet.
#
# Usage:
#   chmod +x create_latency.sh
#   E2B_API_KEY=e2b_xxx ./create_latency.sh
#   ./create_latency.sh --runs 10
#   ./create_latency.sh --runs 10 --host 127.0.0.1 --port 3000
#
# Env:
#   E2B_API_KEY   required
#   E2B_API_HOST  optional (default 127.0.0.1)
#   E2B_API_PORT  optional (default 3000)

set -euo pipefail

# ── defaults ──────────────────────────────────────────────────────────────────
RUNS=5
HOST="${E2B_API_HOST:-127.0.0.1}"
PORT="${E2B_API_PORT:-3000}"
API_KEY="${E2B_API_KEY:-}"

# ── arg parse ─────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --runs)   RUNS="$2";  shift 2 ;;
    --host)   HOST="$2";  shift 2 ;;
    --port)   PORT="$2";  shift 2 ;;
    --key)    API_KEY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "$API_KEY" ]]; then
  echo "ERROR: E2B_API_KEY is not set. Export it or pass --key." >&2
  exit 1
fi

BASE_URL="http://${HOST}:${PORT}"

# curl timing format — all values in seconds, we convert to ms in awk
CURL_FMT='\n%{http_code} dns=%{time_namelookup} connect=%{time_connect} ttfb=%{time_starttransfer} total=%{time_total}\n'

echo
echo "════════════════════════════════════════════════════"
echo "  Sandbox Create Latency — control plane direct"
echo "════════════════════════════════════════════════════"
echo "  Endpoint : POST ${BASE_URL}/sandboxes"
echo "  Runs     : ${RUNS}"
echo "════════════════════════════════════════════════════"
echo

# ── collect samples ───────────────────────────────────────────────────────────
declare -a TIMES_MS
declare -a SANDBOX_IDS

for i in $(seq 1 "$RUNS"); do
  TMPFILE=$(mktemp)

  # POST /sandboxes — body matches what the SDK sends
  TIMING=$(curl -s -o "$TMPFILE" \
    -w "$CURL_FMT" \
    -X POST "${BASE_URL}/sandboxes" \
    -H "Authorization: Bearer ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"templateID":"base","timeout":120}' 2>&1 || true)

  HTTP_CODE=$(echo "$TIMING" | awk 'NR==2{print $1}')
  TOTAL_S=$(echo  "$TIMING" | awk 'NR==2{for(i=1;i<=NF;i++) if($i~/^total=/) print substr($i,7)}')
  TTFB_S=$(echo   "$TIMING" | awk 'NR==2{for(i=1;i<=NF;i++) if($i~/^ttfb=/)  print substr($i,6)}')

  # convert seconds → ms via awk (no bc dependency)
  TOTAL_MS=$(awk "BEGIN{printf \"%.1f\", ${TOTAL_S:-0} * 1000}")
  TTFB_MS=$(awk  "BEGIN{printf \"%.1f\", ${TTFB_S:-0}  * 1000}")

  BODY=$(cat "$TMPFILE")
  rm -f "$TMPFILE"

  # extract sandboxID from JSON response (no jq dependency)
  SBX_ID=$(echo "$BODY" | grep -o '"sandboxID":"[^"]*"' | head -1 | sed 's/"sandboxID":"//;s/"//')

  if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
    TAG="✓"
    SANDBOX_IDS+=("$SBX_ID")
  else
    TAG="✗"
    SBX_ID="(HTTP ${HTTP_CODE})"
  fi

  TIMES_MS+=("$TOTAL_MS")
  printf "  [%s] run %*d  total=%8s ms  ttfb=%8s ms  %s\n" \
    "$TAG" "${#RUNS}" "$i" "$TOTAL_MS" "$TTFB_MS" "$SBX_ID"

  # small pause between runs to avoid hammering postgres
  if [[ "$i" -lt "$RUNS" ]]; then sleep 0.2; fi
done

# ── stats ─────────────────────────────────────────────────────────────────────
echo
echo "────────────────────────────────────────────────────"

awk -v data="${TIMES_MS[*]}" '
BEGIN {
  n = split(data, a, " ")
  if (n == 0) { print "  No successful samples."; exit }

  min = a[1]; max = a[1]; sum = 0
  for (i = 1; i <= n; i++) {
    v = a[i]+0
    if (v < min) min = v
    if (v > max) max = v
    sum += v
    sorted[i] = v
  }
  # bubble sort for percentiles
  for (i = 1; i <= n; i++)
    for (j = i+1; j <= n; j++)
      if (sorted[j] < sorted[i]) { t=sorted[i]; sorted[i]=sorted[j]; sorted[j]=t }

  mean = sum / n

  # linear-interpolation percentile
  p50  = _pct(50,  n, sorted)
  p95  = _pct(95,  n, sorted)
  p99  = _pct(99,  n, sorted)

  printf "  %-8s %10.1f ms\n", "min",  min
  printf "  %-8s %10.1f ms\n", "max",  max
  printf "  %-8s %10.1f ms\n", "mean", mean
  printf "  %-8s %10.1f ms\n", "p50",  p50
  printf "  %-8s %10.1f ms\n", "p95",  p95
  printf "  %-8s %10.1f ms\n", "p99",  p99
  printf "  %-8s %10d\n",      "n",    n
}
function _pct(p, n, a,    k, lo, hi) {
  k  = (n - 1) * p / 100
  lo = int(k) + 1
  hi = int(k) + 2
  if (hi > n) hi = n
  return a[lo] + (a[hi] - a[lo]) * (k - int(k))
}
'

echo "────────────────────────────────────────────────────"
echo

# ── kill created sandboxes ────────────────────────────────────────────────────
if [[ ${#SANDBOX_IDS[@]} -gt 0 ]]; then
  printf "  Killing %d sandbox(es)..." "${#SANDBOX_IDS[@]}"
  for sbx in "${SANDBOX_IDS[@]}"; do
    curl -s -o /dev/null -X DELETE "${BASE_URL}/sandboxes/${sbx}" \
      -H "Authorization: Bearer ${API_KEY}" || true
  done
  echo " done."
  echo
fi
