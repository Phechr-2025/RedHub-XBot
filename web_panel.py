#!/usr/bin/env python3
from __future__ import annotations

import html
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import parse_qs

from wsgiref.simple_server import WSGIRequestHandler, make_server

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
        value = value.replace(r"\n", "\n").replace(r"\"", '"').replace(r"\\", "\\")
        data[key] = value
    return data


def _escape_env_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


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
        if key not in seen and not any(
            ENV_LINE_RE.match(line) and ENV_LINE_RE.match(line).group(2) == key for line in existing_lines
        ):
            out.append(f"{key}={_escape_env_value(value)}")

    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def _env() -> Dict[str, str]:
    data = _parse_env()
    data.setdefault("WEB_PORT", "2026")
    data.setdefault("WEB_PANEL_PATH", DEFAULT_PANEL_PATH)
    data.setdefault("WEB_SERVICE_NAME", f"{data.get('SERVICE_NAME', 'xbot')}-web")
    data.setdefault("SERVICE_NAME", data.get("APP_SLUG", "xbot"))
    data.setdefault("APP_SLUG", "xbot")
    return data


def _normalize_panel_path(path: str) -> str:
    path = (path or DEFAULT_PANEL_PATH).strip()
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or DEFAULT_PANEL_PATH


def _panel_path(env: Dict[str, str]) -> str:
    return _normalize_panel_path(env.get("WEB_PANEL_PATH", DEFAULT_PANEL_PATH))


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
        status = (proc.stdout.strip() or proc.stderr.strip() or "unknown")
        if status == "active":
            return "running"
        return status
    except Exception as exc:
        return f"error: {exc}"


def _restart_service(name: str) -> None:
    subprocess.run(["systemctl", "restart", name], check=False, timeout=30)


def _panel_url(env: Dict[str, str], host: str) -> str:
    port = env.get("WEB_PORT", "2026")
    path = _panel_path(env)
    host = host if host and host != "0.0.0.0" else "127.0.0.1"
    return f"http://{host}:{port}{path}"


def _https_url(env: Dict[str, str]) -> str:
    domain = env.get("WEB_DOMAIN", "").strip()
    if not domain:
        return ""
    path = _panel_path(env)
    return f"https://{domain}{path}"


def _redirect_response(location: str, start_response):
    start_response(
        "302 Found",
        [
            ("Location", location),
            ("Content-Type", "text/plain; charset=utf-8"),
        ],
    )
    return [f"Redirecting to {location}".encode("utf-8")]


def _render_notice(title: str, message: str, level: str = "ok") -> str:
    cls = {"ok": "notice ok", "warn": "notice warn", "error": "notice error"}.get(level, "notice")
    return f"""
      <section class="{cls}">
        <h3>{html.escape(title)}</h3>
        <p>{message}</p>
      </section>
    """


def _service_chip(name: str, status: str) -> str:
    badge = {
        "running": "online",
        "active": "online",
        "inactive": "offline",
        "failed": "error",
    }.get(status, status)
    return f"""
      <div class="chip">
        <span class="chip-label">{html.escape(name)}</span>
        <span class="chip-status {html.escape(badge)}">{html.escape(status)}</span>
      </div>
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
    current_url = (f"http://{host}:{port}{panel_path}" if host and host != "0.0.0.0" else f"http://127.0.0.1:{port}{panel_path}")

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

    bot_status = _service_status(bot_service)
    web_status = _service_status(web_service)

    return f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedHub-XBot Control Panel</title>
  <style>
    :root {{
      --bg: #0b1220;
      --bg2: #111827;
      --card: rgba(17, 24, 39, .78);
      --card-2: rgba(15, 23, 42, .92);
      --line: rgba(148, 163, 184, .14);
      --line-strong: rgba(148, 163, 184, .22);
      --text: #e5e7eb;
      --muted: #94a3b8;
      --accent: #38bdf8;
      --accent-2: #22c55e;
      --accent-3: #8b5cf6;
      --warn: #f59e0b;
      --err: #ef4444;
      --shadow: 0 18px 60px rgba(2, 6, 23, .46);
      --sidebar-w: 280px;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; min-height: 100%; background: var(--bg); color: var(--text); overflow-x: hidden; }}
    body {{
      min-height: 100vh;
      background:
        radial-gradient(circle at 15% 10%, rgba(56, 189, 248, .16), transparent 28%),
        radial-gradient(circle at 95% 8%, rgba(139, 92, 246, .14), transparent 24%),
        linear-gradient(180deg, #0b1220 0%, #0f172a 100%);
    }}
    .layout {{
      display: grid;
      grid-template-columns: var(--sidebar-w) 1fr;
      min-height: 100vh;
    }}
    .sidebar {{
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 18px 16px;
      background: linear-gradient(180deg, rgba(15,23,42,.98), rgba(2,6,23,.98));
      border-right: 1px solid var(--line);
      z-index: 40;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px 14px 10px;
      border-radius: 18px;
      background: rgba(255,255,255,.03);
      border: 1px solid var(--line);
    }}
    .brand-mark {{
      width: 42px;
      height: 42px;
      border-radius: 14px;
      display: grid;
      place-items: center;
      background: linear-gradient(135deg, var(--accent), var(--accent-3));
      box-shadow: 0 10px 28px rgba(56, 189, 248, .18);
      font-weight: 900;
      color: white;
      flex: 0 0 auto;
    }}
    .brand h1 {{
      font-size: 17px;
      line-height: 1.2;
      margin: 0;
      font-weight: 800;
    }}
    .brand p {{
      margin: 3px 0 0;
      color: var(--muted);
      font-size: 12px;
    }}
    .menu-title {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .12em;
      padding: 0 10px;
    }}
    .nav {{
      display: grid;
      gap: 8px;
      padding: 0 4px;
    }}
    .nav button {{
      appearance: none;
      border: 1px solid transparent;
      background: transparent;
      color: var(--text);
      padding: 14px 14px;
      border-radius: 16px;
      text-align: left;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 12px;
      transition: all .18s ease;
    }}
    .nav button:hover {{
      background: rgba(255,255,255,.04);
      border-color: var(--line);
    }}
    .nav button.active {{
      background: linear-gradient(135deg, rgba(56,189,248,.18), rgba(139,92,246,.14));
      border-color: rgba(56,189,248,.3);
      box-shadow: 0 12px 28px rgba(56, 189, 248, .12);
    }}
    .nav-ico {{
      width: 30px;
      height: 30px;
      border-radius: 10px;
      display: grid;
      place-items: center;
      background: rgba(255,255,255,.05);
      border: 1px solid var(--line);
      font-size: 15px;
      flex: 0 0 auto;
    }}
    .sidebar-footer {{
      margin-top: auto;
      padding: 14px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.03);
    }}
    .sidebar-footer .muted {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}
    .main {{
      min-width: 0;
      padding: 18px;
    }}
    .topbar {{
      position: sticky;
      top: 0;
      z-index: 30;
      display: flex;
      align-items: center;
      gap: 14px;
      padding: 14px 18px;
      border: 1px solid var(--line);
      border-radius: 22px;
      background: rgba(15, 23, 42, .78);
      backdrop-filter: blur(16px);
      box-shadow: var(--shadow);
      margin-bottom: 16px;
    }}
    .burger {{
      width: 44px;
      height: 44px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 5px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
      cursor: pointer;
      padding: 0;
      flex: 0 0 auto;
    }}
    .burger span {{
      width: 18px;
      height: 2px;
      border-radius: 999px;
      background: #e2e8f0;
      display: block;
    }}
    .topbar-copy {{
      min-width: 0;
      flex: 1 1 420px;
    }}
    .topbar-copy h2 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    .topbar-copy p {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      border-radius: 999px;
      padding: 9px 12px;
      background: rgba(255,255,255,.04);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .wrap {{
      max-width: 1240px;
      margin: 0 auto;
    }}
    .hero {{
      display: grid;
      gap: 14px;
      padding: 22px;
      border-radius: 26px;
      border: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(17,24,39,.8), rgba(2,6,23,.76));
      box-shadow: var(--shadow);
      margin-bottom: 16px;
    }}
    .hero-head {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }}
    .hero-head > div:first-child {{
      flex: 1 1 420px;
      min-width: 0;
    }}
    .hero-head > .actions {{
      flex: 0 1 auto;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(56,189,248,.12);
      border: 1px solid rgba(56,189,248,.24);
      color: #bae6fd;
      font-size: 13px;
      font-weight: 700;
    }}
    .hero h1 {{
      margin: 0;
      font-size: 30px;
      line-height: 1.15;
      letter-spacing: -.02em;
      overflow-wrap: anywhere;
      word-break: normal;
    }}
    .hero p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.65;
      max-width: 920px;
      overflow-wrap: anywhere;
    }}
    .actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      min-width: 0;
    }}
    .btn {{
      appearance: none;
      border: 1px solid transparent;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      text-decoration: none;
      padding: 12px 16px;
      border-radius: 14px;
      font-weight: 800;
      background: linear-gradient(135deg, var(--accent), #0ea5e9);
      color: white;
      box-shadow: 0 14px 30px rgba(14, 165, 233, .18);
    }}
    .btn.secondary {{
      background: rgba(255,255,255,.04);
      border-color: var(--line);
      box-shadow: none;
      color: var(--text);
    }}
    .grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(12, minmax(0, 1fr));
    }}
    .card {{
      grid-column: span 12;
      border-radius: 24px;
      border: 1px solid var(--line);
      background: var(--card);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      overflow: hidden;
    }}
    .card-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 18px 20px 0;
    }}
    .card-head h3 {{
      margin: 0;
      font-size: 18px;
      line-height: 1.2;
    }}
    .card-body {{
      padding: 18px 20px 20px;
    }}
    .section {{
      display: none;
    }}
    .section.active {{
      display: block;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
    }}
    .stat {{
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(2,6,23,.52);
      border: 1px solid var(--line);
      min-width: 0;
    }}
    .stat .k {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 5px;
      text-transform: uppercase;
      letter-spacing: .06em;
    }}
    .stat .v {{
      font-size: 15px;
      font-weight: 800;
      word-break: break-word;
      line-height: 1.45;
    }}
    .two-col {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .field {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 14px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(2, 6, 23, .4);
    }}
    .field span {{
      font-size: 14px;
      font-weight: 700;
    }}
    .field small {{
      color: var(--muted);
      line-height: 1.45;
    }}
    .field input {{
      width: 100%;
      border-radius: 14px;
      border: 1px solid rgba(148,163,184,.18);
      background: rgba(15,23,42,.92);
      color: var(--text);
      padding: 12px 14px;
      font-size: 15px;
      outline: none;
    }}
    .field input:focus {{
      border-color: rgba(56,189,248,.55);
      box-shadow: 0 0 0 3px rgba(56,189,248,.12);
    }}
    .three-col {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }}
    .notice {{
      grid-column: span 12;
      border-radius: 20px;
      padding: 16px 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.03);
    }}
    .notice.ok {{
      border-color: rgba(34,197,94,.28);
    }}
    .notice.warn {{
      border-color: rgba(245,158,11,.34);
    }}
    .notice.error {{
      border-color: rgba(239,68,68,.38);
    }}
    .notice h3 {{
      margin: 0 0 6px;
      font-size: 16px;
    }}
    .notice p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 12px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
    }}
    .chip-label {{
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: .08em;
    }}
    .chip-status {{
      font-weight: 800;
      font-size: 13px;
      color: #cbd5e1;
    }}
    .chip-status.online {{ color: #86efac; }}
    .chip-status.offline {{ color: #fca5a5; }}
    .chip-status.error {{ color: #fda4af; }}
    .overlay {{
      position: fixed;
      inset: 0;
      background: rgba(2,6,23,.58);
      opacity: 0;
      pointer-events: none;
      transition: opacity .2s ease;
      z-index: 35;
    }}
    .mobile-only {{
      display: none;
    }}
    @media (max-width: 1100px) {{
      .layout {{
        grid-template-columns: 1fr;
      }}
      .sidebar {{
        position: fixed;
        inset: 0 auto 0 0;
        width: min(88vw, var(--sidebar-w));
        transform: translateX(-102%);
        transition: transform .22s ease;
        box-shadow: 0 20px 60px rgba(0,0,0,.45);
        visibility: hidden;
        pointer-events: none;
      }}
      body.sidebar-open .sidebar {{
        visibility: visible;
        pointer-events: auto;
      }}
      body.sidebar-open .sidebar {{
        transform: translateX(0);
      }}
      body.sidebar-open .overlay {{
        opacity: 1;
        pointer-events: auto;
      }}
      .mobile-only {{
        display: inline-flex;
      }}
      .topbar {{
        flex-direction: column;
        align-items: stretch;
      }}
      .topbar > .pill {{
        width: 100%;
        justify-content: center;
        white-space: normal;
      }}
      .hero-head {{
        flex-direction: column;
        align-items: stretch;
      }}
      .hero-head > div:first-child,
      .hero-head > .actions {{
        flex: 1 1 auto;
        width: 100%;
      }}
      .actions {{
        width: 100%;
      }}
      .actions .btn {{
        flex: 1 1 220px;
      }}
      .stats, .three-col, .two-col {{
        grid-template-columns: 1fr;
      }}
      .hero h1 {{
        font-size: 24px;
      }}
      .main {{
        padding: 14px;
      }}
    }}
    @media (max-width: 640px) {{
      .topbar-copy h2 {{
        font-size: 18px;
      }}
      .hero h1 {{
        font-size: 22px;
      }}
      .hero p {{
        font-size: 14px;
      }}
      .hero-head .actions .btn,
      .topbar .pill,
      .btn {{
        width: 100%;
      }}
      .actions {{
        flex-direction: column;
      }}
      .pill {{
        width: 100%;
        justify-content: center;
        white-space: normal;
        text-align: center;
      }}
      .topbar {{
        padding: 12px;
      }}
      .hero {{
        padding: 16px;
      }}
      .card-head, .card-body {{
        padding-left: 14px;
        padding-right: 14px;
      }}
    }}
  </style>
</head>
<body>
  <div class="overlay" id="overlay"></div>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="brand">
        <div class="brand-mark">RH</div>
        <div>
          <h1>RedHub-XBot</h1>
          <p>Control Panel</p>
        </div>
      </div>

      <div class="menu-title">เมนู</div>
      <div class="nav">
        <button class="active" data-tab="overview" type="button">
          <span class="nav-ico">⌂</span>
          ภาพรวม
        </button>
        <button data-tab="general" type="button">
          <span class="nav-ico">⚙</span>
          ตั้งค่าทั่วไป
        </button>
      </div>

      <div class="sidebar-footer">
        <div class="muted">พาเนลนี้อ้างอิงค่า `<code>.env</code>` และเปิดด้วยพาธสุ่มตาม <code>WEB_PANEL_PATH</code></div>
      </div>
    </aside>

    <main class="main">
      <div class="wrap">
        <div class="topbar">
          <button class="burger mobile-only" id="burger" type="button" aria-label="เปิดเมนู">
            <span></span><span></span><span></span>
          </button>
          <div class="topbar-copy">
            <h2>เว็บไซต์ตั้งค่าระบบ</h2>
            <p>UI แนว 3x-ui รุ่นใหม่: เมนูซ้าย, ภาพรวม, และตั้งค่าทั่วไปในหน้าเดียว</p>
          </div>
          <div class="pill">URL: {html.escape(current_url)}</div>
        </div>

        <section class="hero">
          <div class="hero-head">
            <div>
              <div class="eyebrow">RedHub-XBot • Web Panel</div>
              <h1>จัดการระบบจากหน้าเดียว</h1>
            </div>
            <div class="actions">
              <a class="btn" href="{html.escape(panel_url)}" target="_blank" rel="noreferrer">เปิด Panel</a>
              {f'<a class="btn secondary" href="{html.escape(https_url)}" target="_blank" rel="noreferrer">เปิด HTTPS</a>' if https_url else ''}
            </div>
          </div>
          <p>หน้าเว็บนี้รองรับการแก้ค่า <code>.env</code> แสดงสถานะ service และใช้โครงสร้าง sidebar ให้ใช้งานใกล้เคียง 3x-ui ล่าสุดมากขึ้น</p>
        </section>

        { _render_notice("สถานะ", html.escape(note), level) if note else "" }

        <section class="section active" id="section-overview">
          <div class="grid">
            <div class="card">
              <div class="card-head">
                <h3>ภาพรวม</h3>
              </div>
              <div class="card-body">
                <div class="stats">
                  <div class="stat"><span class="k">Bot service</span><span class="v">{html.escape(bot_service)}</span><span class="muted">{html.escape(bot_status)}</span></div>
                  <div class="stat"><span class="k">Web service</span><span class="v">{html.escape(web_service)}</span><span class="muted">{html.escape(web_status)}</span></div>
                  <div class="stat"><span class="k">URL (IP)</span><span class="v">{html.escape(panel_url)}</span></div>
                  <div class="stat"><span class="k">URL (HTTPS)</span><span class="v">{html.escape(https_url or "ยังไม่ผูกโดเมน")}</span></div>
                </div>
              </div>
            </div>

            <div class="card">
              <div class="card-head">
                <h3>ข้อมูลเว็บ</h3>
              </div>
              <div class="card-body">
                <div class="two-col">
                  <div class="stat"><span class="k">Public IP</span><span class="v">{html.escape(host)}</span></div>
                  <div class="stat"><span class="k">Port</span><span class="v">{html.escape(port)}</span></div>
                  <div class="stat"><span class="k">Domain</span><span class="v">{html.escape(domain)}</span></div>
                  <div class="stat"><span class="k">Panel path</span><span class="v">{html.escape(panel_path)}</span></div>
                </div>
              </div>
            </div>

            <div class="card">
              <div class="card-head">
                <h3>Service status</h3>
              </div>
              <div class="card-body">
                <div class="chips">
                  {_service_chip(bot_service, bot_status)}
                  {_service_chip(web_service, web_status)}
                </div>
              </div>
            </div>
          </div>
        </section>

        <section class="section" id="section-general">
          <div class="grid">
            <div class="card">
              <div class="card-head">
                <h3>ตั้งค่าทั่วไป</h3>
              </div>
              <div class="card-body">
                <form method="post" action="{html.escape(panel_path)}/save">
                  <div class="three-col">
                    {cards}
                  </div>
                  <div class="actions" style="margin-top:18px">
                    <button class="btn" type="submit" name="action" value="save">บันทึก</button>
                    <button class="btn secondary" type="submit" name="action" value="save_restart_bot">บันทึกและรีสตาร์ท Bot</button>
                    <button class="btn secondary" type="submit" name="action" value="save_restart_web">บันทึกและรีสตาร์ท Web</button>
                    <button class="btn secondary" type="submit" name="action" value="save_restart_all">บันทึกและรีสตาร์ททั้งหมด</button>
                  </div>
                </form>
              </div>
            </div>
          </div>
        </section>

      </div>
    </main>
  </div>

  <script>
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('overlay');
    const burger = document.getElementById('burger');
    const navButtons = Array.from(document.querySelectorAll('.nav button[data-tab]'));
    const sections = {{
      overview: document.getElementById('section-overview'),
      general: document.getElementById('section-general'),
    }};

    function setTab(tab) {{
      const target = sections[tab] ? tab : 'overview';
      navButtons.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === target));
      Object.entries(sections).forEach(([name, el]) => {{
        if (!el) return;
        el.classList.toggle('active', name === target);
      }});
      history.replaceState(null, '', '#'+target);
      document.title = target === 'general' ? 'ตั้งค่าทั่วไป • RedHub-XBot' : 'ภาพรวม • RedHub-XBot';
    }}

    function openSidebar() {{
      document.body.classList.add('sidebar-open');
    }}

    function closeSidebar() {{
      document.body.classList.remove('sidebar-open');
    }}

    navButtons.forEach(btn => {{
      btn.addEventListener('click', () => {{
        setTab(btn.dataset.tab);
        if (window.innerWidth <= 1100) closeSidebar();
      }});
    }});

    if (burger) burger.addEventListener('click', openSidebar);
    if (overlay) overlay.addEventListener('click', closeSidebar);

    window.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape') closeSidebar();
    }});

    const initial = (location.hash || '#overview').replace('#', '') || 'overview';
    setTab(initial);
  </script>
</body>
</html>
"""


def _parse_post(environ) -> Dict[str, str]:
    try:
        size = int(environ.get("CONTENT_LENGTH") or "0")
    except ValueError:
        size = 0
    body = environ["wsgi.input"].read(size).decode("utf-8", "replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _handle_save(form: Dict[str, str]) -> Tuple[str, str]:
    env = _env()
    updates: Dict[str, str] = {}

    for key, _label, _field_type, _help in KEY_FIELDS:
        if key in form:
            updates[key] = form[key].strip()

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
    path = environ.get("PATH_INFO", "/") or "/"
    method = environ.get("REQUEST_METHOD", "GET").upper()
    env = _env()

    panel_path = _panel_path(env)
    panel_path_alt = panel_path + "/" if panel_path != "/" else "/"

    if method == "GET" and path == "/":
        return _redirect_response(panel_path, start_response)

    if path in {panel_path, panel_path_alt} and method == "GET":
        body = _render_page(env)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]

    if path in {"/save", f"{panel_path}/save", f"{panel_path_alt}save"} and method == "POST":
        form = _parse_post(environ)
        action = form.get("action", "save")
        note, level = _handle_save(form)

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
