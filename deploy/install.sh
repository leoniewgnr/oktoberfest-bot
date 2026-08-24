#!/usr/bin/env bash
# Install the monitor as a systemd service. Idempotent: safe to re-run to upgrade.
#
#   TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... HEALTHCHECK_URL=... \
#     sudo -E bash deploy/install.sh
#
# SCRAPER_TYPES defaults to running every target. On a host that cannot reach the
# booking API, or that cannot run Chromium, narrow it — e.g.
#   SCRAPER_TYPES='["api_fzos","announcement"]'
set -euo pipefail

DEST=${DEST:-/opt/oktoberfest-bot}
# The service must run as a real user, not root: playwright installs its browser
# into $HOME/.cache, and a root-run unit would look in /root and not find it.
RUN_USER=${RUN_USER:-${SUDO_USER:-$(id -un)}}
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
REPO=${REPO:-https://github.com/leoniewgnr/oktoberfest-bot.git}
SCRAPER_TYPES=${SCRAPER_TYPES:-[]}
SERVICE=oktoberfest-bot.service

for var in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  [ -n "${!var:-}" ] || { echo "error: $var is required"; exit 1; }
done
[ -n "${HEALTHCHECK_URL:-}" ] || echo "warning: HEALTHCHECK_URL unset - the external dead-man's switch will be OFF"

echo "== code =="
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" pull --ff-only
else
  git clone "$REPO" "$DEST"
fi
cd "$DEST"

echo "== venv =="
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -e .

echo "== chromium (only needed for the form_select tents) =="
sudo ./venv/bin/playwright install-deps chromium || echo "   (system deps step non-fatal)"
if sudo -u "$RUN_USER" HOME="$RUN_HOME" ./venv/bin/playwright install chromium; then
  echo "   chromium ready"
else
  echo "   WARNING: chromium install failed - narrow SCRAPER_TYPES to exclude form_select"
fi

echo "== config =="
mkdir -p "$DEST/logs"
TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
HEALTHCHECK_URL="${HEALTHCHECK_URL:-}" DEST="$DEST" SCRAPER_TYPES="$SCRAPER_TYPES" \
./venv/bin/python - <<'PY'
import json, os, pathlib
dest = os.environ["DEST"]
path = pathlib.Path(dest) / "config" / "config.json"
config = json.loads(path.read_text()) if path.exists() else {}
config.update({
    "telegram_bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
    "telegram_chat_id": os.environ["TELEGRAM_CHAT_ID"],
    "state_file": f"{dest}/state.json",
    "log_file": f"{dest}/logs/monitor.log",
    "healthcheck_url": os.environ.get("HEALTHCHECK_URL", ""),
    "enabled_scraper_types": json.loads(os.environ["SCRAPER_TYPES"]),
})
config.setdefault("heartbeat_interval_seconds", 3600)
config.setdefault("blind_alert_after_seconds", 900)
config.setdefault("blind_realert_interval_seconds", 1800)
config.setdefault("max_slot_age_for_display_seconds", 3600)
path.write_text(json.dumps(config, indent=2) + "\n")
path.chmod(0o600)  # holds the bot token
print(f"   wrote {path} (types={config['enabled_scraper_types'] or 'ALL'})")
PY

echo "== ownership: everything the service touches must belong to the run user =="
chown -R "$RUN_USER":"$RUN_USER" "$DEST"

echo "== logrotate (monitor.log reached 338 MB once) =="
cat > /etc/logrotate.d/oktoberfest-bot <<LR
$DEST/logs/*.log {
    daily
    rotate 7
    maxsize 50M
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su $RUN_USER $RUN_USER
}
LR

echo "== systemd =="
cat > "/etc/systemd/system/$SERVICE" <<UNIT
[Unit]
Description=Oktoberfest Tent Reservation Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
Environment=HOME=$RUN_HOME
WorkingDirectory=$DEST
ExecStart=$DEST/venv/bin/python -m oktoberfest_bot.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "$SERVICE"

sleep 20
systemctl is-active "$SERVICE" && echo "== running ==" || { journalctl -u "$SERVICE" -n 30 --no-pager; exit 1; }
tail -20 "$DEST/logs/monitor.log" 2>/dev/null || true
echo
echo "Done. Watch it with:  journalctl -u $SERVICE -f"
