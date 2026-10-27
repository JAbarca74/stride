#!/usr/bin/env bash
# Copies the paper databases and the HALT / KILL_SWITCH files into STATE_DIR (a clone
# of the private state repo) and pushes only when something changed. The state repo
# keeps a single commit (amended and force-pushed): it is a mailbox, not a history.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
: "${STATE_DIR:?STATE_DIR is not set}"

mkdir -p "$STATE_DIR/data"
for DB in data/paper_b.db data/paper_dip.db; do
    # the backup file is not byte-stable, so copy only when the source itself changed
    SUM="$(sha256sum "$DB" 2>/dev/null | cut -d' ' -f1 || true)"
    if [ -f "$DB" ] && [ "$SUM" != "$(cat "$DB.saved" 2>/dev/null || true)" ]; then
        python - "$DB" "$STATE_DIR/$DB" <<'PY'
import sqlite3
import sys

src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
        echo "$SUM" > "$DB.saved"
    fi
done
for FLAG in HALT KILL_SWITCH; do
    for F in "$FLAG"*; do
        if [ -f "$F" ]; then cp "$F" "$STATE_DIR/$F"; fi
    done
done

cd "$STATE_DIR"
git add -A
if git diff --cached --quiet; then
    exit 0
fi
git -c user.name=stride-bot -c user.email=stride-bot@users.noreply.github.com \
    commit -q --amend -m "state $(date -u '+%Y-%m-%dT%H:%MZ')"
git push -q --force origin HEAD:main
echo "state saved"
