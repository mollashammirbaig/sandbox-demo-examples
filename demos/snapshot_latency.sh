#!/usr/bin/env bash
# Benchmark snapshot creation and snapshot restore latency against the control
# plane API on localhost. Run this ON the control plane droplet.
#
# Flow per run:
#   1. POST /sandboxes           → create baseline sandbox      (measures create_ms)
#   2. POST /sandboxes/{id}/snapshots → snapshot it             (measures snapshot_ms)
#   3. DELETE /sandboxes/{id}    → kill original
#   4. POST /sandboxes           → restore from snapshot        (measures restore_ms)
#   5. DELETE /sandboxes/{id}    → kill restored sandbox
#   6. DELETE /templates/{snapshotID} → clean up snapshot
#
# Usage:
#   chmod +x snapshot_latency.sh
#   E2B_API_KEY=e2b_xxx ./snapshot_latency.sh
#   ./snapshot_latency.sh --runs 5
#   ./snapshot_latency.sh --runs 5 --host 127.0.0.1 --port 3000
#
# Env:
#   E2B_API_KEY   required
#   E2B_API_HOST  optional (default 127.0.0.1)
#   E2B_API_PORT  optional (default 3000)

set -euo pipefail

RUNS=3
HOST="${E2B_API_HOST:-127.0.0.1}"
PORT="${E2B_API_PORT:-3000}"
API_KEY="${E2B_API_KEY:-}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --runs) RUNS="$2";  shift 2 ;;
    --host) HOST="$2";  shift 2 ;;
    --port) PORT="$2";  shift 2 ;;
    --key)  API_KEY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -z "$API_KEY" ]] && { echo "ERROR: E2B_API_KEY not set" >&2; exit 1; }

BASE_URL="http://${HOST}:${PORT}"
CURL_W='\n%{http_code} total=%{time_total}\n'

# ── helpers ───────────────────────────────────────────────────────────────────

_curl_timed() {
  # Usage: _curl_timed <tmpfile> <curl args...>
  # Prints: "<http_code> <total_s>"
  local tmp="$1"; shift
  local timing
  timing=$(curl -s -o "$tmp" -w "$CURL_W" "$@" 2>&1 || true)
  local code total_s
  code=$(echo    "$timing" | awk 'NR==2{print $1}')
  total_s=$(echo "$timing" | awk 'NR==2{for(i=1;i<=NF;i++) if($i~/^total=/) print substr($i,7)}')
  echo "$code ${total_s:-0}"
}

_to_ms() { awk "BEGIN{printf \"%.1f\", ${1:-0} * 1000}"; }

_json_field() {
  # naive grep-based extraction — no jq needed
  local field="$1" body="$2"
  echo "$body" | grep -o "\"${field}\":\"[^\"]*\"" | head -1 | sed "s/\"${field}\":\"//;s/\"//"
}

_stats() {
  awk -v data="$1" -v label="$2" '
  BEGIN {
    n = split(data, a, " ")
    if (n == 0) { printf "  %-20s  (no samples)\n", label; exit }
    min=a[1]; max=a[1]; sum=0
    for(i=1;i<=n;i++){ v=a[i]+0; if(v<min)min=v; if(v>max)max=v; sum+=v; s[i]=v }
    for(i=1;i<=n;i++) for(j=i+1;j<=n;j++) if(s[j]<s[i]){t=s[i];s[i]=s[j];s[j]=t}
    mean=sum/n
    p50=_p(50,n,s); p95=_p(95,n,s)
    printf "  %-20s  min=%8.1f  max=%8.1f  mean=%8.1f  p50=%8.1f  p95=%8.1f  (n=%d)\n",
           label, min, max, mean, p50, p95, n
  }
  function _p(p,n,a, k,lo,hi){k=(n-1)*p/100;lo=int(k)+1;hi=lo+1;if(hi>n)hi=n;return a[lo]+(a[hi]-a[lo])*(k-int(k))}
  '
}

# ── banner ────────────────────────────────────────────────────────────────────

echo
echo "════════════════════════════════════════════════════════"
echo "  Snapshot / Restore Latency — control plane direct"
echo "════════════════════════════════════════════════════════"
echo "  Endpoint : ${BASE_URL}"
echo "  Runs     : ${RUNS}  (each run: create → snapshot → kill → restore → kill)"
echo "════════════════════════════════════════════════════════"
echo

# ── collect samples ───────────────────────────────────────────────────────────

declare -a CREATE_MS SNAPSHOT_MS RESTORE_MS

for i in $(seq 1 "$RUNS"); do
  echo "  ── run ${i}/${RUNS} ──────────────────────────────────────"
  TMP=$(mktemp)

  # 1. create baseline sandbox
  read -r code total_s < <(_curl_timed "$TMP" \
    -X POST "${BASE_URL}/sandboxes" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"templateID":"base","timeout":300}')
  BODY=$(cat "$TMP")
  C_MS=$(_to_ms "$total_s")

  if [[ "$code" != "200" && "$code" != "201" ]]; then
    echo "    [✗] create failed  HTTP ${code}  ${BODY}"
    rm -f "$TMP"; continue
  fi
  SBX_ID=$(_json_field "sandboxID" "$BODY")
  printf "    [✓] create    %8s ms  sandboxID=%s\n" "$C_MS" "$SBX_ID"

  # 2. snapshot
  read -r scode stotal_s < <(_curl_timed "$TMP" \
    -X POST "${BASE_URL}/sandboxes/${SBX_ID}/snapshots" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{}')
  SBODY=$(cat "$TMP")
  S_MS=$(_to_ms "$stotal_s")

  if [[ "$scode" != "200" && "$scode" != "201" ]]; then
    echo "    [✗] snapshot failed  HTTP ${scode}  ${SBODY}"
    # still kill the original sandbox
    curl -s -o /dev/null -X DELETE "${BASE_URL}/sandboxes/${SBX_ID}" \
      -H "X-API-Key: ${API_KEY}" || true
    rm -f "$TMP"; continue
  fi
  SNAP_ID=$(_json_field "snapshotID" "$SBODY")
  printf "    [✓] snapshot  %8s ms  snapshotID=%s\n" "$S_MS" "$SNAP_ID"

  # 3. kill original sandbox
  curl -s -o /dev/null -X DELETE "${BASE_URL}/sandboxes/${SBX_ID}" \
    -H "X-API-Key: ${API_KEY}" || true

  # 4. restore from snapshot
  read -r rcode rtotal_s < <(_curl_timed "$TMP" \
    -X POST "${BASE_URL}/sandboxes" \
    -H "X-API-Key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"templateID\":\"${SNAP_ID}\",\"timeout\":300}")
  RBODY=$(cat "$TMP")
  R_MS=$(_to_ms "$rtotal_s")

  if [[ "$rcode" != "200" && "$rcode" != "201" ]]; then
    echo "    [✗] restore failed  HTTP ${rcode}  ${RBODY}"
  else
    RESTORE_SBX=$(_json_field "sandboxID" "$RBODY")
    printf "    [✓] restore   %8s ms  sandboxID=%s\n" "$R_MS" "$RESTORE_SBX"

    # 5. kill restored sandbox
    curl -s -o /dev/null -X DELETE "${BASE_URL}/sandboxes/${RESTORE_SBX}" \
      -H "X-API-Key: ${API_KEY}" || true

    CREATE_MS+=("$C_MS")
    SNAPSHOT_MS+=("$S_MS")
    RESTORE_MS+=("$R_MS")
  fi

  # 6. delete snapshot template
  if [[ -n "$SNAP_ID" ]]; then
    curl -s -o /dev/null -X DELETE "${BASE_URL}/templates/${SNAP_ID}" \
      -H "X-API-Key: ${API_KEY}" || true
  fi

  rm -f "$TMP"
  echo
done

# ── summary ───────────────────────────────────────────────────────────────────

echo "════════════════════════════════════════════════════════"
echo "  Summary (ms)"
echo "════════════════════════════════════════════════════════"
_stats "${CREATE_MS[*]:-}"   "create (baseline)"
_stats "${SNAPSHOT_MS[*]:-}" "snapshot"
_stats "${RESTORE_MS[*]:-}"  "restore from snapshot"
echo "════════════════════════════════════════════════════════"
echo
