
import asyncio
import datetime
import json
import logging
import os
import re
import shlex
from contextlib import suppress
from pathlib import Path
from typing import Optional, Tuple, List, Dict

import discord
from discord.ext import commands
from dotenv import load_dotenv

import database as db
from xui_api import XUIApi, SESSION_REFRESH_MINUTES

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("discord-vpn-bot")

TZ_THAI = datetime.timezone(datetime.timedelta(hours=7))

BOT_TOKEN = os.getenv("DISCORD_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
SERVICE_NAME = os.getenv("SERVICE_NAME", "discord-shop3x").strip() or "discord-shop3x"
APP_DIR = Path(__file__).resolve().parent
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

AIS_INBOUND_ID = int(os.getenv("AIS_INBOUND_ID", "1"))
TRUE_INBOUND_ID = int(os.getenv("TRUE_INBOUND_ID", "2"))

RUN_START_FINISH_ENABLED_KEY = "run_start_finish_enabled"
RUN_START_FINISH_COMMANDS_KEY = "run_start_finish_commands"
RUN_START_FINISH_DELAY_KEY = "run_start_finish_delay_seconds"
CANCEL_BUTTON_ENABLED_KEY = "cancel_button_enabled"
BUY_DM_ENABLED_KEY = "buy_dm_enabled"
BUY_GROUP_IDS_KEY = "buy_group_ids"
FREECLIENT_ENABLED_KEY = "freeclient_enabled"
FREECLIENT_HOURS_KEY = "freeclient_hours"
FREECLIENT_DAILY_LIMIT_KEY = "freeclient_daily_limit"
FREECLIENT_RESET_MODE_KEY = "freeclient_reset_mode"
ADDCLIENT_ENABLED_KEY = "addclient_enabled"
CREDIT_CODE_ENABLED_KEY = "credit_code_enabled"
TRUEMONEY_WALLET_PHONE_KEY = "truemoney_wallet_phone"
TRUEMONEY_CREDIT_RATE_KEY = "truemoney_credit_rate"
TRUEMONEY_ENABLED_KEY = "truemoney_enabled"
TRUEMONEY_CHANNEL_MODE_KEY = "truemoney_channel_mode"
TRUEMONEY_GROUP_IDS_KEY = "truemoney_group_ids"
LOG_DISPLAY_LIMIT_BUY_KEY = "log_display_limit_buy"
LOG_DISPLAY_LIMIT_FREE_KEY = "log_display_limit_free"
FREECLIENT_CHANNEL_MODE_KEY = "freeclient_channel_mode"
FREECLIENT_GROUP_IDS_KEY = "freeclient_group_ids"

COMMAND_PREFIXES = ("!", "/")

xui = XUIApi(
    base_url=os.getenv("XUI_URL", ""),
    username=os.getenv("XUI_USERNAME", ""),
    password=os.getenv("XUI_PASSWORD", ""),
    api_token=os.getenv("XUI_API_TOKEN", ""),
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or(*COMMAND_PREFIXES),
    intents=intents,
    help_command=None,
)

user_locks: Dict[int, asyncio.Lock] = {}
flow_tokens: Dict[Tuple[int, str], int] = {}


async def run_shell_command(command: str, *, cwd: Optional[Path] = None, timeout: int = 180) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        with suppress(Exception):
            await proc.communicate()
        return 124, "", f"Command timed out after {timeout} seconds"
    return proc.returncode, stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace")


# ───────────────────────────── helpers ─────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def thai_now() -> datetime.datetime:
    return datetime.datetime.now(TZ_THAI)


def thai_now_iso() -> str:
    return thai_now().isoformat(timespec="seconds")


def thai_now_str() -> str:
    return thai_now().strftime("%d/%m/%Y %H:%M:%S")


def format_credit(value: float) -> str:
    return f"{float(value):.2f}"


def format_hours(hours: float) -> str:
    value = float(hours)
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def format_thai_datetime(iso_text: str) -> str:
    try:
        return datetime.datetime.fromisoformat(str(iso_text)).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return str(iso_text)


def format_limit_count(max_uses: int) -> str:
    return "ไม่จำกัด" if int(max_uses) == 0 else f"{int(max_uses)} คน"


def split_chunks(text: str, limit: int = 1900) -> List[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts = []
    chunk = ""
    for line in text.splitlines(True):
        if len(chunk) + len(line) > limit and chunk:
            parts.append(chunk)
            chunk = line
        else:
            chunk += line
    if chunk:
        parts.append(chunk)
    return parts


def maybe_int(text: str) -> Optional[int]:
    text = str(text or "").strip()
    if not text:
        return None
    if text.startswith("<@") and text.endswith(">"):
        text = text.strip("<@!>")
    if text.isdigit():
        return int(text)
    return None


def resolve_user_id(arg: str, guild: Optional[discord.Guild] = None) -> Optional[int]:
    value = maybe_int(arg)
    if value is not None:
        return value
    if guild:
        for member in guild.members:
            if member.name == arg or member.display_name == arg or (member.global_name and member.global_name == arg):
                return member.id
    return None


def get_lock(user_id: int) -> asyncio.Lock:
    lock = user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        user_locks[user_id] = lock
    return lock


class SessionGuard:
    def __init__(self, user_id: int):
        self.lock = get_lock(user_id)

    async def __aenter__(self):
        await self.lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.lock.locked():
            self.lock.release()


def flow_bump(user_id: int, flow: str) -> int:
    key = (user_id, flow)
    flow_tokens[key] = flow_tokens.get(key, 0) + 1
    return flow_tokens[key]


def flow_current(user_id: int, flow: str) -> int:
    return flow_tokens.get((user_id, flow), 0)


def flow_clear(user_id: int, flow: Optional[str] = None) -> None:
    if flow is None:
        for key in list(flow_tokens):
            if key[0] == user_id:
                flow_tokens.pop(key, None)
    else:
        flow_tokens.pop((user_id, flow), None)


def normalize_command_name(raw: str) -> str:
    text = str(raw or "").strip().lstrip("/").lower()
    return text


def setting_enabled(key: str, default: str = "0") -> bool:
    return str(db.get_setting(key, default)).strip() == "1"


def get_run_start_finish_delay_seconds() -> float:
    try:
        return float(db.get_setting(RUN_START_FINISH_DELAY_KEY, 5))
    except Exception:
        return 5.0


def get_run_start_finish_commands() -> set[str]:
    raw = str(db.get_setting_text(RUN_START_FINISH_COMMANDS_KEY, "addclient") or "")
    return {normalize_command_name(p) for p in raw.split(",") if normalize_command_name(p)}


def set_run_start_finish_commands(commands: set[str]) -> None:
    db.set_setting(RUN_START_FINISH_COMMANDS_KEY, ",".join(sorted(commands)))


def should_run_start_finish(command_name: str) -> bool:
    return setting_enabled(RUN_START_FINISH_ENABLED_KEY, "1") and normalize_command_name(command_name) in get_run_start_finish_commands()


async def maybe_send_start_menu_after_finish(ctx: commands.Context, command_name: str) -> None:
    if not should_run_start_finish(command_name):
        return
    delay = get_run_start_finish_delay_seconds()

    async def _job():
        with suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
            await send_start_menu(ctx.channel, ctx.author)

    bot.loop.create_task(_job())


async def send_long(channel: discord.abc.Messageable, text: str) -> None:
    for chunk in split_chunks(text):
        await channel.send(chunk)


async def prompt_message(
    ctx: commands.Context,
    prompt: str,
    *,
    flow: str,
    timeout: int = 120,
    allow_cancel: bool = True,
) -> Optional[str]:
    await ctx.send(prompt)
    token = flow_current(ctx.author.id, flow)

    def check(message: discord.Message) -> bool:
        return (
            message.author.id == ctx.author.id
            and message.channel.id == ctx.channel.id
            and not message.author.bot
            and flow_current(ctx.author.id, flow) == token
        )

    try:
        message = await bot.wait_for("message", check=check, timeout=timeout)
    except asyncio.TimeoutError:
        await ctx.send("⌛ หมดเวลาแล้ว ลองใหม่อีกครั้ง")
        return None

    content = (message.content or "").strip()
    if allow_cancel and content.lower() in {"cancel", "/cancel", "!cancel"}:
        await ctx.send("⛔ ยกเลิกแล้ว")
        return None
    return content


def get_buy_policy() -> str:
    dm_enabled = setting_enabled(BUY_DM_ENABLED_KEY, "0")
    group_ids = db.get_setting_text(BUY_GROUP_IDS_KEY, "").strip()
    if dm_enabled:
        return "✅ ผู้ใช้ซื้อผ่าน DM ได้"
    if group_ids:
        return f"⛔ ปิด DM แล้ว | กลุ่มที่อนุญาต: {group_ids}"
    return "⛔ ปิด DM แล้ว | ซื้อได้จากทุกกลุ่ม"


def parse_positive_float(text: str) -> Optional[float]:
    try:
        value = float(str(text).strip())
        return value if value > 0 else None
    except Exception:
        return None


def parse_nonnegative_float(text: str) -> Optional[float]:
    try:
        value = float(str(text).strip())
        return value if value >= 0 else None
    except Exception:
        return None


def parse_int(text: str) -> Optional[int]:
    try:
        return int(float(str(text).strip()))
    except Exception:
        return None


def normalize_phone(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def is_valid_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"0\d{9}", phone))


def extract_truemoney_voucher_hash(raw: str) -> Optional[str]:
    text = str(raw or "").strip()
    if not text:
        return None

    match = re.search(
        r"https://gift\.truemoney\.com/campaign(?:/voucher_detail)?/?\?v=([A-Za-z0-9]+)",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1)

    cleaned = text.strip("<>()[]{}'\"")
    try:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(cleaned)
        if parsed.netloc.lower() == "gift.truemoney.com":
            values = parse_qs(parsed.query).get("v", [])
            if values and re.fullmatch(r"[A-Za-z0-9]{20,80}", values[0]):
                return values[0]
    except Exception:
        pass

    direct_match = re.fullmatch(r"(?:v=)?([A-Za-z0-9]{20,80})", cleaned)
    return direct_match.group(1) if direct_match else None


def parse_truemoney_amount(value) -> Optional[float]:
    if isinstance(value, (int, float)):
        amount = float(value)
    else:
        match = re.search(r"\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", str(value or ""))
        if not match:
            return None
        amount = float(match.group(0).replace(",", ""))
    amount = round(amount, 2)
    return amount if amount > 0 else None


def json_compact(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:5000]
    except Exception:
        return ""


def truemoney_error_message(code: str, message: str = "") -> str:
    code = str(code or "").strip()
    mapping = {
        "VOUCHER_NOT_FOUND": "ไม่พบซองอั่งเปานี้",
        "VOUCHER_EXPIRED": "ซองอั่งเปาหมดอายุแล้ว",
        "VOUCHER_OUT_OF_STOCK": "ซองอั่งเปานี้ถูกใช้ไปแล้ว",
        "CANNOT_GET_OWN_VOUCHER": "ไม่สามารถรับซองของตัวเองได้",
        "TARGET_USER_NOT_FOUND": "ไม่พบเบอร์รับซองในระบบ TrueMoney",
        "INTERNAL_ERROR": "ระบบ TrueMoney ไม่พร้อมให้บริการชั่วคราว",
        "INVALID_VOUCHER": "รูปแบบซองอั่งเปาไม่ถูกต้อง",
        "INVALID_PHONE": "เบอร์รับซองอั่งเปาไม่ถูกต้อง",
        "INVALID_AMOUNT": "ระบบไม่สามารถอ่านจำนวนเงินจากซองได้",
        "REQUEST_FAILED": "เชื่อมต่อ TrueMoney ไม่สำเร็จ",
        "DUPLICATE": "ซองนี้เคยถูกเติมเครดิตไปแล้ว",
    }
    if code in mapping:
        return f"{mapping[code]} ({code})"
    if message:
        return f"{message} ({code})" if code else message
    return f"สถานะจาก TrueMoney: {code}" if code else "ไม่สามารถรับซองอั่งเปาได้"


def redeem_truemoney_voucher_sync(phone: str, voucher_hash: str) -> dict:
    import requests
    url = f"https://gift.truemoney.com/campaign/vouchers/{voucher_hash}/redeem"
    payload = {"mobile": phone, "voucher_hash": voucher_hash}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 DiscordBioShopBot/1.0",
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20)
    except requests.RequestException as exc:
        return {"success": False, "status_code": "REQUEST_FAILED", "message": str(exc), "raw_response": {}}
    try:
        data = response.json()
    except ValueError:
        return {
            "success": False,
            "status_code": f"HTTP_{response.status_code}",
            "message": "TrueMoney ไม่ได้ส่งผลลัพธ์เป็น JSON",
            "raw_response": {},
        }
    status = data.get("status") if isinstance(data, dict) else {}
    status = status if isinstance(status, dict) else {}
    code = str(status.get("code") or f"HTTP_{response.status_code}")
    message = str(status.get("message") or "")
    if response.status_code != 200 or code != "SUCCESS":
        return {"success": False, "status_code": code, "message": message, "raw_response": data}
    ticket = ((data.get("data") or {}).get("my_ticket") or {})
    amount = parse_truemoney_amount(ticket.get("amount_baht"))
    if amount is None:
        return {"success": False, "status_code": "INVALID_AMOUNT", "message": "", "raw_response": data}
    return {"success": True, "status_code": code, "amount_baht": amount, "raw_response": data}


def freeclient_channel_allowed(channel: discord.abc.GuildChannel | discord.DMChannel) -> Tuple[bool, str]:
    mode = db.get_setting_text(FREECLIENT_CHANNEL_MODE_KEY, "dm_and_group") or "dm_and_group"
    is_private = isinstance(channel, discord.DMChannel)
    allowed_ids = {int(x) for x in db.get_setting_text(FREECLIENT_GROUP_IDS_KEY, "").split(",") if x.strip().lstrip("-").isdigit()}

    if mode == "dm_only":
        return (True, "") if is_private else (False, "❌ คำสั่งนี้ใช้ได้เฉพาะใน DM ส่วนตัวกับบอท")
    if mode == "dm_and_group":
        return True, ""
    if mode == "group_only":
        return (True, "") if not is_private else (False, "❌ คำสั่งนี้ใช้ได้เฉพาะในกลุ่ม")
    if mode == "specified_only":
        if is_private:
            return False, "❌ คำสั่งนี้ใช้ได้เฉพาะในกลุ่มที่กำหนดเท่านั้น"
        if not allowed_ids or channel.id in allowed_ids:
            return True, ""
        return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาต"
    if mode == "specified_and_dm":
        if is_private:
            return True, ""
        if not allowed_ids or channel.id in allowed_ids:
            return True, ""
        return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาต"
    return True, ""


def truemoney_channel_allowed(channel: discord.abc.GuildChannel | discord.DMChannel) -> Tuple[bool, str]:
    mode = db.get_setting_text(TRUEMONEY_CHANNEL_MODE_KEY, "dm_only") or "dm_only"
    is_private = isinstance(channel, discord.DMChannel)
    allowed_ids = {int(x) for x in db.get_setting_text(TRUEMONEY_GROUP_IDS_KEY, "").split(",") if x.strip().lstrip("-").isdigit()}
    if mode == "dm_only":
        return (True, "") if is_private else (False, "❌ คำสั่ง /addmycredit ใช้ได้เฉพาะใน DM ส่วนตัวกับบอทเท่านั้น")
    if mode == "dm_and_group":
        return True, ""
    if mode == "group_only":
        return (True, "") if not is_private else (False, "❌ คำสั่ง /addmycredit ใช้ได้เฉพาะในกลุ่มเท่านั้น")
    if mode == "specified_only":
        if is_private:
            return False, "❌ คำสั่ง /addmycredit ใช้ได้เฉพาะในกลุ่มที่กำหนดเท่านั้น"
        if not allowed_ids or channel.id in allowed_ids:
            return True, ""
        return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาต กรุณาเติมเครดิตในกลุ่มที่แอดมินกำหนด"
    if mode == "specified_and_dm":
        if is_private:
            return True, ""
        if not allowed_ids or channel.id in allowed_ids:
            return True, ""
        return False, "❌ กลุ่มนี้ไม่ได้รับอนุญาต กรุณาใช้ DM หรือกลุ่มที่แอดมินกำหนด"
    return True, ""


def mycodes_sort_order() -> str:
    return db.get_setting_text("mycodes_sort_order", "newest_bottom") or "newest_bottom"


def mycodes_store_limit() -> int:
    try:
        return max(1, int(float(db.get_setting("mycodes_store_limit", 100))))
    except Exception:
        return 100


def mycodes_display_limit() -> int:
    try:
        n = int(float(db.get_setting("mycodes_display_limit", 100)))
        return max(1, min(n, mycodes_store_limit()))
    except Exception:
        return mycodes_store_limit()


def log_display_limit(kind: str) -> int:
    key = LOG_DISPLAY_LIMIT_BUY_KEY if kind == "buy" else LOG_DISPLAY_LIMIT_FREE_KEY
    try:
        n = int(float(db.get_setting(key, db.LOG_MAX_ENTRIES)))
        return max(1, min(n, db.LOG_MAX_ENTRIES))
    except Exception:
        return db.LOG_MAX_ENTRIES


async def send_start_menu(channel: discord.abc.Messageable, user: discord.User | discord.Member) -> None:
    text = (
        "✨ ยินดีต้อนรับสู่ร้าน Bio-shop ✨\n\n"
        "ใช้คำสั่งต่อไปนี้:\n"
        "!addclient - สร้างโค้ดใหม่\n"
        "!freeclient - ทดลองใช้งานฟรี\n"
        "!mycredit - ตรวจสอบเครดิต\n"
        "!addmycredit - เติมเครดิตด้วยซองอั่งเปา\n"
        "!mycodes - ดูโค้ดที่สร้างไว้\n"
        "!entercode - กรอกโค้ด\n"
        "!checkprice - ดูราคาต่อวัน\n"
        "!help - ดูคำสั่งแอดมินและคำสั่งหลัก\n"
    )
    await channel.send(text)


async def maybe_send_start_menu_command(ctx: commands.Context, command_name: str) -> None:
    if should_run_start_finish(command_name):
        await maybe_send_start_menu_after_finish(ctx, command_name)


def credit_code_key(name: str) -> str:
    return normalize_credit_code_name(name).lower()


def normalize_credit_code_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "").strip())


def format_price_per_day() -> str:
    price = float(db.get_setting("price_per_day", 2))
    return f"{price:.2f}"


# ───────────────────────────── network view ─────────────────────────────

class NetworkView(discord.ui.View):
    def __init__(self, owner_id: int, flow: str, include_cancel: bool = True, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.flow = flow
        self.choice: Optional[str] = None
        self.cancelled = False
        self._include_cancel = include_cancel

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("⚠️ คุณไม่สามารถใช้งานของผู้อื่นได้", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="AIS", style=discord.ButtonStyle.primary)
    async def ais_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "ais"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="TRUE", style=discord.ButtonStyle.success)
    async def true_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "true"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = None
        self.cancelled = True
        await interaction.response.defer()
        self.stop()


async def ask_network(ctx: commands.Context, flow: str, prompt: str) -> Optional[str]:
    view = NetworkView(ctx.author.id, flow)
    msg = await ctx.send(prompt, view=view)
    await view.wait()
    with suppress(Exception):
        await msg.edit(view=None)
    if view.cancelled or view.choice is None:
        return None
    return view.choice


async def ask_text(ctx: commands.Context, flow: str, prompt: str, timeout: int = 120) -> Optional[str]:
    return await prompt_message(ctx, prompt, flow=flow, timeout=timeout, allow_cancel=True)


def _resolve_inbound_id(network: str) -> int:
    return AIS_INBOUND_ID if network == "ais" else TRUE_INBOUND_ID


def _user_display(user_id: int, username: Optional[str]) -> str:
    username = username or ""
    return f"@{username}" if username and not username.isdigit() else f"ID: {user_id}"


# ───────────────────────────── core commands ─────────────────────────────

@bot.event
async def on_ready():
    db.init_db()
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "?")
    with suppress(Exception):
        await bot.change_presence(activity=discord.Game(name="Bio-shop Bot"))


@bot.command(name="start")
async def cmd_start(ctx: commands.Context):
    db.ensure_user(ctx.author.id, getattr(ctx.author, "name", None))
    await send_start_menu(ctx.channel, ctx.author)


@bot.command(name="mycredit")
async def cmd_mycredit(ctx: commands.Context):
    db.ensure_user(ctx.author.id, getattr(ctx.author, "name", None))
    credit = db.get_credit(ctx.author.id)
    await ctx.send(f"💰 เครดิตคงเหลือของคุณ: {format_credit(credit)} เครดิต")


@bot.command(name="checkprice")
async def cmd_checkprice(ctx: commands.Context):
    price = float(db.get_setting("price_per_day", 2))
    await ctx.send(f"💵 ราคาต่อวันปัจจุบัน: {price:.2f} เครดิต/วัน")
    await maybe_send_start_menu_command(ctx, "checkprice")


@bot.command(name="addclient")
async def cmd_addclient(ctx: commands.Context):
    db.ensure_user(ctx.author.id, getattr(ctx.author, "name", None))
    async with SessionGuard(ctx.author.id):
        if not setting_enabled(ADDCLIENT_ENABLED_KEY, "1"):
            await ctx.send("❌ ตอนนี้ระบบสร้างโค้ดถูกปิดใช้งาน")
            return

        if not xui.is_available():
            await ctx.send("❌ ระบบ VPN Panel ไม่พร้อมใช้งานในขณะนี้")
            return

        network = await ask_network(ctx, "addclient", "🌐 เลือกเครือข่าย: AIS หรือ TRUE")
        if not network:
            await ctx.send("⛔ ยกเลิกหรือหมดเวลา")
            return

        price_per_day = float(db.get_setting("price_per_day", 2))
        credit = db.get_credit(ctx.author.id)
        await ctx.send(f"✅ เลือกเครือข่าย: {network.upper()} | เครดิตคุณ: {credit:.2f} | ราคา: {price_per_day:.2f}/วัน")

        name = await ask_text(ctx, "addclient", "📝 ตั้งชื่อโค้ดของคุณ (ตัวอักษร/ตัวเลข/ขีด/จุด ไม่เกิน 50 ตัว):")
        if not name:
            return
        name = name.strip()
        if not re.fullmatch(r"^[\w\-.]{1,50}$", name):
            await ctx.send("❌ ชื่อไม่ถูกต้อง")
            return
        if db.code_name_exists(ctx.author.id, name):
            await ctx.send("❌ คุณมีโค้ดชื่อนี้อยู่แล้ว กรุณาใช้ชื่ออื่น")
            return

        days_text = await ask_text(ctx, "addclient", "📅 ระบุจำนวนวัน (1-60):")
        if not days_text:
            return
        days = parse_int(days_text)
        if days is None or not 1 <= days <= 60:
            await ctx.send("❌ กรุณาระบุตัวเลขระหว่าง 1-60 เท่านั้น")
            return

        gb_text = await ask_text(ctx, "addclient", "💾 ระบุ GB ที่ต้องการจำกัด (0 = ไม่จำกัด):")
        if not gb_text:
            return
        gb = parse_nonnegative_float(gb_text)
        if gb is None:
            await ctx.send("❌ กรุณาระบุตัวเลข GB ให้ถูกต้อง")
            return

        total_cost = float(days) * price_per_day
        if not db.try_deduct_credit(ctx.author.id, total_cost):
            current_credit = db.get_credit(ctx.author.id)
            await ctx.send(
                f"❌ เครดิตไม่เพียงพอ\nเครดิตคุณ: {current_credit:.2f}\nค่าใช้จ่าย: {total_cost:.2f}"
            )
            return

        processing = await ctx.send("⏳ กำลังสร้างโค้ด กรุณารอสักครู่...")
        inbound_id = _resolve_inbound_id(network)

        inbound = await asyncio.to_thread(xui.get_inbound, inbound_id)
        if not inbound:
            db.add_credit(ctx.author.id, total_cost)
            await processing.edit(content="❌ ไม่สามารถดึงข้อมูล inbound จาก 3x-ui ได้")
            return

        remark = str(inbound.get("remark", "") or "").strip()
        full_name = f"{remark}-{name}" if remark else name

        result = await asyncio.to_thread(xui.add_client, inbound_id, full_name, float(days), float(gb))
        if not result:
            db.add_credit(ctx.author.id, total_cost)
            await processing.edit(content="❌ เกิดข้อผิดพลาดในการสร้าง client")
            return

        link = await asyncio.to_thread(xui.generate_link, inbound_id, result["uuid"], full_name, result.get("flow", ""))
        if not link:
            await asyncio.to_thread(xui.delete_client, full_name)
            db.add_credit(ctx.author.id, total_cost)
            await processing.edit(content="❌ สร้าง client ได้แต่สร้างลิงก์ไม่สำเร็จ จึงยกเลิกให้แล้ว")
            return

        expire_date = (thai_now() + datetime.timedelta(days=days)).strftime("%d/%m/%Y %H:%M:%S")
        db.save_code(ctx.author.id, name, result["uuid"], inbound_id, network, expire_date, float(gb), link)
        db.add_buy_log(
            ctx.author.id,
            getattr(ctx.author, "name", None),
            name,
            network,
            int(days),
            float(gb),
            float(total_cost),
            link,
            thai_now_iso(),
        )

        await processing.edit(
            content=(
                f"✅ สร้างโค้ดสำเร็จ\n"
                f"ชื่อ: {name}\n"
                f"เครือข่าย: {network.upper()}\n"
                f"วันใช้งาน: {days} วัน\n"
                f"GB: {format_credit(gb) if gb > 0 else 'ไม่จำกัด'}\n"
                f"ราคา: {total_cost:.2f} เครดิต\n"
                f"ลิงก์: {link}"
            )
        )
        await maybe_send_start_menu_command(ctx, "addclient")


@bot.command(name="freeclient")
async def cmd_freeclient(ctx: commands.Context):
    db.ensure_user(ctx.author.id, getattr(ctx.author, "name", None))
    async with SessionGuard(ctx.author.id):
        if not setting_enabled(FREECLIENT_ENABLED_KEY, "1"):
            await ctx.send("❌ ตอนนี้ระบบทดลองใช้ฟรีถูกปิดใช้งาน")
            return

        allowed, msg = freeclient_channel_allowed(ctx.channel)
        if not allowed:
            await ctx.send(msg)
            return

        daily_limit = int(float(db.get_setting(FREECLIENT_DAILY_LIMIT_KEY, 1)))
        used_count, usage_label = count_free_usage(ctx.author.id)
        if used_count >= daily_limit:
            await ctx.send(f"⚠️ คุณใช้สิทธิ์ทดลองใช้ฟรีครบแล้ว ({usage_label}) {used_count}/{daily_limit}")
            return

        network = await ask_network(ctx, "freeclient", "🧪 เลือกเครือข่ายทดลองใช้ฟรี: AIS หรือ TRUE")
        if not network:
            return

        name = await ask_text(ctx, "freeclient", "📝 ตั้งชื่อโค้ดทดลอง:")
        if not name:
            return
        name = name.strip()
        if not re.fullmatch(r"^[\w\-.]{1,50}$", name):
            await ctx.send("❌ ชื่อไม่ถูกต้อง")
            return

        gb_text = await ask_text(ctx, "freeclient", "💾 ระบุ GB สำหรับทดลอง (0 = ไม่จำกัด):")
        if not gb_text:
            return
        gb = parse_nonnegative_float(gb_text)
        if gb is None:
            await ctx.send("❌ กรุณาระบุตัวเลข GB ให้ถูกต้อง")
            return

        daily_limit = int(float(db.get_setting(FREECLIENT_DAILY_LIMIT_KEY, 1)))
        used_count, usage_label = count_free_usage(ctx.author.id)
        if used_count >= daily_limit:
            await ctx.send(f"⚠️ คุณใช้สิทธิ์ทดลองใช้ฟรีครบแล้ว ({usage_label}) {used_count}/{daily_limit}")
            return

        processing = await ctx.send("⏳ กำลังสร้างโค้ดทดลอง กรุณารอสักครู่...")
        if not xui.is_available():
            await processing.edit(content="❌ ระบบ VPN Panel ไม่พร้อมใช้งานในขณะนี้")
            return

        inbound_id = _resolve_inbound_id(network)
        inbound = await asyncio.to_thread(xui.get_inbound, inbound_id)
        if not inbound:
            await processing.edit(content="❌ ไม่สามารถดึงข้อมูล inbound ได้")
            return

        remark = str(inbound.get("remark", "") or "").strip()
        full_name = f"{remark}-FREE-{name}" if remark else f"FREE-{name}"

        free_hours = float(db.get_setting(FREECLIENT_HOURS_KEY, 1))
        result = await asyncio.to_thread(xui.add_client, inbound_id, full_name, free_hours / 24.0, float(gb))
        if not result:
            await processing.edit(content="❌ เกิดข้อผิดพลาดในการสร้าง client ทดลอง")
            return

        link = await asyncio.to_thread(xui.generate_link, inbound_id, result["uuid"], full_name, result.get("flow", ""))
        if not link:
            await asyncio.to_thread(xui.delete_client, full_name)
            await processing.edit(content="❌ สร้าง client ได้แต่สร้างลิงก์ไม่สำเร็จ จึงยกเลิกให้แล้ว")
            return

        expire_date = (thai_now() + datetime.timedelta(hours=free_hours)).strftime("%d/%m/%Y %H:%M:%S")
        db.add_free_log(
            ctx.author.id,
            getattr(ctx.author, "name", None),
            name,
            network,
            float(free_hours),
            float(gb),
            link,
            thai_now_iso(),
        )

        await processing.edit(
            content=(
                f"✅ สร้างโค้ดทดลองใช้ฟรีสำเร็จ\n"
                f"ชื่อ: {name}\n"
                f"เครือข่าย: {network.upper()}\n"
                f"อายุ: {format_hours(free_hours)} ชั่วโมง\n"
                f"GB: {format_credit(gb) if gb > 0 else 'ไม่จำกัด'}\n"
                f"ลิงก์: {link}"
            )
        )


def count_free_usage(user_id: int) -> Tuple[int, str]:
    mode = str(db.get_setting(FREECLIENT_RESET_MODE_KEY, "midnight") or "midnight")
    if mode == "rolling_24h":
        cutoff = thai_now() - datetime.timedelta(days=1)
        conn = db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM free_log WHERE user_id = ? AND created_at >= ?",
                (user_id, cutoff.isoformat(timespec="seconds")),
            ).fetchone()
            return int(row["total"]) if row else 0, "rolling 24h"
        finally:
            conn.close()
    prefix = thai_now().strftime("%d/%m/%Y")
    return db.count_free_log_by_date(user_id, prefix), "today"


@bot.command(name="mycodes")
async def cmd_mycodes(ctx: commands.Context):
    db.ensure_user(ctx.author.id, getattr(ctx.author, "name", None))
    rows = db.get_user_codes(ctx.author.id, sort_order=mycodes_sort_order(), display_limit=mycodes_display_limit())
    if not rows:
        await ctx.send("📭 ยังไม่มีโค้ดของคุณ")
        return

    lines = [
        f"📄 โค้ดของคุณ ({len(rows)} รายการ)",
        f"เรียง: {mycodes_sort_order()} | แสดงสูงสุด: {mycodes_display_limit()}",
        "────────────────────",
    ]
    for idx, row in enumerate(rows, start=1):
        gb_text = f"{row['gb_limit']} GB" if float(row.get("gb_limit", 0) or 0) > 0 else "ไม่จำกัด"
        lines += [
            f"[{idx}] {row['name']}",
            f"เครือข่าย: {str(row['network']).upper()}",
            f"หมดอายุ: {row['expire_date']}",
            f"GB: {gb_text}",
            f"ลิงก์: {row['link']}",
            "────────────────────",
        ]
    await send_long(ctx.channel, "\n".join(lines))


@bot.command(name="entercode")
async def cmd_entercode(ctx: commands.Context):
    db.ensure_user(ctx.author.id, getattr(ctx.author, "name", None))
    if not setting_enabled(CREDIT_CODE_ENABLED_KEY, "1"):
        await ctx.send("❌ ระบบกรอกโค้ดถูกปิดใช้งาน")
        return

    code = await ask_text(ctx, "entercode", "🎁 พิมพ์โค้ดที่ต้องการกรอก:")
    if not code:
        return
    code_name = normalize_credit_code_name(code)
    code_key = credit_code_key(code_name)
    code_row = db.get_credit_code(code_key)
    if not code_row:
        await ctx.send("❌ ไม่พบโค้ดนี้ในระบบ")
        return

    now = thai_now_iso()
    result = db.redeem_credit_code(code_key, ctx.author.id, getattr(ctx.author, "name", None), now)
    status = result.get("status")
    if status == "not_found":
        await ctx.send("❌ ไม่พบโค้ดนี้ในระบบ")
        return
    if status == "expired":
        await ctx.send("❌ โค้ดนี้หมดอายุแล้ว")
        return
    if status == "already_used":
        await ctx.send("⚠️ คุณเคยใช้โค้ดนี้แล้ว")
        return
    if status == "used_up":
        await ctx.send("⚠️ โค้ดนี้ถูกใช้ครบแล้ว")
        return
    if status == "ok":
        balance = db.get_credit(ctx.author.id)
        if code_row["mode"] == "free_reset":
            await ctx.send(f"✅ กรอกโค้ดสำเร็จและรีเซ็ตสิทธิ์ทดลองใช้ฟรีแล้ว\nเครดิตปัจจุบัน: {balance:.2f}")
        else:
            await ctx.send(f"✅ กรอกโค้ดสำเร็จ ได้รับเครดิต {code_row.get('fixed_credit', 0):.2f}\nเครดิตปัจจุบัน: {balance:.2f}")
        return
    await ctx.send("❌ ไม่สามารถใช้โค้ดนี้ได้")


@bot.command(name="addmycredit")
async def cmd_addmycredit(ctx: commands.Context):
    db.ensure_user(ctx.author.id, getattr(ctx.author, "name", None))
    if not setting_enabled(TRUEMONEY_ENABLED_KEY, "1"):
        await ctx.send("❌ ระบบเติมเครดิตถูกปิดใช้งาน")
        return
    allowed, msg = truemoney_channel_allowed(ctx.channel)
    if not allowed:
        await ctx.send(msg)
        return

    phone = db.get_setting_text(TRUEMONEY_WALLET_PHONE_KEY, "").strip()
    if not phone or not is_valid_phone(normalize_phone(phone)):
        await ctx.send("❌ แอดมินยังไม่ได้ตั้งเบอร์รับซอง TrueMoney")
        return

    rate = float(db.get_setting(TRUEMONEY_CREDIT_RATE_KEY, 1))
    voucher = await ask_text(ctx, "addmycredit", "🔗 ส่งลิงก์/โค้ดซอง TrueMoney มาได้เลย:")
    if not voucher:
        return

    voucher_hash = extract_truemoney_voucher_hash(voucher)
    if not voucher_hash:
        await ctx.send("❌ รูปแบบซองอั่งเปาไม่ถูกต้อง")
        return

    processing = await ctx.send("⏳ กำลังตรวจสอบซองและเติมเครดิต...")
    result = await asyncio.to_thread(redeem_truemoney_voucher_sync, normalize_phone(phone), voucher_hash)
    if not result.get("success"):
        await processing.edit(content=f"❌ เติมเครดิตไม่สำเร็จ: {truemoney_error_message(result.get('status_code', ''), result.get('message', ''))}")
        return

    amount = float(result["amount_baht"])
    credit_amount = round(amount * rate, 2)
    saved = db.add_truemoney_credit(
        ctx.author.id,
        getattr(ctx.author, "name", None),
        voucher_hash,
        normalize_phone(phone),
        amount,
        credit_amount,
        "SUCCESS",
        thai_now_iso(),
        json_compact(result.get("raw_response", {})),
    )
    if saved.get("status") == "duplicate":
        await processing.edit(content="⚠️ ซองนี้เคยถูกเติมเครดิตในระบบแล้ว")
        return

    balance = db.get_credit(ctx.author.id)
    await processing.edit(
        content=(
            f"✅ เติมเครดิตสำเร็จ\n"
            f"จำนวนเงิน: {amount:.2f} บาท\n"
            f"เครดิตที่ได้รับ: {credit_amount:.2f}\n"
            f"เครดิตคงเหลือ: {balance:.2f}"
        )
    )


# ───────────────────────────── admin commands ─────────────────────────────

def require_admin(ctx: commands.Context) -> bool:
    return is_admin(ctx.author.id)


@bot.command(name="addcredits")
async def cmd_addcredits(ctx: commands.Context, target: str = "", amount: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not target or not amount:
        await ctx.send("❌ รูปแบบ: !addcredits @user จำนวน")
        return
    user_id = resolve_user_id(target, ctx.guild)
    if user_id is None:
        await ctx.send("❌ ไม่พบผู้ใช้")
        return
    value = parse_positive_float(amount)
    if value is None:
        await ctx.send("❌ จำนวนเครดิตไม่ถูกต้อง")
        return
    db.ensure_user(user_id, None)
    db.add_credit(user_id, value)
    await ctx.send(f"✅ เพิ่มเครดิต {value:.2f} ให้ {target} แล้ว | ยอดใหม่: {db.get_credit(user_id):.2f}")
    await maybe_send_start_menu_command(ctx, "addcredits")


@bot.command(name="deletecredits", aliases=["Deletecredits"])
async def cmd_deletecredits(ctx: commands.Context, target: str = "", amount: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not target or not amount:
        await ctx.send("❌ รูปแบบ: !deletecredits @user จำนวน")
        return
    user_id = resolve_user_id(target, ctx.guild)
    if user_id is None:
        await ctx.send("❌ ไม่พบผู้ใช้")
        return
    value = parse_positive_float(amount)
    if value is None:
        await ctx.send("❌ จำนวนเครดิตไม่ถูกต้อง")
        return
    db.ensure_user(user_id, None)
    db.deduct_credit(user_id, value)
    await ctx.send(f"✅ ลบเครดิต {value:.2f} จาก {target} แล้ว | ยอดใหม่: {db.get_credit(user_id):.2f}")
    await maybe_send_start_menu_command(ctx, "deletecredits")


@bot.command(name="setprice")
async def cmd_setprice(ctx: commands.Context, price: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    value = parse_nonnegative_float(price)
    if value is None:
        await ctx.send("❌ รูปแบบ: !setprice จำนวน")
        return
    db.set_setting("price_per_day", str(value))
    await ctx.send(f"✅ ตั้งราคาต่อวันเป็น {value:.2f} เครดิต/วัน")
    await maybe_send_start_menu_command(ctx, "setprice")


@bot.command(name="checkangpaophone")
async def cmd_checkangpaophone(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    phone = db.get_setting_text(TRUEMONEY_WALLET_PHONE_KEY, "").strip() or "ยังไม่ได้ตั้งค่า"
    rate = float(db.get_setting(TRUEMONEY_CREDIT_RATE_KEY, 1))
    enabled = "เปิด" if setting_enabled(TRUEMONEY_ENABLED_KEY, "1") else "ปิด"
    mode = db.get_setting_text(TRUEMONEY_CHANNEL_MODE_KEY, "dm_only") or "dm_only"
    await ctx.send(f"📞 เบอร์รับซอง: {phone}\n💱 อัตราเครดิต: {rate:.2f} / 1 บาท\nสถานะ: {enabled}\nช่องทาง: {mode}")


@bot.command(name="setangpaophone")
async def cmd_setangpaophone(ctx: commands.Context, phone: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    phone = normalize_phone(phone)
    if not is_valid_phone(phone):
        await ctx.send("❌ เบอร์ไม่ถูกต้อง")
        return
    db.set_setting(TRUEMONEY_WALLET_PHONE_KEY, phone)
    await ctx.send(f"✅ ตั้งเบอร์รับซองเป็น {phone} แล้ว")


@bot.command(name="setangpaorate")
async def cmd_setangpaorate(ctx: commands.Context, rate: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    value = parse_positive_float(rate)
    if value is None:
        await ctx.send("❌ อัตราไม่ถูกต้อง")
        return
    db.set_setting(TRUEMONEY_CREDIT_RATE_KEY, str(value))
    await ctx.send(f"✅ ตั้งเครดิตต่อ 1 บาท เป็น {value:.2f} แล้ว")


@bot.command(name="settingsmycredit", aliases=["Settingsmycredit"])
async def cmd_settingsmycredit(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    text = (
        "🛠 เมนูระบบเติมเครดิต\n"
        f"สถานะ: {'เปิด' if setting_enabled(TRUEMONEY_ENABLED_KEY, '1') else 'ปิด'}\n"
        f"เบอร์: {str(db.get_setting(TRUEMONEY_WALLET_PHONE_KEY, '') or '').strip() or 'ยังไม่ได้ตั้งค่า'}\n"
        f"อัตรา: {float(db.get_setting(TRUEMONEY_CREDIT_RATE_KEY, 1)):.2f}\n"
        f"ช่องทาง: {str(db.get_setting(TRUEMONEY_CHANNEL_MODE_KEY, 'dm_only') or 'dm_only')}\n\n"
        "ใช้คำสั่งย่อย:\n"
        "!setangpaophone 0XXXXXXXXX\n"
        "!setangpaorate 1.5\n"
        "!checkangpaophone\n"
        "!openmycredit / !offmycredit\n"
    )
    await ctx.send(text)


@bot.command(name="openmycredit")
async def cmd_openmycredit(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    db.set_setting(TRUEMONEY_ENABLED_KEY, "1")
    await ctx.send("✅ เปิดระบบเติมเครดิตแล้ว")


@bot.command(name="offmycredit")
async def cmd_offmycredit(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    db.set_setting(TRUEMONEY_ENABLED_KEY, "0")
    await ctx.send("⛔ ปิดระบบเติมเครดิตแล้ว")


@bot.command(name="toggleaddclient")
async def cmd_toggleaddclient(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    current = setting_enabled(ADDCLIENT_ENABLED_KEY, "1")
    db.set_setting(ADDCLIENT_ENABLED_KEY, "0" if current else "1")
    await ctx.send(f"✅ ระบบสร้างโค้ด /addclient ตอนนี้: {'เปิด' if not current else 'ปิด'}")


@bot.command(name="buydm")
async def cmd_buydm(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    db.set_setting(BUY_DM_ENABLED_KEY, "1")
    await ctx.send(f"✅ เปิดให้ซื้อผ่าน DM แล้ว\n{get_buy_policy()}")


@bot.command(name="nobuydm")
async def cmd_nobuydm(ctx: commands.Context, *group_ids: str):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if group_ids:
        existing = {int(x) for x in str(db.get_setting(BUY_GROUP_IDS_KEY, "") or "").split(",") if x.strip().lstrip("-").isdigit()}
        added = []
        invalid = []
        for raw in group_ids:
            gid = maybe_int(raw)
            if gid is None:
                invalid.append(raw)
            else:
                existing.add(gid)
                added.append(gid)
        if invalid:
            await ctx.send("❌ group ID ไม่ถูกต้อง: " + ", ".join(invalid))
            return
        db.set_setting(BUY_DM_ENABLED_KEY, "0")
        db.set_setting(BUY_GROUP_IDS_KEY, ",".join(str(x) for x in sorted(existing)))
        await ctx.send("⛔ ปิด DM แล้ว และจำกัดกลุ่มซื้อได้\n" + get_buy_policy())
        return
    db.set_setting(BUY_DM_ENABLED_KEY, "0")
    db.set_setting(BUY_GROUP_IDS_KEY, "")
    await ctx.send("⛔ ปิด DM แล้ว ให้ซื้อได้จากทุกกลุ่ม\n" + get_buy_policy())


@bot.command(name="openfreeclient")
async def cmd_openfreeclient(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    db.set_setting(FREECLIENT_ENABLED_KEY, "1")
    await ctx.send(f"✅ เปิดระบบทดลองใช้ฟรีแล้ว ({format_hours(float(db.get_setting(FREECLIENT_HOURS_KEY, 1)))} ชั่วโมง)")


@bot.command(name="offfreeclient")
async def cmd_offfreeclient(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    db.set_setting(FREECLIENT_ENABLED_KEY, "0")
    await ctx.send("⛔ ปิดระบบทดลองใช้ฟรีแล้ว")


@bot.command(name="freeclientlimit")
async def cmd_freeclientlimit(ctx: commands.Context, limit: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    value = parse_int(limit)
    if value is None or value < 0:
        await ctx.send("❌ รูปแบบ: !freeclientlimit จำนวน")
        return
    db.set_setting(FREECLIENT_DAILY_LIMIT_KEY, str(value))
    await ctx.send(f"✅ ตั้งสิทธิ์ทดลองใช้ฟรีเป็น {value} ครั้ง/คน/วัน")


@bot.command(name="freeclienttime")
async def cmd_freeclienttime(ctx: commands.Context, hours: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    value = parse_positive_float(hours)
    if value is None:
        await ctx.send("❌ รูปแบบ: !freeclienttime จำนวนชั่วโมง")
        return
    db.set_setting(FREECLIENT_HOURS_KEY, str(value))
    await ctx.send(f"✅ ตั้งเวลาทดลองฟรีเป็น {format_hours(value)} ชั่วโมง")


@bot.command(name="freeclientresettime", aliases=["freeclientResettime"])
async def cmd_freeclientresettime(ctx: commands.Context, mode: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not mode:
        await ctx.send("รูปแบบ: !freeclientresettime midnight|rolling_24h")
        return
    mode = mode.strip().lower()
    if mode not in {"midnight", "rolling_24h"}:
        await ctx.send("❌ โหมดไม่ถูกต้อง: midnight หรือ rolling_24h")
        return
    db.set_setting(FREECLIENT_RESET_MODE_KEY, mode)
    await ctx.send(f"✅ ตั้งโหมดรีเซ็ตสิทธิ์ทดลองใช้ฟรีเป็น {mode}")


@bot.command(name="resetfreeclientlimit")
async def cmd_resetfreeclientlimit(ctx: commands.Context, target: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not target:
        await ctx.send("รูปแบบ: !resetfreeclientlimit @user|user_id")
        return
    user_id = resolve_user_id(target, ctx.guild)
    if user_id is None:
        await ctx.send("❌ ไม่พบผู้ใช้")
        return
    db.ensure_user(user_id, None)
    db.set_freeclient_limit_reset(user_id, thai_now_iso())
    await ctx.send(f"✅ รีเซ็ตสิทธิ์ทดลองใช้ฟรีของ {target} แล้ว")


@bot.command(name="sorting", aliases=["Sorting"])
async def cmd_sorting(ctx: commands.Context, order: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not order:
        await ctx.send(f"ปัจจุบัน: {mycodes_sort_order()} | ใช้: !sorting newest_top หรือ newest_bottom")
        return
    order = order.strip().lower()
    if order not in {"newest_top", "newest_bottom"}:
        await ctx.send("❌ โหมดไม่ถูกต้อง")
        return
    db.set_setting("mycodes_sort_order", order)
    await ctx.send(f"✅ ตั้งการเรียง mycodes เป็น {order}")


@bot.command(name="purchaseinformation", aliases=["Purchaseinformation"])
async def cmd_purchaseinformation(ctx: commands.Context, n: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not n:
        await ctx.send(f"เก็บโค้ดต่อผู้ใช้: {mycodes_store_limit()} | ใช้ !purchaseinformation 100")
        return
    value = parse_int(n)
    if value is None or value < 1:
        await ctx.send("❌ ตัวเลขไม่ถูกต้อง")
        return
    db.set_setting("mycodes_store_limit", str(value))
    if mycodes_display_limit() > value:
        db.set_setting("mycodes_display_limit", str(value))
    await ctx.send(f"✅ ตั้งจำนวนเก็บโค้ดต่อผู้ใช้เป็น {value}")


@bot.command(name="showpurchaselist", aliases=["Showpurchaselist"])
async def cmd_showpurchaselist(ctx: commands.Context, n: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not n:
        await ctx.send(f"แสดงโค้ดต่อผู้ใช้: {mycodes_display_limit()} | ใช้ !showpurchaselist 50")
        return
    value = parse_int(n)
    if value is None or value < 1:
        await ctx.send("❌ ตัวเลขไม่ถูกต้อง")
        return
    value = min(value, mycodes_store_limit())
    db.set_setting("mycodes_display_limit", str(value))
    await ctx.send(f"✅ ตั้งจำนวนแสดง mycodes เป็น {value}")


@bot.command(name="addcode")
async def cmd_addcode(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    code_name = await ask_text(ctx, "addcode", "🎁 ชื่อโค้ด:")
    if not code_name:
        return
    code_name = normalize_credit_code_name(code_name)

    mode = await ask_text(ctx, "addcode", "รูปแบบ: fixed | random | free_reset")
    if not mode:
        return
    mode = mode.strip().lower()
    if mode not in {"fixed", "random", "free_reset"}:
        await ctx.send("❌ mode ไม่ถูกต้อง")
        return

    if mode == "fixed":
        fixed_credit = await ask_text(ctx, "addcode", "เครดิตต่อคน:")
        max_uses = await ask_text(ctx, "addcode", "จำนวนคน (0 = ไม่จำกัด):")
        duration = await ask_text(ctx, "addcode", "อายุโค้ด (วัน):")
        fc = parse_positive_float(fixed_credit or "")
        mu = parse_int(max_uses or "")
        du = parse_int(duration or "")
        if fc is None or mu is None or du is None or du < 1:
            await ctx.send("❌ ข้อมูลไม่ถูกต้อง")
            return
        code_key = credit_code_key(code_name)
        expires_at = (thai_now() + datetime.timedelta(days=du)).isoformat(timespec="seconds")
        ok = db.create_credit_code(code_key, code_name, mode, fc, fc * (mu or 0), mu or 0, expires_at, ctx.author.id, thai_now_iso())
        await ctx.send("✅ สร้างโค้ดแล้ว" if ok else "❌ ชื่อโค้ดซ้ำ")
        return

    if mode == "random":
        total_credit = await ask_text(ctx, "addcode", "เครดิตรวมทั้งหมด:")
        max_uses = await ask_text(ctx, "addcode", "จำนวนคนที่รับได้ (0 = ไม่จำกัด):")
        duration = await ask_text(ctx, "addcode", "อายุโค้ด (วัน):")
        tc = parse_positive_float(total_credit or "")
        mu = parse_int(max_uses or "")
        du = parse_int(duration or "")
        if tc is None or mu is None or du is None or du < 1:
            await ctx.send("❌ ข้อมูลไม่ถูกต้อง")
            return
        code_key = credit_code_key(code_name)
        expires_at = (thai_now() + datetime.timedelta(days=du)).isoformat(timespec="seconds")
        ok = db.create_credit_code(code_key, code_name, mode, 0, tc, mu or 0, expires_at, ctx.author.id, thai_now_iso())
        await ctx.send("✅ สร้างโค้ดแล้ว" if ok else "❌ ชื่อโค้ดซ้ำ")
        return

    max_uses = await ask_text(ctx, "addcode", "จำนวนครั้งรีเซ็ต (0 = ไม่จำกัด):")
    duration = await ask_text(ctx, "addcode", "อายุโค้ด (วัน):")
    mu = parse_int(max_uses or "")
    du = parse_int(duration or "")
    if mu is None or du is None or du < 1:
        await ctx.send("❌ ข้อมูลไม่ถูกต้อง")
        return
    code_key = credit_code_key(code_name)
    expires_at = (thai_now() + datetime.timedelta(days=du)).isoformat(timespec="seconds")
    ok = db.create_credit_code(code_key, code_name, mode, 0, 0, mu or 0, expires_at, ctx.author.id, thai_now_iso())
    await ctx.send("✅ สร้างโค้ดแล้ว" if ok else "❌ ชื่อโค้ดซ้ำ")


@bot.command(name="deletecode")
async def cmd_deletecode(ctx: commands.Context, code_name: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not code_name:
        await ctx.send("รูปแบบ: !deletecode ชื่อโค้ด")
        return
    if db.delete_credit_code(credit_code_key(normalize_credit_code_name(code_name))):
        await ctx.send("✅ ลบโค้ดแล้ว")
    else:
        await ctx.send("❌ ไม่พบโค้ดนี้ในระบบ")


@bot.command(name="checkcode")
async def cmd_checkcode(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    codes = db.get_all_credit_codes()
    if not codes:
        await ctx.send("📭 ยังไม่มีโค้ดในระบบ")
        return
    lines = [f"🎁 โค้ดทั้งหมดในระบบ ({len(codes)} รายการ)", "────────────────────"]
    for idx, code in enumerate(codes, start=1):
        if code["mode"] == "fixed":
            detail = f"เครดิตต่อคน: {format_credit(code['fixed_credit'])} | ใช้แล้ว: {int(code['used_count'])}/{format_limit_count(int(code['max_uses']))}"
        elif code["mode"] == "random":
            remaining = max(0.0, float(code["total_credit"]) - float(code["distributed_credit"]))
            detail = f"เครดิตรวม: {format_credit(code['total_credit'])} | แจกแล้ว: {format_credit(code['distributed_credit'])} | คงเหลือ: {format_credit(remaining)}"
        else:
            detail = f"ผลลัพธ์: รีเซ็ตสิทธิ์ทดลองใช้ฟรี | ใช้แล้ว: {int(code['used_count'])}/{format_limit_count(int(code['max_uses']))}"
        lines += [
            f"[{idx}] {code['code_name']}",
            f"รูปแบบ: {code['mode']}",
            detail,
            f"สถานะ: {'เปิด' if int(code['active']) == 1 else 'ปิด'}",
            f"หมดอายุ: {format_thai_datetime(code['expires_at'])}",
            "────────────────────",
        ]
    await send_long(ctx.channel, "\n".join(lines))


@bot.command(name="statuscode")
async def cmd_statuscode(ctx: commands.Context, status: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if status.lower() not in {"on", "off"}:
        await ctx.send("รูปแบบ: !statuscode on|off")
        return
    db.set_setting(CREDIT_CODE_ENABLED_KEY, "1" if status.lower() == "on" else "0")
    await ctx.send(f"✅ ตั้งสถานะระบบกรอกโค้ดเป็น {status.lower()}")


@bot.command(name="checkusercode")
async def cmd_checkusercode(ctx: commands.Context, code_name: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not code_name:
        await ctx.send("รูปแบบ: !checkusercode ชื่อโค้ด")
        return
    code = db.get_credit_code(credit_code_key(normalize_credit_code_name(code_name)))
    if not code:
        await ctx.send("❌ ไม่พบโค้ดนี้ในระบบ")
        return
    redemptions = db.get_credit_code_redemptions(credit_code_key(normalize_credit_code_name(code_name)))
    if not redemptions:
        await ctx.send(f"📭 ยังไม่มีผู้ใช้กรอกโค้ด {code['code_name']}")
        return
    lines = [f"👥 ผู้ใช้ที่กรอกโค้ด {code['code_name']} ({len(redemptions)} รายการ)", "────────────────────"]
    for idx, item in enumerate(redemptions, start=1):
        lines += [
            f"[{idx}] ผู้ใช้: {_user_display(item['user_id'], item.get('username'))}",
            f"User ID: {item['user_id']}",
            f"เครดิตที่ได้รับ: {format_credit(item['credit_amount'])}" if code["mode"] != "free_reset" else "ผลลัพธ์: รีเซ็ตสิทธิ์ทดลองใช้ฟรี",
            f"เวลา: {format_thai_datetime(item['redeemed_at'])}",
            "────────────────────",
        ]
    await send_long(ctx.channel, "\n".join(lines))


@bot.command(name="logbuy")
async def cmd_logbuy(ctx: commands.Context, target: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not target:
        await ctx.send("รูปแบบ: !logbuy @user|user_id")
        return
    user_id = resolve_user_id(target, ctx.guild)
    if user_id is None:
        await ctx.send("❌ ไม่พบผู้ใช้")
        return
    logs = db.get_buy_log(user_id, display_limit=log_display_limit("buy"))
    if not logs:
        await ctx.send("📭 ยังไม่มีประวัติการซื้อ")
        return
    lines = [f"🧾 ประวัติการซื้อของ {_user_display(user_id, db.get_username(user_id))}", "────────────────────"]
    for idx, entry in enumerate(logs, start=1):
        gb_text = f"{entry['gb']} GB" if entry.get("gb", 0) and float(entry.get("gb", 0)) > 0 else "ไม่จำกัด"
        lines += [
            f"[{idx}] {entry['created_at']}",
            f"โค้ด: {entry['code_name']}",
            f"เครือข่าย: {entry['network']}",
            f"วัน: {entry['days']} | GB: {gb_text} | ราคา: {float(entry['cost']):.2f}",
            f"ลิงก์: {entry['link']}",
            "────────────────────",
        ]
    await send_long(ctx.channel, "\n".join(lines))


@bot.command(name="logfree")
async def cmd_logfree(ctx: commands.Context, target: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not target:
        await ctx.send("รูปแบบ: !logfree @user|user_id")
        return
    user_id = resolve_user_id(target, ctx.guild)
    if user_id is None:
        await ctx.send("❌ ไม่พบผู้ใช้")
        return
    logs = db.get_free_log(user_id, display_limit=log_display_limit("free"))
    if not logs:
        await ctx.send("📭 ยังไม่มีประวัติทดลองใช้ฟรี")
        return
    lines = [f"🧪 ประวัติทดลองใช้ฟรีของ {_user_display(user_id, db.get_username(user_id))}", "────────────────────"]
    for idx, entry in enumerate(logs, start=1):
        gb_text = f"{entry['gb']} GB" if entry.get("gb", 0) and float(entry.get("gb", 0)) > 0 else "ไม่จำกัด"
        lines += [
            f"[{idx}] {entry['created_at']}",
            f"โค้ด: {entry['code_name']}",
            f"เครือข่าย: {entry['network']}",
            f"อายุ: {format_hours(entry['hours'])} ชั่วโมง | GB: {gb_text}",
            f"ลิงก์: {entry['link']}",
            "────────────────────",
        ]
    await send_long(ctx.channel, "\n".join(lines))


@bot.command(name="logbuyall")
async def cmd_logbuyall(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    logs = db.get_buy_log_all(log_display_limit("buy"))
    if not logs:
        await ctx.send("📭 ยังไม่มีประวัติการซื้อในระบบ")
        return
    lines = [f"🧾 ประวัติการซื้อทั้งหมด ({len(logs)} รายการ)", "────────────────────"]
    for idx, entry in enumerate(logs, start=1):
        lines += [
            f"[{idx}] {entry['created_at']}",
            f"ผู้ใช้: {_user_display(entry['user_id'], entry.get('username'))}",
            f"โค้ด: {entry['code_name']}",
            f"เครือข่าย: {entry['network']} | วัน: {entry['days']} | ราคา: {float(entry['cost']):.2f}",
            f"ลิงก์: {entry['link']}",
            "────────────────────",
        ]
    await send_long(ctx.channel, "\n".join(lines))


@bot.command(name="logfreeall")
async def cmd_logfreeall(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    logs = db.get_free_log_all(log_display_limit("free"))
    if not logs:
        await ctx.send("📭 ยังไม่มีประวัติทดลองใช้ฟรีในระบบ")
        return
    lines = [f"🧪 ประวัติทดลองใช้ฟรีทั้งหมด ({len(logs)} รายการ)", "────────────────────"]
    for idx, entry in enumerate(logs, start=1):
        lines += [
            f"[{idx}] {entry['created_at']}",
            f"ผู้ใช้: {_user_display(entry['user_id'], entry.get('username'))}",
            f"โค้ด: {entry['code_name']}",
            f"เครือข่าย: {entry['network']} | อายุ: {format_hours(entry['hours'])} ชั่วโมง",
            f"ลิงก์: {entry['link']}",
            "────────────────────",
        ]
    await send_long(ctx.channel, "\n".join(lines))


@bot.command(name="listaddclient")
async def cmd_listaddclient(ctx: commands.Context, n: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not n:
        await ctx.send(f"ปัจจุบัน: {log_display_limit('buy')} | ใช้: !listaddclient 50")
        return
    value = parse_int(n)
    if value is None or value < 1:
        await ctx.send("❌ ตัวเลขไม่ถูกต้อง")
        return
    db.set_setting(LOG_DISPLAY_LIMIT_BUY_KEY, str(value))
    await ctx.send(f"✅ ตั้งจำนวนแสดง log ซื้อเป็น {value}")


@bot.command(name="listfreeclient")
async def cmd_listfreeclient(ctx: commands.Context, n: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not n:
        await ctx.send(f"ปัจจุบัน: {log_display_limit('free')} | ใช้: !listfreeclient 50")
        return
    value = parse_int(n)
    if value is None or value < 1:
        await ctx.send("❌ ตัวเลขไม่ถูกต้อง")
        return
    db.set_setting(LOG_DISPLAY_LIMIT_FREE_KEY, str(value))
    await ctx.send(f"✅ ตั้งจำนวนแสดง log ฟรีเป็น {value}")


@bot.command(name="runstartflnish")
async def cmd_runstartflnish(ctx: commands.Context, delay: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if delay:
        value = parse_positive_float(delay)
        if value is None:
            await ctx.send("❌ รูปแบบ: !runstartflnish วินาที")
            return
        db.set_setting(RUN_START_FINISH_DELAY_KEY, str(value))
    if not get_run_start_finish_commands():
        set_run_start_finish_commands({"addclient"})
    db.set_setting(RUN_START_FINISH_ENABLED_KEY, "1")
    await ctx.send("✅ เปิดระบบเด้งเมนูหลังจบคำสั่งแล้ว")


@bot.command(name="stopstartflnish")
async def cmd_stopstartflnish(ctx: commands.Context):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    db.set_setting(RUN_START_FINISH_ENABLED_KEY, "0")
    await ctx.send("⛔ ปิดระบบเด้งเมนูหลังจบคำสั่งแล้ว")


@bot.command(name="addrunstartflnish")
async def cmd_addrunstartflnish(ctx: commands.Context, command_name: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not command_name:
        await ctx.send("รูปแบบ: !addrunstartflnish command")
        return
    commands_set = get_run_start_finish_commands()
    commands_set.add(normalize_command_name(command_name))
    set_run_start_finish_commands(commands_set)
    await ctx.send(f"✅ เพิ่ม {command_name} เข้าเด้งเมนูหลังจบคำสั่งแล้ว")


@bot.command(name="deleterunstartflnish")
async def cmd_deleterunstartflnish(ctx: commands.Context, command_name: str = ""):
    if not require_admin(ctx):
        await ctx.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return
    if not command_name:
        await ctx.send("รูปแบบ: !deleterunstartflnish command")
        return
    commands_set = get_run_start_finish_commands()
    command_name = normalize_command_name(command_name)
    if command_name in commands_set:
        commands_set.remove(command_name)
        set_run_start_finish_commands(commands_set)
    await ctx.send(f"✅ ลบ {command_name} ออกจากเด้งเมนูหลังจบคำสั่งแล้ว")


@bot.command(name="help")
async def cmd_help(ctx: commands.Context):
    text = (
        "คำสั่งหลัก:\n"
        "!start\n!mycredit\n!mycodes\n!checkprice\n!addclient\n!freeclient\n!addmycredit\n!entercode\n\n"
        "คำสั่งแอดมิน:\n"
        "!addcredits !deletecredits !setprice !settingsmycredit !checkangpaophone\n"
        "!setangpaophone !setangpaorate !toggleaddclient !buydm !nobuydm\n"
        "!openfreeclient !offfreeclient !freeclientlimit !freeclienttime !freeclientresettime\n"
        "!resetfreeclientlimit !addcode !deletecode !checkcode !statuscode !checkusercode\n"
        "!logbuy !logfree !logbuyall !logfreeall !listaddclient !listfreeclient\n\n"
        "พิมพ์ menubot เพื่อเปิดเมนูควบคุมบน VPS\n"
    )
    await ctx.send(text)


async def _menu_confirm_three_times(channel, author_id: int, action_label: str, flow_prefix: str) -> bool:
    prompts = [
        f"⚠️ {action_label}\nพิมพ์ `ยืนยัน` เพื่อไปต่อ (1/3) หรือ `cancel` เพื่อยกเลิก",
        f"⚠️ {action_label}\nพิมพ์ `ยืนยัน` เพื่อไปต่อ (2/3) หรือ `cancel` เพื่อยกเลิก",
        f"⚠️ {action_label}\nพิมพ์ `ยืนยัน` เพื่อไปต่อ (3/3) หรือ `cancel` เพื่อยกเลิก",
    ]
    for idx, prompt in enumerate(prompts, start=1):
        answer = await ask_text_from_channel(channel, author_id, prompt, flow=f"{flow_prefix}:{idx}", timeout=60)
        if answer is None:
            return False
        if answer.strip() not in {"ยืนยัน", "confirm", "yes", "y"}:
            await channel.send("⛔ ยกเลิกแล้ว")
            return False
    return True


async def _vps_status_text() -> str:
    active = await run_shell_command(f"systemctl is-active {shlex.quote(SERVICE_NAME)}")
    enabled = await run_shell_command(f"systemctl is-enabled {shlex.quote(SERVICE_NAME)}")
    status_line = active[1].strip() or active[2].strip() or "unknown"
    enabled_line = enabled[1].strip() or enabled[2].strip() or "unknown"
    db_path = Path(str(db.DB_PATH))
    db_size = "ไม่พบไฟล์"
    if db_path.exists():
        try:
            db_size = f"{db_path.stat().st_size:,} bytes"
        except Exception:
            db_size = "อ่านขนาดไม่ได้"
    git_info = "ไม่ใช่ git repo"
    if (APP_DIR / ".git").exists():
        branch = await run_shell_command("git rev-parse --abbrev-ref HEAD", cwd=APP_DIR)
        commit = await run_shell_command("git rev-parse --short HEAD", cwd=APP_DIR)
        git_info = f"{(branch[1].strip() or '?')} @ {(commit[1].strip() or '?')}"
    return (
        "📊 สถานะระบบ VPS\n"
        f"Service: {SERVICE_NAME}\n"
        f"Active: {status_line}\n"
        f"Enabled: {enabled_line}\n"
        f"DB: {db_path} ({db_size})\n"
        f"Code: {APP_DIR}\n"
        f"Repo: {git_info}\n"
    )


async def run_menubot_flow(channel, author) -> None:
    if not is_admin(author.id):
        await channel.send("❌ คุณไม่มีสิทธิ์ใช้คำสั่งนี้")
        return

    menu = (
        "🧭 VPS Control Menu\n"
        "1. ถอนการติดตั้ง (ยืนยัน 3 รอบ)\n"
        "2. ดูสถานะการทำงาน\n"
        "3. รีสตาร์ทระบบ แต่ข้อมูลในฐานข้อมูลไม่หาย\n"
        "4. ล้างข้อมูลทั้งหมดในฐานข้อมูล (ยืนยัน 3 รอบ)\n"
        "5. อัปเดตสคิประบบ\n\n"
        "พิมพ์เลข 1-5 หรือ `cancel` เพื่อยกเลิก"
    )
    choice = await ask_text_from_channel(channel, author.id, menu, flow="menubot:choose", timeout=120)
    if choice is None:
        return
    choice = choice.strip()

    safe_service = shlex.quote(SERVICE_NAME)
    if choice == "1":
        if not await _menu_confirm_three_times(channel, author.id, "ถอนการติดตั้ง", "menubot:uninstall"):
            return
        await channel.send("⚙️ กำลังถอนการติดตั้ง...")
        safe_service = shlex.quote(SERVICE_NAME)
        cmd = (
            f"nohup bash -lc 'sleep 1; systemctl stop {safe_service}; systemctl disable {safe_service}; "
            f"rm -f /etc/systemd/system/{SERVICE_NAME}.service; systemctl daemon-reload; "
            f"rm -rf -- {shlex.quote(str(APP_DIR))}' >/tmp/{SERVICE_NAME}_uninstall.log 2>&1 &"
        )
        await run_shell_command(cmd, cwd=APP_DIR, timeout=10)
        return

    if choice == "2":
        await channel.send(await _vps_status_text())
        return

    if choice == "3":
        await channel.send("🔄 กำลังรีสตาร์ทระบบ... ข้อมูลฐานข้อมูลจะยังอยู่ในไฟล์เดิม")
        cmd = f"nohup bash -lc 'sleep 1; systemctl reboot' >/tmp/{SERVICE_NAME}_reboot.log 2>&1 &"
        await run_shell_command(cmd, cwd=APP_DIR, timeout=10)
        return

    if choice == "4":
        if not await _menu_confirm_three_times(channel, author.id, "ล้างข้อมูลทั้งหมดในฐานข้อมูล", "menubot:clear-db"):
            return
        db.clear_all_data()
        await channel.send("🧹 ล้างข้อมูลทั้งหมดในฐานข้อมูลแล้ว")
        return

    if choice == "5":
        await channel.send("⬆️ กำลังอัปเดตสคริประบบ...")
        cmd = (
            f"nohup bash -lc 'cd {shlex.quote(str(APP_DIR))} && git pull --rebase && "
            f".venv/bin/pip install -r requirements.txt && systemctl restart {safe_service}' "
            f">/tmp/{SERVICE_NAME}_update.log 2>&1 &"
        )
        await run_shell_command(cmd, cwd=APP_DIR, timeout=10)
        return

    await channel.send("❌ เลือกไม่ถูกต้อง")


@bot.command(name="menubot")
async def cmd_menubot(ctx: commands.Context):
    await run_menubot_flow(ctx.channel, ctx.author)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if str(message.content or "").strip().lower() == "menubot":
        await run_menubot_flow(message.channel, message.author)
        return
    await bot.process_commands(message)


# ───────────────────────────── startup ─────────────────────────────

def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN or BOT_TOKEN in environment")
    db.init_db()
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
