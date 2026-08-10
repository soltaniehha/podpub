#!/bin/zsh
# launchd entry point for the morning monitor.
# Spawns a headless Opus agent on automation/MONITOR.md to verify and, where
# safe, repair last night's research podcast run.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export HF_HOME="$HOME/.cache/huggingface"

PODPUB="/Users/mo64/My Drive/_03_Projects/F_Personal-Apps/podpub"
STATE="$PODPUB/automation/state"
LOGDIR="$PODPUB/automation/logs"
RESEARCH_LOCK="$STATE/research.lock"
LOCK="$STATE/monitor.lock"
LOG="$LOGDIR/monitor-$(date +%Y%m%d).log"

mkdir -p "$STATE" "$LOGDIR"

# If the night run is still going, do not pile on. launchd will fire us again tomorrow;
# a late-but-running night needs no rescue.
if [ -d "$RESEARCH_LOCK" ] && [ -z "$(find "$RESEARCH_LOCK" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
  echo "$(date -Iseconds) research run still active, monitor standing down" >> "$LOG"
  exit 0
fi

if [ -d "$LOCK" ]; then
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +240 2>/dev/null)" ]; then
    rmdir "$LOCK" 2>/dev/null
  else
    echo "$(date -Iseconds) another monitor run holds the lock, exiting" >> "$LOG"
    exit 0
  fi
fi
mkdir "$LOCK" || exit 0
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

cd "$PODPUB" || exit 1
echo "$(date -Iseconds) monitor starting" >> "$LOG"

caffeinate -i claude -p \
  "Read '$PODPUB/automation/MONITOR.md' and follow it exactly. Today's date: $(date +%Y-%m-%d)." \
  --model claude-opus-5 \
  --dangerously-skip-permissions \
  >> "$LOG" 2>&1
rc=$?

echo "$(date -Iseconds) monitor finished rc=$rc" >> "$LOG"
exit $rc
