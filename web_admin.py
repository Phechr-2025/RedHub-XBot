"""Secure web control panel for Bio-shop Telegram Bot.

The web panel and Telegram bot deliberately share database.py/SQLite so every
change is visible immediately from both surfaces without a cache or restart.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import threading
import time
import tempfile
import shutil
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

import database as db

SITE_CONFIG_DEFAULTS = {
    "website_name": "Bio_shop",
    "login_description": "ศูนย์ควบคุมบอท VPN",
    "login_title": "Bio_shop Control",
}
SITE_CONFIG_PATH = Path(__file__).resolve().with_name("site_config.json")


def _load_site_config() -> dict[str, str]:
    config = dict(SITE_CONFIG_DEFAULTS)
    try:
        stored = json.loads(SITE_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(stored, dict):
            for key, default in SITE_CONFIG_DEFAULTS.items():
                value = stored.get(key)
                if isinstance(value, str) and value.strip():
                    config[key] = value.strip()
    except (OSError, json.JSONDecodeError):
        pass
    return config


def _render_site_html(template: str) -> str:
    config = _load_site_config()
    for key, value in config.items():
        template = template.replace("{{" + key.upper() + "}}", html.escape(value, quote=True))
    return template


logger = logging.getLogger(__name__)
app = FastAPI(title="Bio_shop Control", docs_url=None, redoc_url=None)

COOKIE = "bioshop_admin"
SESSION_TTL = 60 * 60 * 12
_started = False
_start_lock = threading.Lock()

SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    "addclient_enabled": {"label": "ระบบซื้อ VPN", "type": "bool", "default": "1", "group": "ซื้อ VPN"},
    "freeclient_enabled": {"label": "เปิดระบบทดลองใช้ฟรี", "type": "bool", "default": "1", "group": "ทดลองใช้ฟรี"},
    "credit_code_enabled": {"label": "กรอกโค้ดเครดิต", "type": "bool", "default": "1", "group": "Money"},
    "truemoney_enabled": {"label": "เติมเงิน TrueMoney", "type": "bool", "default": "1", "group": "Money"},
    "cancel_button_enabled": {"label": "ปุ่มยกเลิก", "type": "bool", "default": "1", "group": "การแสดงผล"},
    "run_start_finish_enabled": {"label": "แสดงเมนูหลังจบ", "type": "bool", "default": "1", "group": "การแสดงผล"},
    "buy_channel_mode": {"label": "ช่องทางการซื้อ", "type": "text", "default": "group_only", "group": "ซื้อ VPN"},
    "freeclient_hours": {"label": "ระยะเวลาทดลองใช้ (ชั่วโมง)", "type": "number", "default": "1", "min": 0.01, "group": "ทดลองใช้ฟรี"},
    "freeclient_reset_mode": {"label": "โหมดรีเซ็ตการทดลองใช้", "type": "text", "default": "midnight", "group": "ทดลองใช้ฟรี"},
    "freeclient_channel_mode": {"label": "ช่องทางการทดลองใช้งาน", "type": "text", "default": "dm_and_group", "group": "ทดลองใช้ฟรี"},
    "freeclient_label": {"label": "คำที่แสดงแทน FREE ในชื่อไฟล์", "type": "text", "default": "FREE", "group": "ทดลองใช้ฟรี"},
    "freeclient_daily_limit": {"label": "สิทธิ์ทดลอง/คน", "type": "integer", "default": "1", "min": 0, "group": "ทดลองใช้ฟรี"},
    "cancel_button_delay_seconds": {"label": "หน่วงปุ่มยกเลิก (วินาที)", "type": "number", "default": "1", "min": 0, "group": "การแสดงผล"},
    "run_start_finish_delay_seconds": {"label": "หน่วงเมนู /start (วินาที)", "type": "number", "default": "2", "min": 0.1, "group": "การแสดงผล"},
    "run_start_finish_commands": {"label": "คำสั่งที่ใช้แล้วให้เด้งเมนู /start", "type": "text", "default": "addclient", "group": "การแสดงผล"},
    "truemoney_wallet_phone": {"label": "เบอร์รับซอง", "type": "text", "default": "", "group": "Money"},
    "truemoney_credit_rate": {"label": "เครดิตต่อ 1 บาท", "type": "number", "default": "1", "min": 0.01, "group": "Money"},
    "buy_group_ids": {"label": "กลุ่มที่ซื้อได้ (คั่นด้วย ,)", "type": "text", "default": "", "group": "ซื้อ VPN"},
    "freeclient_group_ids": {"label": "กลุ่มทดลองใช้ฟรี (คั่นด้วย ,)", "type": "text", "default": "", "group": "ทดลองใช้ฟรี"},
}


def _secret() -> bytes:
    value = os.getenv("WEB_ADMIN_SECRET", "").strip()
    if not value:
        # Stable fallback derived from bot token; production README requires a dedicated secret.
        value = hashlib.sha256((os.getenv("BOT_TOKEN", "development-only") + ":web").encode()).hexdigest()
    return value.encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def _new_session() -> str:
    expiry = str(int(time.time()) + SESSION_TTL)
    nonce = secrets.token_urlsafe(18)
    payload = f"{expiry}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def _hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2_sha256$200000${salt}${digest}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, expected = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _web_credentials() -> tuple[str, str, str]:
    """Return username, password/hash, and mode. DB values override environment."""
    username = str(db.get_setting("web_admin_username", "admin") or "admin").strip() or "admin"
    stored_hash = str(db.get_setting("web_admin_password_hash", "") or "").strip()
    if stored_hash:
        return username, stored_hash, "hash"
    env_user = os.getenv("WEB_ADMIN_USERNAME", "").strip()
    env_password = os.getenv("WEB_ADMIN_PASSWORD", "")
    if env_user or env_password:
        return env_user or username, env_password or "123", "plain"
    return username, "123", "plain"


def _valid_session(value: str | None) -> bool:
    try:
        expiry, nonce, signature = (value or "").split(".", 2)
        payload = f"{expiry}.{nonce}"
        return int(expiry) >= int(time.time()) and hmac.compare_digest(signature, _sign(payload))
    except (ValueError, TypeError):
        return False


def _require(request: Request) -> None:
    if not _valid_session(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="กรุณาเข้าสู่ระบบใหม่")


def _clean_setting(key: str, value: Any) -> str:
    spec = SETTING_SCHEMA.get(key)
    if not spec:
        raise HTTPException(400, "ไม่อนุญาตให้แก้ไขค่านี้")
    kind = spec["type"]
    if kind == "bool":
        return "1" if str(value).lower() in {"1", "true", "on", "yes"} else "0"
    if kind in {"number", "integer"}:
        try:
            number = float(value)
            if number < float(spec.get("min", -1e99)):
                raise ValueError
            return str(int(number)) if kind == "integer" else f"{number:g}"
        except (TypeError, ValueError):
            raise HTTPException(400, f"ค่า {spec['label']} ไม่ถูกต้อง")
    text = str(value).strip()[:500]
    if key == "run_start_finish_commands":
        text = ",".join(part.strip().lstrip("/") for part in text.replace("\n", ",").split(",") if part.strip())
        if not text:
            raise HTTPException(400, "กรุณาระบุอย่างน้อย 1 คำสั่ง")
    if key == "truemoney_wallet_phone" and text and (not text.isdigit() or len(text) not in {9, 10}):
        raise HTTPException(400, "เบอร์ TrueMoney ไม่ถูกต้อง")
    if key.endswith("group_ids") and text:
        try:
            text = ",".join(str(int(x.strip())) for x in text.split(",") if x.strip())
        except ValueError:
            raise HTTPException(400, "Group ID ต้องเป็นตัวเลขคั่นด้วย comma")
    return text


class LoginBody(BaseModel):
    username: str
    password: str


class SettingBody(BaseModel):
    value: Any


class DatabaseBackendBody(BaseModel):
    backend: Literal["internal", "google_sheet"] = "internal"
    google_sheet_id: str = Field(default="", max_length=200)
    google_sheet_tab: str = Field(default="BioShop", min_length=1, max_length=80)
    service_account_json: str = Field(default="", max_length=20000)
    confirmed: bool = False
    reset_existing: bool = False


class DatabaseDeleteBody(BaseModel):
    confirmation: str = Field(min_length=1, max_length=40)
    expected_backend: Literal["internal", "google_sheet"]


class NetworkBody(BaseModel):
    network_type: str = Field(default="purchase", pattern=r"^(purchase|trial)$")
    code: str = Field(min_length=1, max_length=24, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    inbound_id: int = Field(default=1, ge=1, le=2_147_483_647)
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=9999)


class PackageBody(BaseModel):
    # GB policy is explicit at the API boundary; the database keeps the
    # backwards-compatible gb_limit/gb_max columns.
    gb_mode: Literal["fixed", "custom"] = "custom"
    gb_limit: float = Field(default=0, ge=0, le=1000000)
    gb_max: float = Field(default=0, ge=0, le=1000000)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    inbound_id: int = Field(ge=1, le=2_147_483_647)
    price_per_day: float = Field(gt=0, le=1_000_000)
    active: bool = True
    sort_order: int = Field(default=0, ge=0, le=9999)
    # Optional per-package URI template applied after the real panel link is fetched.
    template_enabled: bool = False
    template_address: str = Field(default="", max_length=253)
    template_port: int | None = Field(default=None, ge=1, le=65535)
    template_network: str = Field(default="", max_length=32, pattern=r"^[A-Za-z0-9_-]*$")
    template_host: str = Field(default="", max_length=512)
    template_path: str = Field(default="", max_length=2048)


class CreditBody(BaseModel):
    amount: float = Field(ge=-1_000_000, le=1_000_000)


class WebAccountBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(default="", max_length=200)


class CreditCodeBody(BaseModel):
    code_name: str = Field(min_length=1, max_length=64)
    mode: str = Field(pattern=r"^(fixed|random|free_reset)$")
    fixed_credit: float = Field(default=0, ge=0, le=1_000_000)
    total_credit: float = Field(default=0, ge=0, le=1_000_000)
    max_uses: int = Field(default=0, ge=0, le=1_000_000)
    expires_at: str = Field(min_length=1, max_length=80)
    active: bool = True


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "service": "bio-shop-control", "time": int(time.time())}


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> str:
    if not _valid_session(request.cookies.get(COOKIE)):
        return _render_site_html(LOGIN_HTML)
    return _render_site_html(DASHBOARD_HTML)


@app.post("/api/login")
def login(body: LoginBody, response: Response) -> dict[str, bool]:
    expected_user, password_value, password_mode = _web_credentials()
    password_ok = _verify_password(body.password, password_value) if password_mode == "hash" else hmac.compare_digest(body.password, password_value)
    if not (hmac.compare_digest(body.username, expected_user) and password_ok):
        time.sleep(0.35)
        raise HTTPException(401, "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
    response.set_cookie(COOKIE, _new_session(), max_age=SESSION_TTL, httponly=True, secure=os.getenv("WEB_COOKIE_SECURE", "1") == "1", samesite="strict", path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/overview")
def overview(request: Request) -> dict[str, Any]:
    _require(request)
    return db.get_dashboard_overview()


@app.get("/api/web-account")
def get_web_account(request: Request) -> dict[str, Any]:
    _require(request)
    username, _, _ = _web_credentials()
    return {"username": username}


@app.put("/api/web-account")
def update_web_account(body: WebAccountBody, request: Request, response: Response) -> dict[str, Any]:
    _require(request)
    username = body.username.strip()
    if not username:
        raise HTTPException(400, "กรุณากรอกชื่อผู้ใช้")
    db.set_setting("web_admin_username", username)
    if body.password:
        if len(body.password) < 3:
            raise HTTPException(400, "รหัสผ่านต้องมีอย่างน้อย 3 ตัวอักษร")
        db.set_setting("web_admin_password_hash", _hash_password(body.password))
    db.add_audit_log("web", "web-account.update", username, "", "password-changed" if body.password else "username-only")
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True, "message": "บันทึกแล้ว กรุณาเข้าสู่ระบบใหม่"}


@app.get("/api/settings")
def settings(request: Request) -> dict[str, Any]:
    _require(request)
    result = []
    for key, spec in SETTING_SCHEMA.items():
        result.append({"key": key, **spec, "value": db.get_setting_text(key, spec["default"])})
    return {"items": result, "updated_at": int(time.time())}


@app.put("/api/settings/{key}")
def update_setting(key: str, body: SettingBody, request: Request) -> dict[str, Any]:
    _require(request)
    value = _clean_setting(key, body.value)
    old = db.get_setting_text(key, SETTING_SCHEMA[key]["default"])
    db.set_setting(key, value)
    db.add_audit_log("web", "setting.update", key, old, value)
    return {"ok": True, "key": key, "value": value}


@app.get("/api/database-backend")
def database_backend(request: Request) -> dict[str, Any]:
    _require(request)
    return db.get_database_backend_info()


@app.post("/api/database-backend")
def change_database_backend(body: DatabaseBackendBody, request: Request) -> dict[str, Any]:
    _require(request)
    if not body.confirmed:
        raise HTTPException(400, "กรุณายืนยันการเชื่อมต่อฐานข้อมูล")
    backend = body.backend
    sheet_id = body.google_sheet_id.strip()
    tab = body.google_sheet_tab.strip() or "BioShop"
    old_credentials = str(db.get_setting("google_service_account_json", "") or "")
    backup_path = None
    retained_backup = None
    try:
        # Always retain a pre-switch backup before a pull or optional reset.
        backup_path = db.create_database_backup()
        retained_backup = f"{db.DB_PATH}.before-switch.db"
        shutil.copy2(backup_path, retained_backup)
        with db.suspend_backend_sync():
            if body.service_account_json.strip():
                db.set_setting("google_service_account_json", body.service_account_json.strip())
            if backend == "google_sheet":
                if not sheet_id:
                    raise HTTPException(400, "กรุณาระบุ Google Sheet ID")
                # Validate credentials and access before changing local data.
                # Pull is attempted by the database helper; a failed access must
                # leave the current local database untouched.
                if body.reset_existing:
                    db.clear_google_sheet(sheet_id, tab)
                    db.reset_database_data()
                    db.set_database_backend("google_sheet", sheet_id, tab)
                else:
                    # Configure the target temporarily so credentials/settings are
                    # available, then prefer existing Sheet data. If the Sheet is
                    # empty, preserve the local backup and export it to the Sheet.
                    db.set_setting(db.GOOGLE_SHEET_ID_KEY, sheet_id)
                    db.set_setting(db.GOOGLE_SHEET_TAB_KEY, tab)
                    pulled = db.pull_database_from_google_sheet(sheet_id, tab)
                    if not pulled.get("has_data"):
                        db.import_database_file_to_google_sheet(backup_path, sheet_id, tab)
                    # A Sheet export may contain old settings. Restore the
                    # current credentials and selected destination afterward.
                    if body.service_account_json.strip():
                        db.set_setting("google_service_account_json", body.service_account_json.strip())
                    elif old_credentials:
                        db.set_setting("google_service_account_json", old_credentials)
                    db.set_database_backend("google_sheet", sheet_id, tab)
            else:
                if body.reset_existing:
                    db.reset_database_data()
                elif db.get_database_backend() == "google_sheet":
                    current_id = sheet_id or str(db.get_setting(db.GOOGLE_SHEET_ID_KEY, "") or "")
                    current_tab = tab or str(db.get_setting(db.GOOGLE_SHEET_TAB_KEY, "BioShop") or "BioShop")
                    if current_id:
                        db.pull_database_from_google_sheet(current_id, current_tab)
                db.set_database_backend("internal")
            db.add_audit_log("web", "database.backend.change", backend, "", sheet_id or "internal")
    except HTTPException:
        if body.service_account_json.strip():
            db.set_setting("google_service_account_json", old_credentials)
        raise
    except ValueError as exc:
        if body.service_account_json.strip():
            db.set_setting("google_service_account_json", old_credentials)
        raise HTTPException(400, str(exc))
    finally:
        if backup_path and os.path.exists(backup_path):
            os.remove(backup_path)
    return {"ok": True, "backend": backend, "google_sheet_id": sheet_id,
            "google_sheet_tab": tab, "reset_existing": body.reset_existing,
            "backup_file": retained_backup,
            "message": "เชื่อมต่อฐานข้อมูลและดึงข้อมูลสำเร็จ" if not body.reset_existing else "รีเซ็ตและเปลี่ยนฐานข้อมูลแล้ว"}


@app.post("/api/database-pull")
def database_pull(request: Request) -> dict[str, Any]:
    _require(request)
    backend = db.get_database_backend()
    if backend == "internal":
        db.init_db()
        return {"ok": True, "backend": backend, "message": "ใช้ข้อมูลจากฐานข้อมูลในระบบแล้ว"}
    sheet_id = str(db.get_setting(db.GOOGLE_SHEET_ID_KEY, "") or "").strip()
    tab = str(db.get_setting(db.GOOGLE_SHEET_TAB_KEY, "BioShop") or "BioShop").strip() or "BioShop"
    if not sheet_id:
        raise HTTPException(400, "ยังไม่ได้ตั้งค่า Google Sheet ID")
    try:
        result = db.restore_database_from_google_sheet(sheet_id, tab)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not result.get("has_data"):
        raise HTTPException(400, "ยังไม่มีข้อมูลที่ส่งออกไว้ใน Google Sheet")
    db.add_audit_log("web", "database.restore.google_sheet", tab, "", str(result.get("rows", 0)))
    return {"ok": True, "backend": backend, **result,
            "message": "กู้คืนข้อมูลจาก Google Sheets สำเร็จ และสร้างไฟล์สำรองก่อนกู้คืนแล้ว"}


@app.post("/api/database-push")
def database_push(request: Request) -> dict[str, Any]:
    _require(request)
    if db.get_database_backend() != "google_sheet":
        raise HTTPException(400, "กรุณาเลือก Google Sheets เป็นฐานข้อมูลก่อนสำรองข้อมูล")
    try:
        result = db.backup_database_to_google_sheet()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    db.add_audit_log("web", "database.backup.google_sheet", str(result.get("google_sheet_tab", "")), "", str(result.get("rows", 0)))
    return {"ok": True, "backend": "google_sheet", **result,
            "message": "สำรองข้อมูลทั้งหมดไปยัง Google Sheets แล้ว รวมถึงค่า Template ในหมวดแพ็กเกจ"}


@app.post("/api/database-sync")
def database_sync(request: Request) -> dict[str, Any]:
    """Manually refresh the active website cache from its selected backend."""
    _require(request)
    backend = db.get_database_backend()
    if backend == "internal":
        db.init_db()
        return {"ok": True, "backend": backend, "direction": "local_sqlite",
                "message": "เว็บไซต์ใช้ฐานข้อมูล SQLite อยู่แล้ว ไม่มี Google Sheets ให้ซิงก์"}
    sheet_id = str(db.get_setting(db.GOOGLE_SHEET_ID_KEY, "") or "").strip()
    tab = str(db.get_setting(db.GOOGLE_SHEET_TAB_KEY, "BioShop") or "BioShop").strip() or "BioShop"
    if not sheet_id:
        raise HTTPException(400, "ยังไม่ได้ตั้งค่า Google Sheet ID")
    try:
        result = db.restore_database_from_google_sheet(sheet_id, tab)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not result.get("has_data"):
        raise HTTPException(400, "Google Sheets ยังไม่มีข้อมูลสำหรับซิงก์")
    return {"ok": True, "backend": backend, "direction": "google_sheet_to_website", **result,
            "message": "ซิงก์ข้อมูลจาก Google Sheets เข้าสู่เว็บไซต์แล้ว"}


@app.post("/api/database-delete")
def database_delete(body: DatabaseDeleteBody, request: Request) -> dict[str, Any]:
    _require(request)
    if body.confirmation != "ยืนยันการลบ":
        raise HTTPException(400, "กรุณาพิมพ์คำว่า ยืนยันการลบ ให้ตรงตามที่กำหนด")
    if body.expected_backend != db.get_database_backend():
        raise HTTPException(409, "ชนิดฐานข้อมูลที่เลือกเปลี่ยนไปแล้ว กรุณารีเฟรชหน้าและตรวจสอบก่อนลบ")
    try:
        result = db.delete_selected_database_data()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, **result}


@app.get("/api/networks")
def networks(request: Request) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_networks(active_only=False)}


@app.post("/api/networks")
def create_network(body: NetworkBody, request: Request) -> dict[str, Any]:
    _require(request)
    try:
        data = body.model_dump()
        data["code"] = data["code"].strip().upper()
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HTTPException(400, "กรุณากรอกชื่อเครือข่าย")
        network_id = db.create_network(**data)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, "รหัสเครือข่ายนี้มีอยู่แล้ว")
        raise
    db.add_audit_log("web", "network.create", str(network_id), "", body.model_dump_json())
    return {"ok": True, "id": network_id}


@app.put("/api/networks/{network_id}")
def update_network(network_id: int, body: NetworkBody, request: Request) -> dict[str, Any]:
    _require(request)
    try:
        data = body.model_dump()
        data["code"] = data["code"].strip().upper()
        data["name"] = data["name"].strip()
        if not data["name"]:
            raise HTTPException(400, "กรุณากรอกชื่อเครือข่าย")
        changed = db.update_network(network_id, **data)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, "รหัสเครือข่ายนี้มีอยู่แล้ว")
        raise
    if not changed:
        raise HTTPException(404, "ไม่พบเครือข่าย")
    db.add_audit_log("web", "network.update", str(network_id), "", body.model_dump_json())
    return {"ok": True}


@app.delete("/api/networks/{network_id}")
def delete_network(network_id: int, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.delete_network(network_id):
        raise HTTPException(404, "ไม่พบเครือข่าย")
    db.add_audit_log("web", "network.delete", str(network_id), "active", "deleted")
    return {"ok": True}


def _normalize_package_values(body: PackageBody) -> dict[str, Any]:
    """Validate and normalize package values, including optional link overrides."""
    values = body.model_dump()
    for key in ("name", "description", "template_address", "template_network", "template_host", "template_path"):
        values[key] = str(values.get(key) or "").strip()
    # Zero is the database representation for "use the selected Inbound port".
    values["template_port"] = int(values.get("template_port") or 0)
    values["template_network"] = values["template_network"].lower()
    if values["template_address"]:
        address = values["template_address"]
        # This field is a bare hostname/IP only. Port overrides are configured
        # separately with template_port, never embedded into the Address field.
        if any(ch.isspace() for ch in address) or any(ch in address for ch in "/?#@") or "://" in address:
            raise HTTPException(400, "Address ต้องเป็นโดเมนหรือ IP เท่านั้น ห้ามใส่ http://, path หรือ port (ตั้งพอร์ตแยกในช่อง Port)")
        try:
            from urllib.parse import urlsplit
            parsed = urlsplit("//" + address)
            if not parsed.hostname or parsed.port is not None or parsed.path or parsed.query or parsed.fragment:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "Address ไม่ถูกต้อง กรุณาใส่เฉพาะโดเมนหรือ IP")
    if any(ch.isspace() for ch in values["template_host"]):
        raise HTTPException(400, "Host ต้องไม่มีช่องว่างหรือการขึ้นบรรทัดใหม่")
    if values["template_enabled"] and not any(values.get(key) for key in (
        "template_address", "template_port", "template_network", "template_host", "template_path"
    )):
        raise HTTPException(400, "เปิดใช้ Template แล้ว กรุณากำหนดอย่างน้อยหนึ่งค่า")
    return values


@app.get("/api/networks/{network_id}/packages")
def packages(network_id: int, request: Request) -> dict[str, Any]:
    _require(request)
    network = db.get_network(network_id, active_only=False)
    if not network: raise HTTPException(404, "ไม่พบเครือข่าย")
    return {"network": network, "items": db.get_packages(network_id, active_only=False)}


@app.post("/api/networks/{network_id}/packages")
def create_package(network_id: int, body: PackageBody, request: Request) -> dict[str, Any]:
    _require(request)
    network = db.get_network(network_id, active_only=False)
    if not network: raise HTTPException(404, "ไม่พบเครือข่าย")
    values = _normalize_package_values(body)
    # Normalize the two mutually exclusive modes before persisting. This also
    # protects the API when called outside the browser UI.
    if values["gb_mode"] == "fixed":
        if values["gb_limit"] <= 0:
            raise HTTPException(400, "โหมดกำหนดจำนวน GB ต้องมากกว่า 0")
        values["gb_max"] = 0
    else:
        values["gb_limit"] = 0
    if network.get("network_type") == "trial":
        # Keep the legacy positive-price database constraint satisfied; trial
        # flows never read this placeholder and it is never shown in the UI.
        values["price_per_day"] = 0.01
    values.pop("gb_mode", None)
    package_id = db.create_package(network_id=network_id, **values)
    db.add_audit_log("web", "package.create", str(package_id), "", body.model_dump_json())
    return {"ok": True, "id": package_id}


@app.put("/api/packages/{package_id}")
def update_package(package_id: int, body: PackageBody, request: Request) -> dict[str, Any]:
    _require(request)
    package = db.get_package(package_id, active_only=False)
    network = None
    if package:
        network = db.get_network(int(package["network_id"]), active_only=False)
    values = _normalize_package_values(body)
    if values["gb_mode"] == "fixed":
        if values["gb_limit"] <= 0:
            raise HTTPException(400, "โหมดกำหนดจำนวน GB ต้องมากกว่า 0")
        values["gb_max"] = 0
    else:
        values["gb_limit"] = 0
    if network and network.get("network_type") == "trial":
        values["price_per_day"] = 0.01
    values.pop("gb_mode", None)
    if not db.update_package(package_id, **values): raise HTTPException(404, "ไม่พบแพ็กเกจ")
    db.add_audit_log("web", "package.update", str(package_id), "", body.model_dump_json())
    return {"ok": True}


@app.delete("/api/packages/{package_id}")
def delete_package(package_id: int, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.delete_package(package_id): raise HTTPException(404, "ไม่พบแพ็กเกจ")
    db.add_audit_log("web", "package.delete", str(package_id), "active", "deleted")
    return {"ok": True}


@app.get("/api/backup")
def download_backup(request: Request):
    _require(request)
    try:
        path, backend = db.create_selected_backend_backup()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    prefix = "bioshop-google-sheets-backup" if backend == "google_sheet" else "bioshop-sqlite-backup"
    filename = time.strftime(f"{prefix}-%Y%m%d-%H%M%S.db")
    db.add_audit_log("web", "database.backup", filename)
    return FileResponse(
        path, media_type="application/x-sqlite3", filename=filename,
        background=BackgroundTask(lambda: os.path.exists(path) and os.remove(path)),
    )


@app.post("/api/restore")
async def restore_backup(request: Request, mode: str = "replace") -> dict[str, Any]:
    _require(request)
    payload = await request.body()
    if not payload:
        raise HTTPException(400, "กรุณาเลือกไฟล์สำรอง")
    if len(payload) > 100 * 1024 * 1024:
        raise HTTPException(413, "ไฟล์ใหญ่เกิน 100 MB")
    fd, path = tempfile.mkstemp(prefix="bioshop-restore-", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        if mode not in {"replace", "merge"}:
            raise HTTPException(400, "โหมดคืนค่าไม่ถูกต้อง")
        result = db.import_database_file_to_selected_backend(path, mode=mode)
        db.add_audit_log("web", f"database.restore.{mode}", "uploaded-file")
        return {"ok": True, "message": "นำเข้าฐานข้อมูลสำเร็จ", **result}
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        if os.path.exists(path):
            os.remove(path)


@app.get("/api/users")
def users(request: Request, q: str = "", limit: int = 100) -> dict[str, Any]:
    _require(request)
    return {"items": db.search_users(q[:80], min(max(limit, 1), 200))}


@app.post("/api/users/{user_id}/credit")
def user_credit(user_id: int, body: CreditBody, request: Request) -> dict[str, Any]:
    _require(request)
    db.ensure_user(user_id, None)
    before = db.get_credit(user_id)
    if body.amount >= 0:
        db.add_credit(user_id, body.amount)
    else:
        db.deduct_credit(user_id, abs(body.amount))
    after = db.get_credit(user_id)
    db.add_audit_log("web", "credit.adjust", str(user_id), str(before), str(after))
    return {"ok": True, "balance": after}


@app.get("/api/credit-codes")
def credit_codes(request: Request) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_all_credit_codes()}


@app.post("/api/credit-codes")
def create_credit_code_web(body: CreditCodeBody, request: Request) -> dict[str, Any]:
    _require(request)
    name = body.code_name.strip()
    key = name.lower()
    if db.credit_code_exists(key):
        raise HTTPException(409, "มีโค้ดชื่อนี้อยู่แล้ว")
    if body.mode == "fixed" and body.fixed_credit <= 0:
        raise HTTPException(400, "เครดิตต่อคนต้องมากกว่า 0")
    if body.mode == "random" and body.total_credit <= 0:
        raise HTTPException(400, "เครดิตรวมต้องมากกว่า 0")
    if body.mode == "free_reset":
        body.fixed_credit = 0; body.total_credit = 0
    if not db.create_credit_code(key, name, body.mode, body.fixed_credit, body.total_credit,
                                 body.max_uses, body.expires_at, 0, db._now_th_str()):
        raise HTTPException(409, "สร้างโค้ดไม่สำเร็จ")
    return {"ok": True}


@app.put("/api/credit-codes/{code_key}")
def update_credit_code_web(code_key: str, body: CreditCodeBody, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.get_credit_code(code_key):
        raise HTTPException(404, "ไม่พบโค้ด")
    if body.mode == "fixed" and body.fixed_credit <= 0:
        raise HTTPException(400, "เครดิตต่อคนต้องมากกว่า 0")
    if body.mode == "random" and body.total_credit <= 0:
        raise HTTPException(400, "เครดิตรวมต้องมากกว่า 0")
    ok = db.update_credit_code(code_key, body.code_name.strip(), body.mode,
                               body.fixed_credit if body.mode == "fixed" else 0,
                               body.total_credit if body.mode == "random" else 0,
                               body.max_uses, body.expires_at, body.active)
    if not ok: raise HTTPException(404, "ไม่พบโค้ด")
    return {"ok": True}


@app.delete("/api/credit-codes/{code_key}")
def delete_credit_code_web(code_key: str, request: Request) -> dict[str, Any]:
    _require(request)
    if not db.delete_credit_code(code_key): raise HTTPException(404, "ไม่พบโค้ด")
    return {"ok": True}


@app.get("/api/orders")
def orders(request: Request, limit: int = 100) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_recent_activity(min(max(limit, 1), 200))}


@app.get("/api/audit")
def audit(request: Request, limit: int = 100) -> dict[str, Any]:
    _require(request)
    return {"items": db.get_audit_logs(min(max(limit, 1), 200))}


def start_web_server() -> None:
    global _started
    with _start_lock:
        if _started or os.getenv("WEB_ADMIN_ENABLED", "1") != "1":
            return
        _started = True
        host = os.getenv("WEB_HOST", "0.0.0.0")
        port = int(os.getenv("PORT", os.getenv("WEB_PORT", "8080")))
        thread = threading.Thread(target=lambda: uvicorn.run(app, host=host, port=port, log_level="info", access_log=False), daemon=True, name="web-admin")
        thread.start()
        logger.info("Web control panel started on %s:%s", host, port)


LOGIN_HTML = r'''<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{LOGIN_TITLE}}</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#202124;background:#f6f8fb}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px}.login{width:min(420px,100%);background:white;border:1px solid #e2e7ef;border-radius:16px;padding:32px;box-shadow:0 16px 50px #17325b12}.brand{display:flex;align-items:center;gap:12px;margin-bottom:28px}.logo{width:46px;height:46px;display:grid;place-items:center;background:#e8f1ff;color:#1769d2;border-radius:12px;font-size:24px}.brand h1{font-size:20px;margin:0}.brand p{margin:3px 0 0;color:#6b7280;font-size:14px}label{display:block;font-size:14px;font-weight:650;margin:16px 0 7px}input{width:100%;height:46px;border:1px solid #d8dee9;border-radius:9px;padding:0 13px;font-size:16px;outline:none}input:focus{border-color:#2783de;box-shadow:0 0 0 3px #2783de20}button{width:100%;height:46px;border:0;border-radius:9px;margin-top:22px;background:#2783de;color:white;font-weight:700;font-size:15px;cursor:pointer}button:disabled{opacity:.6}.error{min-height:20px;color:#d14343;font-size:14px;margin-top:12px}</style></head><body><main class="login"><div class="brand"><div class="logo">◈</div><div><h1>{{LOGIN_TITLE}}</h1><p>{{LOGIN_DESCRIPTION}}</p></div></div><form id="f"><label>ชื่อผู้ใช้</label><input id="u" autocomplete="username" required><label>รหัสผ่าน</label><input id="p" type="password" autocomplete="current-password" required><button>เข้าสู่ระบบ</button><div class="error" id="e"></div></form></main><script>f.onsubmit=async x=>{x.preventDefault();let b=f.querySelector('button');b.disabled=true;e.textContent='';let r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:u.value,password:p.value})});if(r.ok)location.reload();else{let j=await r.json();e.textContent=j.detail||'เข้าสู่ระบบไม่สำเร็จ';b.disabled=false}}</script></body></html>'''

DASHBOARD_HTML = r'''<!doctype html><html lang="th"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{{WEBSITE_NAME}}</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#202124;background:#f6f8fb;--blue:#2783de;--green:#2f9e6f;--red:#dc5a52;--muted:#6b7280;--border:#e1e6ee}*{box-sizing:border-box}body{margin:0;overflow-x:hidden}.app{display:grid;grid-template-columns:244px 1fr;min-height:100vh;transition:grid-template-columns .2s ease}.app.sidebar-collapsed{grid-template-columns:76px 1fr}.app.sidebar-collapsed .side{padding-left:10px;padding-right:10px}.app.sidebar-collapsed .brand-full,.app.sidebar-collapsed .brand small,.app.sidebar-collapsed .nav-label{display:none}.app.sidebar-collapsed .brand-mini{display:inline}.app.sidebar-collapsed .brand{text-align:center}.app.sidebar-collapsed .nav button{justify-content:center;padding-left:0;padding-right:0}.app.sidebar-collapsed .nav-icon{font-size:20px}.app.sidebar-collapsed .logout{font-size:0}.app.sidebar-collapsed .logout:after{content:"↪";font-size:18px}.side{background:#111827;color:#e5e7eb;padding:22px 14px;position:sticky;top:0;height:100vh}.brand{font-weight:800;font-size:18px;padding:4px 12px 12px;display:flex;align-items:center;gap:7px}.brand-mini{display:none}.side-toggle{width:100%;height:36px;margin:0 0 12px;border:1px solid #344258;background:#1b2638;color:#cbd5e1;border-radius:8px;cursor:pointer;font-size:18px}.side-toggle:hover{background:#253247;color:white}.brand small{display:block;color:#8fa0b8;font-weight:500;font-size:12px;margin-top:4px}.nav button{display:flex;align-items:center;gap:11px;width:100%;height:44px;padding:0 12px;border:0;background:transparent;color:#aeb9c9;border-radius:8px;font-size:15px;text-align:left;cursor:pointer;margin:3px 0}.nav button.active,.nav button:hover{background:#253247;color:white}.logout{position:absolute;bottom:20px;left:14px;right:14px;height:42px;border:1px solid #344258;background:#1b2638;color:#cbd5e1;border-radius:8px;cursor:pointer;font:inherit}.logout:hover{background:#253247;color:white}.main{padding:30px 36px;min-width:0}.app.focus-mode .top{display:none}.app.focus-mode .main{width:100%;max-width:1100px;margin:0 auto;padding:28px 22px}.focus-head{display:flex;align-items:center;gap:12px;margin-bottom:18px}.focus-head h1{margin:0;font-size:24px}.focus-card{background:#fff;border:1px solid var(--border);border-radius:12px;padding:20px}.focus-actions{display:flex;justify-content:flex-end;gap:9px;margin-top:18px}.settings-action{align-items:center}.settings-action .btn{min-width:92px}.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:26px}.top h1{font-size:25px;margin:0}.top p{color:var(--muted);margin:5px 0 0;font-size:14px}.status{font-size:13px;color:var(--green);background:#e9f6f0;border:1px solid #ccebdd;padding:7px 10px;border-radius:8px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}.backup-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.card,.panel{background:#fff;border:1px solid var(--border);border-radius:12px}.card{padding:18px}.card .label{font-size:13px;color:var(--muted)}.card .value{font-size:27px;font-weight:800;margin-top:8px}.panel{margin-top:18px;overflow:hidden}.panel-head{padding:17px 19px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}.panel-head h2{font-size:16px;margin:0}.content{padding:18px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.setting{border:1px solid var(--border);border-radius:10px;padding:14px;display:flex;align-items:center;justify-content:space-between;gap:12px}.setting b{font-size:14px}.setting small{display:block;color:var(--muted);margin-top:3px}.switch{width:48px;height:28px;border:0;border-radius:16px;background:#cfd6df;position:relative;cursor:pointer;flex:none}.switch:after{content:"";position:absolute;width:22px;height:22px;background:white;border-radius:50%;left:3px;top:3px;transition:.18s}.switch.on{background:var(--green)}.switch.on:after{left:23px}.btn{border:1px solid var(--border);background:white;border-radius:8px;padding:9px 13px;cursor:pointer;font-weight:650}.btn.primary{background:var(--blue);border-color:var(--blue);color:white}.btn.danger{color:var(--red)}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:12px 14px;border-bottom:1px solid #edf0f4;white-space:nowrap}th{font-size:12px;color:var(--muted);background:#fafbfc}.table-wrap{overflow:auto}.tag{font-size:12px;padding:4px 7px;border-radius:6px;background:#eef3f8}.tag.on{background:#e7f5ee;color:#16764e}.purchase-tag{background:#e5f2fc;color:#1769d2}.trial-tag{background:#fbebde;color:#a85c20}.empty{padding:40px;text-align:center;color:var(--muted)}.hidden{display:none!important}.modal-bg{position:fixed;inset:0;background:#10182870;display:grid;place-items:center;padding:18px;z-index:5}.modal{width:min(560px,100%);max-height:92vh;overflow:auto;background:white;border-radius:14px;padding:24px}.modal h2{margin:0 0 18px}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:13px}.field.full{grid-column:1/-1}.field label{display:block;font-size:13px;font-weight:650;margin-bottom:6px}.field input,.field select,.field textarea{width:100%;border:1px solid #d8dee9;border-radius:8px;padding:10px;font:inherit}.field textarea{min-height:220px;resize:vertical;line-height:1.55}.gb-choice{display:grid;grid-template-columns:1fr 1fr;gap:10px}.gb-option{display:flex!important;align-items:flex-start;gap:10px;border:1px solid var(--border);border-radius:10px;padding:13px;cursor:pointer;margin:0!important}.gb-option:has(input:checked){border-color:var(--blue);background:#f2f8ff;box-shadow:0 0 0 2px #2783de20}.gb-option input{width:auto;margin-top:3px}.gb-option small{display:block;color:var(--muted);font-weight:400;margin-top:4px}.actions{display:flex;justify-content:flex-end;gap:9px;margin-top:20px}.working-overlay{position:fixed;inset:0;z-index:20;display:grid;place-items:center;padding:18px;background:#10182870}.working-card{display:flex;align-items:center;gap:13px;background:#fff;border:1px solid var(--border);border-radius:12px;padding:18px 22px;box-shadow:0 16px 48px #0003;font-weight:650}.working-spinner{width:25px;height:25px;border:3px solid #d7e6f8;border-top-color:var(--blue);border-radius:50%;animation:working-spin .75s linear infinite;flex:none}@keyframes working-spin{to{transform:rotate(360deg)}}@media(prefers-reduced-motion:reduce){.working-spinner{animation-duration:1.8s}}.toast{position:fixed;right:20px;bottom:20px;background:#172033;color:white;padding:12px 16px;border-radius:9px;z-index:10;box-shadow:0 8px 30px #0003}.search{height:38px;border:1px solid var(--border);border-radius:8px;padding:0 11px;width:230px}.action-card{display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:start}.action-card p{color:var(--muted);line-height:1.55}.value.compact{font-size:20px}.action-icon{width:42px;height:42px;border-radius:10px;display:grid;place-items:center;font-size:22px;font-weight:800}.action-icon.blue{background:#e5f2fc;color:#2783de}.action-icon.orange{background:#fbebde;color:#d5803b}.restore-file{border:1px solid var(--border);background:#f9fafb;border-radius:10px;padding:14px}.restore-file small,.restore-option small{display:block;color:var(--muted);margin-top:4px}.modal-note{color:var(--muted)}.restore-options{display:grid;gap:10px}.restore-option{border:1px solid var(--border);background:white;border-radius:10px;padding:14px;text-align:left;cursor:pointer;font:inherit}.restore-option:hover{border-color:var(--blue);background:#f7fbff}.restore-option.danger-zone{border-color:#f0c8c4}.restore-option.danger-zone:hover{border-color:var(--red);background:#fff8f7}.panel.danger-zone{border-color:#f0c8c4}.panel.danger-zone .panel-head{border-color:#f0c8c4}@media(max-width:900px){.app,.app.sidebar-collapsed{display:block;min-height:100vh}.side,.nav,.main,.panel{min-width:0;max-width:100vw}.side{height:auto;position:sticky;top:0;z-index:4;padding:8px 10px;display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:8px}.brand{padding:0;font-size:15px;white-space:nowrap}.brand small{display:none}.nav{display:flex;overflow-x:auto;scrollbar-width:none}.nav::-webkit-scrollbar{display:none}.nav button{min-width:max-content;height:40px;padding:0 10px;font-size:13px}.logout{position:static;width:auto;height:40px;padding:0 10px;font-size:13px}.main{padding:18px 14px}.top{margin-bottom:16px}.cards{grid-template-columns:1fr 1fr}.grid{grid-template-columns:1fr}.panel-head{gap:10px;flex-wrap:wrap}}@media(max-width:900px){.app.sidebar-collapsed .side{display:flex;justify-content:space-between}.app.sidebar-collapsed .nav{display:none}.app.sidebar-collapsed .side-toggle{width:42px;margin:0}.app.sidebar-collapsed .logout{font-size:13px}.app.sidebar-collapsed .logout:after{content:none}}@media(max-width:520px){.gb-choice{grid-template-columns:1fr}.cards,.backup-grid{grid-template-columns:1fr}.top{align-items:flex-start}.top h1{font-size:22px}.top p{font-size:13px;line-height:1.45}.status{display:none}.card{padding:16px}.action-card{grid-template-columns:1fr}.action-icon{width:38px;height:38px}.form-grid{grid-template-columns:1fr}.field.full{grid-column:auto}th,td{padding:11px 12px}.modal-bg{align-items:end;padding:0}.modal{width:100%;max-height:94vh;border-radius:16px 16px 0 0;padding:20px}.actions{position:sticky;bottom:-20px;background:white;padding-top:12px}}

.app{display:block;min-height:100vh}
.app .side{position:fixed;z-index:12;left:0;top:0;bottom:0;width:min(290px,86vw);height:100vh;height:100dvh;overflow-y:auto;padding:22px 14px;transform:translateX(-105%);transition:transform .24s ease;box-shadow:12px 0 36px #10182824}
.app.menu-open .side{transform:translateX(0)}
.menu-backdrop{position:fixed;inset:0;z-index:11;background:#10182870;opacity:0;visibility:hidden;transition:opacity .2s ease,visibility .2s ease}
.app.menu-open .menu-backdrop{opacity:1;visibility:visible}
.sitebar{height:72px;margin-left:-36px;margin-right:-36px;position:sticky;top:0;z-index:7;display:flex;align-items:center;justify-content:center;background:#f6f8fbf2;backdrop-filter:blur(10px);border-bottom:1px solid #e1e6ee}
.sitebar-brand{display:flex;align-items:center;gap:9px;font-size:21px;font-weight:800;color:#202124;letter-spacing:.15px}
.sitebar-brand .site-mark{color:#2783de;font-size:24px}
.menu-toggle{position:absolute;left:clamp(14px,3vw,32px);top:50%;transform:translateY(-50%);width:44px;height:44px;margin:0;border:1px solid var(--border);border-radius:10px;background:white;color:#263449;cursor:pointer;font-size:22px;line-height:1}
.menu-toggle:hover{background:#edf4fc;border-color:#c7d9ee}
.app .main{display:block;min-width:0;padding:0 36px 30px}
.app .top{max-width:1600px;margin:0 auto 26px;padding-top:24px}
.app.focus-mode .top{display:none}
.app.focus-mode .main{width:100%;max-width:1100px;margin:0 auto;padding:0 22px 28px}.app.focus-mode .sitebar{margin-left:-22px;margin-right:-22px}
@media(max-width:900px){.sitebar{margin-left:-18px;margin-right:-18px}.app .main{padding:0 18px 24px}.app .top{margin-bottom:16px}}
@media(max-width:520px){.sitebar{height:64px;margin-left:-14px;margin-right:-14px}.sitebar-brand{font-size:19px}.sitebar-brand .site-mark{font-size:22px}.menu-toggle{left:12px;width:42px;height:42px}.app .main{padding:0 14px 20px}.app .top{align-items:flex-start;margin:0 auto 16px;padding-top:19px}.app .top h1{font-size:22px}.app .top p{font-size:13px;line-height:1.45}}

.app .side{display:flex;flex-direction:column;align-items:stretch}
.app .side .brand{display:flex;align-items:center;gap:8px;padding:4px 12px 14px;margin:0 0 12px;border-bottom:1px solid #344258;white-space:normal}
.app .side .brand-mini{display:inline}
.app .side .brand-full{display:inline}
.app .side .nav{display:flex;flex:1 1 auto;flex-direction:column;align-items:stretch;min-height:0;overflow-x:hidden;overflow-y:auto;margin:0 0 52px;scrollbar-width:thin}
.app .side .nav button{display:flex;flex:0 0 44px;align-items:center;justify-content:flex-start;width:100%;min-width:0;height:44px;margin:3px 0;padding:0 12px;text-align:left}
.app .side .nav-icon{flex:0 0 20px;text-align:center}
.app .side .logout{position:absolute;left:14px;right:14px;bottom:20px;width:auto;height:42px;margin:0;padding:0 12px;font-size:14px}
@media(max-height:620px){.app .side{padding-top:12px;padding-bottom:12px}.app .side .brand{padding-bottom:9px;margin-bottom:7px}.app .side .nav{margin-bottom:48px}.app .side .nav button{flex-basis:40px;height:40px;margin:2px 0}}
</style></head><body><div class="app"><div class="menu-backdrop" onclick="toggleSidebar(false)" aria-hidden="true"></div><aside class="side"><div class="brand"><span class="brand-mini">◈</span><span class="brand-full">Admin</span></div><nav class="nav"><button data-page="overview" class="active"><span class="nav-icon">▦</span><span class="nav-label">ภาพรวม</span></button><button data-page="settings"><span class="nav-icon">⚙</span><span class="nav-label">ตั้งค่าบอท</span></button><button data-page="networks"><span class="nav-icon">◫</span><span class="nav-label">เครือข่าย</span></button><button data-page="users"><span class="nav-icon">◎</span><span class="nav-label">ผู้ใช้และเครดิต</span></button><button data-page="orders"><span class="nav-icon">≡</span><span class="nav-label">รายการสั่งซื้อ</span></button><button data-page="audit"><span class="nav-icon">⌁</span><span class="nav-label">ประวัติการแก้ไข</span></button><button data-page="backup"><span class="nav-icon">▣</span><span class="nav-label">ฐานข้อมูล</span></button><button data-page="account"><span class="nav-icon">♙</span><span class="nav-label">จัดการบัญชี</span></button></nav><button class="logout" onclick="logout()">ออกจากระบบ</button></aside><main class="main"><header class="sitebar"><button class="menu-toggle" id="menuToggle" type="button" aria-label="เปิดเมนูด้านข้าง" aria-expanded="false" onclick="toggleSidebar()">☰</button><div class="sitebar-brand"><span class="site-mark">◈</span><span>{{WEBSITE_NAME}}</span></div></header><div class="top"><div><h1 id="title">ภาพรวมระบบ</h1><p id="subtitle">ข้อมูลจากบอทและเว็บไซต์ใช้ฐานข้อมูลเดียวกันแบบเรียลไทม์</p></div><div class="status">● ระบบออนไลน์</div></div><section id="page"></section></main></div><div id="modal" class="hidden"></div><div id="workingOverlay" class="working-overlay hidden" role="status" aria-live="polite"><div class="working-card"><span class="working-spinner" aria-hidden="true"></span><span id="workingMessage">กำลังดำเนินการ...</span></div></div><div id="toast" class="toast hidden"></div><script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), enc=s=>encodeURIComponent(String(s??''));let networks=[],packages=[],activeNetwork=null;
function showWorkingOverlay(message='กำลังดำเนินการ...'){let overlay=$('#workingOverlay');if(!overlay)return;$('#workingMessage').textContent=message;overlay.classList.remove('hidden')}function hideWorkingOverlay(){$('#workingOverlay')?.classList.add('hidden')}async function resetDashboardToOverview(successMessage){closeModal();toggleSidebar(false);leaveFocus();networks=[];packages=[];activeNetwork=null;window.pendingRestoreFile=null;document.querySelectorAll('[data-page]').forEach(button=>button.classList.toggle('active',button.dataset.page==='overview'));$('#title').textContent='ภาพรวมระบบ';$('#subtitle').textContent='ข้อมูลจากบอทและเว็บไซต์ใช้ฐานข้อมูลเดียวกันแบบเรียลไทม์';showWorkingOverlay('กำลังโหลดภาพรวมระบบ...');try{await overview();toast(successMessage)}finally{hideWorkingOverlay()}}
async function api(url,opt={}){let r=await fetch(url,{headers:{'content-type':'application/json',...(opt.headers||{})},...opt});if(r.status===401){location.reload();throw Error('กรุณาเข้าสู่ระบบใหม่')}let j=await r.json().catch(()=>({}));if(!r.ok){let d=j.detail;if(Array.isArray(d))d=d.map(x=>x.msg||String(x)).join(', ');throw Error(d||'เกิดข้อผิดพลาด')}return j}window.addEventListener('unhandledrejection',e=>{e.preventDefault();toast(e.reason?.message||'เกิดข้อผิดพลาด')});function money(n){return Number(n||0).toLocaleString('th-TH',{maximumFractionDigits:2})}function toast(t){let x=$('#toast');x.textContent=t;x.classList.remove('hidden');setTimeout(()=>x.classList.add('hidden'),2200)}
async function overview(){let d=await api('/api/overview');$('#page').innerHTML=`<div class="cards"><div class="card"><div class="label">ผู้ใช้ทั้งหมด</div><div class="value">${money(d.users)}</div></div><div class="card"><div class="label">ยอดขายรวม (เครดิต)</div><div class="value">${money(d.revenue)}</div></div><div class="card"><div class="label">ออเดอร์ทั้งหมด</div><div class="value">${money(d.orders)}</div></div><div class="card"><div class="label">เครือข่ายที่เปิดใช้</div><div class="value">${money(d.active_networks)}</div></div></div><div class="panel"><div class="panel-head"><h2>รายการสั่งซื้อล่าสุด</h2></div>${ordersTable(d.recent_orders)}</div>`}
function ordersTable(a){if(!a?.length)return '<div class="empty">ยังไม่มีรายการ</div>';return `<div class="table-wrap"><table><thead><tr><th>ประเภท</th><th>เวลา</th><th>ผู้ใช้</th><th>ชื่อ</th><th>เครือข่าย</th><th>จำนวนเวลาที่เช่า</th><th>GB</th><th>ราคา</th></tr></thead><tbody>${a.map(x=>`<tr><td><span class="tag ${x.activity_type==='trial'?'trial-tag':'purchase-tag'}">${x.activity_type==='trial'?'ทดลองใช้งาน':'ซื้อ'}</span></td><td>${esc(x.created_at)}</td><td>${esc(x.username?'@'+x.username:x.user_id)}</td><td>${esc(x.code_name)}</td><td><span class="tag">${esc(x.network)}</span></td><td>${esc(x.duration_text||((x.days||0)+' วัน'))}</td><td>${x.gb||'∞'}</td><td>${x.activity_type==='trial'?'ฟรี':money(x.cost)}</td></tr>`).join('')}</tbody></table></div>`}function settingAction(title,description,handler){return `<div class="setting settings-action"><div><b>${title}</b><small>${description}</small></div><button class="btn" onclick="${handler}()">ตั้งค่า</button></div>`}function actionsForSettingGroup(group){if(group==='การแสดงผล')return settingAction('คำสั่งที่ใช้แล้วให้เด้งเมนู /start','ตั้งค่าในหน้าเฉพาะแบบเต็มจอ','commandSettingsPage');if(group==='ซื้อ VPN')return settingAction('ช่องทางการซื้อ','เลือก DM กลุ่ม หรือกลุ่มที่กำหนด','buyChannelPage')+settingAction('กลุ่มที่ซื้อได้','ตั้งค่า Group ID ในหน้าเฉพาะ','buyGroupsPage');if(group==='ทดลองใช้ฟรี')return settingAction('โหมดรีเซ็ตการทดลองใช้','เลือก 00:00 พร้อมกัน หรือครบเวลาตามการสร้างไฟล์','freeResetModePage')+settingAction('ช่องทางการทดลองใช้งาน','เลือก DM กลุ่ม หรือกลุ่มที่กำหนด','freeTrialChannelPage')+settingAction('กลุ่มทดลองใช้ฟรี','กำหนด Group ID ทีละบรรทัดหรือคั่นด้วย comma','freeTrialGroupsPage');if(group==='Money')return settingAction('จัดการโค้ดรับเครดิต','สร้าง ดู แก้ไข และลบโค้ด','creditCodes');return ''}async function settings(){leaveFocus();let d=await api('/api/settings'),groups={};d.items.forEach(x=>(groups[x.group]??=[]).push(x));let actionKeys=new Set(['run_start_finish_commands','buy_channel_mode','buy_group_ids','freeclient_reset_mode','freeclient_channel_mode','freeclient_group_ids']);$('#page').innerHTML=Object.entries(groups).map(([g,a])=>`<div class="panel"><div class="panel-head"><h2>${esc(g)}</h2></div><div class="content grid">${a.filter(x=>!actionKeys.has(x.key)).map(settingRow).join('')}${actionsForSettingGroup(g)}</div></div>`).join('')}
function settingRow(x){if(x.type==='bool')return `<div class="setting"><div><b>${esc(x.label)}</b><small>${esc(x.key)}</small></div><button class="switch ${x.value==='1'?'on':''}" aria-label="${esc(x.label)}" onclick="setSetting(decodeURIComponent('${enc(x.key)}'),${x.value!=='1'})"></button>`+`</div>`;if(x.key==='run_start_finish_commands')return `<div class="setting"><div><b>${esc(x.label)}</b><small>ใส่คำสั่งทีละบรรทัด หรือคั่นด้วย comma เช่น test, fast</small></div><button class="btn" onclick="commandSettingsPage()">แก้ไขรายการ</button></div>`;return `<div class="setting"><div><b>${esc(x.label)}</b><small>${esc(x.key)}</small></div><button class="btn" onclick="editSetting(decodeURIComponent('${enc(x.key)}'),decodeURIComponent('${enc(x.label)}'),decodeURIComponent('${enc(x.value)}'))">${esc(x.value)||'ตั้งค่า'}</button></div>`}
async function setSetting(k,v){await api('/api/settings/'+k,{method:'PUT',body:JSON.stringify({value:v})});toast('บันทึกแล้ว — บอทใช้ค่าใหม่ทันที');settings()}function editSetting(k,l,v){showModal(`<h2>${esc(l)}</h2><div class="field"><label>ค่าใหม่</label><input id="sv" value="${esc(v)}"></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveSetting('${k}')">บันทึก</button></div>`)}async function saveSetting(k){await api('/api/settings/'+k,{method:'PUT',body:JSON.stringify({value:$('#sv').value})});closeModal();toast('บันทึกแล้ว');settings()}
function enterFocus(title,content){document.querySelector('.app').classList.add('focus-mode');$('#title').textContent=title;$('#page').innerHTML=`<div class="focus-head"><button class="btn" onclick="settings()">← กลับไปตั้งค่าบอท</button><h1>${esc(title)}</h1></div>${content}`;window.scrollTo(0,0)}function leaveFocus(){document.querySelector('.app').classList.remove('focus-mode')}
async function commandSettingsPage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='run_start_finish_commands'),lines=String(x?.value||'').replaceAll(',',"\n").split(/\n+/).map(a=>a.trim()).filter(Boolean).join('\n');enterFocus('คำสั่งที่ใช้แล้วให้เด้งเมนู /start',`<div class="focus-card"><p class="modal-note">ใส่คำสั่งทีละบรรทัด หรือคั่นด้วย comma ได้</p><div class="field"><label>รายการคำสั่ง</label><textarea id="commandList" rows="16" placeholder="test\nfast">${esc(lines)}</textarea></div><div class="focus-actions"><button class="btn" onclick="settings()">ยกเลิก</button><button class="btn primary" onclick="saveCommandSettings()">บันทึก</button></div></div>`)}async function saveCommandSettings(){await api('/api/settings/run_start_finish_commands',{method:'PUT',body:JSON.stringify({value:$('#commandList').value})});toast('บันทึกรายการคำสั่งแล้ว');settings()}
async function freeTrialGroupsPage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='freeclient_group_ids'),lines=String(x?.value||'').replaceAll(',',"\n").split(/\n+/).map(a=>a.trim()).filter(Boolean).join('\n');enterFocus('กลุ่มทดลองใช้ฟรี',`<div class="focus-card"><p class="modal-note">ใส่ Group ID ทีละบรรทัด หรือคั่นด้วย comma ได้ หากไม่กำหนด ระบบจะใช้ตามโหมดช่องทางที่ตั้งไว้</p><div class="field"><label>Group ID ที่อนุญาต</label><textarea id="freeGroupList" rows="16" inputmode="numeric" placeholder="-1001234567890\n-1009876543210">${esc(lines)}</textarea></div><div class="focus-actions"><button class="btn" onclick="settings()">ยกเลิก</button><button class="btn primary" onclick="saveFreeTrialGroups()">บันทึก</button></div></div>`)}async function saveFreeTrialGroups(){await api('/api/settings/freeclient_group_ids',{method:'PUT',body:JSON.stringify({value:$('#freeGroupList').value})});toast('บันทึกกลุ่มทดลองใช้ฟรีแล้ว');settings()}
async function buyChannelPage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='buy_channel_mode'),mode=x?.value||'group_only';let options=[['dm_and_group','DM และกลุ่มทั้งหมด'],['dm_only','DM เท่านั้น'],['group_only','กลุ่มทั้งหมดเท่านั้น'],['specified_and_dm','กลุ่มที่กำหนด + DM'],['specified_only','กลุ่มที่กำหนดเท่านั้น']];enterFocus('ช่องทางการซื้อ',`<div class="focus-card"><p class="modal-note">เลือกช่องทางที่อนุญาตให้ซื้อ VPN</p><div class="restore-options">${options.map(([k,label])=>`<button class="restore-option" onclick="saveBuyChannel('${k}')"><b>${mode===k?'✅':'⬜'} ${label}</b></button>`).join('')}</div></div>`)}async function saveBuyChannel(mode){await api('/api/settings/buy_channel_mode',{method:'PUT',body:JSON.stringify({value:mode})});toast('บันทึกช่องทางการซื้อแล้ว');buyChannelPage()}
async function buyGroupsPage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='buy_group_ids'),lines=String(x?.value||'').replaceAll(',',"\n").split(/\n+/).map(a=>a.trim()).filter(Boolean).join('\n');enterFocus('กลุ่มที่ซื้อได้',`<div class="focus-card"><p class="modal-note">ถ้าไม่กำหนด ระบบอนุญาตให้ซื้อได้ทุกกลุ่มที่มีบอทอยู่ หากกำหนดแล้วจะอนุญาตเฉพาะ Group ID ที่ระบุ</p><div class="field"><label>Group ID ที่อนุญาต</label><textarea id="buyGroupList" rows="18" inputmode="numeric" placeholder="-1001234567890\n-1009876543210">${esc(lines)}</textarea></div><div class="focus-actions"><button class="btn" onclick="settings()">ยกเลิก</button><button class="btn primary" onclick="saveBuyGroups()">บันทึก</button></div></div>`)}async function saveBuyGroups(){await api('/api/settings/buy_group_ids',{method:'PUT',body:JSON.stringify({value:$('#buyGroupList').value})});toast('บันทึกกลุ่มที่ซื้อได้แล้ว');settings()}
async function freeResetModePage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='freeclient_reset_mode'),mode=x?.value||'midnight';enterFocus('โหมดรีเซ็ตการทดลองใช้',`<div class="focus-card"><p class="modal-note">เลือกวิธีเริ่มนับสิทธิ์ทดลองใช้ฟรีของผู้ใช้</p><div class="restore-options"><button class="restore-option" onclick="saveFreeResetMode('midnight')"><b>${mode==='midnight'?'✅':'⬜'} รีเซ็ต 00:00 พร้อมกันทุกคน</b><small>เริ่มรอบใหม่พร้อมกันทุกวัน</small></button><button class="restore-option" onclick="saveFreeResetMode('rolling_24h')"><b>${mode==='rolling_24h'?'✅':'⬜'} รีเซ็ตตามเวลาที่แต่ละคนสร้างไฟล์</b><small>ครบ 24 ชั่วโมงนับจากเวลาที่ใช้สิทธิ์</small></button></div></div>`)}async function saveFreeResetMode(mode){await api('/api/settings/freeclient_reset_mode',{method:'PUT',body:JSON.stringify({value:mode})});toast('บันทึกโหมดรีเซ็ตแล้ว');freeResetModePage()}
async function freeTrialChannelPage(){let d=await api('/api/settings'),x=d.items.find(a=>a.key==='freeclient_channel_mode'),mode=x?.value||'dm_and_group';let options=[['dm_and_group','DM และกลุ่มทั้งหมด'],['dm_only','DM เท่านั้น'],['group_only','กลุ่มทั้งหมดเท่านั้น'],['specified_and_dm','กลุ่มที่กำหนด + DM'],['specified_only','กลุ่มที่กำหนดเท่านั้น']];enterFocus('ช่องทางการทดลองใช้งาน',`<div class="focus-card"><p class="modal-note">เลือกช่องทางที่อนุญาตให้ใช้ทดลองใช้ฟรี</p><div class="restore-options">${options.map(([k,label])=>`<button class="restore-option" onclick="saveFreeTrialChannel('${k}')"><b>${mode===k?'✅':'⬜'} ${label}</b></button>`).join('')}</div></div>`)}async function saveFreeTrialChannel(mode){await api('/api/settings/freeclient_channel_mode',{method:'PUT',body:JSON.stringify({value:mode})});toast('บันทึกช่องทางทดลองใช้ฟรีแล้ว');freeTrialChannelPage()}
async function webAccountPage(){let d=await api('/api/web-account');enterFocus('บัญชีเว็บไซต์จัดการ',`<div class="focus-card"><p class="modal-note">หากไม่เคยตั้งค่า ค่าเริ่มต้นคือ admin / 123 แนะนำให้เปลี่ยนทันที</p><div class="form-grid"><div class="field full"><label>ชื่อผู้ใช้เว็บไซต์</label><input id="webUser" value="${esc(d.username||'admin')}" autocomplete="username"></div><div class="field full"><label>รหัสผ่านใหม่</label><input id="webPass" type="password" placeholder="เว้นว่างไว้หากไม่ต้องการเปลี่ยน" autocomplete="new-password"></div><div class="field full"><label>ยืนยันรหัสผ่านใหม่</label><input id="webPassConfirm" type="password" placeholder="กรอกรหัสผ่านเดิมอีกครั้ง" autocomplete="new-password"></div></div><div class="focus-actions"><button class="btn" onclick="settings()">ยกเลิก</button><button class="btn primary" onclick="saveWebAccount()">บันทึก</button></div></div>`)}async function saveWebAccount(){let username=$('#webUser').value.trim(),password=$('#webPass').value,confirmPassword=$('#webPassConfirm').value;if(!username)return toast('กรุณากรอกชื่อผู้ใช้');if(password!==confirmPassword)return toast('ยืนยันรหัสผ่านไม่ตรงกัน');let result=await api('/api/web-account',{method:'PUT',body:JSON.stringify({username,password})});alert(result.message||'บันทึกแล้ว');location.reload()}
async function networkPage(){let d=await api('/api/networks');networks=d.items;activeNetwork=null;$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>เครือข่าย VPN</h2><button class="btn primary" onclick="networkModal()">+ เพิ่มเครือข่าย</button></div>${networkTable(networks)}</div>`}function networkTable(a){if(!a.length)return '<div class="empty">ยังไม่มีเครือข่าย</div>';return `<div class="table-wrap"><table><thead><tr><th>ID Data</th><th>ประเภทเครือข่าย</th><th>ชื่อเครือข่าย</th><th>แพ็คเกจ</th><th>ลำดับ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>`<tr><td>${x.id}</td><td>${x.network_type==='trial'?'สินค้าเครือข่าย VPN ทดลอง':'สินค้าเครือข่าย VPN ซื้อ'}</td><td><button class="btn" onclick="packagePage(${x.id})"><b>${esc(x.name)}</b></button></td><td>${x.active_package_count||0} เปิด / ${x.package_count||0} ทั้งหมด</td><td>${x.sort_order}</td><td><span class="tag ${x.active?'on':''}">${x.active?'เปิด':'ปิด'}</span></td><td><button class="btn primary" onclick="packagePage(${x.id})">จัดการแพ็คเกจ</button> <button class="btn" onclick="networkModal(${x.id})">แก้ไข</button> <button class="btn danger" onclick="removeNetwork(${x.id})">ลบ</button></td></tr>`).join('')}</tbody></table></div>`}function networkModal(id){let x=networks.find(n=>n.id===id)||{code:'',name:'',network_type:'purchase',inbound_id:1,active:true,sort_order:0};showModal(`<h2>${id?'แก้ไข':'เพิ่ม'}เครือข่าย</h2><div class="form-grid"><div class="field"><label>ID Data</label><input id="nc" maxlength="24" value="${esc(x.code)}" autocomplete="off"></div><div class="field"><label>ชื่อที่แสดง</label><input id="nn" value="${esc(x.name)}" autocomplete="off"></div><div class="field"><label>ประเภทเครือข่าย</label><select id="nt"><option value="purchase" ${x.network_type==='purchase'?'selected':''}>สินค้าเครือข่าย VPN ซื้อ</option><option value="trial" ${x.network_type==='trial'?'selected':''}>สินค้าเครือข่าย VPN ทดลอง</option></select></div><div class="field"><label>ลำดับ (0 = อัตโนมัติ)</label><input id="ns" type="number" min="0" value="${x.sort_order}"></div><div class="field"><label>สถานะ</label><select id="na"><option value="1" ${x.active?'selected':''}>เปิด</option><option value="0" ${!x.active?'selected':''}>ปิด</option></select></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button id="networkSave" class="btn primary" onclick="saveNetwork(${id||0})">บันทึก</button></div>`)}async function saveNetwork(id){let button=$('#networkSave');try{let code=$('#nc').value.trim().toUpperCase(),name=$('#nn').value.trim(),order=Number($('#ns').value||0),network_type=$('#nt').value,inbound_id=Number(networks.find(n=>n.id===id)?.inbound_id||1);if(!/^[A-Z0-9_-]{1,24}$/.test(code))throw Error('กรุณากรอก ID Data เช่น AIS');if(!name)throw Error('กรุณากรอกชื่อที่แสดง');if(!Number.isInteger(order)||order<0)throw Error('ลำดับต้องเป็นเลข 0 ขึ้นไป');button.disabled=true;await api('/api/networks'+(id?'/'+id:''),{method:id?'PUT':'POST',body:JSON.stringify({network_type,code,name,inbound_id,active:$('#na').value==='1',sort_order:order})});closeModal();toast(id?'แก้ไขเครือข่ายแล้ว':'เพิ่มเครือข่ายแล้ว');await networkPage()}catch(e){toast(e.message||'บันทึกเครือข่ายไม่สำเร็จ')}finally{if(button)button.disabled=false}}async function removeNetwork(id){try{if(!confirm('ลบเครือข่ายและแพ็กเกจทั้งหมดภายใน? ระบบจะจัดลำดับใหม่อัตโนมัติ'))return;await api('/api/networks/'+id,{method:'DELETE'});toast('ลบและจัดลำดับใหม่แล้ว');await networkPage()}catch(e){toast(e.message||'ลบเครือข่ายไม่สำเร็จ')}}
async function packagePage(networkId){let d=await api(`/api/networks/${networkId}/packages`);activeNetwork=d.network;packages=d.items;let trial=activeNetwork.network_type==='trial';$('#title').textContent=`${trial?'แพ็คเกจทดลอง':'แพ็คเกจ'} • ${activeNetwork.name}`;$('#page').innerHTML=`<div class="panel"><div class="panel-head"><div><button class="btn" onclick="networkPage();$('#title').textContent='จัดการเครือข่าย'">← เครือข่าย</button> <b style="margin-left:10px">${esc(activeNetwork.name)}</b></div><button class="btn primary" onclick="packageModal()">+ เพิ่มแพ็คเกจ</button></div>${trial?'<div class="content"><p class="modal-note">ระบบทดลองใช้ฟรีใช้แพ็คเกจทดลองที่เปิดใช้งานและอยู่ลำดับแรก เพื่อกำหนด Inbound, GB และ Template</p></div>':''}${packageTable(packages)}</div>`}
function packageTable(a){let trial=activeNetwork?.network_type==='trial';if(!a.length)return `<div class="empty"><b>ยังไม่มีรายการแพ็คเกจ</b><br><small>${trial?'เพิ่มแพ็คเกจทดลองเพื่อกำหนด Inbound, GB และ Template':'เพิ่มแพ็คเกจแรกเพื่อให้ผู้ใช้เลือกในบอท'}</small></div>`;return `<div class="table-wrap"><table><thead><tr><th>แพ็คเกจ</th><th>Inbound ID (3x-ui)</th>${trial?'':'<th>ราคา/วัน</th>'}<th>Template</th><th>ลำดับ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>{let tpl=[x.template_address,x.template_port?('Port: '+x.template_port):'',x.template_network,x.template_host,x.template_path].filter(Boolean).join(' • ');return `<tr><td><b>${esc(x.name)}</b><br><small>${esc(x.description)||'—'}</small></td><td>${x.inbound_id}</td>${trial?'':`<td>${money(x.price_per_day)} เครดิต</td>`}<td>${x.template_enabled?`<span class="tag on">เปิด</span><br><small>${esc(tpl||'ไม่ได้กำหนดค่า')}</small>`:'<span class="tag">ปิด</span>'}</td><td>${x.sort_order}</td><td><span class="tag ${x.active?'on':''}">${x.active?'เปิด':'ปิด'}</span></td><td><button class="btn" onclick="packageModal(${x.id})">แก้ไข</button> <button class="btn danger" onclick="removePackage(${x.id})">ลบ</button></td></tr>`}).join('')}</tbody></table></div>`}function packageModal(id){let x=packages.find(p=>p.id===id)||{name:'',description:'',inbound_id:'',price_per_day:'',gb_limit:0,gb_max:0,active:true,sort_order:0,template_enabled:false,template_address:'',template_port:0,template_network:'',template_host:'',template_path:''};let gbMode=Number(x.gb_limit||0)>0?'fixed':'custom';showModal(`<h2>${id?'แก้ไข':'เพิ่ม'}แพ็กเกจ • ${esc(activeNetwork.name)}</h2><div class="form-grid"><div class="field full"><label>ชื่อแพ็กเกจ</label><input id="pkname" value="${esc(x.name)}" placeholder="เช่น โปรกันรั่ว SSH"></div><div class="field full"><label>รายละเอียดแพ็กเกจ</label><textarea id="pkdesc" rows="10" maxlength="500" placeholder="อธิบายสิทธิ์การใช้งาน เงื่อนไข หรือข้อจำกัดของแพ็กเกจ">${esc(x.description)}</textarea><small>แนะนำให้เขียนเป็นข้อ ๆ ได้ สูงสุด 500 ตัวอักษร</small></div><div class="field"><label>Inbound ID ใน 3x-ui</label><input id="pkinbound" type="number" min="1" value="${x.inbound_id}" placeholder="เช่น 1"></div>${activeNetwork.network_type==='trial'?'':`<div class="field"><label>ราคาต่อวัน (เครดิต)</label><input id="pkprice" type="number" min="0.01" step="0.01" value="${x.price_per_day}"></div>`}<div class="field full"><label>การกำหนดจำนวน GB</label><div class="gb-choice"><label class="gb-option"><input type="radio" name="gbMode" value="fixed" ${gbMode==='fixed'?'checked':''} onchange="toggleGbMode()"><span><b>กำหนดจำนวน GB</b><small>ใช้ค่า GB นี้ทันที ไม่ถามผู้ใช้ซ้ำ</small></span></label><label class="gb-option"><input type="radio" name="gbMode" value="custom" ${gbMode==='custom'?'checked':''} onchange="toggleGbMode()"><span><b>ให้ผู้ใช้กำหนด</b><small>ผู้ใช้กรอก GB เอง และกำหนดเพดานได้</small></span></label></div></div><div class="field" id="fixedGbField"><label>จำนวน GB ที่กำหนด</label><input id="pkgb" type="number" min="0.01" step="0.01" value="${Number(x.gb_limit||0)>0?x.gb_limit:''}" placeholder="เช่น 5"><small>ต้องมากกว่า 0</small></div><div class="field hidden" id="maxGbField"><label>จำนวน GB สูงสุดที่ผู้ใช้ตั้งได้</label><input id="pkgbmax" type="number" min="0" step="0.01" value="${x.gb_max||0}" placeholder="0 = ไม่จำกัด"><small>ใช้เฉพาะโหมดให้ผู้ใช้กำหนด; ใส่ 0 เพื่อไม่จำกัด</small></div><div class="field"><label>ลำดับ (0 = อัตโนมัติ)</label><input id="pkorder" type="number" min="0" value="${x.sort_order}"></div><div class="field"><label>สถานะ</label><select id="pkactive"><option value="1" ${x.active?'selected':''}>เปิด</option><option value="0" ${!x.active?'selected':''}>ปิด</option></select></div><div class="field full"><label><input id="pktplenabled" type="checkbox" style="width:auto;margin-right:8px" ${x.template_enabled?'checked':''} onchange="togglePackageTemplate()"> เปิดใช้ Template เพื่อปรับลิงก์ก่อนส่งให้ผู้ใช้</label><small>ปิด Template ระบบจะไม่ใช้ค่า Template; เมื่อเปิดใช้ ช่อง Address/Network/Host/Path ที่เว้นว่างจะคงค่าจากลิงก์เดิม ส่วน Port ที่เว้นว่างจะใช้พอร์ตจาก Inbound</small></div><div id="packageTemplateFields" class="form-grid ${x.template_enabled?'':'hidden'}"><div class="field full"><label>Address / Server (โดเมนหรือ IP)</label><input id="pktpladdress" value="${esc(x.template_address||'')}" maxlength="253" placeholder="vpn.example.com หรือ 203.0.113.10"><small>แทนเฉพาะที่อยู่เซิร์ฟเวอร์ หากไม่กำหนด Port จะใช้พอร์ตจาก Inbound (IPv6 ให้ใส่ในวงเล็บเหลี่ยม เช่น [2001:db8::1])</small></div><div class="field"><label>Port (พอร์ตปลายทาง)</label><input id="pktplport" type="number" min="1" max="65535" step="1" value="${x.template_port||''}" placeholder="เว้นว่าง = ใช้พอร์ต Inbound"><small>กำหนดพอร์ตของลิงก์ VPN ได้เอง 1–65535; เว้นว่างเพื่อใช้พอร์ตจาก Inbound ที่เลือก</small></div><div class="field full"><label>Protocol / Network (ชนิด Transport)</label><input id="pktplnetwork" list="packageNetworkChoices" value="${esc(x.template_network||'')}" maxlength="32" placeholder="เช่น ws, tcp, grpc, httpupgrade"><datalist id="packageNetworkChoices"><option value="tcp"><option value="raw"><option value="ws"><option value="grpc"><option value="http"><option value="kcp"><option value="quic"><option value="domainsocket"><option value="httpupgrade"><option value="splithttp"><option value="xhttp"></datalist><small>เลือกค่าที่แนะนำหรือพิมพ์ค่าอื่นเองได้ เช่น tcp, ws, grpc, kcp, quic, httpupgrade, splithttp</small></div><div class="field"><label>Host</label><input id="pktplhost" value="${esc(x.template_host||'')}" maxlength="512" placeholder="cdn.example.com"><small>เช่น Host header สำหรับ WS</small></div><div class="field"><label>Path</label><input id="pktplpath" value="${esc(x.template_path||'')}" maxlength="2048" placeholder="/ws หรือ /connect"><small>สำหรับ gRPC จะใส่เป็น serviceName</small></div></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button id="packageSave" class="btn primary" onclick="savePackage(${id||0})">บันทึก</button></div>`);toggleGbMode();togglePackageTemplate()}
function toggleGbMode(){let mode=document.querySelector('input[name="gbMode"]:checked')?.value||'custom';$('#fixedGbField')?.classList.toggle('hidden',mode!=='fixed');$('#maxGbField')?.classList.toggle('hidden',mode!=='custom')}
function togglePackageTemplate(){$('#packageTemplateFields')?.classList.toggle('hidden',!$('#pktplenabled')?.checked)}
async function savePackage(id){let button=$('#packageSave');try{let trial=activeNetwork.network_type==='trial',name=$('#pkname').value.trim(),description=$('#pkdesc').value.trim(),inbound=Number($('#pkinbound').value),price=trial?0.01:Number($('#pkprice')?.value),mode=document.querySelector('input[name="gbMode"]:checked')?.value,gb_limit=mode==='fixed'?Number($('#pkgb').value||0):0,gb_max=mode==='custom'?Number($('#pkgbmax').value||0):0,order=Number($('#pkorder').value||0),template_enabled=$('#pktplenabled').checked,template_address=$('#pktpladdress').value.trim(),template_port_text=$('#pktplport').value.trim(),template_port=template_port_text?Number(template_port_text):null,template_network=$('#pktplnetwork').value.trim(),template_host=$('#pktplhost').value.trim(),template_path=$('#pktplpath').value.trim();if(!name)throw Error('กรุณากรอกชื่อแพ็กเกจ');if(!Number.isInteger(inbound)||inbound<1)throw Error('Inbound ID ต้องเป็นเลขตั้งแต่ 1');if(!trial&&!(price>0))throw Error('ราคาต่อวันต้องมากกว่า 0');if(mode==='fixed'&&!(gb_limit>0))throw Error('กรุณาระบุจำนวน GB ที่กำหนดมากกว่า 0');if(mode==='custom'&&gb_max<0)throw Error('GB สูงสุดต้องเป็น 0 ขึ้นไป');if(!Number.isInteger(order)||order<0)throw Error('ลำดับต้องเป็นเลข 0 ขึ้นไป');if(template_port!==null&&(!Number.isInteger(template_port)||template_port<1||template_port>65535))throw Error('Template Port ต้องเป็นเลขจำนวนเต็ม 1–65535');if(template_enabled&&![template_address,template_port,template_network,template_host,template_path].some(v=>v!==null&&v!==''))throw Error('กรุณากำหนดค่า Template อย่างน้อยหนึ่งช่อง');button.disabled=true;await api(id?`/api/packages/${id}`:`/api/networks/${activeNetwork.id}/packages`,{method:id?'PUT':'POST',body:JSON.stringify({gb_mode:mode,name,description,inbound_id:inbound,price_per_day:price,gb_limit,gb_max,active:$('#pkactive').value==='1',sort_order:order,template_enabled,template_address,template_port,template_network,template_host,template_path})});closeModal();toast('บันทึกแพ็กเกจแล้ว');await packagePage(activeNetwork.id)}catch(e){toast(e.message||'บันทึกแพ็กเกจไม่สำเร็จ')}finally{if(button)button.disabled=false}}
async function removePackage(id){try{if(!confirm('ลบแพ็กเกจนี้? ระบบจะจัดลำดับใหม่อัตโนมัติ'))return;await api(`/api/packages/${id}`,{method:'DELETE'});toast('ลบและจัดลำดับใหม่แล้ว');await packagePage(activeNetwork.id)}catch(e){toast(e.message||'ลบแพ็กเกจไม่สำเร็จ')}}
async function users(){let query=($('#uq')?.value||'').trim();let d=await api('/api/users?q='+encodeURIComponent(query));$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>ผู้ใช้</h2><input class="search" id="uq" value="${esc(query)}" placeholder="ค้นหา ID หรือ username" onkeydown="if(event.key==='Enter')users()"></div><div class="table-wrap"><table><thead><tr><th>User ID</th><th>Username</th><th>เครดิต</th><th>เริ่ม DM</th><th></th></tr></thead><tbody>${d.items.map(x=>`<tr><td>${x.user_id}</td><td>${esc(x.username?'@'+x.username:'—')}</td><td><b>${money(x.credit)}</b></td><td>${x.dm_started?'ใช่':'ไม่'}</td><td><button class="btn" onclick="credit(${x.user_id})">ปรับเครดิต</button></td></tr>`).join('')}</tbody></table></div></div>`}function credit(id){showModal(`<h2>ปรับเครดิต User ${id}</h2><div class="field"><label>จำนวน (+ เพิ่ม / - ลด)</label><input id="ca" type="number" step=".01" value="0"></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveCredit(${id})">บันทึก</button></div>`)}async function saveCredit(id){await api(`/api/users/${id}/credit`,{method:'POST',body:JSON.stringify({amount:+ca.value})});closeModal();toast('ปรับเครดิตแล้ว');users()}
async function orderPage(){let d=await api('/api/orders');$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>รายการสั่งซื้อ</h2></div>${ordersTable(d.items)}</div>`}function creditCodePanel(a){return `<div class="panel"><div class="panel-head"><h2>จัดการโค้ดรับเครดิต</h2><button class="btn primary" onclick="creditCodeModal()">+ สร้างโค้ดรับเครดิต</button></div>${creditCodeTable(a)}</div>`}async function creditCodes(){let d=await api('/api/credit-codes');creditCodeItems=d.items;enterFocus('จัดการโค้ดรับเครดิต',creditCodePanel(d.items))}function creditCodeTable(a){if(!a.length)return '<div class="empty">ยังไม่มีโค้ดในระบบ</div>';return `<div class="table-wrap"><table><thead><tr><th>ชื่อโค้ด</th><th>ประเภท</th><th>รายละเอียด</th><th>หมดอายุ</th><th>สถานะ</th><th></th></tr></thead><tbody>${a.map(x=>`<tr><td><b>${esc(x.code_name)}</b></td><td>${x.mode==='free_reset'?'รีเซ็ตทดลอง':x.mode==='random'?'สุ่มเครดิต':'เครดิตเท่ากัน'}</td><td>${x.mode==='fixed'?money(x.fixed_credit)+' / คน':x.mode==='random'?money(x.total_credit)+' รวม':'รีเซ็ตสิทธิ์'}</td><td>${formatCreditCodeExpiry(x.expires_at)}</td><td>${x.active?'เปิด':'ปิด'}</td><td><button class="btn" onclick="creditCodeModal('${esc(x.code_key)}')">แก้ไข</button> <button class="btn danger" onclick="removeCreditCode('${esc(x.code_key)}')">ลบ</button></td></tr>`).join('')}</tbody></table></div>`}function formatCreditCodeExpiry(value){let d=new Date(value);if(!Number.isFinite(d.getTime()))return String(value||'—');return new Intl.DateTimeFormat('th-TH',{timeZone:'Asia/Bangkok',dateStyle:'long',timeStyle:'short',hourCycle:'h23'}).format(d)}function creditCodeExpiryParts(value){if(!value)return {date:'',time:''};let d=new Date(value);if(!Number.isFinite(d.getTime()))return {date:'',time:''};let dateParts=Object.fromEntries(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Bangkok',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(d).map(x=>[x.type,x.value]));let timeParts=Object.fromEntries(new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Bangkok',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(d).map(x=>[x.type,x.value]));return {date:`${dateParts.year}-${dateParts.month}-${dateParts.day}`,time:`${timeParts.hour}:${timeParts.minute}`}}function creditCodeModal(key){let x=(creditCodeItems||[]).find(a=>a.code_key===key)||{code_name:'',mode:'fixed',fixed_credit:1,total_credit:0,max_uses:0,expires_at:'',active:true},expiry=creditCodeExpiryParts(x.expires_at);showModal(`<h2>${key?'แก้ไข':'สร้าง'}โค้ดรับเครดิต</h2><div class="form-grid"><div class="field full"><label>ชื่อโค้ด</label><input id="ccname" value="${esc(x.code_name)}" ${key?'readonly':''}></div><div class="field"><label>ประเภท</label><select id="ccmode"><option value="fixed" ${x.mode==='fixed'?'selected':''}>เครดิตเท่ากันทุกคน</option><option value="random" ${x.mode==='random'?'selected':''}>สุ่มเครดิต</option><option value="free_reset" ${x.mode==='free_reset'?'selected':''}>รีเซ็ตทดลอง</option></select></div><div class="field"><label>เครดิตต่อคน</label><input id="ccfixed" type="number" min="0" step="0.01" value="${x.fixed_credit||0}"></div><div class="field"><label>เครดิตรวม (สุ่ม)</label><input id="cctotal" type="number" min="0" step="0.01" value="${x.total_credit||0}"></div><div class="field"><label>จำนวนผู้ใช้สูงสุด (0=ไม่จำกัด)</label><input id="ccmax" type="number" min="0" value="${x.max_uses||0}"></div><div class="field"><label>วันที่หมดอายุ</label><input id="ccexpDate" type="date" lang="th" value="${expiry.date}" required></div><div class="field"><label>เวลาไทย (24 ชั่วโมง, UTC+7)</label><input id="ccexpTime" type="time" lang="th" step="60" value="${expiry.time}" required></div><div class="field full"><small>เลือกวันที่จากปฏิทินและเลือกเวลาจากนาฬิกา ระบบจะบันทึกเป็นเวลาไทยโดยอัตโนมัติ</small></div></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button class="btn primary" onclick="saveCreditCode('${key||''}')">บันทึก</button></div>`)}async function saveCreditCode(key){let date=$('#ccexpDate').value,time=$('#ccexpTime').value;if(!date||!time)return toast('กรุณาเลือกวันที่และเวลาหมดอายุ');let body={code_name:ccname.value.trim(),mode:ccmode.value,fixed_credit:+ccfixed.value,total_credit:+cctotal.value,max_uses:+ccmax.value,expires_at:`${date}T${time}:00+07:00`,active:true};try{await api(key?`/api/credit-codes/${encodeURIComponent(key)}`:'/api/credit-codes',{method:key?'PUT':'POST',body:JSON.stringify(body)});closeModal();toast('บันทึกโค้ดแล้ว');creditCodes()}catch(e){toast(e.message||'บันทึกโค้ดไม่สำเร็จ')}}async function removeCreditCode(key){if(!confirm('ยืนยันลบโค้ดนี้?'))return;try{await api('/api/credit-codes/'+encodeURIComponent(key),{method:'DELETE'});toast('ลบโค้ดแล้ว');creditCodes()}catch(e){toast(e.message||'ลบไม่สำเร็จ')}}async function audit(){let d=await api('/api/audit');$('#page').innerHTML=`<div class="panel"><div class="panel-head"><h2>ประวัติการแก้ไข</h2></div><div class="table-wrap"><table><thead><tr><th>เวลา</th><th>ช่องทาง</th><th>การทำงาน</th><th>เป้าหมาย</th><th>ค่าใหม่</th></tr></thead><tbody>${d.items.map(x=>`<tr><td>${esc(x.created_at)}</td><td><span class="tag">${esc(x.actor)}</span></td><td>${esc(x.action)}</td><td>${esc(x.target)}</td><td>${esc(x.new_value).slice(0,80)}</td></tr>`).join('')}</tbody></table></div></div>`}
async function databaseBackendPage(){let d=await api('/api/database-backend');let backend=d.backend||'internal';enterFocus('เปลี่ยนฐานข้อมูล',`<div class="focus-card"><p class="modal-note">เลือกแหล่งจัดเก็บข้อมูลหลัก ค่าเริ่มต้นคือ <b>ในระบบ</b> เมื่อเชื่อมต่อ ระบบจะดึงข้อมูลจากฐานข้อมูลที่มีอยู่มาใช้ก่อน หากต้องการล้างข้อมูลเดิมให้เลือกรีเซ็ต</p><div class="field"><label>ฐานข้อมูล</label><select id="databaseBackend" onchange="toggleDatabaseBackendFields()"><option value="internal" ${backend==='internal'?'selected':''}>SQL Lite</option><option value="google_sheet" ${backend==='google_sheet'?'selected':''}>Google Sheet</option></select></div><div id="googleBackendFields" class="form-grid ${backend==='google_sheet'?'':'hidden'}"><div class="field full"><label>Google Sheet ID</label><input id="googleSheetId" value="${esc(d.google_sheet_id||'')}" placeholder="เช่น 1AbC..."></div><div class="field"><label>คำนำหน้าชีต</label><input id="googleSheetTab" value="${esc(d.google_sheet_tab||'BioShop')}" placeholder="BioShop"></div><div class="field full"><label>Google Service Account JSON</label><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px"><input id="credentialFile" type="file" class="hidden" onchange="loadGoogleCredentialsFile(this.files[0])"><button type="button" class="btn" onclick="$('#credentialFile').click()">📁 อัปโหลดไฟล์</button><small id="credentialsFileName">รองรับทุกนามสกุลที่มีเนื้อหาเป็น JSON</small></div><textarea id="googleCredentials" rows="8" placeholder="วาง JSON ของ Service Account ที่นี่ หรือเลือกไฟล์ JSON">${esc(d.google_service_account_json||'')}</textarea><small>${d.google_credentials_source==='environment'?'กำลังใช้ค่าจาก Environment Variables ซึ่งมีลำดับความสำคัญเหนือไฟล์อัปโหลด':d.google_credentials_source==='website'?'มีข้อมูลที่บันทึกจากเว็บไซต์แล้ว หากไม่ต้องการเปลี่ยนให้เว้นว่างไว้':'ยังไม่ได้ตั้งค่าข้อมูล Service Account'} • ไฟล์ต้องมีเนื้อหา JSON ของ Google Service Account</small></div></div><div class="field"><label><input id="resetExisting" type="checkbox"> รีเซ็ตข้อมูลเดิมก่อนเชื่อมต่อ (ข้อมูลเดิมจะหาย หลังจากระบบสำรองไว้แล้ว)</label></div><div class="actions"><button class="btn" onclick="backupPage()">ยกเลิก</button><button class="btn primary" onclick="saveDatabaseBackend()">เชื่อมต่อและดึงข้อมูล</button></div></div>`)}function toggleDatabaseBackendFields(){let type=$('#databaseBackend')?.value;$('#googleBackendFields')?.classList.toggle('hidden',type!=='google_sheet')}function loadGoogleCredentialsFile(file){if(!file)return;if(file.size>20000){toast('ไฟล์ใหญ่เกิน 20 KB');return}let reader=new FileReader();reader.onload=()=>{try{let raw=String(reader.result||''),value=JSON.parse(raw);if(!value||typeof value!=='object'||Array.isArray(value)||!value.client_email||!value.private_key)throw Error();$('#googleCredentials').value=raw;$('#credentialsFileName').textContent=`เลือกแล้ว: ${file.name}`;toast('อ่านไฟล์ Service Account แล้ว')}catch(e){toast('ไฟล์ต้องเป็น JSON ของ Google Service Account ที่มี client_email และ private_key')}};reader.onerror=()=>toast('อ่านไฟล์ไม่สำเร็จ');reader.readAsText(file)}async function saveDatabaseBackend(){let backend=$('#databaseBackend').value,id=$('#googleSheetId')?.value.trim()||'',tab=$('#googleSheetTab')?.value.trim()||'BioShop',credentials=$('#googleCredentials')?.value.trim()||'',resetExisting=$('#resetExisting')?.checked||false;if(backend==='google_sheet'&&!id)return toast('กรุณาระบุ Google Sheet ID');let message=resetExisting?'ยืนยันรีเซ็ตข้อมูลเดิม? ข้อมูลเดิมจะหายหลังการสำรอง':'ยืนยันเชื่อมต่อและดึงข้อมูลจากฐานข้อมูลที่มีอยู่?';if(!confirm(message))return;showWorkingOverlay('กำลังเปลี่ยนฐานข้อมูล...');try{await api('/api/database-backend',{method:'POST',body:JSON.stringify({backend,google_sheet_id:id,google_sheet_tab:tab,service_account_json:credentials,confirmed:true,reset_existing:resetExisting})});await resetDashboardToOverview(resetExisting?'รีเซ็ตและเปลี่ยนฐานข้อมูลแล้ว':'เชื่อมต่อและดึงข้อมูลแล้ว')}catch(e){toast(e.message||'เปลี่ยนฐานข้อมูลไม่สำเร็จ')}finally{hideWorkingOverlay()}}async function syncGoogleSheetsNow(){if(!confirm('ยืนยันซิงข้อมูลจาก Google Sheets เข้าสู่เว็บไซต์? ข้อมูลในเว็บไซต์จะถูกปรับให้ตรงกับข้อมูลในชีต และระบบจะสำรองข้อมูลเดิมไว้ก่อน'))return;showWorkingOverlay('กำลังซิงข้อมูลจาก Google Sheets...');try{let r=await api('/api/database-sync',{method:'POST'});await resetDashboardToOverview(`ซิงข้อมูล Sheets สำเร็จ ${r.rows||0} แถว`)}catch(e){toast(e.message||'ซิงข้อมูล Sheets ไม่สำเร็จ')}finally{hideWorkingOverlay()}}
async function backupPage(){let d=await api('/api/database-backend');let isSheet=d.backend==='google_sheet';let sheetReady=!!d.google_sheet_id&&!!d.google_configured;let backupTitle=isSheet?'ดาวน์โหลดฐานข้อมูล Google Sheets':'ดาวน์โหลดฐานข้อมูล SQLite .db';let backupDescription=isSheet?'ดึงข้อมูลล่าสุดจาก Google Sheets แล้วสร้างไฟล์ฐานข้อมูล .db สำหรับเก็บสำรอง':'ดาวน์โหลดฐานข้อมูล SQLite .db รวมผู้ใช้ เครดิต การตั้งค่า เครือข่าย แพ็กเกจ และ Template';let restoreDescription=isSheet?'อัปโหลดไฟล์ .db แล้วเลือกแทนที่หรือรวมข้อมูล โดยผลลัพธ์จะถูกบันทึกลง Google Sheets และซิงก์ฐานข้อมูลที่เว็บไซต์ใช้งาน':'อัปโหลดไฟล์ .db แล้วเลือกแทนที่หรือรวมข้อมูลในฐานข้อมูล SQLite';let sheetCard=isSheet?`<div class="card action-card"><div class="action-icon blue">☁</div><div><div class="label">Google Sheets</div><div class="value compact">ซิงข้อมูล Sheets</div><p><b>ฐานข้อมูลที่เลือก: Google Sheets</b><br><small>ชีต: ${esc(d.google_sheet_tab||'BioShop')} • ID: ${esc(d.google_sheet_id||'')}</small></p>${sheetReady?`<button class="btn primary" onclick="syncGoogleSheetsNow()">ซิงข้อมูล Sheets</button>`:`<button class="btn primary" disabled>ซิงข้อมูล Sheets</button> <button class="btn" onclick="databaseBackendPage()">ตั้งค่า Google Sheets</button><small class="modal-note">ต้องตั้งค่า Sheet ID และ Service Account ก่อนซิงข้อมูล</small>`}<small class="modal-note">ใช้เมื่อเว็บไซต์ยังไม่ได้ดึงข้อมูลล่าสุดจาก Google Sheets ปุ่มนี้จะดึงข้อมูลจาก Sheets เข้าสู่เว็บไซต์ ไม่เขียนทับชีตด้วยข้อมูลเก่าในเครื่อง</small></div></div>`:'';$('#page').innerHTML=`<div class="panel"><div class="panel-head"><div><h2>สำรอง/คืนค่าข้อมูล</h2><small class="modal-note">ฐานข้อมูลที่กำลังใช้งาน: <b>${isSheet?'Google Sheets':'SQLite'}</b></small></div><button class="btn primary" onclick="databaseBackendPage()">เปลี่ยนฐานข้อมูล</button></div><div class="content"><p class="modal-note">ค่า Template ทั้งหมดจะถูกเก็บและสำรองไปพร้อมกับข้อมูลแพ็กเกจในตาราง <b>network_packages</b> ได้แก่ เปิดใช้, Address, Port, Network, Host และ Path</p></div></div><div class="cards backup-grid"><div class="card action-card"><div class="action-icon blue">↓</div><div><div class="label">สำรองข้อมูลเป็นไฟล์</div><div class="value compact">${backupTitle}</div><p>${backupDescription}</p><button class="btn primary" onclick="downloadBackup()">ดาวน์โหลดไฟล์สำรอง .db</button></div></div><div class="card action-card"><div class="action-icon orange">↑</div><div><div class="label">คืนค่าจากไฟล์</div><div class="value compact">อัปโหลดไฟล์ .db</div><p>${restoreDescription}</p><input id="restoreFile" type="file" accept=".db,.sqlite,.sqlite3,application/x-sqlite3" class="hidden" onchange="selectRestoreFile(this.files[0])"><button class="btn" onclick="$('#restoreFile').click()">เลือกไฟล์ .db</button></div></div>${sheetCard}</div><div class="panel danger-zone" style="margin-top:18px"><div class="panel-head"><div><h2>ลบฐานข้อมูล</h2><small class="modal-note">ลบข้อมูลในฐานข้อมูลที่เลือกใช้อยู่ขณะนี้: <b>${isSheet?'Google Sheets':'SQLite'}</b></small></div><button class="btn danger" onclick="showDeleteDatabaseDialog('${isSheet?'google_sheet':'internal'}')">ลบฐานข้อมูล</button></div><div class="content"><p class="modal-note">ระบบจะลบข้อมูลผู้ใช้ เครดิต Client แพ็กเกจ ประวัติ และข้อมูลรายการในฐานข้อมูลชนิดที่เลือก เครือข่ายจะกลับเป็นค่าเริ่มต้น AIS/TRUE โดยเก็บการตั้งค่าระบบ บัญชีเว็บไซต์ และข้อมูลการเชื่อมต่อฐานข้อมูลไว้ พร้อมสร้างไฟล์สำรองก่อนลบ</p></div></div>`}
function downloadBackup(){location.href='/api/backup'}
function selectRestoreFile(file){if(!file)return;window.pendingRestoreFile=file;let dBackend=document.querySelector('#page')?.textContent.includes('Google Sheets')?'Google Sheets':'SQLite';showModal(`<h2>นำเข้าข้อมูล</h2><div class="restore-file"><b>${esc(file.name)}</b><small>${(file.size/1024/1024).toFixed(2)} MB</small></div><p class="modal-note">ข้อมูลจากไฟล์ .db จะถูกนำเข้าไปยังฐานข้อมูล ${dBackend} ที่เลือกใช้อยู่ ${dBackend==='Google Sheets'?'โดยอัปเดตชีตทุกตาราง และซิงก์ข้อมูลกลับมายังเว็บไซต์':''}</p><div class="restore-options"><button class="restore-option danger-zone" onclick="runRestore('replace')"><b>แทนที่ข้อมูล</b><small>ล้างข้อมูลเดิมแล้วใช้ข้อมูลจากไฟล์นี้ทั้งหมด</small></button><button class="restore-option" onclick="runRestore('merge')"><b>รวมข้อมูล</b><small>เพิ่มข้อมูลที่ยังไม่มี โดยคงข้อมูลเดิมไว้</small></button></div><div class="actions"><button class="btn" onclick="cancelRestore()">ยกเลิก</button></div>`)}
function cancelRestore(){window.pendingRestoreFile=null;closeModal();let f=$('#restoreFile');if(f)f.value=''}
async function runRestore(mode){let file=window.pendingRestoreFile;if(!file)return;let warning=mode==='replace'?'ยืนยันนำเข้าข้อมูลโดยแทนที่ข้อมูลปัจจุบัน?':'ยืนยันรวมข้อมูลจากไฟล์กับฐานข้อมูลที่ใช้อยู่?';if(!confirm(warning))return;let buttons=document.querySelectorAll('.restore-option');buttons.forEach(x=>x.disabled=true);showWorkingOverlay('กำลังคืนค่าฐานข้อมูลจากไฟล์...');try{await api('/api/restore?mode='+mode,{method:'POST',headers:{'content-type':'application/octet-stream'},body:file});await resetDashboardToOverview(mode==='merge'?'รวมข้อมูลสำเร็จ':'คืนค่าฐานข้อมูลสำเร็จ')}catch(e){toast(e.message||'นำเข้าข้อมูลไม่สำเร็จ');document.querySelectorAll('.restore-option').forEach(x=>x.disabled=false)}finally{hideWorkingOverlay()}}
function showDeleteDatabaseDialog(backend){window.databaseDeleteBackend=backend;let target=backend==='google_sheet'?'Google Sheets':'SQLite';showModal(`<h2>ยืนยันลบฐานข้อมูล</h2><p>การดำเนินการนี้จะลบข้อมูลทั้งหมดในฐานข้อมูล <b>${target}</b> ที่กำลังใช้งานอยู่</p><p class="modal-note">ระบบจะเก็บการตั้งค่าระบบและบัญชีเว็บไซต์ไว้ พร้อมสร้างไฟล์สำรองก่อนลบ แต่ข้อมูลผู้ใช้ เครดิต Client แพ็กเกจ และประวัติรายการจะถูกลบ ส่วนเครือข่ายจะกลับเป็นค่าเริ่มต้น AIS/TRUE</p><div class="field"><label>พิมพ์คำว่า <b>ยืนยันการลบ</b> เพื่อดำเนินการ</label><input id="databaseDeleteConfirmation" type="text" autocomplete="off" placeholder="ยืนยันการลบ" oninput="checkDatabaseDeleteConfirmation()"></div><div class="actions"><button class="btn" onclick="closeModal()">ยกเลิก</button><button id="confirmDatabaseDelete" class="btn danger" disabled onclick="deleteDatabaseNow()">ยืนยันลบฐานข้อมูล</button></div>`)}
function checkDatabaseDeleteConfirmation(){let field=$('#databaseDeleteConfirmation'),button=$('#confirmDatabaseDelete');if(button)button.disabled=(field?.value||'')!=='ยืนยันการลบ'}
async function deleteDatabaseNow(){let confirmation=$('#databaseDeleteConfirmation')?.value||'';if(confirmation!=='ยืนยันการลบ')return toast('กรุณาพิมพ์ ยืนยันการลบ ให้ตรงตามที่กำหนด');let button=$('#confirmDatabaseDelete');if(button)button.disabled=true;showWorkingOverlay('กำลังลบข้อมูลฐานข้อมูล...');try{let r=await api('/api/database-delete',{method:'POST',body:JSON.stringify({confirmation,expected_backend:window.databaseDeleteBackend})});await resetDashboardToOverview(`ลบข้อมูลในฐานข้อมูล ${r.backend==='google_sheet'?'Google Sheets':'SQLite'} แล้ว โดยเก็บไฟล์สำรองก่อนลบ`)}catch(e){toast(e.message||'ลบฐานข้อมูลไม่สำเร็จ');if(button)button.disabled=false}finally{hideWorkingOverlay()}}
function toggleSidebar(force){let app=document.querySelector('.app'),open=typeof force==='boolean'?force:!app.classList.contains('menu-open');app.classList.toggle('menu-open',open);let button=document.querySelector('#menuToggle');if(button){button.setAttribute('aria-expanded',open?'true':'false');button.setAttribute('aria-label',open?'ปิดเมนูด้านข้าง':'เปิดเมนูด้านข้าง');button.textContent=open?'×':'☰'}document.body.style.overflow=open?'hidden':''}document.addEventListener('keydown',event=>{if(event.key==='Escape')toggleSidebar(false)});const pages={overview:["ภาพรวมระบบ",overview],settings:["ตั้งค่าบอท",settings],networks:["จัดการเครือข่าย",networkPage],users:["ผู้ใช้และเครดิต",users],orders:["รายการสั่งซื้อ",orderPage],audit:["ประวัติการแก้ไข",audit],backup:["ฐานข้อมูล",backupPage],account:["จัดการบัญชี",webAccountPage]};document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>{document.querySelectorAll('[data-page]').forEach(x=>x.classList.remove('active'));b.classList.add('active');toggleSidebar(false);leaveFocus();$('#title').textContent=pages[b.dataset.page][0];pages[b.dataset.page][1]().catch(e=>toast(e.message))});function showModal(c){$('#modal').innerHTML=`<div class="modal-bg" onclick="if(event.target===this)closeModal()"><div class="modal">${c}</div></div>`;$('#modal').classList.remove('hidden')}function closeModal(){$('#modal').classList.add('hidden')}async function logout(){await api('/api/logout',{method:'POST'});location.reload()}overview().catch(e=>toast(e.message));
</script></body></html>'''
