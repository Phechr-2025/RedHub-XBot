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
  WEB_PORT="${WEB_PORT:-2026}"
  WEB_DOMAIN="${WEB_DOMAIN:-}"
  WEB_PANEL_PATH="${WEB_PANEL_PATH:-/panel}"
  WEB_SERVICE_NAME="${WEB_SERVICE_NAME:-${SERVICE_NAME:-xbot}-web}"
  BOT_SERVICE_NAME="${SERVICE_NAME:-xbot}"
  panel_path="$(sanitize_path "${WEB_PANEL_PATH}")"
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
  local port="$2"
  local path="$3"

  if ! domain_resolves_to_vps "$domain"; then
    warn "โดเมนยังไม่ชี้มาที่ VPS นี้"
    warn "ให้ตั้ง A record ไปที่ IP: $(public_ip)"
    return 1
  fi

  info "ติดตั้ง nginx / certbot"
  apt-get update -y
  apt-get install -y nginx certbot python3-certbot-nginx

  write_nginx_conf "$domain" "$port" "$path"

  nginx -t
  systemctl enable nginx
  systemctl restart nginx

  info "ขอใบรับรอง Let's Encrypt"
  certbot --nginx \
    --non-interactive \
    --agree-tos \
    --register-unsafely-without-email \
    --redirect \
    -d "$domain"

  systemctl enable certbot.timer >/dev/null 2>&1 || true
  systemctl restart nginx
  return 0
}

show_urls() {
  refresh_env
  local ip url https_url domain
  ip="$(public_ip)"
  domain="${WEB_DOMAIN:-}"
  url="http://${ip}:${WEB_PORT}${panel_path}"
  https_url=""
  if [[ -n "$domain" ]]; then
    https_url="https://${domain}${panel_path}"
  fi

  echo "=============================="
  echo "Bot service : ${BOT_SERVICE_NAME}"
  echo "Web service : ${WEB_SERVICE_NAME}"
  echo "Web port    : ${WEB_PORT}"
  echo "Panel path  : ${panel_path}"
  echo "Public IP   : ${ip}"
  echo "HTTP URL    : ${url}"
  if [[ -n "$https_url" ]]; then
    echo "HTTPS URL   : ${https_url}"
  else
    echo "HTTPS URL   : ยังไม่ได้ผูกโดเมน"
  fi
  echo "Domain      : ${domain:-ยังไม่ได้ตั้งค่า}"
  echo "=============================="
  systemctl status "${WEB_SERVICE_NAME}" --no-pager || true
}

restart_web() {
  info "รีสตาร์ทเว็บ service: ${WEB_SERVICE_NAME}"
  systemctl restart "${WEB_SERVICE_NAME}"
}

start_web() {
  info "เริ่มเว็บ service: ${WEB_SERVICE_NAME}"
  systemctl start "${WEB_SERVICE_NAME}"
}

stop_web() {
  info "หยุดเว็บ service: ${WEB_SERVICE_NAME}"
  systemctl stop "${WEB_SERVICE_NAME}"
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

set_web_port() {
  local new_port="$1"
  if [[ ! "$new_port" =~ ^[0-9]+$ ]] || [[ "$new_port" -lt 1 || "$new_port" -gt 65535 ]]; then
    err "พอร์ตไม่ถูกต้อง"
    exit 1
  fi
  update_env "WEB_PORT" "$new_port"
  refresh_env
  if [[ -n "${WEB_DOMAIN:-}" ]]; then
    write_nginx_conf "$WEB_DOMAIN" "$WEB_PORT" "$panel_path"
    nginx -t >/dev/null 2>&1 || true
    systemctl reload nginx >/dev/null 2>&1 || true
  fi
  restart_web
  info "ตั้งพอร์ตเว็บเป็น ${new_port} แล้ว"
}

add_domain() {
  local domain="$1"
  if [[ -z "$domain" ]]; then
    err "ต้องระบุโดเมน"
    exit 1
  fi
  refresh_env
  if ensure_domain_https "$domain" "${WEB_PORT}" "$panel_path"; then
    update_env "WEB_DOMAIN" "$domain"
    refresh_env
    restart_web
    info "ผูกโดเมนเรียบร้อย: https://${domain}${panel_path}"
  else
    err "ผูกโดเมนไม่สำเร็จ"
    exit 1
  fi
}

case "${1:-}" in
  show|"")
    show_urls
    ;;
  reset)
    reset_panel_web
    ;;
  set-port)
    shift
    set_web_port "${1:-}"
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
  *)
    cat <<EOF
Usage: $0 [show|reset|set-port <port>|stop|start|restart|add-domain <domain>]
EOF
    exit 1
    ;;
esac
