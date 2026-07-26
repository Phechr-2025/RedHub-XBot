#!/usr/bin/env python3
from __future__ import annotations

import html
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.parse import parse_qs

from wsgiref.simple_server import make_server, WSGIRequestHandler

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("ENV_FILE", str(APP_DIR / ".env")))
DEFAULT_PANEL_PATH = "/panel"

KEY_FIELDS: List[Tuple[str, str, str, str]] = [
    ("DISCORD_TOKEN", "Discord token", "password", "Bot token สำหรับ Discord"),
    ("ADMIN_IDS", "Admin IDs", "text", "คั่นด้วย comma เช่น 123,456"),
    ("XUI_URL", "3x-ui URL", "text", "เช่น http://127.0.0.1:2053"),
    ("XUI_API_TOKEN", "3x-ui API token", "password", "ถ้ามี"),
    ("XUI_USERNAME", "3x-ui username", "text", "ใช้กรณี login แบบ session"),
    ("XUI_PASSWORD", "3x-ui password", "password", "ใช้กรณี login แบบ session"),
    ("AIS_INBOUND_ID", "AIS inbound ID", "number", "หมายเลข inbound สำหรับ AIS"),
    ("TRUE_INBOUND_ID", "TRUE inbound ID", "number", "หมายเลข inbound สำหรับ TRUE"),
    ("DB_PATH", "SQLite DB path", "text", "เช่น /data/bot.db"),
    ("TRUEMONEY_WALLET_PHONE", "Truemoney wallet phone", "text", "เบอร์รับเงิน"),
    ("APP_SLUG", "App slug", "text", "ชื่อโฟลเดอร์/บริการ"),
    ("MENU_COMMAND", "Menu command", "text", "คำสั่งเปิด menubot"),
    ("WEB_SERVICE_NAME", "Web service name", "text", "service ของหน้าเว็บ"),
    ("WEB_PORT", "Web port", "number", "พอร์ตเว็บไซต์"),
    ("WEB_DOMAIN", "Web domain", "text", "โดเมนสำหรับ Let's Encrypt"),
    ("WEB_PANEL_PATH", "Panel path", "text", "เส้นทางหน้าเว็บ เช่น /panel"),
]

BOOLEAN_HINTS = {
    "WEB_DOMAIN": "ถ้ายังไม่ผูกโดเมน ให้เว้นว่างไว้ แล้วเพิ่มภายหลังจากเมนูจัดการเว็บไซต์",
}

ENV_LINE_RE = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.*?)(\s*)$')


def _load_raw_lines() -> List[str]:
    if ENV_FILE.exists():
        return ENV_FILE.read_text(encoding="utf-8").splitlines()
    return []


def _parse_env() -> Dict[str, str]:
    data: Dict[str, str] = {}
    for raw in _load_raw_lines():
        m = ENV_LINE_RE.match(raw)
        if not m:
            continue
        key = m.group(2)
        value = m.group(4).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        value = value.replace(r'\n', '\n').replace(r'\"', '"').replace(r'\\', '\\')
        data[key] = value
    return data


def _escape_env_value(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def _write_env(updates: Dict[str, str]) -> None:
    existing_lines = _load_raw_lines()
    out: List[str] = []
    seen = set()

    for raw in existing_lines:
        m = ENV_LINE_RE.match(raw)
        if not m:
            out.append(raw)
            continue
        key = m.group(2)
        if key in updates:
            out.append(f"{key}={_escape_env_value(updates[key])}")
            seen.add(key)
        else:
            out.append(raw)

    for key, value in updates.items():
        if key not in seen and not any(ENV_LINE_RE.match(line) and ENV_LINE_RE.match(line).group(2) == key for line in existing_lines):
            out.append(f"{key}={_escape_env_value(value)}")

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _env() -> Dict[str, str]:
    data = _parse_env()
    # sensible defaults
    data.setdefault("WEB_PORT", "2026")
    data.setdefault("WEB_PANEL_PATH", DEFAULT_PANEL_PATH)
    data.setdefault("WEB_SERVICE_NAME", f"{data.get('SERVICE_NAME', 'xbot')}-web")
    data.setdefault("SERVICE_NAME", data.get("APP_SLUG", "xbot"))
    data.setdefault("APP_SLUG", "xbot")
    return data



def _panel_path(env: Dict[str, str]) -> str:
    path = _panel_path(env)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path


def _public_ip() -> str:
    try:
        proc = subprocess.run(
            ["python3", "-c", "import urllib.request; print(urllib.request.urlopen('https://api.ipify.org', timeout=8).read().decode().strip())"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        ip = proc.stdout.strip()
        if ip:
            return ip
    except Exception:
        pass

    try:
        proc = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5, check=False)
        parts = [p for p in proc.stdout.split() if p and not p.startswith("127.")]
        if parts:
            return parts[0]
    except Exception:
        pass

    return "0.0.0.0"


def _service_status(name: str) -> str:
    try:
        proc = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (proc.stdout.strip() or proc.stderr.strip() or "unknown")
    except Exception as exc:
        return f"error: {exc}"


def _restart_service(name: str) -> None:
    subprocess.run(["systemctl", "restart", name], check=False, timeout=30)


def _panel_url(env: Dict[str, str], host: str) -> str:
    port = env.get("WEB_PORT", "2026")
    path = _panel_path(env)
    return f"http://{host}:{port}{path}" if host and host != "0.0.0.0" else f"http://127.0.0.1:{port}{path}"


def _https_url(env: Dict[str, str]) -> str:
    domain = env.get("WEB_DOMAIN", "").strip()
    if not domain:
        return ""
    path = _panel_path(env)
    return f"https://{domain}{path}"


def _render_notice(title: str, message: str, level: str = "ok") -> str:
    cls = {"ok": "card ok", "warn": "card warn", "error": "card error"}.get(level, "card")
    return f"""
    <section class="{cls}">
      <h3>{html.escape(title)}</h3>
      <p>{message}</p>
      <p><a href="{html.escape(_panel_path(_env()))}">กลับไปหน้าเว็บ</a></p>
    </section>
    """


def _render_page(env: Dict[str, str], note: str = "", level: str = "ok") -> str:
    host = _public_ip()
    panel_url = _panel_url(env, host)
    https_url = _https_url(env)
    bot_service = env.get("SERVICE_NAME", "xbot")
    web_service = env.get("WEB_SERVICE_NAME", f"{bot_service}-web")
    port = env.get("WEB_PORT", "2026")
    domain = env.get("WEB_DOMAIN", "").strip() or "ยังไม่ได้ผูกโดเมน"
    panel_path = _panel_path(env)

    cards = "".join(
        f"""
        <label class="field">
          <span>{html.escape(label)}</span>
          <input type="{field_type}" name="{key}" value="{html.escape(env.get(key, ''))}" placeholder="{html.escape(help_text)}">
          <small>{html.escape(help_text)}</small>
        </label>
        """
        for key, label, field_type, help_text in KEY_FIELDS
    )

    status_badge_bot = _service_status(bot_service)
    status_badge_web = _service_status(web_service)

    quick_links = f"""
      <a class="btn" href="{html.escape(panel_url)}" target="_blank">เปิด Panel</a>
      {"<a class='btn secondary' href='" + html.escape(https_url) + "' target='_blank'>เปิด HTTPS</a>" if https_url else ""}
    """

    return f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedHub-XBot Control Panel</title>
  <style>
    :root {{
      --bg1: #07111f;
      --bg2: #0f172a;
      --card: rgba(15, 23, 42, .78);
      --line: rgba(148, 163, 184, .16);
      --text: #e5e7eb;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent2: #22c55e;
      --warn: #f59e0b;
      --err: #ef4444;
      --shadow: 0 24px 80px rgba(2, 6, 23, .45);
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(circle at top left, rgba(56, 189, 248, .18), transparent 30%),
        radial-gradient(circle at top right, rgba(34, 197, 94, .12), transparent 25%),
        linear-gradient(160deg, var(--bg1), var(--bg2) 70%);
    }}
    .wrap {{ max-width: 1180px; margin: 0 auto; padding: 28px 18px 48px; }}
    .hero {{
      padding: 28px;
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(15,23,42,.92), rgba(2,6,23,.86));
      border-radius: 28px;
      box-shadow: var(--shadow);
      margin-bottom: 18px;
    }}
    .eyebrow {{
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(56, 189, 248, .12);
      color: #bae6fd;
      border: 1px solid rgba(56, 189, 248, .22);
      font-size: 13px;
      margin-bottom: 14px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 34px; line-height: 1.1; }}
    .sub {{ margin: 0; color: var(--muted); max-width: 850px; line-height: 1.6; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 16px;
      margin-top: 18px;
    }}
    .card {{
      grid-column: span 12;
      border: 1px solid var(--line);
      border-radius: 24px;
      padding: 20px;
      background: var(--card);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }}
    .card.ok {{ border-color: rgba(34,197,94,.28); }}
    .card.warn {{ border-color: rgba(245,158,11,.32); }}
    .card.error {{ border-color: rgba(239,68,68,.38); }}
    .stats {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
    .stat {{
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(2, 6, 23, .5);
      border: 1px solid var(--line);
    }}
    .stat .k {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 4px; }}
    .stat .v {{ font-size: 15px; font-weight: 700; word-break: break-word; }}
    .field {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 14px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(2, 6, 23, .42);
    }}
    .field span {{ font-size: 14px; font-weight: 700; }}
    .field small {{ color: var(--muted); line-height: 1.5; }}
    .field input {{
      width: 100%;
      border-radius: 14px;
      border: 1px solid rgba(148,163,184,.18);
      background: rgba(15,23,42,.9);
      color: var(--text);
      padding: 12px 14px;
      font-size: 15px;
      outline: none;
    }}
    .field input:focus {{ border-color: rgba(56,189,248,.55); box-shadow: 0 0 0 3px rgba(56,189,248,.12); }}
    .two-col {{ grid-column: span 12; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .three-col {{ grid-column: span 12; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin-top: 18px;
    }}
    .btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      border: 0;
      text-decoration: none;
      cursor: pointer;
      padding: 12px 18px;
      border-radius: 14px;
      background: linear-gradient(135deg, var(--accent), #0ea5e9);
      color: white;
      font-weight: 800;
      box-shadow: 0 14px 30px rgba(14,165,233,.18);
    }}
    .btn.secondary {{
      background: rgba(15,23,42,.9);
      border: 1px solid var(--line);
      box-shadow: none;
    }}
    .muted {{ color: var(--muted); }}
    code {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 8px;
      background: rgba(148,163,184,.12);
      border: 1px solid rgba(148,163,184,.16);
    }}
    .split {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
    }}
    @media (max-width: 960px) {{
      .stats, .split, .two-col, .three-col {{ grid-template-columns: 1fr; display: grid; }}
      .card {{ padding: 16px; }}
      h1 {{ font-size: 28px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="eyebrow">RedHub-XBot • Control Panel</div>
      <h1>เว็บไซต์ตั้งค่าระบบ</h1>
      <p class="sub">หน้าเว็บนี้ใช้แก้ค่า <code>.env</code> ได้โดยตรง, แสดง URL ที่ใช้งานจริง, และสั่งรีสตาร์ท service ที่เกี่ยวข้องได้จากหน้าเดียว</p>
      <div class="actions">
        <a class="btn" href="{html.escape(panel_url)}" target="_blank">เปิด Panel</a>
        {quick_links}
      </div>
    </section>

    {f'<section class="card {"ok" if level == "ok" else "warn" if level == "warn" else "error"}"><strong>{html.escape(note)}</strong></section>' if note else ''}

    <section class="grid">
      <div class="card">
        <h2 style="margin-top:0">สถานะระบบ</h2>
        <div class="stats">
          <div class="stat"><span class="k">Bot service</span><span class="v">{html.escape(bot_service)}</span><span class="muted">{html.escape(status_badge_bot)}</span></div>
          <div class="stat"><span class="k">Web service</span><span class="v">{html.escape(web_service)}</span><span class="muted">{html.escape(status_badge_web)}</span></div>
          <div class="stat"><span class="k">URL (IP)</span><span class="v">{html.escape(panel_url)}</span></div>
          <div class="stat"><span class="k">URL (HTTPS)</span><span class="v">{html.escape(https_url or 'ยังไม่ผูกโดเมน')}</span></div>
        </div>
      </div>

      <div class="card">
        <h2 style="margin-top:0">ข้อมูลเว็บ</h2>
        <div class="split">
          <div class="stat"><span class="k">Public IP</span><span class="v">{html.escape(host)}</span></div>
          <div class="stat"><span class="k">Port</span><span class="v">{html.escape(port)}</span></div>
          <div class="stat"><span class="k">Domain</span><span class="v">{html.escape(domain)}</span></div>
          <div class="stat"><span class="k">Panel path</span><span class="v">{html.escape(panel_path)}</span></div>
        </div>
      </div>

      <div class="card">
        <h2 style="margin-top:0">แก้ไขค่า</h2>
        <form method="post" action="{html.escape(panel_path)}/save">
          <div class="three-col">
            {cards}
          </div>
          <div class="actions">
            <button class="btn" type="submit" name="action" value="save">บันทึก</button>
            <button class="btn secondary" type="submit" name="action" value="save_restart_bot">บันทึกและรีสตาร์ท Bot</button>
            <button class="btn secondary" type="submit" name="action" value="save_restart_web">บันทึกและรีสตาร์ท Web</button>
            <button class="btn secondary" type="submit" name="action" value="save_restart_all">บันทึกและรีสตาร์ททั้งหมด</button>
          </div>
        </form>
      </div>
    </section>
  </div>
</body>
</html>
"""


def _parse_post(environ) -> Dict[str, str]:
    try:
        size = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        size = 0
    body = environ["wsgi.input"].read(size).decode("utf-8", "replace")
    data = {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}
    return data


def _handle_save(form: Dict[str, str]) -> Tuple[str, str]:
    env = _env()
    updates: Dict[str, str] = {}

    for key, _label, _field_type, _help in KEY_FIELDS:
        if key in form:
            updates[key] = form[key].strip()

    # Preserve useful defaults.
    if not updates.get("WEB_PORT"):
        updates["WEB_PORT"] = env.get("WEB_PORT", "2026")
    if not updates.get("WEB_PANEL_PATH"):
        updates["WEB_PANEL_PATH"] = env.get("WEB_PANEL_PATH", DEFAULT_PANEL_PATH) or DEFAULT_PANEL_PATH
    if not updates.get("WEB_SERVICE_NAME"):
        updates["WEB_SERVICE_NAME"] = env.get("WEB_SERVICE_NAME", f"{env.get('SERVICE_NAME', 'xbot')}-web")

    _write_env(updates)
    return ("บันทึกค่าเว็บไซต์เรียบร้อยแล้ว", "ok")


def _restart_after(action: str) -> None:
    env = _env()
    bot_service = env.get("SERVICE_NAME", "xbot")
    web_service = env.get("WEB_SERVICE_NAME", f"{bot_service}-web")

    if action == "bot":
        _restart_service(bot_service)
    elif action == "web":
        _restart_service(web_service)
    elif action == "all":
        _restart_service(web_service)
        _restart_service(bot_service)


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    env = _env()

    panel_path = _panel_path(env)

    if path == panel_path and method == "GET":
        body = _render_page(env)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]

    if path in {"/save", f"{panel_path}/save"} and method == "POST":
        form = _parse_post(environ)
        action = form.get("action", "save")
        note, level = _handle_save(form)

        # Restart after save according to action.
        if action == "save_restart_bot":
            _restart_after("bot")
            note = "บันทึกแล้วและสั่งรีสตาร์ท Bot service"
        elif action == "save_restart_web":
            _restart_after("web")
            note = "บันทึกแล้วและสั่งรีสตาร์ท Web service"
        elif action == "save_restart_all":
            _restart_after("all")
            note = "บันทึกแล้วและสั่งรีสตาร์ททั้งหมด"

        body = _render_page(_env(), note=note, level=level)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]

    if path in {"/restart-bot", f"{panel_path}/restart-bot"} and method == "POST":
        _restart_after("bot")
        body = _render_page(_env(), note="สั่งรีสตาร์ท Bot service แล้ว", level="ok")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]

    if path in {"/restart-web", f"{panel_path}/restart-web"} and method == "POST":
        _restart_after("web")
        body = _render_page(_env(), note="สั่งรีสตาร์ท Web service แล้ว", level="ok")
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]

    start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
    return [b"Not found"]


def main() -> None:
    env = _env()
    port = int(env.get("WEB_PORT", "2026") or 2026)
    host = "0.0.0.0"
    print(f"Web panel listening on {host}:{port}")
    with make_server(host, port, application, handler_class=type("QuietHandler", (WSGIRequestHandler,), {"log_message": lambda *args, **kwargs: None})) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
