#!/usr/bin/env bash
# One-time server setup (run on your EC2/VPS as root or with sudo).
# Example: sudo bash server-setup.sh /home/azureuser/convoy_prod azureuser

set -euo pipefail

DEPLOY_PATH="${1:-/home/azureuser/convoy_prod}"
APP_USER="${2:-azureuser}"

echo "Installing system packages..."
apt-get update
apt-get install -y python3 python3-venv python3-pip rsync curl

mkdir -p "${DEPLOY_PATH}/logs"
chown -R "${APP_USER}:${APP_USER}" "${DEPLOY_PATH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sed "s|/home/azureuser/convoy_prod|${DEPLOY_PATH}|g; s|User=azureuser|User=${APP_USER}|g; s|Group=azureuser|Group=${APP_USER}|g" \
  "${SCRIPT_DIR}/../deploy/convoy-api.service" > /etc/systemd/system/convoy-api.service

systemctl daemon-reload
systemctl enable convoy-api

echo "Done. Next steps:"
echo "1. Create ${DEPLOY_PATH}/.env with production secrets"
echo "2. Push to main — GitHub Actions will sync code and restart convoy-api"
echo "3. Or start manually: sudo systemctl start convoy-api"
