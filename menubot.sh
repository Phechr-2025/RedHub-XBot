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

APP_SLUG="${APP_SLUG:-${SERVICE_NAME:-}}"
MENU_COMMAND="${MENU_COMMAND:-menubot}"
PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/${APP_SLUG}}"
PROJECT_NAME="${PROJECT_NAME:-$(basename "$PROJECT_DIR")}"
SERVICE_NAME="${SERVICE_NAME:-${APP_SLUG}}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-${SERVICE_NAME}-web}"
SERVICE_USER="${SERVICE_USER:-root}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
VERSION_FILE="${VERSION_FILE:-${PROJECT_DIR}/.version}"
WEBCTL_SCRIPT="${WEBCTL_SCRIPT:-${PROJECT_DIR}/webctl.sh}"
HOME_BASE="$(dirname "$PROJECT_DIR")"

if [[ ! -d "$PROJECT_DIR" ]]; then
  err "ไม่พบโฟลเดอร์โปรเจกต์: ${PROJECT_DIR}"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  err "ไม่พบไฟล์ .env ที่ ${ENV_FILE}"
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

get_current_version() {
  if [[ -f "$VERSION_FILE" ]]; then
    tr -d '\r\n' < "$VERSION_FILE"
  else
    printf '%s' "unknown"
  fi
}

get_latest_release_info() {
  python3 - "$@" <<'PY'
import json
import sys
import urllib.request

owner = "Phechr-2025"
repo = "RedHub-XBot"
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
print(tag)
print(tarball_url)
PY
}

download_release_source() {
  local tarball_url="$1"
  python3 - "$tarball_url" <<'PY'
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

tarball_url = sys.argv[1]
headers = {"User-Agent": "Mozilla/5.0"}
work_dir = tempfile.mkdtemp(prefix="redhub-release-")
archive_path = os.path.join(work_dir, "release.tar.gz")
extract_dir = os.path.join(work_dir, "extract")
os.makedirs(extract_dir, exist_ok=True)

req = urllib.request.Request(tarball_url, headers=headers)
with urllib.request.urlopen(req, timeout=120) as resp, open(archive_path, "wb") as out:
    shutil.copyfileobj(resp, out)

with tarfile.open(archive_path, "r:gz") as tf:
    tf.extractall(extract_dir)

roots = [os.path.join(extract_dir, name) for name in os.listdir(extract_dir)]
roots = [path for path in roots if os.path.isdir(path)]
if not roots:
    raise SystemExit("release archive extract root not found")
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
  local version
  version="$(get_current_version)"
  info "สถานะ service: ${SERVICE_NAME}"
  info "Version: ${version}"
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

  tmp_dir="$(mktemp -d "/tmp/${APP_SLUG}-env-edit.XXXXXX")"
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
APP_SLUG = "${APP_SLUG:-${SERVICE_NAME:-}}"
MENU_COMMAND = "${MENU_COMMAND:-menubot}"
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
    "APP_SLUG",
    "MENU_COMMAND",
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
  systemctl stop "${WEB_SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${WEB_SERVICE_NAME}" 2>/dev/null || true
  systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
  systemctl reset-failed "${WEB_SERVICE_NAME}" 2>/dev/null || true
  rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
  rm -f "/etc/systemd/system/${WEB_SERVICE_NAME}.service"
  systemctl daemon-reload 2>/dev/null || true

  info "ลบคำสั่ง ${MENU_COMMAND} และไฟล์ตั้งค่า"
  rm -f "/usr/local/bin/${MENU_COMMAND}"
  rm -f "$CONFIG_FILE"

  info "ลบฐานข้อมูล SQLite: ${DB_PATH}"
  if [[ -n "${DB_PATH:-}" && -e "${DB_PATH:-}" ]]; then
    rm -f "${DB_PATH}"
    rmdir "$(dirname "${DB_PATH}")" 2>/dev/null || true
  fi

  info "ลบโฟลเดอร์โปรเจกต์: ${PROJECT_DIR}"
  rm -rf "$PROJECT_DIR" 2>/dev/null || true

  legacy_dirs=()
  for candidate in "$HOME_BASE"/*shop3x* "$HOME_BASE"/*Shop3x* "$HOME_BASE"/RedHub-XBot "$HOME_BASE"/redhub-xbot; do
    [[ -d "$candidate" ]] || continue
    if [[ "$candidate" == "$PROJECT_DIR" ]]; then
      continue
    fi
    legacy_dirs+=("$candidate")
  done
  for old_dir in "${legacy_dirs[@]}"; do
    info "ลบโฟลเดอร์ดาวน์โหลดเดิม: ${old_dir}"
    rm -rf "$old_dir" 2>/dev/null || true
  done

  info "ลบไฟล์ชั่วคราวของตัวติดตั้ง"
  rm -rf /tmp/redhub-release-* /tmp/${APP_SLUG}-env-edit.* 2>/dev/null || true

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

  local current_version latest_tag latest_url release_work_dir release_source_dir
  current_version="$(get_current_version)"

  if release_output="$(get_latest_release_info 2>/dev/null)"; then
    mapfile -t release_lines <<<"$release_output"
    latest_tag="${release_lines[0]:-}"
    latest_url="${release_lines[1]:-}"
  else
    err "ไม่สามารถตรวจสอบ Release ล่าสุดได้"
    exit 1
  fi

  if [[ -z "$latest_tag" || -z "$latest_url" ]]; then
    err "อ่านข้อมูล Release ล่าสุดไม่สำเร็จ"
    exit 1
  fi

  info "เวอร์ชั่นก่อนอัปเดต: ${current_version}"
  info "เวอร์ชั่นที่ตรวจพบ: ${latest_tag}"

  if [[ "$current_version" == "$latest_tag" ]]; then
    info "ตอนนี้เป็นเวอร์ชั่นล่าสุดอยู่แล้ว"
    return 0
  fi

  info "ดาวน์โหลด Release ล่าสุด"
  if ! release_output="$(download_release_source "$latest_url" 2>/dev/null)"; then
    err "ดาวน์โหลด Release ล่าสุดไม่สำเร็จ"
    exit 1
  fi

  mapfile -t release_lines <<<"$release_output"
  release_work_dir="${release_lines[0]:-}"
  release_source_dir="${release_lines[1]:-}"

  if [[ ! -d "$release_source_dir" ]]; then
    err "ไม่พบไฟล์ที่แตกออกมาจาก Release"
    cleanup_path "$release_work_dir"
    exit 1
  fi

  sync_tree "$release_source_dir" "$PROJECT_DIR"
  printf '%s\n' "$latest_tag" > "$VERSION_FILE"
  cleanup_path "$release_work_dir"

  chown -R "${SERVICE_USER}:${SERVICE_USER}" "$PROJECT_DIR" 2>/dev/null || true

  if systemctl is-active --quiet "${SERVICE_NAME}" || systemctl is-active --quiet "${WEB_SERVICE_NAME}"; then
    info "รีสตาร์ท service เพื่อใช้งาน Release ล่าสุด"
    systemctl restart "${WEB_SERVICE_NAME}" 2>/dev/null || true
    systemctl restart "${SERVICE_NAME}" 2>/dev/null || true
  fi

  info "อัปเดตสคริประบบเรียบร้อย"
}
webctl() {
  if [[ ! -x "$WEBCTL_SCRIPT" ]]; then
    err "ไม่พบสคริปต์ควบคุมเว็บ: ${WEBCTL_SCRIPT}"
    return 1
  fi
  "$WEBCTL_SCRIPT" "$@"
}

web_status() {
  webctl show
}

web_reset() {
  confirm_numeric "รีเซ็ต Panel web" 1
  webctl reset
}

web_set_port() {
  local port=""
  read -r -p "พอร์ตเว็บไซต์ใหม่ (1-65535): " port
  webctl set-port "$port"
}

web_stop() {
  confirm_numeric "หยุดเว็บ" 1
  webctl stop
}

web_start() {
  confirm_numeric "เริ่มเว็บ" 1
  webctl start
}

web_restart() {
  confirm_numeric "รีสตาร์ทเว็บ" 1
  webctl restart
}

web_add_domain() {
  local domain=""
  read -r -p "พิมพ์โดเมนที่จะผูกกับเว็บ: " domain
  webctl add-domain "$domain"
}

show_web_menu() {
  while true; do
    cat <<EOF_WEB
========================================
 จัดการเว็บไซต์
========================================
1) ดู url ต่างๆ ดูพอร์ตที่เว็บไซต์ใช้
2) รีเซ็ต Panel web
3) ตั้งพอร์ตเว็บไซต์
4) หยุดเว็บ
5) เริ่มเว็บ
6) รีสตาร์ทเว็บ
7) เพิ่มโดเมน
0) กลับ
EOF_WEB
    read -r -p "เลือกเมนู: " web_choice
    case "$web_choice" in
      1) web_status ;;
      2) web_reset ;;
      3) web_set_port ;;
      4) web_stop ;;
      5) web_start ;;
      6) web_restart ;;
      7) web_add_domain ;;
      0) return 0 ;;
      *) warn "เลือกไม่ถูกต้อง" ;;
    esac
    echo
  done
}

cd "$PROJECT_DIR"

while true; do
  cat <<EOF_MENU
========================================
 ${MENU_COMMAND} - ${PROJECT_NAME} VPS control
 โฟลเดอร์หลัก: ${PROJECT_DIR}
 service: ${SERVICE_NAME}
 web service: ${WEB_SERVICE_NAME}
 Version: $(get_current_version)
========================================
1) ถอนการติดตั้ง (ลบทุกอย่างที่ดาวน์โหลดมา)
2) ดูสถานะการทำงาน
3) รีสตาร์ทระบบ (ข้อมูลฐานข้อมูลไม่หาย)
4) จัดการเว็บไซต์
5) อัปเดตสคริประบบ
0) ออก
EOF_MENU
  read -r -p "เลือกเมนู: " choice
  case "$choice" in
    1) uninstall_app ;;
    2) show_status ;;
    3) restart_service ;;
    4) show_web_menu ;;
    5) update_script ;;
    0) exit 0 ;;
    *) warn "เลือกไม่ถูกต้อง" ;;
  esac
  echo
done
