#!/bin/zsh
# launchd entry point for the 2am research podcast run.
# Spawns a headless Opus agent on automation/RESEARCH-NIGHTLY.md.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HF_HOME="$HOME/.cache/huggingface"

PODPUB="/Users/mo64/My Drive/_03_Projects/F_Personal-Apps/podpub"
STATE="$PODPUB/automation/state"
LOGDIR="$PODPUB/automation/logs"
LOCK="$STATE/research.lock"
LOG="$LOGDIR/research-$(date +%Y%m%d).log"

mkdir -p "$STATE" "$LOGDIR"

# Single instance. A lock older than 6h is from a dead run; clear it.
if [ -d "$LOCK" ]; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null
  else
    echo "$(date -Iseconds) another research run holds the lock, exiting" >> "$LOG"
    exit 0
  fi
fi
mkdir "$LOCK" || exit 0
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$PODPUB" || exit 1
echo "$(date -Iseconds) research run starting" >> "$LOG"

# caffeinate keeps the machine awake for the pipeline wait and MOSS synthesis.
caffeinate -i claude -p \
  "Read '$PODPUB/automation/RESEARCH-NIGHTLY.md' and follow it exactly. Tonight's run date: $(date +%Y-%m-%d)." \
  --model claude-opus-5 \
  --dangerously-skip-permissions \
  >> "$LOG" 2>&1
rc=$?

echo "$(date -Iseconds) research run finished rc=$rc" >> "$LOG"
exit $rc
