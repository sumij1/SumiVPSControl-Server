#!/usr/bin/env bash
set -euo pipefail

: "${SERVER_NAME:?SERVER_NAME is required}"
: "${AGENT_KEY:?AGENT_KEY is required}"
: "${HUB_URL:?HUB_URL is required, for example http://127.0.0.1:8080/api/report}"
HUB_BASE_URL="${HUB_BASE_URL:-${HUB_URL%/api/report}}"
CHECK_PORTS="${CHECK_PORTS:-80,443}"
LOG_FILES="${LOG_FILES:-}"
SERVER_ID="${SERVER_ID:-}"
REPORT_INTERVAL="${REPORT_INTERVAL:-60}"
SSH_HOST="${SSH_HOST:-}"
SSH_PORT="${SSH_PORT:-22}"
SSH_USER="${SSH_USER:-root}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this installer as root" >&2
  exit 1
fi

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends python3 python3-psutil curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-psutil curl ca-certificates
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-psutil curl ca-certificates
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache python3 py3-psutil curl ca-certificates
elif ! python3 -c 'import psutil' >/dev/null 2>&1; then
  echo "python3 and psutil are required" >&2
  exit 1
fi

curl -fsSL \
  -H "User-Agent: OpsHub-Installer/1.0" \
  "${HUB_BASE_URL}/api/agent/reporter.py" \
  -o /usr/local/bin/ops-hub-reporter
chmod 755 /usr/local/bin/ops-hub-reporter

cat > /etc/ops-hub-agent.env <<EOF
SERVER_ID=$SERVER_ID
SERVER_NAME=$SERVER_NAME
HUB_URL=$HUB_URL
AGENT_KEY=$AGENT_KEY
CHECK_PORTS=$CHECK_PORTS
LOG_FILES=$LOG_FILES
REPORT_INTERVAL=$REPORT_INTERVAL
SSH_HOST=$SSH_HOST
SSH_PORT=$SSH_PORT
SSH_USER=$SSH_USER
EOF
chmod 600 /etc/ops-hub-agent.env

cat > /etc/systemd/system/ops-hub-reporter.service <<'EOF'
[Unit]
Description=Ops Hub remote server reporter
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
EnvironmentFile=/etc/ops-hub-agent.env
ExecStartPre=/bin/sleep 5
ExecStart=/usr/local/bin/ops-hub-reporter
Restart=always
RestartSec=15
TimeoutStopSec=20
DynamicUser=yes
SupplementaryGroups=systemd-journal
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ops-hub-reporter.service
systemctl is-enabled ops-hub-reporter.service
systemctl --no-pager --full status ops-hub-reporter.service
