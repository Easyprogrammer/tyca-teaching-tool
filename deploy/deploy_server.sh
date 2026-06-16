#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/tyca-tool/app}"
SERVICE_NAME="${SERVICE_NAME:-tyca-tool}"

cd "$APP_ROOT"

python3.11 -m py_compile server/app.py server/smoke_test.py
python3.11 server/smoke_test.py

sudo systemctl daemon-reload
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl --no-pager --full status "$SERVICE_NAME"
