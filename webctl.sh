#!/usr/bin/env bash
set -euo pipefail

info() { printf '\033[1;32m[INFO]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[ERR]\033[0m  %s\n' "$*"; }

CONFIG_FILE="/etc/menubot.conf"
if [[ ! -f "$CONFIG_FILE" ]]; then
  err "ไม่พบไฟล์ตั้งค่า ${CONFIG_FILE}"
  exit 1
fi

# shellcheck disable=SC1090
source "$CONFIG_FILE"

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/${APP_SLUG:-xbot}}"
ENV_FILE="${ENV_FILE:-${PROJECT_DIR}/.env}"
WEB_PANEL_PATH="${WEB_PANEL_PATH:-/panel}"
WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-${SERVICE_NAME:-xbot}-web}"
BOT_SERVICE_NAME="${SERVICE_NAME:-xbot}"
NGINX_CONF="/etc/nginx/sites-available/${WEB_SERVICE_NAME}.conf"
NGINX_LINK="/etc/nginx/sites-enabled/${WEB_SERVICE_NAME}.conf"

if [[ ! -f "$ENV_FILE" ]]; then
  err "ไม่พบไฟล์ .env ที่ ${ENV_FILE}"
  exit 1
fi

# shellcheck disable=SC1090
set -a
source "$ENV_FILE"
set +a

sanitize_path() {
  local p="${1:-$WEB_PANEL_PATH}"
  [[ "$p" == /* ]] || p="/$p"
  printf '%s' "$p"
}

panel_path="$(sanitize_path "${WEB_PANEL_PATH:-/panel}")"

update_env() {
  local key="$1"
  local value="$2"
  python3 - "$ENV_FILE" "$key" "$value" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
pattern = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s*)$')
lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
out = []
replaced = False

def esc(v: str) -> str:
    return '"' + v.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'

for line in lines:
    m = pattern.match(line)
    if m and m.group(2) == key:
        out.append(f"{key}={esc(value)}")
        replaced = True
    else:
        out.append(line)

if not replaced:
    out.append(f"{key}={esc(value)}")

path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("\n".join(out).rstrip() + "\n", encoding='utf-8')
PY
}

refresh_env() {
  # shellcheck disable=SC1090
  set -a
  source "$ENV_FILE"
  set +a
  WEB_HTTP_PORT="${WEB_HTTP_PORT:-${WEB_PORT:-9090}}"
  WEB_HTTPS_PORT="${WEB_HTTPS_PORT:-9943}"
  WEB_PORT="${WEB_PORT:-${WEB_HTTP_PORT}}"
  WEB_DOMAIN="${WEB_DOMAIN:-}"
  WEB_PANEL_PATH="${WEB_PANEL_PATH:-/panel}"
  WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-${SERVICE_NAME:-xbot}-web}"
  BOT_SERVICE_NAME="${SERVICE_NAME:-xbot}"
  panel_path="$(sanitize_path "${WEB_PANEL_PATH}")"
}

ensure_runtime() {
  local venv_dir="${PROJECT_DIR}/.venv"
  local venv_python="${venv_dir}/bin/python"
  local venv_pip="${venv_dir}/bin/pip"

  if [[ ! -x "$venv_python" || ! -x "$venv_pip" ]]; then
    info "สร้าง virtual environment ใหม่สำหรับเว็บ"
    rm -rf "$venv_dir"
    python3 -m venv "$venv_dir"
  fi

  "$venv_pip" install --upgrade pip >/dev/null 2>&1 || true
  "$venv_pip" install -r "${PROJECT_DIR}/requirements.txt"
}


generate_panel_path() {
  python3 - <<'PY'
import random
import string
print("/" + "".join(random.choice(string.ascii_lowercase) for _ in range(10)))
PY
}

public_ip() {
  local ip=""
  ip="$(python3 - <<'PY'
import urllib.request
try:
    print(urllib.request.urlopen("https://api.ipify.org", timeout=8).read().decode().strip())
except Exception:
    pass
PY
)"
  if [[ -n "$ip" ]]; then
    printf '%s' "$ip"
    return
  fi
  ip="$(hostname -I 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i !~ /^127\./) {print $i; exit}}' || true)"
  printf '%s' "${ip:-0.0.0.0}"
}

domain_resolves_to_vps() {
  local domain="$1"
  local vps_ip
  vps_ip="$(public_ip)"
  python3 - "$domain" "$vps_ip" <<'PY'
import socket
import sys
domain = sys.argv[1]
vps_ip = sys.argv[2]
try:
    _, _, ips = socket.gethostbyname_ex(domain)
except Exception as exc:
    print(f"FAIL: resolve error: {exc}")
    raise SystemExit(1)
if vps_ip not in ips:
    print("MISMATCH:" + ",".join(ips))
    raise SystemExit(2)
print("OK")
PY
}

write_nginx_conf() {
  local domain="$1"
  local port="$2"
  local path="$3"
  mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
  cat > "$NGINX_CONF" <<EOF
server {
    listen 80;
    server_name ${domain};

    location / {
        proxy_pass http://127.0.0.1:${port};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;
    }

    location ${path} {
        proxy_pass http://127.0.0.1:${port}${path};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  ln -sf "$NGINX_CONF" "$NGINX_LINK"
}

ensure_domain_https() {
  local domain="$1"

  if ! domain_resolves_to_vps "$domain"; then
    warn "โดเมนยังไม่ชี้มาที่ VPS นี้"
    warn "ให้ตั้ง A record ไปที่ IP: $(public_ip)"
    return 1
  fi

  info "ติดตั้ง certbot"
  apt-get update -y
  apt-get install -y certbot

  info "ขอใบรับรอง Let's Encrypt แบบ standalone"
  certbot certonly     --standalone     --non-interactive     --agree-tos     --register-unsafely-without-email     -d "$domain"

  return 0
}

show_urls() {
  refresh_env
  local ip url https_url domain service_state
  ip="$(public_ip)"
  domain="${WEB_DOMAIN:-}"
  url="http://${ip}:${WEB_HTTP_PORT}${panel_path}"
  https_url=""
  if [[ -n "$domain" ]]; then
    https_url="https://${domain}:${WEB_HTTPS_PORT}${panel_path}"
  fi
  service_state="$(systemctl is-active "${WEB_SERVICE_NAME}" 2>/dev/null || true)"

  echo "=============================="
  echo "Bot service : ${BOT_SERVICE_NAME}"
  echo "Web service : ${WEB_SERVICE_NAME}"
  echo "HTTP port   : ${WEB_HTTP_PORT}"
  echo "HTTPS port  : ${WEB_HTTPS_PORT}"
  echo "Panel path  : ${panel_path}"
  echo "Public IP   : ${ip}"
  echo "HTTP URL    : ${url}"
  if [[ -n "$https_url" ]]; then
    echo "HTTPS URL   : ${https_url}"
  else
    echo "HTTPS URL   : ยังไม่ได้ผูกโดเมน"
  fi
  echo "Domain      : ${domain:-ยังไม่ได้ตั้งค่า}"
  echo "State       : ${service_state:-unknown}"
  echo "=============================="
}

restart_web() {
  ensure_runtime
  info "รีสตาร์ทเว็บ service: ${WEB_SERVICE_NAME}"
  systemctl restart "${WEB_SERVICE_NAME}"
}

start_web() {
  ensure_runtime
  info "เริ่มเว็บ service: ${WEB_SERVICE_NAME}"
  systemctl start "${WEB_SERVICE_NAME}"
}

stop_web() {
  info "หยุดเว็บ service: ${WEB_SERVICE_NAME}"
  systemctl stop "${WEB_SERVICE_NAME}"
}

check_web_status() {
  refresh_env
  echo "=============================="
  echo "สถานะเว็บไซต์"
  echo "Service : ${WEB_SERVICE_NAME}"
  echo "Active  : $(systemctl is-active "${WEB_SERVICE_NAME}" 2>/dev/null || true)"
  echo "Enabled : $(systemctl is-enabled "${WEB_SERVICE_NAME}" 2>/dev/null || true)"
  echo "HTTP Port: ${WEB_HTTP_PORT}"
  echo "HTTPS Port: ${WEB_HTTPS_PORT}"
  echo "Path    : ${panel_path}"
  echo "Domain  : ${WEB_DOMAIN:-ยังไม่ได้ตั้งค่า}"
  echo "=============================="
  systemctl status "${WEB_SERVICE_NAME}" --no-pager -l || true
  echo
  journalctl -u "${WEB_SERVICE_NAME}" -n 10 --no-pager || true
}

update_web_libraries() {
  refresh_env
  local was_active=0
  if systemctl is-active --quiet "${WEB_SERVICE_NAME}" 2>/dev/null; then
    was_active=1
  fi

  info "อัปเดตแพ็กเกจที่เว็บต้องใช้"
  apt-get update -y
  apt-get install -y certbot

  if [[ "$was_active" -eq 1 ]]; then
    info "รีสตาร์ทเว็บ service"
    systemctl restart "${WEB_SERVICE_NAME}"
  fi

  info "อัปเดตไลบารี่เว็บเรียบร้อย"
}

reset_panel_web() {
  refresh_env
  local new_path
  new_path="$(generate_panel_path)"
  update_env "WEB_PANEL_PATH" "$new_path"
  refresh_env

  if [[ -n "${WEB_DOMAIN:-}" ]]; then
    write_nginx_conf "$WEB_DOMAIN" "$WEB_PORT" "$panel_path"
    nginx -t >/dev/null 2>&1 || true
    systemctl reload nginx >/dev/null 2>&1 || true
  fi

  restart_web
  info "สุ่ม Panel path ใหม่แล้ว: ${panel_path}"
  show_urls
}

set_web_http_port() {
  local new_port="$1"
  if [[ ! "$new_port" =~ ^[0-9]+$ ]] || [[ "$new_port" -lt 1 || "$new_port" -gt 65535 ]]; then
    err "พอร์ตไม่ถูกต้อง"
    exit 1
  fi
  update_env "WEB_HTTP_PORT" "$new_port"
  update_env "WEB_PORT" "$new_port"
  refresh_env
  restart_web
  info "ตั้งพอร์ต HTTP เว็บเป็น ${new_port} แล้ว"
}

set_web_https_port() {
  local new_port="$1"
  if [[ ! "$new_port" =~ ^[0-9]+$ ]] || [[ "$new_port" -lt 1 || "$new_port" -gt 65535 ]]; then
    err "พอร์ตไม่ถูกต้อง"
    exit 1
  fi
  update_env "WEB_HTTPS_PORT" "$new_port"
  refresh_env
  restart_web
  info "ตั้งพอร์ต HTTPS เว็บเป็น ${new_port} แล้ว"
}

add_domain() {
  local domain="$1"
  if [[ -z "$domain" ]]; then
    err "ต้องระบุโดเมน"
    exit 1
  fi
  refresh_env
  if ensure_domain_https "$domain"; then
    update_env "WEB_DOMAIN" "$domain"
    refresh_env
    restart_web
    info "ผูกโดเมนเรียบร้อย: https://${domain}:${WEB_HTTPS_PORT}${panel_path}"
  else
    err "ผูกโดเมนไม่สำเร็จ"
    exit 1
  fi
}

case "${1:-}" in
  show|"")
    show_urls
    ;;
  status)
    check_web_status
    ;;
  reset)
    reset_panel_web
    ;;
  set-port)
    shift
    set_web_http_port "${1:-}"
    ;;
  set-http-port)
    shift
    set_web_http_port "${1:-}"
    ;;
  set-https-port)
    shift
    set_web_https_port "${1:-}"
    ;;
  stop)
    stop_web
    ;;
  start)
    start_web
    ;;
  restart)
    restart_web
    ;;
  add-domain)
    shift
    add_domain "${1:-}"
    ;;
  update-libs)
    update_web_libraries
    ;;
  *)
    cat <<EOF
Usage: $0 [show|status|reset|set-port <port>|set-http-port <port>|set-https-port <port>|stop|start|restart|add-domain <domain>|update-libs]
EOF
    exit 1
    ;;
esac
