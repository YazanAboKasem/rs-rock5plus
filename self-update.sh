#!/bin/bash
# =============================================================================
# RoadShield — fast-forward this checkout to its upstream branch.
# Called by connect-to-server.sh on every service start, so every device runs
# the same code without anyone logging in. Never fails the caller:
#   - no network / git error      → keep the current code
#   - upstream not a fast-forward → keep the current code (local commits)
#   - new code doesn't compile    → roll back to the previous commit
# Device-specific files written by sync_camera_config.py are preserved.
# Exit status 10 means "updated" (caller should re-exec itself).
# =============================================================================
cd "$(dirname "$0")" || exit 0
DEVICE_FILES="cameras_config.json mediamtx.yml"

timeout 20 git fetch -q 2>/dev/null || { echo "[update] fetch failed — keeping current code"; exit 0; }
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse '@{u}' 2>/dev/null) || exit 0
[ "$OLD" = "$NEW" ] && exit 0
git merge-base --is-ancestor "$OLD" "$NEW" || { echo "[update] upstream is not a fast-forward — skipped"; exit 0; }

TMP=$(mktemp -d)
for f in $DEVICE_FILES; do [ -f "$f" ] && cp -a "$f" "$TMP/"; done
git checkout -q -- $DEVICE_FILES 2>/dev/null
restore() { for f in $DEVICE_FILES; do [ -f "$TMP/$f" ] && cp -a "$TMP/$f" "$f"; done; rm -rf "$TMP"; }

if ! git merge -q --ff-only "$NEW" 2>/dev/null; then
    echo "[update] merge failed (local edits?) — keeping current code"
    restore; exit 0
fi
if ! python3 -m py_compile camera-control.py device_stats.py gps_service.py sync_camera_config.py device_identity.py 2>/dev/null \
   || ! bash -n connect-to-server.sh; then
    echo "[update] new code does not compile — rolling back to ${OLD:0:7}"
    git reset -q --hard "$OLD"
    restore; exit 0
fi
restore
echo "[update] ${OLD:0:7} → $(git log --oneline -1)"
exit 10
