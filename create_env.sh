#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${1:-$(cd "$SCRIPT_DIR/.." && pwd)}"
ENV_FILE="$ROOT/.env"

umask 077
mkdir -p "$ROOT/data"
chmod 700 "$ROOT/data"

if [[ ! -s "$ENV_FILE" ]]; then
  PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || command -v python)}"
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "python3 is required to generate credentials" >&2
    exit 1
  fi
  random_hex() {
    "$PYTHON_BIN" -c 'import secrets, sys; print(secrets.token_hex(int(sys.argv[1])))' "$1"
  }
  ADMIN_KEY="$(random_hex 32)"
  AGENT_KEY="$(random_hex 32)"
  CONFIG_MASTER_KEY="$(random_hex 48)"
  cat > "$ENV_FILE" <<EOF
# Runtime and authentication
ADMIN_KEY=$ADMIN_KEY
AGENT_KEY=$AGENT_KEY
CONFIG_MASTER_KEY=$CONFIG_MASTER_KEY
DATABASE_PATH=$ROOT/data/ops_hub.sqlite3

# Public identity. Leave blank until you choose your own values.
APP_NAME=
APP_OWNER=
APP_CONTACT_TEXT=
APP_CONTACT_URL=

# Leave both blank for same-origin use. Fill only when the web client
# is hosted on a different origin.
PUBLIC_BASE_URL=
CORS_ORIGINS=

BIND_HOST=0.0.0.0
BIND_PORT=8080
SERVICE_NAME=

# Monitoring and alert policy
HEARTBEAT_TIMEOUT_SECONDS=120
ALERT_COOLDOWN_SECONDS=600
CPU_THRESHOLD=90
MEMORY_THRESHOLD=90
DISK_THRESHOLD=90
OFFLINE_CHECK_INTERVAL_SECONDS=30

# NoneBot runtime
DRIVER=~fastapi+~aiohttp
HOST=127.0.0.1
PORT=8080
LOG_LEVEL=INFO

# QQ: NapCat/OneBot11 reverse WebSocket connects to /onebot/v11/ws
ONEBOT_V11_ACCESS_TOKEN=
QQ_GROUP_IDS=

# Telegram and Discord adapters
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_IDS=
DISCORD_BOT_TOKEN=
DISCORD_CHANNEL_IDS=

# WeChat HTTP bridge (Gewechat / WeChatFerry / PadLocal compatible)
WECHAT_PROVIDER=generic
WECHAT_SEND_URL=
WECHAT_API_TOKEN=
WECHAT_APP_ID=
WECHAT_RECEIVE_IDS=
WECHAT_WEBHOOK_SECRET=

# WhatsApp Business Cloud API
WHATSAPP_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_RECIPIENTS=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_APP_SECRET=
WHATSAPP_GRAPH_VERSION=v23.0

# Optional OpenAI-compatible AI classification service
AI_API_KEY=
AI_API_BASE=
AI_MODEL=
AI_TIMEOUT_SECONDS=10
AI_CLASSIFY_COOLDOWN_SECONDS=1200
EOF
fi

chmod 600 "$ENV_FILE"
echo "Credentials file ready: $ENV_FILE"
awk -F= '/^(ADMIN_KEY|AGENT_KEY)=/{print $1 "=" $2}' "$ENV_FILE"
