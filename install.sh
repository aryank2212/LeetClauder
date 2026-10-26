#!/usr/bin/env bash
# Install leetcode-daily as a systemd user timer. It runs 2 minutes after you log
# in, then hourly; once today's problems are solved, each run exits immediately.
# Re-run this script after editing leetcode_daily.py to install the new version.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/leetcode-daily"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/leetcode-daily"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
  echo "uv is required (sudo pacman -S uv)" >&2
  exit 1
fi

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$UNIT_DIR"

# Copy onto the root filesystem: the drive this project lives on may not be
# mounted yet when the timer fires after login.
install -m 755 "$SRC/leetcode_daily.py" "$APP_DIR/leetcode_daily.py"

if [[ ! -f "$CONFIG_DIR/config.env" ]]; then
  install -m 600 "$SRC/config.example.env" "$CONFIG_DIR/config.env"
  echo "Created $CONFIG_DIR/config.env"
fi

echo "Installing Python dependencies..."
"$UV" run --quiet --script "$APP_DIR/leetcode_daily.py" --help >/dev/null

cat > "$UNIT_DIR/leetcode-daily.service" <<EOF
[Unit]
Description=Solve today's random LeetCode problems with a local model

[Service]
Type=oneshot
ExecStart=$UV run --quiet --script $APP_DIR/leetcode_daily.py
TimeoutStartSec=2h
EOF

cat > "$UNIT_DIR/leetcode-daily.timer" <<EOF
[Unit]
Description=Run leetcode-daily after login, then hourly until today's problems are solved

[Timer]
OnStartupSec=2min
OnUnitInactiveSec=1h

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now leetcode-daily.timer

cat <<EOF

Installed. Next:
  1. Fill in $CONFIG_DIR/config.env
  2. Check the setup:  $UV run --script $APP_DIR/leetcode_daily.py --check
EOF
