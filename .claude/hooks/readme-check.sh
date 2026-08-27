#!/usr/bin/env bash
# Stop hook: notice when a session changed behaviour but left README.md alone.
#
# The README is this project's only documentation and it quotes real numbers,
# so it goes stale the moment behaviour changes. A skill alone cannot catch
# that - a skill only loads when Claude thinks to load it. This fires on the
# actual event instead, and hands the work to the update-readme skill.
#
# Blocks the stop ONCE per distinct set of changed files. Declining to update
# the README is a valid answer; being asked twice about the same change is not.
set -uo pipefail

root="${CLAUDE_PROJECT_DIR:-$PWD}"
cd "$root" 2>/dev/null || exit 0

payload=$(cat 2>/dev/null || true)

# Guard 1: never fire on the continuation our own block caused.
printf '%s' "$payload" | grep -q '"stop_hook_active"[[:space:]]*:[[:space:]]*true' && exit 0

command -v git >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

# Only the files whose behaviour the README actually describes. Tests, the
# scratchpad and the journals are deliberately absent - they change constantly
# and document nothing a reader sees.
changed=$(git status --porcelain -- \
  'flightbot/*.py' 'ui/index.html' 'check.py' 'watches.json' '.github/workflows' \
  2>/dev/null)
[ -n "$changed" ] || exit 0

# Already edited alongside the code - nothing to say.
[ -n "$(git status --porcelain -- README.md 2>/dev/null)" ] && exit 0

# Guard 2: one prompt per distinct change set.
key=$(printf '%s' "$changed" | git hash-object --stdin 2>/dev/null) || exit 0
slot=$(printf '%s' "$root" | git hash-object --stdin 2>/dev/null | cut -c1-12)
stamp="${TMPDIR:-/tmp}/claude-readme-hook-$slot"
[ "$(cat "$stamp" 2>/dev/null)" = "$key" ] && exit 0
printf '%s' "$key" >"$stamp" 2>/dev/null

files=$(printf '%s' "$changed" | awk '{print $NF}' | sort | tr '\n' ' ')

python - "$files" <<'PY'
import json, sys
files = sys.argv[1].strip()
print(json.dumps({"decision": "block", "reason": (
    f"README.md may be stale: this session changed {files} but did not touch "
    "README.md. Invoke the update-readme skill, check the sections that cover "
    "what changed, and verify any numbers by running the code rather than "
    "reasoning about them. If this change genuinely needs no README edit, say "
    "so in one line and stop - you will not be asked again for this change set."
)}))
PY
