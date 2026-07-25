#!/usr/bin/env bash
set -euo pipefail
APP_NAME="xbot"
DEFAULT_SERVICE_NAME="xbot"
REPO_URL="https://github.com/Phechr2554/Discord-shop3x.git"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info() { printf '\033[1;32m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[ERR]\033[0m  %s\n' "$*"; }
prompt_yes_no() {
  local prompt="$1"
  local answer=""
  while true; do
    read -r -p "${prompt} [Y/N]: " answer
    case "${answer,,}" in
      ""|y|yes)
        return 0
        ;;
      n|no)
        return 1
        ;;
      *)
        warn "กรุณาพิมพ์ y หรือ n"
        ;;
    esac
  done
}
prompt_env() {
  local label="$1"
  local default="${2:-}"
  local value=""
  if [[ -n "$default" ]]; then
    read -r -p "${label} [${default}]: " value
    value="${value:-$default}"
  else
    read -r -p "${label}: " value
  fi
  printf '%s' "$value"
}
escape_env_value() {
  local value="${1:-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  printf '"%s"' "$value"
}
resolve_home_dir() {
  local user="$1"
  local home_dir=""
  if [[ -n "$user" ]] && id -u "$user" >/dev/null 2>&1; then
    home_dir="$(getent passwd "$user" | cut -d: -f6 || true)"
  fi
  if [[ -z "$home_dir" || ! -d "$home_dir" ]]; then
    if [[ -d /home/ubuntu ]]; then
      home_dir="/home/ubuntu"
    else
      home_dir="$(getent passwd root | cut -d: -f6 || echo /root)"
    fi
  fi
  printf '%s' "$home_dir"
}
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  err "กรุณารันด้วย root (หรือ sudo) เพื่อให้ติดตั้ง systemd service ได้"
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
info "ติดตั้งแพ็กเกจพื้นฐาน"
apt-get update -y
apt-get install -y git rsync nano python3 python3-venv python3-pip
INSTALL_USER="${SUDO_USER:-}"
if [[ -z "$INSTALL_USER" || "$INSTALL_USER" == "root" ]]; then
  if id -u ubuntu >/dev/null 2>&1; then
    INSTALL_USER="ubuntu"
  else
    INSTALL_USER="root"
  fi
fi
HOME_DIR="$(resolve_home_dir "$INSTALL_USER")"
ROOT_DIR="${HOME_DIR}/${APP_NAME}"
SERVICE_NAME="${DEFAULT_SERVICE_NAME}"
info "โฟลเดอร์ติดตั้งหลัก: ${ROOT_DIR}"
if [[ "$SOURCE_DIR" == "$ROOT_DIR" ]]; then
  err "ไม่ควรรันตัวติดตั้งจากโฟลเดอร์ปลายทางเดียวกัน (${ROOT_DIR})"
  err "ให้รันจากโฟลเดอร์ต้นฉบับหรือจากไฟล์ zip ที่แยกไว้"
  exit 1
fi
SOURCE_HAS_GIT="0"
if [[ -d "${SOURCE_DIR}/.git" ]]; then
  SOURCE_HAS_GIT="1"
fi
if [[ -d "$ROOT_DIR" ]]; then
  if [[ -d "$ROOT_DIR/.git" ]]; then
    info "พบการติดตั้งเดิมที่ ${ROOT_DIR}"
  elif [[ -n "$(find "$ROOT_DIR" -mindepth 1 -maxdepth 1 2>/dev/null | head -n 1 || true)" ]]; then
    warn "โฟลเดอร์ปลายทางมีไฟล์อยู่แล้ว: ${ROOT_DIR}"
    if ! prompt_yes_no "ต้องการเขียนทับด้วยไฟล์เวอร์ชันนี้หรือไม่"; then
      err "ยกเลิก"
      exit 1
    fi
  fi
fi
mkdir -p "$ROOT_DIR"
RSYNC_EXCLUDES=(
  "--exclude" ".venv"
  "--exclude" ".env"
  "--exclude" "__pycache__"
  "--exclude" "*.pyc"
  "--exclude" "*.pyo"
  "--exclude" "*.pyd"
)
if [[ "$SOURCE_HAS_GIT" != "1" ]]; then
  RSYNC_EXCLUDES+=("--exclude" ".git")
fi
info "ซิงก์ไฟล์โปรเจกต์ไปยัง ${ROOT_DIR}"
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$SOURCE_DIR"/ "$ROOT_DIR"/
cd "$ROOT_DIR"
if [[ "$SOURCE_HAS_GIT" != "1" && ! -d ".git" ]]; then
  info "สร้าง git repository ภายใน ${ROOT_DIR}"
  git init >/dev/null 2>&1 || true
  git branch -M main >/dev/null 2>&1 || true
  git remote add origin "$REPO_URL" >/dev/null 2>&1 || git remote set-url origin "$REPO_URL" >/dev/null 2>&1 || true
  git add -A
  GIT_AUTHOR_NAME="xbot installer" \
  GIT_AUTHOR_EMAIL="installer@localhost" \
  GIT_COMMITTER_NAME="xbot installer" \
  GIT_COMMITTER_EMAIL="installer@localhost" \
    git commit -m "Initial install" >/dev/null 2>&1 || true
fi
if [[ ! -f "menubot.sh" ]]; then
  err "ไม่พบไฟล์ menubot.sh ใน ${ROOT_DIR}"
  exit 1
fi
chmod +x menubot.sh
if [[ -f ".venv/bin/python" ]]; then
  info "พบ virtual environment เดิม"
else
  info "สร้าง virtual environment"
  python3 -m venv .venv
fi
info "ติดตั้ง dependencies"
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
ENV_SETUP="0"
if prompt_yes_no "ตั้งค่าไฟล์ .env เลยไหม / [Y/N] (พิมพ์ใหญ่พิมพ์เล็กก็ได้)"; then
  ENV_SETUP="1"
  info "กรอกค่าที่ต้องใช้ใน .env"
  DISCORD_TOKEN="$(prompt_env "Token")"
  ADMIN_IDS="$(prompt_env "Discord admin IDs (คั่นด้วย comma)" "")"
  XUI_URL="$(prompt_env "3x-ui URL" "")"
  XUI_API_TOKEN="$(prompt_env "3x-ui API token (ถ้าไม่มีค่อยปล่อยว่าง)" "")"
  XUI_USERNAME="$(prompt_env "3x-ui username (ถ้าใช้ login แบบ session)" "")"
  XUI_PASSWORD="$(prompt_env "3x-ui password (ถ้าใช้ login แบบ session)" "")"
  AIS_INBOUND_ID="$(prompt_env "AIS inbound ID" "1")"
  TRUE_INBOUND_ID="$(prompt_env "TRUE inbound ID" "2")"
  DB_PATH="$(prompt_env "ที่เก็บฐานข้อมูล SQLite" "/data/bot.db")"
  TRUEMONEY_WALLET_PHONE="$(prompt_env "เบอร์ wallet สำหรับรับเงิน" "")"
else
  info "ข้ามการตั้งค่า .env ไว้ก่อน"
  DISCORD_TOKEN=""
  ADMIN_IDS=""
  XUI_URL=""
  XUI_API_TOKEN=""
  XUI_USERNAME=""
  XUI_PASSWORD=""
  AIS_INBOUND_ID="1"
  TRUE_INBOUND_ID="2"
  DB_PATH="/data/bot.db"
  TRUEMONEY_WALLET_PHONE=""
fi
cat > .env <<__ENV__
DISCORD_TOKEN=$(escape_env_value "$DISCORD_TOKEN")
ADMIN_IDS=$(escape_env_value "$ADMIN_IDS")
XUI_URL=$(escape_env_value "$XUI_URL")
XUI_USERNAME=$(escape_env_value "$XUI_USERNAME")
XUI_PASSWORD=$(escape_env_value "$XUI_PASSWORD")
XUI_API_TOKEN=$(escape_env_value "$XUI_API_TOKEN")
AIS_INBOUND_ID=$(escape_env_value "$AIS_INBOUND_ID")
TRUE_INBOUND_ID=$(escape_env_value "$TRUE_INBOUND_ID")
DB_PATH=$(escape_env_value "$DB_PATH")
TRUEMONEY_WALLET_PHONE=$(escape_env_value "$TRUEMONEY_WALLET_PHONE")
SERVICE_NAME=$(escape_env_value "$SERVICE_NAME")
__ENV__
if [[ ! -f .env ]]; then
  err "สร้างไฟล์ .env ไม่สำเร็จ"
  exit 1
fi
mkdir -p "$(dirname "$DB_PATH")"
SERVICE_USER="$INSTALL_USER"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  SERVICE_USER="root"
fi
info "สร้างไฟล์ตั้งค่าของ menubot"
cat > /etc/menubot.conf <<__CONF__
PROJECT_DIR=${ROOT_DIR}
SERVICE_NAME=${SERVICE_NAME}
SERVICE_USER=${SERVICE_USER}
ENV_FILE=${ROOT_DIR}/.env
__CONF__
chmod 644 /etc/menubot.conf
chown -R "$SERVICE_USER:$SERVICE_USER" "$ROOT_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$(dirname "$DB_PATH")" 2>/dev/null || true
chmod 600 .env
chmod 644 /etc/menubot.conf
info "ติดตั้งคำสั่ง menubot"
install -m 755 "$ROOT_DIR/menubot.sh" /usr/local/bin/menubot
info "สร้าง systemd service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<__SERVICE__
[Unit]
Description=Discord Shop 3x Bot
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
EnvironmentFile=${ROOT_DIR}/.env
ExecStart=${ROOT_DIR}/.venv/bin/python ${ROOT_DIR}/main.py
Restart=always
RestartSec=5
User=${SERVICE_USER}
Group=${SERVICE_USER}
[Install]
WantedBy=multi-user.target
__SERVICE__
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
if [[ "$ENV_SETUP" == "1" ]]; then
  systemctl start "${SERVICE_NAME}.service"
  info "เริ่ม service แล้ว"
else
  warn "ยังไม่ได้ตั้งค่า .env ครบ ระบบจึงยังไม่เริ่ม service อัตโนมัติ"
fi
info "ติดตั้งเสร็จแล้ว"
info "โฟลเดอร์หลักของโปรเจกต์: ${ROOT_DIR}"
info "menubot เพื่อใช้งานเมนู"
info "ดูสถานะ: systemctl status ${SERVICE_NAME} --no-pager"
info "ดู log สด: journalctl -u ${SERVICE_NAME} -f"
echo
echo "========================================"
info "ระบบจะรีบูตอัตโนมัติใน 10 วินาที..."
echo "========================================"
for i in $(seq 10 -1 1); do
  printf '%s\n' "$i"
  sleep 1
done
sync || true
reboot