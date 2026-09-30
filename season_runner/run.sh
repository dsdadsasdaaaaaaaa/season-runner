#!/bin/bash
# Home Assistant add-on entry point: virtual display, engine, dashboard.
set -u

# Everything that must survive an update lives in /data (the add-on's own
# persistent volume): schedule state, logs, http cache, and the browser
# profile that holds the Sleeper session (HOME=/data -> /data/.mishpacha-browser).
mkdir -p /data/state /data/logs
rm -rf /app/data /app/logs
ln -sfn /data/state /app/data
ln -sfn /data/logs /app/logs
[ -f /data/state/runner_state.json ] || cp /app/seed_state.json /data/state/runner_state.json

export HOME=/data
export DISPLAY=:99

# Which league and whose team: from the add-on's Configuration tab. It stays on
# this machine; nothing identifying is in the image.
export MISHPACHA_LEAGUE_FILE=/data/league.json
python3 - <<'PY'
import json
try:
    o = json.load(open("/data/options.json"))
except Exception:
    o = {}
json.dump({k: o.get(k) for k in ("league_id", "draft_id", "user_id", "username",
                                  "league_name", "draft_slot")},
          open("/data/league.json", "w"))
if not (o.get("league_id") and o.get("user_id")):
    print("[season] league_id / user_id are not set - open the add-on's Configuration tab")
PY
export MISHPACHA_MISH="$(command -v mish)"

# A killed container leaves Chromium's profile "locked" by a dead hostname.
rm -f /data/.mishpacha-browser/Singleton* 2>/dev/null

echo "[season] timezone: ${TZ:-unset}  $(date)"

rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x960x24 -nolisten tcp &
sleep 1
x11vnc -display :99 -forever -shared -nopw -localhost -rfbport 5900 -quiet -bg -o /data/logs/x11vnc.log

# The engine. If it ever exits, start it again.
( while true; do
    mish run --daemon
    echo "[season] engine exited ($?) - restarting in 10s"
    sleep 10
  done ) &

# The dashboard (Home Assistant ingress). If this exits the add-on stops and
# the Supervisor restarts it.
exec python3 -m uvicorn mishpacha.web:app --host 0.0.0.0 --port 8099 --log-level warning
