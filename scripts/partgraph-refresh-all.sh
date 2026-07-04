#!/usr/bin/env bash
#
# partgraph-refresh-all.sh
# ----------------------------------------------------------------------------
# One-shot orchestrator that runs BOTH PartGraph refresh commands, in order:
#
#   phase 1/2:  partgraph refresh        (stock / price / basic status)
#   phase 2/2:  partgraph refresh-links  (datasheet link freshness + auto-purge)
#
# It is meant to be invoked by an EXTERNAL scheduler (a systemd timer or a cron
# job) — PartGraph ships no in-app daemon. See docs/scheduling.md.
#
# Behaviour:
#   * Both phases are ALWAYS attempted, even if phase 1 fails, so a stock/price
#     hiccup never skips datasheet-link maintenance.
#   * The exit status is aggregated: 0 only when BOTH phases exit 0, otherwise
#     the first non-zero phase status is propagated to the caller / scheduler.
#   * The CLI is resolved through ${PARTGRAPH_BIN:-partgraph}, so no install
#     path is baked in; override PARTGRAPH_BIN to point at a specific binary.
#   * Set PARTGRAPH_REFRESH_FETCH to any non-empty value to add --fetch to
#     phase 1 (re-download the ~1 GB source snapshot); unset/empty = no fetch.
#
# This wrapper assumes the database is already running and reachable; it never
# starts, stops, or health-checks it, and it opens no network ports itself.
# ----------------------------------------------------------------------------

set -euo pipefail

PARTGRAPH_BIN="${PARTGRAPH_BIN:-partgraph}"

command -v "$PARTGRAPH_BIN" >/dev/null 2>&1 || {
    echo "partgraph-refresh-all: required command not found: ${PARTGRAPH_BIN}" >&2
    echo "partgraph-refresh-all: install PartGraph, or set PARTGRAPH_BIN to the CLI name or path." >&2
    exit 127
}

# Emit a UTC-timestamped, path-free banner line to stdout.
log() {
    printf '%s  %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"
}

# --- phase 1/2: stock / price / basic-status refresh ------------------------
log "phase 1/2 refresh — start"
rc1=0
if [[ -n "${PARTGRAPH_REFRESH_FETCH:-}" ]]; then
    log "phase 1/2 refresh — PARTGRAPH_REFRESH_FETCH set; adding --fetch"
    "$PARTGRAPH_BIN" refresh --fetch || rc1=$?
else
    log "phase 1/2 refresh — no --fetch (set PARTGRAPH_REFRESH_FETCH to opt in)"
    "$PARTGRAPH_BIN" refresh || rc1=$?
fi
log "phase 1/2 refresh — exit=${rc1}"

# --- phase 2/2: datasheet-link freshness + auto-purge -----------------------
# Attempted unconditionally, even when phase 1 failed above.
log "phase 2/2 refresh-links — start"
rc2=0
"$PARTGRAPH_BIN" refresh-links || rc2=$?
log "phase 2/2 refresh-links — exit=${rc2}"

# --- aggregate exit: 0 iff both phases succeeded ----------------------------
rc=0
if [[ "$rc1" -ne 0 ]]; then
    rc="$rc1"
elif [[ "$rc2" -ne 0 ]]; then
    rc="$rc2"
fi
log "overall exit=${rc} (phase1=${rc1} phase2=${rc2})"
exit "$rc"
