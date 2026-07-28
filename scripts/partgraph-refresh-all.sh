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
#   * PARTGRAPH_AUTOSTART is FORCED to 0 below and is deliberately NOT
#     overridable from the environment. See the block above the export.
#
# This wrapper assumes the database is already running and reachable; it never
# starts, stops, or health-checks it, and it opens no network ports itself.
# ----------------------------------------------------------------------------

set -euo pipefail

PARTGRAPH_BIN="${PARTGRAPH_BIN:-partgraph}"

# --- the scheduling layer never starts the database (ADR-0014 D1) -----------
# `partgraph refresh` and `partgraph refresh-links` are BOTH autostart-capable
# when a human runs them (ADR-0022 Section 7). On a schedule they must not be:
# a weekly timer that silently starts a container is precisely the unattended,
# nobody-asked-for-it resource use ADR-0022 exists to eliminate.
#
# THIS export is the authoritative mechanism, not the unit's own
# `Environment=PARTGRAPH_AUTOSTART=0`. systemd.exec(5) states, verbatim, of
# EnvironmentFile=: "Settings from these files override settings made with
# Environment=." That is unconditional — it does NOT depend on the order the
# two directives appear in, so the unit's line loses to ANY value the optional
# operator env file (~/.config/partgraph/refresh-all.env) sets for this key,
# and no reordering can change that. A shell assignment here is applied AFTER
# systemd has finished assembling the environment and before `partgraph` is
# ever exec'd, so it is the last word by construction.
#
# Hard-coded on purpose: it is not read from the environment, because the
# environment is the exact channel that cannot be trusted to carry it. An
# operator who genuinely wants a scheduled run to start the database edits
# THIS line, visibly, in a file they installed.
export PARTGRAPH_AUTOSTART=0

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
