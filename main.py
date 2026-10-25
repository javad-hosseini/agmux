import asyncio
import html
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from aiogram.exceptions import TelegramAPIError

from db import (
    create_session,
    get_or_create_context,
    get_session,
    init_db,
    list_sessions,
    log_audit,
    set_active_project,
    set_active_session,
    update_session,
)
from artifacts import deliver_artifacts
from queue_manager import queue_mgr
from runner import ACTIVE_PROCS, ARTIFACTS_BASE_URL, BRAIN_ROOT, execute_agy_turn

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agmux")

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ALLOWED_USER_ID_STR = os.getenv("ALLOWED_USER_ID")
PROJECTS_FILE = Path(os.getenv("PROJECTS_FILE", "./projects.json"))

if not BOT_TOKEN or not ALLOWED_USER_ID_STR:
    logger.error("BOT_TOKEN and ALLOWED_USER_ID must be configured in environment or .env file.")
    if "pytest" not in sys.modules:
        print("ERROR: BOT_TOKEN and ALLOWED_USER_ID must be set in .env. Copy .env.example to .env and configure.")

ALLOWED_USER_ID = int(ALLOWED_USER_ID_STR) if ALLOWED_USER_ID_STR and ALLOWED_USER_ID_STR.isdigit() else 0


def load_projects() -> Dict[str, str]:
    if not PROJECTS_FILE.exists():
        logger.warning(f"Projects file {PROJECTS_FILE} not found; using default.")
        return {"agmux": str(Path.cwd().resolve())}
    try:
        data = json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.error(f"Error loading {PROJECTS_FILE}: {e}")
    return {"agmux": str(Path.cwd().resolve())}


PROJECTS = load_projects()
DEFAULT_PROJECT = next(iter(PROJECTS.keys()), "agmux")


def save_allowed_user_id_to_env(user_id: int, env_path: Optional[Path] = None) -> None:
    env_file = env_path or Path(".env")
    if env_file.exists():
        try:
            content = env_file.read_text(encoding="utf-8")
            if "ALLOWED_USER_ID=" in content:
                new_content = re.sub(r"ALLOWED_USER_ID=\d*", f"ALLOWED_USER_ID={user_id}", content)
            else:
                new_content = content + f"\nALLOWED_USER_ID={user_id}\n"
            env_file.write_text(new_content, encoding="utf-8")
            logger.info(f"Persisted ALLOWED_USER_ID={user_id} into {env_file}")
        except Exception as e:
            logger.warning(f"Could not persist ALLOWED_USER_ID to {env_file}: {e}")


# Strict Single-User Auth Middleware with First-Turn Auto-Claim
class OwnerOnlyMiddleware:
    def __init__(self, owner_id: int, env_path: Optional[Path] = None):
        self.owner_id = owner_id
        self.env_path = env_path

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user:
            return

        # Auto-claim mode: if owner_id is 0, auto-bind to the first user
        if self.owner_id == 0:
            self.owner_id = user.id
            global ALLOWED_USER_ID
            ALLOWED_USER_ID = user.id
            save_allowed_user_id_to_env(user.id, self.env_path)
            logger.info(
                f"🎉 Auto-claimed by user {user.full_name} (@{user.username or 'no_handle'}, ID: {user.id})"
            )
            if isinstance(event, Message):
                await event.answer(
                    f"🔐 **Identity Verified!**\n"
                    f"Bound your Telegram ID (`{user.id}`) as the authorized owner of agmux.\n"
                    f"Persisted to `.env`.",
                    parse_mode="Markdown",
                )

        if user.id != self.owner_id:
            logger.warning(
                f"Unauthorized access dropped from user ID: {user.id} (@{user.username})"
            )
            return  # Silently drop unauthorized updates

        return await handler(event, data)


# Keyboards
def main_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📂 Projects"),
                KeyboardButton(text="💬 Sessions"),
                KeyboardButton(text="🆕 New Session"),
            ],
            [
                KeyboardButton(text="📊 Status"),
                KeyboardButton(text="🧠 Brain"),
                KeyboardButton(text="⚙️ Toggle Mode"),
            ],
            [
                KeyboardButton(text="🛑 Cancel"),
                KeyboardButton(text="📝 Note (/btw)"),
                KeyboardButton(text="📖 Help"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def dashboard_inline_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📂 Projects", callback_data="nav_projects"),
                InlineKeyboardButton(text="💬 Sessions", callback_data="nav_sessions"),
                InlineKeyboardButton(text="🆕 New Session", callback_data="nav_new"),
            ],
            [
                InlineKeyboardButton(text="📊 Status", callback_data="nav_status"),
                InlineKeyboardButton(text="🧠 Brain", callback_data="nav_brain"),
                InlineKeyboardButton(text="⚙️ Toggle Mode", callback_data="nav_toggle_mode"),
            ],
            [
                InlineKeyboardButton(text="🛑 Cancel Task", callback_data="nav_cancel"),
                InlineKeyboardButton(text="📖 Help", callback_data="nav_help"),
            ],
        ]
    )


MENU_BUTTON_TEXTS = {
    "📂 Projects",
    "💬 Sessions",
    "🆕 New Session",
    "📊 Status",
    "🧠 Brain",
    "⚙️ Toggle Mode",
    "🛑 Cancel",
    "📝 Note (/btw)",
    "📖 Help",
}


def approval_kb(session_id: int, mode: str) -> InlineKeyboardMarkup:
    if mode == "PLAN":
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Approve & Execute Plan",
                        callback_data=f"exec:{session_id}",
                    ),
                    InlineKeyboardButton(
                        text="🔒 Stay in Plan Mode",
                        callback_data=f"plan:{session_id}",
                    ),
                ]
            ]
        )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔒 Switch back to Plan Mode",
                    callback_data=f"plan:{session_id}",
                )
            ]
        ]
    )


def task_running_kb(session_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🛑 Cancel Current Task",
                    callback_data=f"cancel_run:{session_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Drop Queued Prompt",
                    callback_data=f"clear_queue:{session_id}",
                ),
            ]
        ]
    )


def project_list_kb(active_slug: str) -> InlineKeyboardMarkup:
    buttons = []
    for slug in PROJECTS.keys():
        prefix = "👉 " if slug == active_slug else ""
        buttons.append([
            InlineKeyboardButton(
                text=f"{prefix}{slug}",
                callback_data=f"switch_proj:{slug}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def session_list_kb(sessions: List[Dict[str, Any]], active_id: Optional[int]) -> InlineKeyboardMarkup:
    buttons = []
    for s in sessions:
        sid = s["id"]
        icon = "🟢" if sid == active_id else "⚪"
        mode_icon = "🔍" if s.get("mode") == "PLAN" else "🚀"
        title = s.get("title", "Untitled")[:25]
        btn_text = f"{icon} #{sid} [{mode_icon}] {title}"
        buttons.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"switch_sess:{sid}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Helpers
async def send_chunked_reply(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    chunk_size = 4000
    for i in range(0, max(len(text), 1), chunk_size):
        chunk = text[i : i + chunk_size]
        is_last = (i + chunk_size) >= len(text)
        kb = reply_markup if is_last else None
        try:
            await bot.send_message(chat_id, chunk, reply_markup=kb, parse_mode="Markdown")
        except TelegramAPIError:
            # Fallback to plain text if markdown formatting is invalid
            await bot.send_message(chat_id, chunk, reply_markup=kb)


DEFAULT_DUMMY_TOKEN = "123456789:ABCDEF-mock-token-agmux-placeholder0123"
bot = Bot(token=BOT_TOKEN if BOT_TOKEN else DEFAULT_DUMMY_TOKEN)
dp = Dispatcher()
dp.message.middleware(OwnerOnlyMiddleware(ALLOWED_USER_ID))
dp.callback_query.middleware(OwnerOnlyMiddleware(ALLOWED_USER_ID))


HELP_TEXT = (
    "📖 **agmux Command & Button Reference**\n\n"
    "**Project & Session Navigation:**\n"
    "• `📂 Projects` (/projects) - View projects with tap-to-switch buttons.\n"
    "• `/project <slug>` - Set active project directly.\n"
    "• `🆕 New Session` (/new) - Start a fresh session (starts in read-only PLAN mode).\n"
    "• `💬 Sessions` (/sessions) - List sessions with tap-to-switch buttons.\n"
    "• `/resume <session_id>` - Switch to a session by ID.\n\n"
    "**Execution & Control:**\n"
    "• `[✅ Approve & Execute]` - Approves implementation plan and triggers mutations.\n"
    "• `⚙️ Toggle Mode` (/mode) - Switch between PLAN (read-only) and EXECUTE (write).\n"
    "• `🛑 Cancel` (/cancel) - Send SIGTERM to the running CLI task and clear queue.\n"
    "• `📝 Note (/btw)` (/btw <note>) - Pass an aside note to Antigravity.\n\n"
    "**Inspection & Diagnostics:**\n"
    "• `🧠 Brain` (/brain) - Browse recent native desktop Antigravity conversations.\n"
    "• `📊 Status` (/status) - Check current session mode, process state, and queue depth."
)


# Command Handlers
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    session = await get_session(sid) if sid else None

    sess_desc = (
        f"`#{session['id']}` - **{html.escape(session['title'])}** (Mode: `{session['mode']}`)"
        if session
        else "_None (tap 🆕 New Session to start)_"
    )

    text = (
        "🤖 **Welcome to agmux (Remote Antigravity Agent)**\n\n"
        f"📂 **Active Project:** `{ctx['active_project']}`\n"
        f"💬 **Active Session:** {sess_desc}\n\n"
        "👇 _Tap any button below or send a prompt to begin:_"
    )
    await msg.answer(text, reply_markup=dashboard_inline_kb(), parse_mode="Markdown")
    await msg.answer("🎛️ Keyboard controls ready below:", reply_markup=main_reply_keyboard())


@dp.message(Command("menu"))
async def cmd_menu(msg: Message):
    await msg.answer("🎛️ **agmux Quick Controls Dashboard**", reply_markup=dashboard_inline_kb(), parse_mode="Markdown")


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(HELP_TEXT, reply_markup=main_reply_keyboard(), parse_mode="Markdown")


@dp.message(Command("projects"))
async def cmd_projects(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    lines = [
        f"{'👉' if k == ctx['active_project'] else '•'} **{k}**: `{v}`"
        for k, v in PROJECTS.items()
    ]
    text = "📂 **Configured Projects:**\n" + "\n".join(lines) + "\n\n_Tap below to switch project:_"
    await msg.answer(text, reply_markup=project_list_kb(ctx["active_project"]), parse_mode="Markdown")


@dp.message(Command("project"))
async def cmd_set_project(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or parts[1] not in PROJECTS:
        await msg.answer(
            f"Usage: `/project <slug>`\nAvailable projects: `{', '.join(PROJECTS.keys())}`",
            parse_mode="Markdown",
        )
        return
    slug = parts[1]
    sid = await create_session(msg.from_user.id, slug, f"Initial session for {slug}")
    await msg.answer(
        f"📂 Active project set to `{slug}`.\n🆕 Started **Session #{sid}** (Mode: `PLAN`).",
        parse_mode="Markdown",
    )


@dp.message(Command("new"))
async def cmd_new(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    title = msg.text.partition(" ")[2].strip() or "Untitled session"
    sid = await create_session(msg.from_user.id, ctx["active_project"], title)
    await msg.answer(
        f"🆕 Created **Session #{sid}** on `{ctx['active_project']}` (Mode: `PLAN`).\n"
        f"**Title:** {title}\n\n"
        "Send your prompt to start investigating.",
        parse_mode="Markdown",
    )


@dp.message(Command("sessions"))
async def cmd_sessions(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    rows = await list_sessions(ctx["active_project"])
    if not rows:
        await msg.answer(
            f"No sessions found for project `{ctx['active_project']}`. Use `/new` to create one.",
            parse_mode="Markdown",
        )
        return
    lines = [
        f"{'🟢' if r['id'] == ctx['active_session_id'] else '⚪'} `#{r['id']}` [{r['mode']}] {r['title']}"
        for r in rows
    ]
    text = f"💬 **Sessions ({ctx['active_project']}):**\n" + "\n".join(lines)
    await msg.answer(
        text,
        reply_markup=session_list_kb(rows, ctx["active_session_id"]),
        parse_mode="Markdown",
    )


@dp.message(Command("resume"))
async def cmd_resume(msg: Message):
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        await msg.answer("Usage: `/resume <session_id>`", parse_mode="Markdown")
        return
    sid = int(parts[1])
    sess = await get_session(sid)
    if not sess:
        await msg.answer(f"Session #{sid} not found.", parse_mode="Markdown")
        return
    await set_active_project(msg.from_user.id, sess["project_slug"], sid)
    await msg.answer(
        f"🔄 Resumed **Session #{sid}** (`{sess['project_slug']}` | Mode: `{sess['mode']}`).\n"
        f"**Title:** {sess['title']}",
        parse_mode="Markdown",
    )


@dp.message(Command("mode"))
async def cmd_mode(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    if not sid:
        await msg.answer("No active session. Use `/new` first.")
        return
    sess = await get_session(sid)
    parts = msg.text.split(maxsplit=1)
    if len(parts) < 2:
        await msg.answer(
            f"Current mode for Session #{sid}: `{sess['mode']}`\n"
            "Use `/mode plan` or `/mode execute` to change.",
            parse_mode="Markdown",
        )
        return
    target_mode = parts[1].strip().upper()
    if target_mode not in ("PLAN", "EXECUTE"):
        await msg.answer("Valid modes are: `plan` or `execute`", parse_mode="Markdown")
        return
    await update_session(sid, mode=target_mode)
    await msg.answer(f"✅ Session #{sid} mode updated to `{target_mode}`.", parse_mode="Markdown")


@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    if not sid:
        await msg.answer("No active session.")
        return

    proc = ACTIVE_PROCS.get(sid)
    cleared = queue_mgr.clear_queue(sid)
    if proc and proc.returncode is None:
        proc.terminate()
        await log_audit(sid, "CANCEL", {"action": "proc.terminate", "cleared_queue": cleared})
        await msg.answer(
            f"🛑 Sent SIGTERM to Session #{sid} task."
            + (f" Also cleared {cleared} queued prompt(s)." if cleared else "")
        )
    else:
        if cleared:
            await msg.answer(f"Cleared {cleared} queued prompt(s). No process was actively running.")
        else:
            await msg.answer(f"Session #{sid} is not running any active task.")


@dp.message(Command("btw"))
async def cmd_btw(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    if not sid:
        await msg.answer("No active session. Use `/new` first.")
        return
    note = msg.text.partition(" ")[2].strip()
    if not note:
        await msg.answer("Usage: `/btw <note text>`\nExample: `/btw remember to check python3 tests`")
        return
    queue_mgr.add_btw_note(sid, note)
    await log_audit(sid, "BTW_NOTE", {"note": note})
    await msg.answer(f"📝 Added by-the-way note for Session #{sid}:\n`{note}`", parse_mode="Markdown")


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    sess = await get_session(sid) if sid else None

    running = sid in ACTIVE_PROCS if sid else False
    qsize = queue_mgr.queue_size(sid) if sid else 0
    notes = queue_mgr.peek_btw_notes(sid) if sid else []

    status_str = "RUNNING" if running else ("IDLE" if sess else "NO_SESSION")

    text = (
        f"📊 **System Status**\n\n"
        f"• **Project:** `{ctx['active_project']}`\n"
        f"• **Session ID:** `{sid or 'None'}`\n"
        f"• **Session Title:** {sess['title'] if sess else 'N/A'}\n"
        f"• **Mode:** `{sess['mode'] if sess else 'N/A'}`\n"
        f"• **Status:** `{status_str}`\n"
        f"• **Antigravity Conv ID:** `{sess.get('agy_conv_id') or 'Unlinked'}`\n"
        f"• **Queued Prompts:** `{qsize}`\n"
        f"• **Pending /btw Notes:** `{len(notes)}`"
    )
    await msg.answer(text, parse_mode="Markdown")


@dp.message(Command("brain"))
async def cmd_brain(msg: Message):
    if not BRAIN_ROOT.exists():
        await msg.answer(f"Antigravity brain folder `{BRAIN_ROOT}` does not exist.", parse_mode="Markdown")
        return

    # List up to 5 most recently modified folders in brain
    dirs = [
        d for d in BRAIN_ROOT.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]
    if not dirs:
        await msg.answer("No conversations found in Antigravity brain.", parse_mode="Markdown")
        return

    dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    recent = dirs[:5]

    lines = []
    buttons = []
    for d in recent:
        mtime = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        plan_file = d / "implementation_plan.md"
        walk_file = d / "walkthrough.md"
        artifacts = []
        if plan_file.exists():
            artifacts.append("plan.md")
        if walk_file.exists():
            artifacts.append("walkthrough.md")
        art_str = f" ({', '.join(artifacts)})" if artifacts else ""

        cid = d.name
        lines.append(f"• `{cid[:8]}...` | {mtime}{art_str}")
        buttons.append([
            InlineKeyboardButton(
                text=f"Attach `{cid[:8]}`",
                callback_data=f"attach_brain:{cid}",
            )
        ])

    text = (
        "🧠 **Recent Antigravity Brain Conversations:**\n\n"
        + "\n".join(lines)
        + "\n\n_Tap to link conversation to a new session:_"
    )
    await msg.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")


# Persistent Keyboard Button Handlers
@dp.message(F.text == "📂 Projects")
async def btn_projects(msg: Message):
    await cmd_projects(msg)


@dp.message(F.text == "💬 Sessions")
async def btn_sessions(msg: Message):
    await cmd_sessions(msg)


@dp.message(F.text == "🆕 New Session")
async def btn_new(msg: Message):
    await cmd_new(msg)


@dp.message(F.text == "📊 Status")
async def btn_status(msg: Message):
    await cmd_status(msg)


@dp.message(F.text == "🧠 Brain")
async def btn_brain(msg: Message):
    await cmd_brain(msg)


@dp.message(F.text == "⚙️ Toggle Mode")
async def btn_toggle_mode(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    if not sid:
        await msg.answer("No active session. Tap 🆕 New Session first.", reply_markup=main_reply_keyboard())
        return
    sess = await get_session(sid)
    new_mode = "EXECUTE" if sess.get("mode") == "PLAN" else "PLAN"
    await update_session(sid, mode=new_mode)
    icon = "🚀" if new_mode == "EXECUTE" else "🔍"
    await msg.answer(
        f"{icon} **Session #{sid} mode switched to `{new_mode}`.**",
        reply_markup=main_reply_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(F.text == "🛑 Cancel")
async def btn_cancel(msg: Message):
    await cmd_cancel(msg)


@dp.message(F.text == "📝 Note (/btw)")
async def btn_btw(msg: Message):
    await msg.answer(
        "📝 **Add an Aside Note (/btw)**\n\n"
        "To pass a context note or clarification to Antigravity without interrupting the active task, send:\n"
        "`/btw <your note here>`\n\n"
        "_Example:_ `/btw remember to run pytest before finishing`",
        reply_markup=main_reply_keyboard(),
        parse_mode="Markdown",
    )


@dp.message(F.text == "📖 Help")
async def btn_help(msg: Message):
    await cmd_help(msg)


# Navigation Dashboard Callback Queries
@dp.callback_query(F.data == "nav_projects")
async def cb_nav_projects(cb: CallbackQuery):
    ctx = await get_or_create_context(cb.from_user.id, DEFAULT_PROJECT)
    lines = [
        f"{'👉' if k == ctx['active_project'] else '•'} **{k}**: `{v}`"
        for k, v in PROJECTS.items()
    ]
    text = "📂 **Configured Projects:**\n" + "\n".join(lines) + "\n\n_Tap below to switch project:_"
    await cb.message.edit_text(text, reply_markup=project_list_kb(ctx["active_project"]), parse_mode="Markdown")
    await cb.answer()


@dp.callback_query(F.data == "nav_sessions")
async def cb_nav_sessions(cb: CallbackQuery):
    ctx = await get_or_create_context(cb.from_user.id, DEFAULT_PROJECT)
    rows = await list_sessions(ctx["active_project"])
    if not rows:
        await cb.answer("No sessions found.")
        await cb.message.answer(
            f"No sessions found for project `{ctx['active_project']}`. Tap 🆕 New Session.",
            reply_markup=main_reply_keyboard(),
            parse_mode="Markdown",
        )
        return
    lines = [
        f"{'🟢' if r['id'] == ctx['active_session_id'] else '⚪'} `#{r['id']}` [{r['mode']}] {r['title']}"
        for r in rows
    ]
    text = f"💬 **Sessions ({ctx['active_project']}):**\n" + "\n".join(lines)
    await cb.message.edit_text(
        text,
        reply_markup=session_list_kb(rows, ctx["active_session_id"]),
        parse_mode="Markdown",
    )
    await cb.answer()


@dp.callback_query(F.data == "nav_new")
async def cb_nav_new(cb: CallbackQuery):
    ctx = await get_or_create_context(cb.from_user.id, DEFAULT_PROJECT)
    sid = await create_session(cb.from_user.id, ctx["active_project"], "New session")
    await cb.answer("Created new session!")
    await cb.message.answer(
        f"🆕 Created **Session #{sid}** on `{ctx['active_project']}` (Mode: `PLAN`).\nSend your prompt to begin.",
        reply_markup=main_reply_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "nav_status")
async def cb_nav_status(cb: CallbackQuery):
    ctx = await get_or_create_context(cb.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    sess = await get_session(sid) if sid else None
    running = sid in ACTIVE_PROCS if sid else False
    qsize = queue_mgr.queue_size(sid) if sid else 0
    notes = queue_mgr.peek_btw_notes(sid) if sid else []
    status_str = "RUNNING" if running else ("IDLE" if sess else "NO_SESSION")

    text = (
        f"📊 **System Status**\n\n"
        f"• **Project:** `{ctx['active_project']}`\n"
        f"• **Session ID:** `{sid or 'None'}`\n"
        f"• **Session Title:** {sess['title'] if sess else 'N/A'}\n"
        f"• **Mode:** `{sess['mode'] if sess else 'N/A'}`\n"
        f"• **Status:** `{status_str}`\n"
        f"• **Antigravity Conv ID:** `{sess.get('agy_conv_id') or 'Unlinked'}`\n"
        f"• **Queued Prompts:** `{qsize}`\n"
        f"• **Pending /btw Notes:** `{len(notes)}`"
    )
    await cb.message.edit_text(text, reply_markup=dashboard_inline_kb(), parse_mode="Markdown")
    await cb.answer()


@dp.callback_query(F.data == "nav_brain")
async def cb_nav_brain(cb: CallbackQuery):
    if not BRAIN_ROOT.exists():
        await cb.answer("Brain folder not found.")
        return
    dirs = [d for d in BRAIN_ROOT.iterdir() if d.is_dir() and not d.name.startswith(".")]
    if not dirs:
        await cb.answer("No conversations in brain.")
        return
    dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    recent = dirs[:5]
    lines = []
    buttons = []
    for d in recent:
        mtime = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        cid = d.name
        lines.append(f"• `{cid[:8]}...` | {mtime}")
        buttons.append([
            InlineKeyboardButton(text=f"Attach `{cid[:8]}`", callback_data=f"attach_brain:{cid}")
        ])
    text = "🧠 **Recent Antigravity Brain Conversations:**\n\n" + "\n".join(lines) + "\n\n_Tap to link conversation:_"
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="Markdown")
    await cb.answer()


@dp.callback_query(F.data == "nav_toggle_mode")
async def cb_nav_toggle_mode(cb: CallbackQuery):
    ctx = await get_or_create_context(cb.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    if not sid:
        await cb.answer("No active session.")
        return
    sess = await get_session(sid)
    new_mode = "EXECUTE" if sess.get("mode") == "PLAN" else "PLAN"
    await update_session(sid, mode=new_mode)
    icon = "🚀" if new_mode == "EXECUTE" else "🔍"
    await cb.answer(f"Switched to {new_mode} mode!")
    await cb.message.answer(
        f"{icon} **Session #{sid} mode switched to `{new_mode}`.**",
        reply_markup=main_reply_keyboard(),
        parse_mode="Markdown",
    )


@dp.callback_query(F.data == "nav_cancel")
async def cb_nav_cancel(cb: CallbackQuery):
    ctx = await get_or_create_context(cb.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    if not sid:
        await cb.answer("No active session.")
        return
    proc = ACTIVE_PROCS.get(sid)
    cleared = queue_mgr.clear_queue(sid)
    if proc and proc.returncode is None:
        proc.terminate()
        await cb.answer("Terminated running task!")
        await cb.message.answer(f"🛑 Sent SIGTERM to Session #{sid} task.", reply_markup=main_reply_keyboard())
    else:
        if cleared:
            await cb.answer(f"Cleared {cleared} queued prompt(s).")
            await cb.message.answer(f"Cleared {cleared} queued prompt(s).", reply_markup=main_reply_keyboard())
        else:
            await cb.answer("No active task running.")


@dp.callback_query(F.data == "nav_help")
async def cb_nav_help(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(HELP_TEXT, reply_markup=main_reply_keyboard(), parse_mode="Markdown")


# Callback Queries
@dp.callback_query(F.data.startswith("switch_proj:"))
async def cb_switch_proj(cb: CallbackQuery):
    slug = cb.data.partition(":")[2]
    if slug not in PROJECTS:
        await cb.answer("Project not found.")
        return
    sid = await create_session(cb.from_user.id, slug, f"Session for {slug}")
    await cb.answer(f"Switched to {slug}!")
    await cb.message.edit_text(
        f"📂 Active project set to `{slug}`.\n🆕 Switched to **Session #{sid}** (Mode: `PLAN`).",
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("switch_sess:"))
async def cb_switch_sess(cb: CallbackQuery):
    sid = int(cb.data.partition(":")[2])
    sess = await get_session(sid)
    if not sess:
        await cb.answer("Session not found.")
        return
    await set_active_project(cb.from_user.id, sess["project_slug"], sid)
    await cb.answer(f"Switched to Session #{sid}!")
    await cb.message.edit_text(
        f"🔄 Resumed **Session #{sid}** (`{sess['project_slug']}` | Mode: `{sess['mode']}`).\n"
        f"**Title:** {sess['title']}",
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("attach_brain:"))
async def cb_attach_brain(cb: CallbackQuery):
    conv_id = cb.data.partition(":")[2]
    ctx = await get_or_create_context(cb.from_user.id, DEFAULT_PROJECT)
    sid = await create_session(
        cb.from_user.id, ctx["active_project"], f"Linked desktop session {conv_id[:8]}"
    )
    await update_session(sid, agy_conv_id=conv_id)
    await cb.answer("Conversation attached!")
    await cb.message.answer(
        f"🔗 Created **Session #{sid}** linked to Antigravity conversation `{conv_id}`.\n"
        f"Mode: `PLAN`. Send a prompt to continue.",
        parse_mode="Markdown",
    )


@dp.callback_query(F.data.startswith("cancel_run:"))
async def cb_cancel_run(cb: CallbackQuery):
    sid = int(cb.data.partition(":")[2])
    proc = ACTIVE_PROCS.get(sid)
    if proc and proc.returncode is None:
        proc.terminate()
        await cb.answer("Cancelled running task!")
        await cb.message.answer(f"🛑 Terminated active process for Session #{sid}.")
    else:
        await cb.answer("Task already finished.")


@dp.callback_query(F.data.startswith("clear_queue:"))
async def cb_clear_queue(cb: CallbackQuery):
    sid = int(cb.data.partition(":")[2])
    cleared = queue_mgr.clear_queue(sid)
    await cb.answer(f"Cleared {cleared} queued prompt(s).")
    await cb.message.answer(f"❌ Cleared {cleared} queued prompt(s) for Session #{sid}.")


@dp.callback_query(F.data.startswith(("exec:", "plan:")))
async def cb_mode_switch(cb: CallbackQuery):
    action, _, raw_sid = cb.data.partition(":")
    sid = int(raw_sid)
    session = await get_session(sid)
    if not session:
        await cb.answer("Session not found.")
        return

    if action == "plan":
        await update_session(sid, mode="PLAN")
        try:
            await cb.message.edit_reply_markup(reply_markup=approval_kb(sid, "PLAN"))
        except TelegramAPIError:
            pass
        await cb.answer("Session locked to PLAN mode.")
    elif action == "exec":
        await update_session(sid, mode="EXECUTE")
        await cb.answer("Approved! Starting execution turn...")
        session["mode"] = "EXECUTE"
        status_msg = await cb.message.answer(
            f"🚀 **[{session['project_slug']} | #{sid}]** Executing approved plan...",
            parse_mode="Markdown",
        )
        reply, changed_files, outbox_dir = await execute_agy_turn(
            bot=bot,
            chat_id=cb.message.chat.id,
            session=session,
            project_path=PROJECTS.get(session["project_slug"], str(Path.cwd())),
            user_prompt=(
                "The user approved your implementation plan. Apply the changes now, "
                "run relevant test suites, and write walkthrough.md."
            ),
            status_msg_id=status_msg.message_id,
        )
        # Auto-relock to PLAN mode after execution for safety
        await update_session(sid, mode="PLAN")
        await send_chunked_reply(
            bot=bot,
            chat_id=cb.message.chat.id,
            text=reply,
            reply_markup=approval_kb(sid, "PLAN"),
        )
        if changed_files:
            await deliver_artifacts(
                bot=bot,
                chat_id=cb.message.chat.id,
                files=changed_files,
                base_url=ARTIFACTS_BASE_URL,
                outbox_dir=outbox_dir,
            )


# Prompt Handler with Turn Execution & Async Queue Processing
@dp.message(F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTON_TEXTS))
async def handle_prompt(msg: Message):
    ctx = await get_or_create_context(msg.from_user.id, DEFAULT_PROJECT)
    sid = ctx.get("active_session_id")
    if not sid:
        sid = await create_session(
            msg.from_user.id, ctx["active_project"], msg.text[:60]
        )

    session = await get_session(sid)
    if not session:
        await msg.answer("Error: Active session not found. Send `/new` to create one.")
        return

    # Check if a turn is actively running for this session
    if queue_mgr.is_running(sid) or sid in ACTIVE_PROCS:
        qlen = await queue_mgr.enqueue(sid, msg.text, msg.message_id)
        await log_audit(sid, "PROMPT_QUEUED", {"prompt": msg.text, "position": qlen})
        await msg.answer(
            f"⏳ **Task already running on Session #{sid}**\n\n"
            f"Your prompt has been queued (position **#{qlen}**) and will run automatically "
            "as soon as the current turn completes.",
            reply_markup=task_running_kb(sid),
            parse_mode="Markdown",
        )
        return

    # Acquire session lock and run
    lock = queue_mgr.get_lock(sid)
    async with lock:
        current_prompt = msg.text
        while current_prompt:
            # Refresh session state
            session = await get_session(sid)
            project_path = PROJECTS.get(session["project_slug"], str(Path.cwd()))

            status_msg = await msg.answer(
                f"⏳ **[{session['project_slug']} | #{sid} | {session['mode']}]** Launching Antigravity...",
                parse_mode="Markdown",
            )

            changed_files = []
            outbox_dir = None
            try:
                reply, changed_files, outbox_dir = await execute_agy_turn(
                    bot=bot,
                    chat_id=msg.chat.id,
                    session=session,
                    project_path=project_path,
                    user_prompt=current_prompt,
                    status_msg_id=status_msg.message_id,
                )
            except Exception as e:
                reply = f"❌ Execution error: {e}"

            # Message 1 (status_msg) remains visible in chat with the complete command history.
            # Message 2: Send agent's answer
            kb = approval_kb(sid, session["mode"])
            await send_chunked_reply(bot, msg.chat.id, reply, reply_markup=kb)

            # Message 3+: Deliver artifacts after the answer
            if changed_files:
                await deliver_artifacts(
                    bot=bot,
                    chat_id=msg.chat.id,
                    files=changed_files,
                    base_url=ARTIFACTS_BASE_URL,
                    outbox_dir=outbox_dir,
                )

            # Check if more prompts queued during this turn
            q = queue_mgr.get_queue(sid)
            if not q.empty():
                next_prompt, _ = await q.get()
                q.task_done()
                current_prompt = next_prompt
                await msg.answer(
                    f"⏩ **[Session #{sid}]** Processing queued prompt:\n_{current_prompt[:80]}..._",
                    parse_mode="Markdown",
                )
            else:
                current_prompt = None


async def main():
    if not BOT_TOKEN:
        print("\n" + "=" * 60)
        print("🛑 AGMUX CONFIGURATION REQUIRED")
        print("=" * 60)
        print("Please copy .env.example to .env and configure:")
        print("  1. BOT_TOKEN       (from @BotFather on Telegram)")
        print("=" * 60 + "\n")
        sys.exit(1)

    if ALLOWED_USER_ID == 0:
        logger.info(
            "⚠️ ALLOWED_USER_ID is 0. Auto-claim mode is ACTIVE: "
            "Send /start from your Telegram account to bind as owner."
        )

    logger.info("Initializing SQLite database...")
    await init_db()

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Show main menu & dashboard"),
            BotCommand(command="menu", description="Open quick action dashboard"),
            BotCommand(command="projects", description="List & switch configured projects"),
            BotCommand(command="sessions", description="List & switch project sessions"),
            BotCommand(command="new", description="Start a new session (PLAN mode)"),
            BotCommand(command="status", description="Show active session & queue state"),
            BotCommand(command="mode", description="Toggle between PLAN and EXECUTE mode"),
            BotCommand(command="brain", description="Browse Antigravity native conversations"),
            BotCommand(command="cancel", description="Terminate running CLI turn"),
            BotCommand(command="btw", description="Pass an aside note to Antigravity"),
            BotCommand(command="help", description="Show command reference"),
        ])
        logger.info("Registered Telegram bot commands successfully.")
    except Exception as e:
        logger.warning(f"Could not register Telegram bot commands: {e}")

    logger.info(f"Loaded {len(PROJECTS)} projects. Starting long polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("agmux shut down cleanly.")
