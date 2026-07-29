#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import os
import re
import shlex
import shutil
import subprocess
import time
import math
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import parse_qs

from wsgiref.simple_server import WSGIRequestHandler, make_server

import database as db

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = Path(os.getenv("ENV_FILE", str(APP_DIR / ".env")))
DEFAULT_PANEL_PATH = "/panel"

# เปลี่ยนรูปตรงนี้จุดเดียว หรือจะตั้งผ่าน WEB_PANEL_IMAGE_URL ใน .env ก็ได้
DEFAULT_PANEL_IMAGE_URL = os.getenv("WEB_PANEL_IMAGE_URL", "").strip()

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
    data.setdefault("WEB_PANEL_IMAGE_URL", DEFAULT_PANEL_IMAGE_URL)
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
            [
                "python3",
                "-c",
                "import urllib.request; print(urllib.request.urlopen('https://api.ipify.org', timeout=8).read().decode().strip())",
            ],
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
        if "System has not been booted" in status or "Failed to connect" in status:
            return "unknown"
        if status == "active":
            return "running"
        return status
    except Exception:
        return "unknown"


def _process_running(name: str) -> bool:
    try:
        proc = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True, timeout=3, check=False)
        return proc.returncode == 0
    except Exception:
        return False


def _service_status_any(names: List[str]) -> str:
    for name in names:
        if _process_running(name):
            return "running"
    for name in names:
        status = _service_status(name)
        if status == "running":
            return status
    for name in names:
        status = _service_status(name)
        if status not in {"inactive", "unknown"}:
            return status
    return _service_status(names[0]) if names else "unknown"


def _restart_service(name: str) -> None:
    subprocess.run(["systemctl", "restart", name], check=False, timeout=30)


def _restart_service_async(name: str, delay: float = 1.0) -> None:
    command = (
        "sh -lc '"
        f"sleep {max(0.0, float(delay)):.1f}; "
        f"systemctl restart {shlex.quote(name)} >/dev/null 2>&1'"
    )
    subprocess.Popen(
        command,
        shell=True,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _start_service(name: str) -> None:
    subprocess.run(["systemctl", "start", name], check=False, timeout=30)


def _stop_service(name: str) -> None:
    subprocess.run(["systemctl", "stop", name], check=False, timeout=30)


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


def _safe_run(cmd: List[str], timeout: int = 4) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        out = (proc.stdout or proc.stderr or "").strip()
        return out
    except Exception:
        return ""


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _cpu_cores() -> int:
    try:
        if hasattr(os, "sched_getaffinity"):
            return max(1, len(os.sched_getaffinity(0)))
    except Exception:
        pass
    return max(1, os.cpu_count() or 1)


def _cpu_usage_percent(sample_interval: float = 0.08) -> float:
    def read_proc_stat() -> tuple[int, int]:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            first = f.readline().split()
        if not first or first[0] != "cpu":
            return 0, 0
        values = [int(v) for v in first[1:]]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return total, idle

    try:
        t1, i1 = read_proc_stat()
        time.sleep(sample_interval)
        t2, i2 = read_proc_stat()
        total_delta = t2 - t1
        idle_delta = i2 - i1
        if total_delta <= 0:
            return 0.0
        used = (1.0 - (idle_delta / total_delta)) * 100.0
        return max(0.0, min(100.0, used))
    except Exception:
        return 0.0


def _read_meminfo() -> Dict[str, int]:
    data: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                key, rest = line.split(":", 1)
                parts = rest.strip().split()
                if not parts:
                    continue
                try:
                    value = int(parts[0])
                except ValueError:
                    continue
                unit = parts[1] if len(parts) > 1 else "kB"
                multiplier = 1024 if unit.lower() == "kb" else 1
                data[key] = value * multiplier
    except Exception:
        pass
    return data


def _format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(max(0.0, value))
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(round(value))} {units[unit_index]}"
    return f"{value:.2f} {units[unit_index]}"


def _format_short_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(max(0.0, value))
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(round(value))} {units[unit_index]}"
    return f"{value:.2f} {units[unit_index]}"


def _metric_pair(used: float, total: float) -> Tuple[str, str]:
    return _format_short_bytes(used), _format_short_bytes(total)


def _disk_usage() -> Tuple[float, float, float]:
    try:
        usage = shutil.disk_usage("/")
        return float(usage.used), float(usage.total), float(usage.free)
    except Exception:
        return 0.0, 0.0, 0.0


def _percent(used: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (used / total) * 100.0))


def _ring_style(percent: float) -> str:
    angle_deg = 220.0 + (percent * 3.6)
    angle = math.radians(angle_deg)
    radius = 52.0
    dot_x = math.cos(angle) * radius
    dot_y = math.sin(angle) * radius
    return f"--pct:{percent:.2f};--angle:{angle_deg:.2f}deg;--dot-x:{dot_x:.2f}px;--dot-y:{dot_y:.2f}px;"


def _stat_card(metric: str, title: str, percent: float, value_line: str, accent: str = "blue") -> str:
    percent_str = f"{percent:.2f}"
    return f"""
      <div class="stat-card" data-metric="{html.escape(metric)}">
        <div class="gauge {accent}" id="{html.escape(metric)}-gauge" style="{_ring_style(percent)}">
          <div class="gauge-core">
            <span id="{html.escape(metric)}-percent">{percent_str}%</span>
          </div>
          <i class="gauge-dot"></i>
        </div>
        <div class="stat-text">
          <div class="stat-title">{html.escape(title)}</div>
          <div class="stat-value" id="{html.escape(metric)}-value">{value_line}</div>
        </div>
      </div>
    """


def _service_action_icon(label: str) -> str:
    return f"""
      <button class="icon-btn" type="button" aria-label="{html.escape(label)}" title="{html.escape(label)}">
        <span>{html.escape(label)}</span>
      </button>
    """


def _collect_stats() -> Dict[str, str]:
    mem = _read_meminfo()
    mem_total = float(mem.get("MemTotal", 0))
    mem_available = float(mem.get("MemAvailable", mem.get("MemFree", 0)))
    mem_used = max(0.0, mem_total - mem_available)

    swap_total = float(mem.get("SwapTotal", 0))
    swap_free = float(mem.get("SwapFree", 0))
    swap_used = max(0.0, swap_total - swap_free)

    disk_used, disk_total, _disk_free = _disk_usage()

    cpu_percent = _cpu_usage_percent()
    cores = _cpu_cores()

    stats = {
        "cpu_cores": f"{cores} Core" if cores == 1 else f"{cores} Cores",
        "cpu_percent": f"{cpu_percent:.2f}",
        "cpu_ring": _ring_style(cpu_percent),
        "mem_used": _format_short_bytes(mem_used),
        "mem_total": _format_short_bytes(mem_total),
        "mem_percent": f"{_percent(mem_used, mem_total):.2f}",
        "mem_ring": _ring_style(_percent(mem_used, mem_total)),
        "swap_used": _format_short_bytes(swap_used),
        "swap_total": _format_short_bytes(swap_total),
        "swap_percent": f"{_percent(swap_used, swap_total):.2f}",
        "swap_ring": _ring_style(_percent(swap_used, swap_total)),
        "disk_used": _format_short_bytes(disk_used),
        "disk_total": _format_short_bytes(disk_total),
        "disk_percent": f"{_percent(disk_used, disk_total):.2f}",
        "disk_ring": _ring_style(_percent(disk_used, disk_total)),
    }
    return stats


def _stats_payload() -> Dict[str, object]:
    stats = _collect_stats()
    return {
        "cpu": {
            "cores": stats["cpu_cores"],
            "percent": float(stats["cpu_percent"]),
            "value": stats["cpu_cores"],
        },
        "ram": {
            "used": stats["mem_used"],
            "total": stats["mem_total"],
            "percent": float(stats["mem_percent"]),
            "value": f"{stats['mem_used']} / {stats['mem_total']}",
        },
        "storage": {
            "used": stats["disk_used"],
            "total": stats["disk_total"],
            "percent": float(stats["disk_percent"]),
            "value": f"{stats['disk_used']} / {stats['disk_total']}",
        },
        "swap": {
            "used": stats["swap_used"],
            "total": stats["swap_total"],
            "percent": float(stats["swap_percent"]),
            "value": f"{stats['swap_used']} / {stats['swap_total']}",
        },
    }


def _field_cards(env: Dict[str, str]) -> str:
    cards = []
    for key, label, field_type, help_text in KEY_FIELDS:
        value = html.escape(env.get(key, ""))
        cards.append(
            f"""
              <label class="field">
                <span>{html.escape(label)}</span>
                <input type="{field_type}" name="{key}" value="{value}" placeholder="{html.escape(help_text)}">
                <small>{html.escape(help_text)}</small>
              </label>
            """
        )
    return "".join(cards)




def _safe_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return default

def _safe_float(value: str, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except Exception:
        return default

def _shop_section() -> str:
    categories = db.list_sale_categories()
    products_all = db.list_sale_products(active_only=False)

    category_cards = []
    for cat in categories:
        cat_products = [p for p in products_all if int(p["category_id"]) == int(cat["id"])]
        prod_rows = []
        for prod in cat_products:
            prod_rows.append(f"""
              <div class="mini-row">
                <div>
                  <strong>{html.escape(prod.get('name',''))}</strong>
                  <div class="subtle">{html.escape(str(prod.get('detail',''))[:120])}</div>
                </div>
                <div class="row-actions">
                  <form method="post" action="/save">
                    <input type="hidden" name="action" value="shop_product_dispatch">
                    <input type="hidden" name="product_id" value="{html.escape(str(prod.get('id','')))}">
                    <input type="hidden" name="channel_id" value="{html.escape(str(prod.get('channel_id','')))}">
                    <button class="btn tiny" type="submit">เรียกใช้การ์ด</button>
                  </form>
                </div>
              </div>
            """)
        category_cards.append(f"""
          <section class="card">
            <div class="card-head">
              <div>
                <h3>{html.escape(cat.get('name',''))}</h3>
                <p class="subtle">{html.escape(cat.get('description',''))}</p>
              </div>
              <span class="pill">#{cat.get('id')}</span>
            </div>
            <div class="card-body">
              <div class="stack">{''.join(prod_rows) if prod_rows else '<div class="subtle">ยังไม่มีสินค้า</div>'}</div>
            </div>
          </section>
        """)

    category_options = "".join(
        f'<option value="{html.escape(str(cat.get("id")))}">{html.escape(cat.get("name",""))}</option>'
        for cat in categories
    )
    product_options = "".join(
        f'<option value="{html.escape(str(prod.get("id")))}">{html.escape(prod.get("name",""))}</option>'
        for prod in products_all
    )

    category_list = []
    for cat in categories:
        category_list.append(f"""
          <div class="mini-card">
            <div class="mini-row">
              <div>
                <strong>{html.escape(cat.get('name',''))}</strong>
                <div class="subtle">{html.escape(cat.get('description',''))}</div>
              </div>
              <div class="row-actions">
                <form method="post" action="/save">
                  <input type="hidden" name="action" value="shop_category_delete">
                  <input type="hidden" name="category_id" value="{html.escape(str(cat.get('id','')))}">
                  <button class="btn danger tiny" type="submit">ลบ</button>
                </form>
              </div>
            </div>
          </div>
        """)

    product_list = []
    for prod in products_all:
        product_list.append(f"""
          <div class="mini-card">
            <div class="mini-row">
              <div>
                <strong>{html.escape(prod.get('name',''))}</strong>
                <div class="subtle">หมวด: {html.escape(str(prod.get('category_id','')))} | ราคา {html.escape(str(prod.get('price','0')))} บาท | ช่อง {html.escape(str(prod.get('channel_id','')))}</div>
              </div>
              <div class="row-actions">
                <form method="post" action="/save">
                  <input type="hidden" name="action" value="shop_product_delete">
                  <input type="hidden" name="product_id" value="{html.escape(str(prod.get('id','')))}">
                  <button class="btn danger tiny" type="submit">ลบ</button>
                </form>
                <form method="post" action="/save">
                  <input type="hidden" name="action" value="shop_product_dispatch">
                  <input type="hidden" name="product_id" value="{html.escape(str(prod.get('id','')))}">
                  <input type="hidden" name="channel_id" value="{html.escape(str(prod.get('channel_id','')))}">
                  <button class="btn tiny" type="submit">เรียกใช้</button>
                </form>
              </div>
            </div>
          </div>
        """)

    return f"""
      <section class="card">
        <div class="card-head">
          <div>
            <h3>สร้างฟอร์มขายสินค้า</h3>
            <p class="subtle">เพิ่ม / แก้ไข / ลบ ได้ไม่จำกัด และส่งการ์ดไปยังช่อง Discord ได้ทันที</p>
          </div>
        </div>
        <div class="card-body">
          <form method="post" action="/save">
            <input type="hidden" name="action" value="shop_category_save">
            <div class="form-grid">
              <label class="field"><span>ID หมวดหมู่ (เว้นว่าง = สร้างใหม่)</span><input name="category_id" type="number" placeholder="เช่น 1"><small>ใส่เพื่อแก้ไข</small></label>
              <label class="field"><span>ค่าสร้างรายการหมวดหมู่</span><input name="category_name" type="text" placeholder="เช่น Netflix"><small>ชื่อหมวดหมู่</small></label>
              <label class="field"><span>คำอธิบายหมวดหมู่</span><input name="category_description" type="text" placeholder="รายละเอียดหมวดหมู่"><small>แสดงในหน้าเว็บ</small></label>
              <label class="field"><span>ลำดับการแสดง</span><input name="category_sort_order" type="number" value="0"><small>น้อยขึ้นก่อน</small></label>
            </div>
            <div class="form-actions">
              <button class="btn" type="submit">บันทึกหมวดหมู่</button>
            </div>
          </form>

          <div class="split-grid">
            <div>
              <h4>หมวดหมู่ทั้งหมด</h4>
              <div class="stack">{''.join(category_list) if category_list else '<div class="subtle">ยังไม่มีหมวดหมู่</div>'}</div>
            </div>
            <div>
              <h4>สร้างรายการสินค้า</h4>
              <form method="post" action="/save">
                <input type="hidden" name="action" value="shop_product_save">
                <div class="form-grid">
                  <label class="field"><span>ID สินค้า (เว้นว่าง = สร้างใหม่)</span><input name="product_id" type="number" placeholder="เช่น 1"><small>ใส่เพื่อแก้ไข</small></label>
                  <label class="field"><span>หมวดหมู่</span><select name="product_category_id">{category_options}</select><small>เลือกหมวดหมู่</small></label>
                  <label class="field"><span>ชื่อสินค้าที่จะขาย</span><input name="product_name" type="text" placeholder="ชื่อสินค้า"><small>ตั้งชื่อสินค้า</small></label>
                  <label class="field"><span>ราคา</span><input name="product_price" type="number" step="0.01" value="0"><small>ราคาเป็นบาท</small></label>
                  <label class="field"><span>id Inbound</span><input name="product_inbound_id" type="number" value="0"><small>หมายเลข inbound</small></label>
                  <label class="field"><span>url 3x-ui</span><input name="product_xui_url" type="text" placeholder="http://..."><small>URL ของ 3x-ui</small></label>
                  <label class="field"><span>url ตั้งรูปฟอร์ม</span><input name="product_form_url" type="text" placeholder="https://..."><small>URL ฟอร์มหรือรูปฟอร์ม</small></label>
                  <label class="field"><span>รายละเอียดฟอร์ม</span><input name="product_form_detail" type="text" placeholder="รายละเอียดฟอร์ม"><small>รายละเอียดฟอร์มเพิ่มเติม</small></label>
                  <label class="field"><span>รายละเอียดสินค้า</span><input name="product_detail" type="text" placeholder="รายละเอียดสินค้า"><small>อธิบายสินค้า</small></label>
                  <label class="field"><span>url รูปปกสินค้า</span><input name="product_image_url" type="text" placeholder="https://..."><small>รูปภาพสินค้า</small></label>
                  <label class="field"><span>ID ช่องเช่น 1241740147842748556</span><input name="product_channel_id" type="text" placeholder="1241740147842748556"><small>ช่องที่จะส่งการ์ด</small></label>
                  <label class="field"><span>ตัวเลือกเพิ่ม</span><textarea name="product_options_text" rows="4" placeholder="เพิ่มบรรทัดละ 1 ข้อ"></textarea><small>ใส่ได้หลายบรรทัด</small></label>
                </div>
                <div class="form-actions">
                  <button class="btn" type="submit">บันทึกรายการสินค้า</button>
                </div>
              </form>
            </div>
          </div>

          <div class="split-grid">
            <div>
              <h4>รายการสินค้า</h4>
              <div class="stack">{''.join(product_list) if product_list else '<div class="subtle">ยังไม่มีสินค้า</div>'}</div>
            </div>
            <div>
              <h4>ตัวอย่างหมวดหมู่ / สินค้าที่พร้อมใช้</h4>
              <div class="stack">{''.join(category_cards) if category_cards else '<div class="subtle">ยังไม่มีข้อมูลสำหรับส่งการ์ด</div>'}</div>
            </div>
          </div>
        </div>
      </section>
    """
def _render_page(env: Dict[str, str], note: str = "", level: str = "ok") -> str:
    host = _public_ip()
    panel_url = _panel_url(env, host)
    https_url = _https_url(env)
    bot_service = env.get("SERVICE_NAME", "xbot")
    web_service = env.get("WEB_SERVICE_NAME", f"{bot_service}-web")
    xray_service = env.get("XRAY_SERVICE_NAME", "xray")
    port = env.get("WEB_PORT", "2026")
    domain = env.get("WEB_DOMAIN", "").strip() or "ยังไม่ได้ผูกโดเมน"
    panel_path = _panel_path(env)
    current_url = (
        f"http://{host}:{port}{panel_path}" if host and host != "0.0.0.0" else f"http://127.0.0.1:{port}{panel_path}"
    )

    bot_status = _service_status(bot_service)
    web_status = _service_status(web_service)
    xray_status = _service_status_any([xray_service, "x-ui", "xui"])
    stats = _collect_stats()

    xray_version = _first_line(_safe_run(["xray", "version"], timeout=3)) or "v26.7.11"
    xui_version = (
        _first_line(_safe_run(["3x-ui", "version"], timeout=3))
        or _first_line(_safe_run(["x-ui", "version"], timeout=3))
        or "v3.5.0"
    )

    notice_html = _render_notice("บันทึกการตั้งค่า", note, level) if note else ""

    overview_cards = f"""
      <section class="card">
        <div class="card-head">
          <div>
            <h3>ภาพรวมระบบ</h3>
            <p class="subtle">สถิติคำนวณแบบเรียลไทม์</p>
          </div>
          <span class="pill">{html.escape(domain)}</span>
        </div>
        <div class="card-body">
          <div class="stats-grid compact">
            {_stat_card("cpu", "CPU", float(stats["cpu_percent"]), html.escape(stats['cpu_cores']), "blue")}
            {_stat_card("ram", "RAM", float(stats["mem_percent"]), f"{html.escape(stats['mem_used'])} / {html.escape(stats['mem_total'])}", "violet")}
            {_stat_card("storage", "Storage", float(stats["disk_percent"]), f"{html.escape(stats['disk_used'])} / {html.escape(stats['disk_total'])}", "blue")}
            {_stat_card("swap", "Swap", float(stats["swap_percent"]), f"{html.escape(stats['swap_used'])} / {html.escape(stats['swap_total'])}", "gray")}
          </div>
        </div>
      </section>
    """
    general_fields = _field_cards(env)
    shop_section = _shop_section()
    brand_image_url = (env.get("WEB_PANEL_IMAGE_URL", "").strip() or DEFAULT_PANEL_IMAGE_URL)
    brand_image_url_html = html.escape(brand_image_url)
    if brand_image_url:
        brand_media_html = (
            f'<div class="brand-media"><img src="{brand_image_url_html}" alt="RedHub-xbot" '
            f'referrerpolicy="no-referrer" loading="eager" decoding="async"></div>'
        )
    else:
        brand_media_html = '<div class="brand-media brand-fallback">R</div>'

    service_actions = f"""
      <section class="card service-card">
        <div class="card-head">
          <div>
            <h3>การควบคุมบริการ</h3>
            <p class="subtle">ปุ่มขนาดพอดีสำหรับรีสตาร์ทและสลับสถานะบอท</p>
          </div>
        </div>
        <div class="card-body">
          <div class="service-control-grid">
            <form method="post" action="{html.escape(panel_path)}/service-action" class="service-control-form">
              <input type="hidden" name="action" value="restart_web">
              <button class="service-control-btn" type="submit">รีสตาร์ทเว็บ</button>
            </form>
            <form method="post" action="{html.escape(panel_path)}/service-action" class="service-control-form">
              <input type="hidden" name="action" value="restart_bot">
              <button class="service-control-btn" type="submit">รีสตาร์ทบอท</button>
            </form>
            <form method="post" action="{html.escape(panel_path)}/service-action" class="service-control-form">
              <input type="hidden" name="action" value="stop_bot">
              <button class="service-control-btn danger" type="submit">หยุดบอท</button>
            </form>
            <form method="post" action="{html.escape(panel_path)}/service-action" class="service-control-form">
              <input type="hidden" name="action" value="start_bot">
              <button class="service-control-btn success" type="submit">เริ่มบอท</button>
            </form>
          </div>
        </div>
      </section>
    """

    return f"""<!doctype html>
<html lang="th">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RedHub-xbot • Web Panel</title>
  <style>
    :root {{
      --bg: #070b14;
      --bg2: #0b1220;
      --card: rgba(16, 22, 39, .92);
      --card-2: rgba(13, 19, 35, .96);
      --line: rgba(148, 163, 184, .12);
      --line-2: rgba(148, 163, 184, .18);
      --text: #e5e7eb;
      --muted: #94a3b8;
      --accent: #39a9ff;
      --accent-2: #7c3aed;
      --accent-3: #19c37d;
      --warn: #f59e0b;
      --err: #ef4444;
      --shadow: 0 18px 60px rgba(2, 6, 23, .55);
      --sidebar-w: 278px;
      font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{
      margin: 0;
      min-height: 100%;
      background: var(--bg);
      color: var(--text);
      overflow-x: hidden;
    }}
    body {{
      min-height: 100vh;
      background:
        radial-gradient(circle at 15% 8%, rgba(57, 169, 255, .18), transparent 24%),
        radial-gradient(circle at 92% 10%, rgba(124, 58, 237, .16), transparent 24%),
        linear-gradient(180deg, #090d16 0%, #0b1020 100%);
    }}
    .layout {{
      display: grid;
      grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
      min-height: 100vh;
    }}
    .sidebar {{
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 18px 14px;
      background: linear-gradient(180deg, rgba(11, 16, 32, .98), rgba(4, 8, 19, .98));
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
      padding: 14px;
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
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 10px 28px rgba(57, 169, 255, .16);
      font-weight: 900;
      color: white;
      flex: 0 0 auto;
      overflow: hidden;
    }}
    .brand-media {{
      width: 42px;
      height: 42px;
      border-radius: 14px;
      flex: 0 0 auto;
      overflow: hidden;
      border: 1px solid rgba(148,163,184,.14);
      background: rgba(255,255,255,.04);
      display: grid;
      place-items: center;
      box-shadow: 0 10px 28px rgba(57, 169, 255, .16);
    }}
    .brand-media img {{
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }}
    .brand-fallback {{
      font-size: 18px;
      font-weight: 900;
      color: white;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
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
      background: linear-gradient(135deg, rgba(57,169,255,.18), rgba(124,58,237,.12));
      border-color: rgba(57,169,255,.32);
      box-shadow: 0 12px 28px rgba(57, 169, 255, .12);
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
      background: rgba(12, 18, 33, .78);
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
    .notice {{
      border-radius: 18px;
      padding: 14px 16px;
      margin-bottom: 14px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
    }}
    .notice h3 {{
      margin: 0 0 4px;
      font-size: 15px;
    }}
    .notice p {{
      margin: 0;
      color: var(--muted);
    }}
    .notice.ok {{ border-color: rgba(34,197,94,.28); }}
    .notice.warn {{ border-color: rgba(245,158,11,.28); }}
    .notice.error {{ border-color: rgba(239,68,68,.28); }}
    .section {{
      display: none;
    }}
    .section.active {{
      display: block;
    }}
    .hero-card {{
      min-height: 250px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      padding: 20px 20px 18px;
      border-radius: 28px;
      border: 1px solid var(--line);
      background:
        linear-gradient(180deg, rgba(15, 22, 40, .88), rgba(8, 13, 24, .92)),
        radial-gradient(circle at 50% 20%, rgba(57,169,255,.07), transparent 36%);
      box-shadow: var(--shadow);
      margin-bottom: 16px;
      overflow: hidden;
    }}
    .hero-top {{
      display: flex;
      align-items: flex-start;
      gap: 16px;
    }}
    .burger-mini {{
      width: 52px;
      height: 52px;
      border-radius: 18px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.04);
      display: grid;
      place-items: center;
      gap: 4px;
      cursor: pointer;
      flex: 0 0 auto;
      padding: 0;
      box-shadow: inset 0 1px 0 rgba(255,255,255,.03);
    }}
    .burger-mini span {{
      width: 18px;
      height: 2px;
      border-radius: 999px;
      background: #e2e8f0;
      display: block;
    }}
    .hero-copy h1 {{
      margin: 10px 0 8px;
      font-size: 24px;
      line-height: 1.2;
      letter-spacing: -.02em;
    }}
    .hero-copy p {{
      margin: 0;
      color: var(--muted);
      line-height: 1.6;
      font-size: 14px;
    }}
    .hero-spacer {{
      flex: 1 1 auto;
      min-height: 40px;
    }}
    .hero-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: fit-content;
      padding: 9px 14px;
      border-radius: 999px;
      border: 1px solid rgba(57,169,255,.24);
      background:
        linear-gradient(135deg, rgba(57,169,255,.18), rgba(124,58,237,.16));
      color: #d8efff;
      font-size: 13px;
      font-weight: 800;
      letter-spacing: .02em;
      box-shadow:
        0 0 0 1px rgba(255,255,255,.02),
        0 12px 30px rgba(57,169,255,.10),
        inset 0 1px 0 rgba(255,255,255,.08);
    }}
    .topbar-badge {{
      margin-bottom: 6px;
    }}

    .card {{
      grid-column: span 12;
      border-radius: 24px;
      border: 1px solid var(--line);
      background: var(--card);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      overflow: hidden;
      margin-bottom: 16px;
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
    .subtle {{
      margin: 4px 0 0;
      color: var(--muted);
      font-size: 13px;
    }}
    .eyebrow {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: rgba(57,169,255,.12);
      border: 1px solid rgba(57,169,255,.24);
      color: #b9e4ff;
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 10px;
    }}
    .promo-card {{
      background:
        linear-gradient(180deg, rgba(15,22,40,.84), rgba(8,13,24,.9));
    }}
    .promo-body {{
      padding-top: 8px;
    }}
    .promo-visual {{
      height: 210px;
      border-radius: 22px;
      border: 1px solid rgba(57,169,255,.14);
      background:
        linear-gradient(180deg, rgba(63,191,255,.12), rgba(11,18,32,.05)),
        linear-gradient(135deg, rgba(57,169,255,.42), rgba(89, 194, 255, .98));
      position: relative;
      overflow: hidden;
    }}
    .promo-glow {{
      position: absolute;
      inset: auto -30px -40px auto;
      width: 180px;
      height: 180px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(255,255,255,.38), rgba(255,255,255,0) 66%);
      filter: blur(3px);
      opacity: .55;
    }}
    .stats-grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .stats-grid.compact {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .stat-card {{
      min-height: 150px;
      display: grid;
      align-content: center;
      justify-items: center;
      gap: 12px;
      padding: 14px 12px 16px;
      border-radius: 22px;
      background: rgba(255,255,255,.02);
      border: 1px solid rgba(148,163,184,.10);
    }}
    .gauge {{
      --pct: 0;
      --angle: 220deg;
      width: 108px;
      height: 108px;
      border-radius: 50%;
      background: conic-gradient(from 220deg, var(--accent) calc(var(--pct) * 1%), rgba(255,255,255,.08) 0);
      position: relative;
      display: grid;
      place-items: center;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.02);
    }}
    .gauge.violet {{
      background: conic-gradient(from 220deg, var(--accent-2) calc(var(--pct) * 1%), rgba(255,255,255,.08) 0);
    }}
    .gauge.gray {{
      background: conic-gradient(from 220deg, rgba(148,163,184,.76) calc(var(--pct) * 1%), rgba(255,255,255,.08) 0);
    }}
    .gauge.blue {{
      background: conic-gradient(from 220deg, var(--accent) calc(var(--pct) * 1%), rgba(255,255,255,.08) 0);
    }}
    .gauge::before {{
      content: "";
      position: absolute;
      inset: 7px;
      border-radius: 50%;
      background: linear-gradient(180deg, rgba(13, 19, 35, .96), rgba(16, 22, 39, .96));
      box-shadow: inset 0 0 0 1px rgba(148,163,184,.10);
    }}
    .gauge-core {{
      position: relative;
      z-index: 2;
      display: grid;
      place-items: center;
      text-align: center;
      font-size: 18px;
      font-weight: 700;
      letter-spacing: -.02em;
      color: #dbeafe;
    }}
    .gauge-core span {{
      display: block;
    }}
    .gauge-dot {{
      position: absolute;
      z-index: 3;
      left: 50%;
      top: 50%;
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--accent);
      box-shadow: 0 0 0 4px rgba(57,169,255,.12);
      transform: translate(-50%, -50%) rotate(var(--angle)) translateY(-58px);
      transform-origin: center;
    }}
    .gauge.violet .gauge-dot {{ background: var(--accent-2); box-shadow: 0 0 0 4px rgba(124,58,237,.12); }}
    .gauge.gray .gauge-dot {{ background: rgba(148,163,184,.92); box-shadow: 0 0 0 4px rgba(148,163,184,.12); }}
    .stat-text {{
      text-align: center;
      line-height: 1.45;
      font-size: 15px;
    }}
    .stat-text strong {{
      font-size: 18px;
      letter-spacing: -.02em;
    }}
    .service-card .card-head {{
      padding-bottom: 18px;
    }}
    .service-head {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      width: 100%;
      justify-content: space-between;
    }}
    .service-title {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: -.01em;
    }}
    .service-name {{
      font-size: 21px;
      font-weight: 500;
    }}
    .version-tag {{
      padding: 5px 10px;
      border-radius: 8px;
      background: rgba(34,197,94,.12);
      color: #b8f6c3;
      font-size: 12px;
      font-weight: 700;
    }}
    .status-dot {{
      width: 9px;
      height: 9px;
      border-radius: 50%;
      margin-left: auto;
      background: var(--accent-3);
      box-shadow: 0 0 0 4px rgba(25,195,125,.12);
    }}
    .status-dot.offline {{
      background: var(--err);
      box-shadow: 0 0 0 4px rgba(239,68,68,.12);
    }}
    .service-status {{
      font-size: 15px;
      color: #d1d5db;
    }}
    .service-actions {{
      border-top: 1px solid rgba(148,163,184,.12);
      padding: 14px 0 0;
      margin: 0 20px 18px;
    }}
    .service-icon-row {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      align-items: center;
    }}
    .service-card:nth-of-type(3) .service-icon-row {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .service-card:nth-of-type(4) .service-icon-row {{
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }}
    .service-card:nth-of-type(5) .service-icon-row {{
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .icon-btn {{
      appearance: none;
      border: 0;
      background: transparent;
      color: #cbd5e1;
      font-size: 22px;
      padding: 10px 0;
      border-radius: 14px;
      cursor: pointer;
      opacity: .9;
    }}
    .icon-btn:hover {{
      background: rgba(255,255,255,.04);
      opacity: 1;
    }}
    .chart-placeholder {{
      border-radius: 22px;
      border: 1px solid rgba(148,163,184,.12);
      background: linear-gradient(180deg, rgba(57,169,255,.10), rgba(16,22,39,.02));
      min-height: 220px;
      display: grid;
      place-items: center;
    }}
    .chart-bars {{
      display: flex;
      align-items: flex-end;
      gap: 12px;
      width: min(100%, 420px);
      height: 120px;
    }}
    .chart-bars span {{
      flex: 1 1 0;
      border-radius: 14px 14px 0 0;
      background: linear-gradient(180deg, rgba(57,169,255,.85), rgba(124,58,237,.62));
      box-shadow: 0 10px 24px rgba(57,169,255,.16);
    }}
    .chart-bars span:nth-child(1) {{ height: 38%; }}
    .chart-bars span:nth-child(2) {{ height: 58%; }}
    .chart-bars span:nth-child(3) {{ height: 28%; }}
    .chart-bars span:nth-child(4) {{ height: 72%; }}
    .chart-bars span:nth-child(5) {{ height: 46%; }}
    .chart-bars span:nth-child(6) {{ height: 82%; }}
    .grid {{
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(12, minmax(0, 1fr));
    }}
    .general-wrap {{
      display: grid;
      gap: 16px;
    }}
    .general-card {{
      padding-bottom: 8px;
    }}
    .form-grid {{
      display: grid;
      gap: 14px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }}
    .field {{
      display: grid;
      gap: 7px;
      align-content: start;
    }}
    .field span {{
      font-size: 13px;
      color: #dbe3ef;
      font-weight: 700;
    }}
    .field input {{
      width: 100%;
      border: 1px solid rgba(148,163,184,.14);
      background: rgba(255,255,255,.03);
      color: var(--text);
      border-radius: 14px;
      padding: 13px 14px;
      font-size: 14px;
      outline: none;
    }}
    .field input:focus {{
      border-color: rgba(57,169,255,.46);
      box-shadow: 0 0 0 4px rgba(57,169,255,.10);
    }}
    .field small {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.4;
    }}
    .form-actions {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }}
    .service-control-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .service-control-form {{
      margin: 0;
    }}
    .service-control-btn {{
      width: 100%;
      appearance: none;
      border: 1px solid rgba(57,169,255,.32);
      background: linear-gradient(135deg, rgba(57,169,255,.18), rgba(124,58,237,.12));
      color: var(--text);
      border-radius: 16px;
      padding: 14px 14px;
      font-weight: 800;
      font-size: 15px;
      cursor: pointer;
      box-shadow: 0 12px 26px rgba(2, 6, 23, .22);
    }}
    .service-control-btn:hover {{
      border-color: rgba(57,169,255,.56);
      transform: translateY(-1px);
    }}
    .service-control-btn.danger {{
      border-color: rgba(239,68,68,.30);
      background: linear-gradient(135deg, rgba(239,68,68,.16), rgba(249,115,22,.10));
    }}
    .service-control-btn.success {{
      border-color: rgba(25,195,125,.30);
      background: linear-gradient(135deg, rgba(25,195,125,.18), rgba(14,165,233,.10));
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
    .overlay {{
      display: none;
    }}
    @media (max-width: 1100px) {{
      .layout {{ grid-template-columns: 1fr; }}
      .sidebar {{
        position: fixed;
        inset: 0 auto 0 0;
        width: min(var(--sidebar-w), 86vw);
        transform: translateX(-102%);
        transition: transform .2s ease;
      }}
      body.sidebar-open .sidebar {{ transform: translateX(0); }}
      body.sidebar-open .overlay {{
        display: block;
        position: fixed;
        inset: 0;
        background: rgba(2, 6, 23, .72);
        z-index: 35;
      }}
      .main {{ padding: 12px; }}
      .topbar {{ margin-bottom: 12px; }}
      .hero-card {{ min-height: 220px; }}
      .stats-grid, .form-grid {{ grid-template-columns: 1fr; }}
      .service-icon-row {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }}
    }}
    @media (max-width: 640px) {{
      .topbar {{
        padding: 12px;
        gap: 10px;
      }}
      .topbar-copy h2 {{ font-size: 17px; }}
      .topbar-copy p {{ font-size: 12px; }}
      .hero-card {{ min-height: 220px; padding: 16px; }}
      .hero-copy h1 {{ font-size: 22px; }}
      .card-head, .card-body {{ padding-left: 16px; padding-right: 16px; }}
      .service-actions {{ margin-left: 16px; margin-right: 16px; }}
      .gauge {{ width: 96px; height: 96px; }}
      .stat-card {{ min-height: 136px; }}
      .service-name {{ font-size: 19px; }}
    }}
  </style>
</head>
<body>
  <div class="overlay" id="overlay"></div>
  <div class="layout">
    <aside class="sidebar" id="sidebar">
      <div class="brand">
        {brand_media_html}
        <div>
          <h1>RedHub-xbot</h1>
          <p>Web Panel Control</p>
        </div>
      </div>

      <div class="menu-title">เมนู</div>
      <nav class="nav">
        <button class="active" data-tab="overview" type="button">
          <span class="nav-ico">⌂</span>
          <span>ภาพรวม</span>
        </button>
        <button data-tab="general" type="button">
          <span class="nav-ico">⚙</span>
          <span>ตั้งค่าทั่วไป</span>
        </button>
        <button data-tab="shop" type="button">
          <span class="nav-ico">🛒</span>
          <span>ฟอร์มขายสินค้า</span>
        </button>
      </nav>

      <div class="sidebar-footer">
        <div class="muted">
          แก้ค่า .env ได้จากหน้าเว็บนี้โดยตรง<br>
          path ปัจจุบัน: {html.escape(panel_path)}
        </div>
      </div>
    </aside>

    <main class="main">
      <div class="topbar">
        <button class="burger" id="burger" type="button" aria-label="เปิดเมนู">
          <span></span><span></span><span></span>
        </button>
        <div class="topbar-copy">
          <div class="hero-badge topbar-badge">RedHub-xbot</div>
          <h2 id="topbar-title">แดชบอร์ดสถานะระบบ</h2>
          <p id="topbar-subtitle">ภาพรวมแบบสดของ CPU, RAM, Storage และ Swap</p>
        </div>
      </div>

      <div class="wrap">
        {notice_html}

        <section class="section active" id="section-overview">
          {overview_cards}
          {service_actions}
        </section>

        <section class="section" id="section-general">
          <div class="general-wrap">
            <section class="card general-card">
              <div class="card-head">
                <div>
                  <h3>ตั้งค่าทั่วไป</h3>
                  <p class="subtle">ปรับค่าพื้นฐานของบอทและเว็บพาเนล</p>
                </div>
              </div>
              <div class="card-body">
                <form method="post" action="{html.escape(panel_path)}/save">
                  <div class="form-grid">
                    {general_fields}
                  </div>
                  <div class="form-actions">
                    <button class="btn" type="submit" name="action" value="save">บันทึก</button>
                    <button class="btn secondary" type="submit" name="action" value="save_restart_bot">บันทึกและรีสตาร์ท Bot</button>
                    <button class="btn secondary" type="submit" name="action" value="save_restart_web">บันทึกและรีสตาร์ท Web</button>
                    <button class="btn secondary" type="submit" name="action" value="save_restart_all">บันทึกและรีสตาร์ททั้งหมด</button>
                  </div>
                </form>
              </div>
            </section>
          </div>
        </section>

        <section class="section" id="section-shop">
          {shop_section}
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
      shop: document.getElementById('section-shop'),
    }};
    const topbarTitle = document.getElementById('topbar-title');
    const topbarSubtitle = document.getElementById('topbar-subtitle');
    const tabMeta = {{
      overview: {{
        title: 'แดชบอร์ดสถานะระบบ',
        subtitle: 'ภาพรวมแบบสดของ CPU, RAM, Storage และ Swap',
      }},
      general: {{
        title: 'ตั้งค่าทั่วไป',
        subtitle: 'ปรับค่า .env และสั่งรีสตาร์ทได้จากหน้าเดียว',
      }},
      shop: {{
        title: 'ฟอร์มขายสินค้า',
        subtitle: 'สร้างหมวดหมู่ สินค้า และสั่งส่งการ์ดไปยังช่อง Discord',
      }},
    }};

    const statNodes = {{
      cpu: {{
        percent: document.getElementById('cpu-percent'),
        value: document.getElementById('cpu-value'),
        gauge: document.getElementById('cpu-gauge'),
      }},
      ram: {{
        percent: document.getElementById('ram-percent'),
        value: document.getElementById('ram-value'),
        gauge: document.getElementById('ram-gauge'),
      }},
      storage: {{
        percent: document.getElementById('storage-percent'),
        value: document.getElementById('storage-value'),
        gauge: document.getElementById('storage-gauge'),
      }},
      swap: {{
        percent: document.getElementById('swap-percent'),
        value: document.getElementById('swap-value'),
        gauge: document.getElementById('swap-gauge'),
      }},
    }};

    function setMetric(metric, payload) {{
      const node = statNodes[metric];
      if (!node) return;
      const percent = Math.max(0, Math.min(100, Number(payload && payload.percent) || 0));
      if (node.percent) node.percent.textContent = percent.toFixed(2) + '%';
      if (node.gauge) {{
        node.gauge.style.setProperty('--pct', percent.toFixed(2));
        node.gauge.style.setProperty('--angle', (220 + percent * 3.6).toFixed(2) + 'deg');
      }}
      if (node.value) {{
        node.value.textContent = (payload && payload.value) || '';
      }}
    }}

    async function refreshStats() {{
      try {{
        const res = await fetch('./stats', {{
          cache: 'no-store',
          headers: {{ 'Accept': 'application/json' }},
        }});
        if (!res.ok) return;
        const data = await res.json();
        setMetric('cpu', data.cpu || {{}});
        setMetric('ram', data.ram || {{}});
        setMetric('storage', data.storage || {{}});
        setMetric('swap', data.swap || {{}});
      }} catch (err) {{
        // ignore
      }}
    }}

    function setTab(tab) {{
      const target = sections[tab] ? tab : 'overview';
      navButtons.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === target));
      Object.entries(sections).forEach(([name, el]) => {{
        if (!el) return;
        el.classList.toggle('active', name === target);
      }});
      const meta = tabMeta[target] || tabMeta.overview;
      if (topbarTitle) topbarTitle.textContent = meta.title;
      if (topbarSubtitle) topbarSubtitle.textContent = meta.subtitle;
      history.replaceState(null, '', '#'+target);
      document.title = meta.title + ' • RedHub-xbot';
      window.scrollTo({{ top: 0, behavior: 'smooth' }});
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
    refreshStats();
    setInterval(refreshStats, 3000);
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




def _handle_shop_action(form: Dict[str, str]) -> Tuple[str, str]:
    action = (form.get("action") or "").strip()

    if action == "shop_category_save":
        name = (form.get("category_name") or "").strip()
        if not name:
            return ("กรุณาระบุชื่อหมวดหมู่", "warn")
        description = (form.get("category_description") or "").strip()
        sort_order = _safe_int(form.get("category_sort_order", "0"))
        cat_id = form.get("category_id")
        if cat_id and str(cat_id).strip().isdigit():
            db.update_sale_category(int(cat_id), name, description, sort_order, 1)
            return ("อัปเดตหมวดหมู่เรียบร้อยแล้ว", "ok")
        db.create_sale_category(name, description, sort_order)
        return ("สร้างหมวดหมู่เรียบร้อยแล้ว", "ok")

    if action == "shop_category_delete":
        cat_id = form.get("category_id", "").strip()
        if not cat_id.isdigit():
            return ("ไม่พบ category_id", "warn")
        db.delete_sale_category(int(cat_id))
        return ("ลบหมวดหมู่เรียบร้อยแล้ว", "ok")

    if action == "shop_product_save":
        category_id = _safe_int(form.get("product_category_id", "0"))
        name = (form.get("product_name") or "").strip()
        if not category_id or not name:
            return ("กรุณาเลือกหมวดหมู่และระบุชื่อสินค้า", "warn")
        product_id = form.get("product_id", "").strip()
        payload = dict(
            category_id=category_id,
            name=name,
            price=_safe_float(form.get("product_price", "0")),
            form_url=(form.get("product_form_url") or "").strip(),
            xui_url=(form.get("product_xui_url") or "").strip(),
            form_detail=(form.get("product_form_detail") or "").strip(),
            inbound_id=_safe_int(form.get("product_inbound_id", "0")),
            detail=(form.get("product_detail") or "").strip(),
            image_url=(form.get("product_image_url") or "").strip(),
            channel_id=(form.get("product_channel_id") or "").strip(),
            options_text=(form.get("product_options_text") or "").strip(),
            sort_order=_safe_int(form.get("product_sort_order", "0")),
            active=1,
        )
        if product_id and product_id.isdigit():
            db.update_sale_product(int(product_id), **payload)
            return ("อัปเดตรายการสินค้าเรียบร้อยแล้ว", "ok")
        db.create_sale_product(**payload)
        return ("สร้างรายการสินค้าเรียบร้อยแล้ว", "ok")

    if action == "shop_product_delete":
        product_id = form.get("product_id", "").strip()
        if not product_id.isdigit():
            return ("ไม่พบ product_id", "warn")
        db.delete_sale_product(int(product_id))
        return ("ลบรายการสินค้าเรียบร้อยแล้ว", "ok")

    if action == "shop_product_dispatch":
        product_id = form.get("product_id", "").strip()
        channel_id = (form.get("channel_id") or "").strip()
        if not product_id.isdigit():
            return ("ไม่พบ product_id", "warn")
        prod = db.get_sale_product(int(product_id))
        if not prod:
            return ("ไม่พบรายการสินค้า", "warn")
        channel_id = channel_id or str(prod.get("channel_id", "")).strip()
        if not channel_id:
            return ("ไม่มี channel_id สำหรับส่งการ์ด", "warn")
        db.enqueue_sale_card(int(product_id), channel_id)
        return ("เพิ่มคิวส่งการ์ดเรียบร้อยแล้ว", "ok")

    return ("ไม่พบ action ที่ต้องการ", "warn")
def _restart_after(action: str) -> None:
    env = _env()
    bot_service = env.get("SERVICE_NAME", "xbot")
    web_service = env.get("WEB_SERVICE_NAME", f"{bot_service}-web")

    if action == "bot":
        _restart_service(bot_service)
    elif action == "web":
        _restart_service_async(web_service, delay=1.2)
    elif action == "all":
        _restart_service(bot_service)
        _restart_service_async(web_service, delay=1.8)


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
        if action.startswith("shop_"):
            note, level = _handle_shop_action(form)
            body = _render_page(_env(), note=note, level=level)
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [body.encode("utf-8")]

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

    if path in {"/service-action", f"{panel_path}/service-action", f"{panel_path_alt}service-action"} and method == "POST":
        form = _parse_post(environ)
        action = (form.get("action") or "").strip()
        note = "ดำเนินการเรียบร้อย"
        level = "ok"
        if action == "restart_web":
            _restart_after("web")
            note = "สั่งรีสตาร์ท Web service แล้ว"
        elif action == "restart_bot":
            _restart_after("bot")
            note = "สั่งรีสตาร์ท Bot service แล้ว"
        elif action == "stop_bot":
            _stop_service(_env().get("SERVICE_NAME", "xbot"))
            note = "สั่งหยุด Bot service แล้ว"
        elif action == "start_bot":
            _start_service(_env().get("SERVICE_NAME", "xbot"))
            note = "สั่งเริ่ม Bot service แล้ว"
        else:
            level = "warn"
            note = "ไม่พบคำสั่งที่ต้องการ"
        body = _render_page(_env(), note=note, level=level)
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [body.encode("utf-8")]

    if path in {"/stats", f"{panel_path}/stats", f"{panel_path_alt}stats"} and method == "GET":
        payload = json.dumps(_stats_payload(), ensure_ascii=False).encode("utf-8")
        start_response(
            "200 OK",
            [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
            ],
        )
        return [payload]

    if method == "GET":
        return _redirect_response(panel_path, start_response)

    start_response("404 Not Found", [("Content-Type", "text/plain; charset=utf-8")])
    return [b"Not found"]


def main() -> None:
    db.init_db()
    env = _env()
    port = int(env.get("WEB_PORT", "2026") or 2026)
    host = "0.0.0.0"
    print(f"Web panel listening on {host}:{port}")
    with make_server(
        host,
        port,
        application,
        handler_class=type("QuietHandler", (WSGIRequestHandler,), {"log_message": lambda *args, **kwargs: None}),
    ) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
