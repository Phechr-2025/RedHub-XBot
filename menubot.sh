#!/usr/bin/env bash
set -euo pipefail

info() { printf '\033[1;32m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[ERR]\033[0m  %s\n' "$*"; }

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  if command -v sudo >/dev/null 2>&1; then
    exec sudo -E "$0" "$@"
  fi
  err "ต้องใช้ root หรือ sudo"
  exit 1
fi

CONFIG_FILE="/etc/menubot.conf"
if [[ ! -f "$CONFIG_FILE" ]]; then
  err "ไม่พบไฟล์ตั้งค่า ${CONFIG_FILE}"
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/xbot}"
SERVICE_NAME="${SERVICE_NAME:-xbot}"
SERVICE_USER="${SERVICE_USER:-root}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
VENV_PY="${PROJECT_DIR}/.venv/bin/python"

if [[ ! -d "$PROJECT_DIR" ]]; then
  err "ไม่พบโฟลเดอร์โปรเจกต์: ${PROJECT_DIR}"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  err "ไม่พบไฟล์ .env ที่ ${ENV_FILE}"
  exit 1
fi

# โหลดค่า .env ของโปรเจกต์ เพื่อให้ database.py อ่าน DB_PATH ได้ถูกต้อง
# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

confirm_numeric() {
  local prompt="$1"
  local rounds="$2"
  local ans=""
  local step=1

  while [[ $step -le $rounds ]]; do
    while true; do
      printf '%s\n' "${prompt} (รอบ ${step}/${rounds})"
      printf '1) ยกเลิก\n2) ยืนยัน\n'
      read -r -p "เลือก: " ans
      case "$ans" in
        1)
          err "ยกเลิก"
          exit 1
          ;;
        2)
          break
          ;;
        *)
          warn "กรุณาเลือก 1 หรือ 2"
          ;;
      esac
    done
    step=$((step + 1))
  done
}

show_status() {
  info "สถานะ service: ${SERVICE_NAME}"
  systemctl status "${SERVICE_NAME}" --no-pager || true
  echo
  info "log ล่าสุด (10 บรรทัด)"
  journalctl -u "${SERVICE_NAME}" -n 10 --no-pager || true
}

restart_service() {
  confirm_numeric "รีสตาร์ท service ${SERVICE_NAME}" 1
  info "รีสตาร์ท service ${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"
  info "รีสตาร์ทเรียบร้อย"
}

edit_env() {
  local tmp_dir tmp_env editor_bin
  editor_bin=""

  if command -v nano >/dev/null 2>&1; then
    editor_bin="nano"
  elif command -v vi >/dev/null 2>&1; then
    editor_bin="vi"
  else
    err "ไม่พบโปรแกรมแก้ไขไฟล์ (nano/vi)"
    exit 1
  fi

  tmp_dir="$(mktemp -d /tmp/xbot-env-edit.XXXXXX)"
  tmp_env="${tmp_dir}/.env.template"

  cat > "$tmp_env" <<__ENV__
========== ตั้งค่า .env ==========

# แก้ค่าภายในเครื่องหมาย "" แล้วบันทึกด้วย Ctrl+O จากนั้นกด Ctrl+X

DISCORD_TOKEN = "${DISCORD_TOKEN:-}"
ADMIN_IDS = "${ADMIN_IDS:-}"
XUI_URL = "${XUI_URL:-}"
XUI_API_TOKEN = "${XUI_API_TOKEN:-}"
XUI_USERNAME = "${XUI_USERNAME:-}"
XUI_PASSWORD = "${XUI_PASSWORD:-}"
AIS_INBOUND_ID = "${AIS_INBOUND_ID:-1}"
TRUE_INBOUND_ID = "${TRUE_INBOUND_ID:-2}"
DB_PATH = "${DB_PATH:-/data/bot.db}"
TRUEMONEY_WALLET_PHONE = "${TRUEMONEY_WALLET_PHONE:-}"
SERVICE_NAME = "${SERVICE_NAME:-xbot}"
__ENV__

  info "ตั้งค่าไฟล์ .env"
  info "ใช้ ${editor_bin} แก้ไขไฟล์: ${tmp_env}"
  "${editor_bin}" "$tmp_env"

  python3 - "$tmp_env" "$ENV_FILE" <<'PY'
import re
import sys
from pathlib import Path

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

pattern = re.compile(r'^\s*([A-Z0-9_]+)\s*=\s*"(.*)"\s*$')
data = {}
for raw in src.read_text(encoding='utf-8').splitlines():
    m = pattern.match(raw)
    if not m:
        continue
    key, value = m.group(1), m.group(2)
    value = value.replace(r'\\n', '\n').replace(r'\\"', '"').replace(r'\\\\', '\\')
    data[key] = value

order = [
    "DISCORD_TOKEN",
    "ADMIN_IDS",
    "XUI_URL",
    "XUI_API_TOKEN",
    "XUI_USERNAME",
    "XUI_PASSWORD",
    "AIS_INBOUND_ID",
    "TRUE_INBOUND_ID",
    "DB_PATH",
    "TRUEMONEY_WALLET_PHONE",
    "SERVICE_NAME",
]

def escape(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')

lines = [f'{key}="{escape(data.get(key, ""))}"' for key in order]
dst.write_text("\n".join(lines) + "\n", encoding='utf-8')
PY

  chmod 600 "$ENV_FILE"
  chown "${SERVICE_USER}:${SERVICE_USER}" "$ENV_FILE" 2>/dev/null || true

  set -a
  source "$ENV_FILE"
  set +a

  mkdir -p "$(dirname "${DB_PATH:-/data/bot.db}")" 2>/dev/null || true
  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$PROJECT_DIR" 2>/dev/null || true

  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "รีสตาร์ท service เพื่อให้ค่าทำงานทันที"
    systemctl restart "${SERVICE_NAME}"
  elif systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
    info "เริ่ม service เพื่อให้ค่าทำงานทันที"
    systemctl start "${SERVICE_NAME}" || true
  fi

  rm -rf "$tmp_dir" 2>/dev/null || true
  info "บันทึกค่า .env เรียบร้อย"
}

uninstall_app() {
  confirm_numeric "ถอนการติดตั้งระบบ" 2

  info "หยุด service และลบ unit file"
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  systemctl daemon-reload 2>/dev/null || true

  info "ลบคำสั่ง menubot และไฟล์ตั้งค่า"
  rm -f /usr/local/bin/menubot
  rm -f "$CONFIG_FILE"

  info "ลบฐานข้อมูล SQLite: ${DB_PATH}"
  if [[ -n "${DB_PATH:-}" && -e "${DB_PATH:-}" ]]; then
    rm -f "${DB_PATH}"
  fi

  info "ลบโฟลเดอร์โปรเจกต์: ${PROJECT_DIR}"
  rm -rf "$PROJECT_DIR" 2>/dev/null || true

  old_dir="/home/ubuntu/Discord-shop3x"
  if [[ -d "$old_dir" ]]; then
    info "ลบโฟลเดอร์ดาวน์โหลดเดิม: ${old_dir}"
    rm -rf "$old_dir" 2>/dev/null || true
  fi

  info "ถอนการติดตั้งและลบไฟล์ที่ดาวน์โหลด/สร้างทั้งหมดเรียบร้อย"
  echo "========================================"
  info "ระบบจะรีบูตอัตโนมัติใน 10 วินาที..."
  echo "========================================"
  for i in $(seq 10 -1 1); do
    printf '%s\n' "$i"
    sleep 1
  done
  sync || true
  reboot
}

update_script() {
  confirm_numeric "อัปเดตสคริประบบ" 1

  if [[ ! -d "$PROJECT_DIR/.git" ]]; then
    err "ไม่พบ git repository ภายใน ${PROJECT_DIR}"
    exit 1
  fi

  cd "$PROJECT_DIR"

  local current_branch=""
  current_branch="$(git branch --show-current 2>/dev/null || true)"
  if [[ -z "$current_branch" ]]; then
    current_branch="main"
  fi

  info "ดึงโค้ดล่าสุดจาก origin"
  git fetch origin --prune

  if git show-ref --verify --quiet "refs/remotes/origin/${current_branch}"; then
    git reset --hard "origin/${current_branch}"
  elif git show-ref --verify --quiet "refs/remotes/origin/main"; then
    git reset --hard origin/main
  elif git show-ref --verify --quiet "refs/remotes/origin/master"; then
    git reset --hard origin/master
  else
    err "ไม่พบ branch ระยะไกลสำหรับอัปเดต"
    exit 1
  fi

  info "ติดตั้ง dependencies ใหม่"
  "${PROJECT_DIR}/.venv/bin/pip" install -r requirements.txt

  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$PROJECT_DIR" 2>/dev/null || true

  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "รีสตาร์ท service เพื่อใช้งานโค้ดล่าสุด"
    systemctl restart "${SERVICE_NAME}"
  fi

  info "อัปเดตสคริประบบเรียบร้อย"
}

cd "$PROJECT_DIR"

while true; do
  cat <<EOF_MENU
========================================
 menubot - Discord shop VPS control
 โฟลเดอร์หลัก: ${PROJECT_DIR}
 service: ${SERVICE_NAME}
========================================
1) ถอนการติดตั้ง (ลบทุกอย่างที่ดาวน์โหลดมา)
2) ดูสถานะการทำงาน
3) รีสตาร์ทระบบ (ข้อมูลฐานข้อมูลไม่หาย)
5) อัปเดตสคริประบบ
6) ตั้งค่า .env
0) ออก
EOF_MENU
  read -r -p "เลือกเมนู: " choice
  case "$choice" in
    1) uninstall_app ;;
    2) show_status ;;
    3) restart_service ;;
    5) update_script ;;
    6) edit_env ;;
    0) exit 0 ;;
    *) warn "เลือกไม่ถูกต้อง" ;;
  esac
  echo
done
