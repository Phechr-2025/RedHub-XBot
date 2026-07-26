#!/usr/bin/env bash
set -euo pipefail

# เปลี่ยนชื่อแอป/บริการได้ที่ APP_SLUG จุดเดียว และเปลี่ยนคำสั่งเมนูได้ที่ MENU_COMMAND จุดเดียว
PROJECT_NAME="RedHub-XBot"
APP_SLUG="xbot"
MENU_COMMAND="menubot"
REPO_OWNER="Phechr-2025"
REPO_NAME="RedHub-XBot"

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

get_latest_release_source() {
  python3 - "$REPO_OWNER" "$REPO_NAME" <<'PY'
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

owner, repo = sys.argv[1:3]
api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
headers = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "Mozilla/5.0",
}
req = urllib.request.Request(api_url, headers=headers)
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.load(resp)

tag = str(data.get("tag_name", "")).strip()
tarball_url = str(data.get("tarball_url", "")).strip()
if not tag or not tarball_url:
    raise SystemExit("release metadata missing")

work_dir = tempfile.mkdtemp(prefix=f"{repo}-{tag}-")
archive_path = os.path.join(work_dir, f"{repo}-{tag}.tar.gz")
extract_dir = os.path.join(work_dir, "extract")
os.makedirs(extract_dir, exist_ok=True)

archive_req = urllib.request.Request(tarball_url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(archive_req, timeout=120) as resp, open(archive_path, "wb") as out:
    shutil.copyfileobj(resp, out)

with tarfile.open(archive_path, "r:gz") as tf:
    tf.extractall(extract_dir)

roots = [os.path.join(extract_dir, p) for p in os.listdir(extract_dir)]
roots = [p for p in roots if os.path.isdir(p)]
if not roots:
    raise SystemExit("release archive extract root not found")

print(tag)
print(work_dir)
print(roots[0])
PY
}
sync_tree() {
  local source_dir="$1"
  local target_dir="$2"
  rsync -a --delete     --exclude ".env"     --exclude ".version"     --exclude "__pycache__"     --exclude "*.pyc"     --exclude "*.pyo"     --exclude "*.pyd"     "${source_dir}/" "${target_dir}/"
}

cleanup_path() {
  local path="${1:-}"
  [[ -n "$path" && -e "$path" ]] || return 0
  rm -rf "$path" 2>/dev/null || true
}

generate_panel_path() {
  python3 - <<'PY'
import random
import string
print("/" + "".join(random.choice(string.ascii_lowercase) for _ in range(10)))
PY
}

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  err "กรุณารันด้วย root (หรือ sudo) เพื่อให้ติดตั้ง systemd service ได้"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
info "ติดตั้งแพ็กเกจพื้นฐาน"
apt-get update -y
apt-get install -y rsync nano python3 python3-venv python3-pip

INSTALL_USER="${SUDO_USER:-}"
if [[ -z "$INSTALL_USER" || "$INSTALL_USER" == "root" ]]; then
  if id -u ubuntu >/dev/null 2>&1; then
    INSTALL_USER="ubuntu"
  else
    INSTALL_USER="root"
  fi
fi

HOME_DIR="$(resolve_home_dir "$INSTALL_USER")"
ROOT_DIR="${HOME_DIR}/${APP_SLUG}"
SERVICE_NAME="${APP_SLUG}"
VERSION_FILE="${ROOT_DIR}/.version"

info "โฟลเดอร์ติดตั้งหลัก: ${ROOT_DIR}"

if [[ "$SOURCE_DIR" == "$ROOT_DIR" ]]; then
  err "ไม่ควรรันตัวติดตั้งจากโฟลเดอร์ปลายทางเดียวกัน (${ROOT_DIR})"
  err "ให้รันจากโฟลเดอร์ต้นฉบับหรือจากไฟล์ zip ที่แยกไว้"
  exit 1
fi

mkdir -p "$ROOT_DIR"

RELEASE_TAG=""
RELEASE_WORK_DIR=""
RELEASE_SOURCE_DIR=""

info "ตรวจสอบ Release ล่าสุดจาก GitHub"
if release_output="$(get_latest_release_source 2>/dev/null)"; then
  mapfile -t release_lines <<<"$release_output"
  RELEASE_TAG="${release_lines[0]:-}"
  RELEASE_WORK_DIR="${release_lines[1]:-}"
  RELEASE_SOURCE_DIR="${release_lines[2]:-}"
fi

if [[ -n "$RELEASE_TAG" && -n "$RELEASE_WORK_DIR" && -n "$RELEASE_SOURCE_DIR" && -d "$RELEASE_SOURCE_DIR" ]]; then
  info "ดาวน์โหลด Release ล่าสุด: ${RELEASE_TAG}"
  sync_tree "$RELEASE_SOURCE_DIR" "$ROOT_DIR"
  printf '%s\n' "$RELEASE_TAG" > "$VERSION_FILE"
  cleanup_path "$RELEASE_WORK_DIR"
else
  warn "ไม่สามารถดึง Release ล่าสุดได้ ใช้ไฟล์ local ในแพ็กเกจแทน"
  sync_tree "$SOURCE_DIR" "$ROOT_DIR"
  printf '%s\n' "local" > "$VERSION_FILE"
fi
cd "$ROOT_DIR"

if [[ ! -f "menubot.sh" ]]; then
  err "ไม่พบไฟล์ menubot.sh ใน ${ROOT_DIR}"
  exit 1
fi

chmod +x menubot.sh webctl.sh web_panel.py
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
  WEB_PORT="$(prompt_env "พอร์ตเว็บไซต์" "2026")"
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
  WEB_PORT="2026"
fi

if prompt_yes_no "ผูกโดเมนเข้ากับเว็บเลยไหม หรือใช้ IP VPS ไปก่อน (เพิ่มภายหลังได้)?"; then
  WEB_DOMAIN="$(prompt_env "โดเมนสำหรับเว็บไซต์" "")"
else
  WEB_DOMAIN=""
fi

WEB_PANEL_PATH="$(generate_panel_path)"
WEB_SERVICE_NAME="${SERVICE_NAME}-web"

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
APP_SLUG=$(escape_env_value "$APP_SLUG")
MENU_COMMAND=$(escape_env_value "$MENU_COMMAND")
WEB_PORT=$(escape_env_value "$WEB_PORT")
WEB_DOMAIN=$(escape_env_value "$WEB_DOMAIN")
WEB_PANEL_PATH=$(escape_env_value "$WEB_PANEL_PATH")
WEB_SERVICE_NAME=$(escape_env_value "$WEB_SERVICE_NAME")
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
PROJECT_NAME=${PROJECT_NAME}
APP_SLUG=${APP_SLUG}
MENU_COMMAND=${MENU_COMMAND}
PROJECT_DIR=${ROOT_DIR}
SERVICE_NAME=${SERVICE_NAME}
WEB_SERVICE_NAME=${WEB_SERVICE_NAME}
SERVICE_USER=${SERVICE_USER}
ENV_FILE=${ROOT_DIR}/.env
VERSION_FILE=${VERSION_FILE}
WEB_PORT=${WEB_PORT}
WEB_DOMAIN=${WEB_DOMAIN}
WEB_PANEL_PATH=${WEB_PANEL_PATH}
__CONF__
chmod 644 /etc/menubot.conf
chown -R "$SERVICE_USER:$SERVICE_USER" "$ROOT_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$(dirname "$DB_PATH")" 2>/dev/null || true
chmod 600 .env
chmod 644 /etc/menubot.conf

info "ติดตั้งคำสั่ง ${MENU_COMMAND}"
install -m 755 "$ROOT_DIR/menubot.sh" "/usr/local/bin/${MENU_COMMAND}"

info "สร้าง systemd service"
cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<__SERVICE__
[Unit]
Description=RedHub-XBot Discord Bot
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

cat > "/etc/systemd/system/${WEB_SERVICE_NAME}.service" <<__WEBSERVICE__
[Unit]
Description=RedHub-XBot Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
EnvironmentFile=${ROOT_DIR}/.env
ExecStart=${ROOT_DIR}/.venv/bin/python ${ROOT_DIR}/web_panel.py
Restart=always
RestartSec=5
User=root
Group=root

[Install]
WantedBy=multi-user.target
__WEBSERVICE__

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
systemctl enable "${WEB_SERVICE_NAME}.service"

systemctl start "${WEB_SERVICE_NAME}.service"

if [[ "$ENV_SETUP" == "1" ]]; then
  systemctl start "${SERVICE_NAME}.service"
  info "เริ่ม service แล้ว"
else
  warn "ยังไม่ได้ตั้งค่า .env ครบ ระบบจึงยังไม่เริ่ม service หลักอัตโนมัติ"
fi

if [[ -n "${WEB_DOMAIN}" ]]; then
  info "กำลังผูกโดเมนกับเว็บอัตโนมัติ"
  "${ROOT_DIR}/webctl.sh" add-domain "${WEB_DOMAIN}" || warn "ผูกโดเมนอัตโนมัติไม่สำเร็จ กรุณาใช้เมนูจัดการเว็บไซต์ภายหลัง"
fi

info "ติดตั้งเสร็จแล้ว"
info "โฟลเดอร์หลักของโปรเจกต์: ${ROOT_DIR}"
info "ใช้คำสั่ง ${MENU_COMMAND} เพื่อใช้งานเมนู"
info "Version: $(cat "$VERSION_FILE" 2>/dev/null || echo unknown)"
info "Web URL: http://$(hostname -I 2>/dev/null | awk '{print $1}'):$(grep -E '^WEB_PORT=' .env | cut -d= -f2 | tr -d '"')$(grep -E '^WEB_PANEL_PATH=' .env | cut -d= -f2 | tr -d '"')"
info "ดูสถานะ: systemctl status ${SERVICE_NAME} --no-pager"
info "ดูเว็บสถานะ: systemctl status ${WEB_SERVICE_NAME} --no-pager"
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
