#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH_HOST="${DEPLOY_SSH_HOST:-ovh2}"
REMOTE_PATH="${DEPLOY_REMOTE_PATH:-/opt/infer-takehome}"
SITE_NAME="infer.discordwell.com"
CONTROL_DIR="$(mktemp -d "${TMPDIR:-/tmp}/infer-deploy-ssh.XXXXXX")"
CONTROL_PATH="${CONTROL_DIR}/control"
SSH=(ssh -o ControlMaster=auto -o ControlPath="${CONTROL_PATH}" -o ControlPersist=60)
SCP=(scp -o ControlMaster=auto -o ControlPath="${CONTROL_PATH}" -o ControlPersist=60)
RSYNC_SSH="ssh -o ControlMaster=auto -o ControlPath=${CONTROL_PATH} -o ControlPersist=60"

cleanup() {
  "${SSH[@]}" -O exit "${SSH_HOST}" >/dev/null 2>&1 || true
  rm -rf "${CONTROL_DIR}"
}
trap cleanup EXIT

echo "=== Infer Takehome Deploy ==="
echo ">> Checking SSH..."
"${SSH[@]}" -o ConnectTimeout=10 -o BatchMode=yes "${SSH_HOST}" "true"

echo ">> Syncing to ${SSH_HOST}:${REMOTE_PATH}..."
"${SSH[@]}" "${SSH_HOST}" "sudo mkdir -p ${REMOTE_PATH} && sudo chown \$(id -un):\$(id -gn) ${REMOTE_PATH}"
rsync -az --delete \
  --exclude='.git/' \
  --exclude='.venv/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.env' \
  --exclude='.env.local' \
  --exclude='storage/' \
  --exclude='*.pyc' \
  -e "${RSYNC_SSH}" \
  "${SCRIPT_DIR}/" \
  "${SSH_HOST}:${REMOTE_PATH}/"

echo ">> Ensuring production env exists..."
"${SSH[@]}" "${SSH_HOST}" "cd ${REMOTE_PATH} && if [ ! -f .env ]; then printf '%s\n' 'CARRIER_MOCK=false' 'USAA_MFA_EMAIL=cordwell@gmail.com' 'DEV_PREFILL_CREDS=false' > .env; fi"

echo ">> Building and starting container..."
"${SSH[@]}" "${SSH_HOST}" "cd ${REMOTE_PATH} && docker compose -f docker-compose.prod.yml up -d --build"

echo ">> Updating Caddy site..."
"${SCP[@]}" -q "${SCRIPT_DIR}/caddy.conf" "${SSH_HOST}:/tmp/${SITE_NAME}"
"${SSH[@]}" "${SSH_HOST}" "sudo mv /tmp/${SITE_NAME} /etc/caddy/sites/${SITE_NAME} && sudo systemctl reload caddy"

echo ">> Health check..."
sleep 5
"${SSH[@]}" "${SSH_HOST}" "curl -sf http://127.0.0.1:8310/ >/dev/null && echo 'Local app: OK' || (docker compose -f ${REMOTE_PATH}/docker-compose.prod.yml logs --tail=80 infer && exit 1)"
curl -fsS "https://${SITE_NAME}/" >/dev/null

echo "=== Deploy complete ==="
echo "URL: https://${SITE_NAME}"
