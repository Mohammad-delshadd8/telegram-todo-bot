import os
import html
import psycopg2
import asyncio
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional, Set, Dict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =============================
# Config & Admin parsing
# =============================
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]

# Default local timezone (Iran). Can be overridden by env TZ.
TZ_NAME = os.environ.get("TZ", "Asia/Tehran")
TZ = ZoneInfo(TZ_NAME)

def _parse_csv(env_val: Optional[str]) -> List[str]:
    if not env_val:
        return []
    return [x.strip() for x in env_val.split(",") if x.strip()]

_ADMIN_IDS_ENV = os.environ.get("ADMIN_IDS") or os.environ.get("ADMIN_ID") or ""
_ADMIN_USERNAMES_ENV = os.environ.get("ADMIN_USERNAMES", "")

def _parse_admin_ids(raw: str) -> Set[int]:
    out: Set[int] = set()
    for tok in _parse_csv(raw):
        try:
            out.add(int(tok))
        except ValueError:
            pass
    return out

def _parse_admin_usernames(raw: str) -> Set[str]:
    return {tok.lower().lstrip("@") for tok in _parse_csv(raw)}

# Bootstrap admins from ENV (protected)
ADMINS_BY_ID: Set[int] = _parse_admin_ids(_ADMIN_IDS_ENV)
ADMINS_BY_USERNAME: Set[str] = _parse_admin_usernames(_ADMIN_USERNAMES_ENV)

# Thread pool for blocking DB calls
EXECUTOR = ThreadPoolExecutor(max_workers=8)

# =============================
# Utils
# =============================
def is_admin_env(user_id: int, username: Optional[str]) -> bool:
    if user_id in ADMINS_BY_ID:
        return True
    if username and username.lower() in ADMINS_BY_USERNAME:
        return True
    return False

def esc(s: Optional[str]) -> str:
    return html.escape(s or "")

def clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"

def _clamp_hour(h: int) -> int:
    return max(0, min(24, h))

def _within_hours(local_dt: datetime, start_h: int, end_h: int) -> bool:
    """True if local time is within [start_h, end_h) with wrap-around and 24/7 support."""
    h = local_dt.hour
    if start_h == 0 and end_h == 24:
        return True
    start_h = _clamp_hour(start_h)
    end_h = _clamp_hour(end_h)
    if start_h == end_h:
        return False
    if start_h < end_h:
        return start_h <= h < end_h
    return h >= start_h or h < end_h

async def safe_edit_or_send(update: Update, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, message=None):
    """Edit existing message or send new one; tolerate minor BadRequest cases."""
    try:
        if message:
            await message.edit_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await update.message.reply_text(
                text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except BadRequest:
        await update.effective_chat.send_message(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

# =============================
# DB (sync) -> run in executor
# =============================
def _get_conn():
    # Railway Postgres usually requires SSL
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def _init_db_sync():
    """Create/upgrade schema in a migration-safe order."""
    with _get_conn() as conn, conn.cursor() as c:
        # Core tables
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                registered_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks(
                task_id SERIAL PRIMARY KEY,
                admin_id BIGINT,
                user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
                task_text TEXT,
                is_done BOOLEAN DEFAULT FALSE,
                created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        # Add new columns idempotently BEFORE creating indexes that depend on them
        c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS is_daily BOOLEAN DEFAULT TRUE;")
        c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_reset DATE DEFAULT CURRENT_DATE;")
        c.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP NULL;")

        # Settings table for per-user preferences
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings(
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                mute_reminders BOOLEAN DEFAULT FALSE,
                work_start SMALLINT DEFAULT 9,   -- inclusive, 0..23
                work_end SMALLINT DEFAULT 21     -- exclusive, 1..24 (24 means 24/7 with start=0)
            );
            """
        )

        # Dynamic admins table (separate from ENV)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS admins(
                user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
                added_by BIGINT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        # Indexes
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_pending ON tasks(user_id, is_done);")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_completed_at ON tasks(completed_at);")

def _ensure_user_and_settings_sync(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]):
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            INSERT INTO users (user_id, username, first_name, last_name)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name
            """,
            (user_id, username, first_name, last_name),
        )
        c.execute("INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))

def _add_task_sync(admin_id: int, user_id: int, task_text: str):
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("INSERT INTO tasks (admin_id, user_id, task_text) VALUES (%s, %s, %s)", (admin_id, user_id, task_text))

def _toggle_task_sync(task_id: int, user_id: int):
    """Toggle task status and set/clear completed_at accordingly."""
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT is_done FROM tasks WHERE task_id=%s AND user_id=%s", (task_id, user_id))
        row = c.fetchone()
        if not row:
            return
        current = row[0]
        if current:
            c.execute("UPDATE tasks SET is_done = FALSE, completed_at = NULL WHERE task_id=%s AND user_id=%s", (task_id, user_id))
        else:
            c.execute("UPDATE tasks SET is_done = TRUE, completed_at = NOW() AT TIME ZONE 'UTC' WHERE task_id=%s AND user_id=%s", (task_id, user_id))

def _delete_task_sync(task_id: int):
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("DELETE FROM tasks WHERE task_id = %s", (task_id,))

def _get_user_tasks_sync(user_id: int) -> List[Tuple[int, str, bool, datetime]]:
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT task_id, task_text, is_done, created_date
            FROM tasks
            WHERE user_id = %s
            ORDER BY created_date DESC
            """,
            (user_id,),
        )
        return c.fetchall()

def _get_all_users_sync(offset: int = 0, limit: int = 10) -> List[Tuple[int, Optional[str], Optional[str], int, int]]:
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT u.user_id, u.first_name, u.username,
                   COUNT(t.task_id) AS task_count,
                   COALESCE(SUM(CASE WHEN t.is_done THEN 1 ELSE 0 END), 0) AS done_count
            FROM users u
            LEFT JOIN tasks t ON u.user_id = t.user_id
            GROUP BY u.user_id, u.first_name, u.username
            ORDER BY task_count DESC, u.user_id ASC
            OFFSET %s LIMIT %s
            """,
            (offset, limit),
        )
        return c.fetchall()

def _get_users_count_sync() -> int:
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT COUNT(*) FROM users")
        return c.fetchone()[0]

def _get_global_stats_sync():
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users_cnt,
                (SELECT COUNT(*) FROM tasks) AS tasks_cnt,
                (SELECT COALESCE(SUM(CASE WHEN is_done THEN 1 ELSE 0 END), 0) FROM tasks) AS done_cnt
            """
        )
        users_cnt, tasks_cnt, done_cnt = c.fetchone()
        return users_cnt, tasks_cnt, done_cnt

def _get_pending_grouped_sync(limit_per_user: int = 5) -> List[Tuple[int, int, List[str]]]:
    """Return [(user_id, pending_count, sample_texts<=limit_per_user), ...]"""
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT user_id, task_text
            FROM tasks
            WHERE is_done = FALSE
            ORDER BY created_date ASC
            """
        )
        rows = c.fetchall()
    grouped: Dict[int, Tuple[int, List[str]]] = {}
    for user_id, task_text in rows:
        if user_id not in grouped:
            grouped[user_id] = (0, [])
        cnt, samples = grouped[user_id]
        cnt += 1
        if len(samples) < limit_per_user:
            samples.append(task_text)
        grouped[user_id] = (cnt, samples)
    return [(uid, data[0], data[1]) for uid, data in grouped.items()]

def _get_all_settings_map_sync() -> Dict[int, Tuple[bool, int, int]]:
    """Returns {user_id: (mute_reminders, work_start, work_end)}."""
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT u.user_id,
                   COALESCE(s.mute_reminders, FALSE) AS mute_reminders,
                   COALESCE(s.work_start, 9) AS work_start,
                   COALESCE(s.work_end, 21) AS work_end
            FROM users u
            LEFT JOIN user_settings s ON s.user_id = u.user_id
            """
        )
        rows = c.fetchall()
    return {r[0]: (bool(r[1]), int(r[2]), int(r[3])) for r in rows}

def _get_user_settings_sync(user_id: int) -> Tuple[bool, int, int]:
    """Fetch user's settings, ensure defaults exist."""
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        c.execute("SELECT mute_reminders, work_start, work_end FROM user_settings WHERE user_id=%s", (user_id,))
        row = c.fetchone()
    if not row:
        return (False, 9, 21)
    return (bool(row[0]), int(row[1]), int(row[2]))

def _update_user_settings_sync(user_id: int, mute: Optional[bool] = None, start: Optional[int] = None, end: Optional[int] = None):
    """Update settings selectively."""
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
        sets = []
        vals: List[object] = []
        if mute is not None:
            sets.append("mute_reminders=%s")
            vals.append(mute)
        if start is not None:
            sets.append("work_start=%s")
            vals.append(_clamp_hour(start))
        if end is not None:
            sets.append("work_end=%s")
            vals.append(_clamp_hour(end))
        if not sets:
            return
        q = f"UPDATE user_settings SET {', '.join(sets)} WHERE user_id=%s"
        vals.append(user_id)
        c.execute(q, tuple(vals))

# ---------- Dynamic admins (DB) ----------
def _is_admin_db_sync(user_id: int) -> bool:
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("SELECT 1 FROM admins WHERE user_id=%s", (user_id,))
        return c.fetchone() is not None

def _add_admin_sync(target_user_id: int, added_by: int):
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            "INSERT INTO admins (user_id, added_by) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING",
            (target_user_id, added_by),
        )

def _remove_admin_sync(target_user_id: int):
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("DELETE FROM admins WHERE user_id=%s", (target_user_id,))

def _get_admins_db_detailed_sync() -> List[Tuple[int, Optional[str], Optional[str], Optional[int], Optional[datetime]]]:
    """Return DB admins joined with user profile info."""
    with _get_conn() as conn, conn.cursor() as c:
        c.execute(
            """
            SELECT a.user_id, u.first_name, u.username, a.added_by, a.added_at
            FROM admins a
            LEFT JOIN users u ON u.user_id = a.user_id
            ORDER BY a.added_at DESC
            """
        )
        return c.fetchall()

def _get_users_info_sync(user_ids: List[int]) -> Dict[int, Tuple[Optional[str], Optional[str]]]:
    if not user_ids:
        return {}
    with _get_conn() as conn, conn.cursor() as c:
        q = "SELECT user_id, first_name, username FROM users WHERE user_id = ANY(%s)"
        c.execute(q, (user_ids,))
        rows = c.fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}

async def run_db(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(EXECUTOR, func, *args)

# ---------- Admin check (ENV + DB) ----------
async def is_admin_async(user_id: int, username: Optional[str]) -> bool:
    if is_admin_env(user_id, username):
        return True
    return await run_db(_is_admin_db_sync, user_id)

# =============================
# UI / Menus (HTML)
# =============================
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message=None):
    user = update.effective_user
    await run_db(_ensure_user_and_settings_sync, user.id, user.username, user.first_name, user.last_name)

    if await is_admin_async(user.id, user.username):
        keyboard = [
            [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users:0")],
            [InlineKeyboardButton("📊 Global Stats", callback_data="admin_stats")],
            [InlineKeyboardButton("🔧 Admins", callback_data="admins_menu")],
            [InlineKeyboardButton("✅ My Tasks", callback_data="my_tasks")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
        ]
        text = "<b>👑 Admin Panel — Main Menu</b>\n\nWhat do you want to do?"
    else:
        tasks = await run_db(_get_user_tasks_sync, user.id)
        pending = sum(1 for t in tasks if not t[2])
        keyboard = [
            [InlineKeyboardButton("✅ My Tasks", callback_data="my_tasks")],
            [InlineKeyboardButton("➕ New Task", callback_data="add_self_task")],
            [InlineKeyboardButton("📊 My Status", callback_data="my_stats")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
        ]
        text = f"👋 <b>Hello {esc(user.first_name)}</b>\n\n📊 You have <b>{pending}</b> pending task(s)."

    await safe_edit_or_send(update, text, InlineKeyboardMarkup(keyboard), message)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def mytasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_user_tasks_menu(update, update.effective_user.id)

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Let user add a task for themselves via /add."""
    context.user_data["adding_self_task"] = True
    await update.message.reply_text(
        "✏️ Send me the task text to add it to your list.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await is_admin_async(user.id, user.username):
        await update.message.reply_text("❌ Access denied.")
        return
    await show_admin_users_menu(update, page=0)

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    env_id = u.id in ADMINS_BY_ID
    env_un = (u.username or "").lower() in ADMINS_BY_USERNAME
    db_admin = await run_db(_is_admin_db_sync, u.id)
    await update.message.reply_text(
        f"ID: <code>{u.id}</code>\n"
        f"Username: <code>{esc(u.username or '')}</code>\n"
        f"TZ: <code>{esc(TZ_NAME)}</code>\n"
        f"Admin (env/db): <b>{'YES' if (env_id or env_un or db_admin) else 'NO'}</b>\n"
        f" - env:id: <b>{'YES' if env_id else 'NO'}</b>\n"
        f" - env:username: <b>{'YES' if env_un else 'NO'}</b>\n"
        f" - db: <b>{'YES' if db_admin else 'NO'}</b>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def amadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    verdict = await is_admin_async(u.id, u.username)
    await update.message.reply_text(
        f"Admin: <b>{'YES' if verdict else 'NO'}</b>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_settings_menu(update)

async def show_user_tasks_menu(update: Update, user_id: int, message=None):
    tasks = await run_db(_get_user_tasks_sync, user_id)
    if not tasks:
        keyboard = []
        if update.effective_user.id == user_id:
            keyboard.append([InlineKeyboardButton("➕ New Task", callback_data="add_self_task")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
        await safe_edit_or_send(update, "🎉 <b>No tasks!</b>\n\nYou’re all caught up.", InlineKeyboardMarkup(keyboard), message)
        return

    pending = sum(1 for t in tasks if not t[2])
    done = sum(1 for t in tasks if t[2])

    lines = [f"📋 <b>Your Tasks</b>\n", f"📊 Status: ✅ {done} done | ⏳ {pending} pending\n"]
    keyboard = []

    for task_id, task_text, is_done, created_date in tasks[:40]:
        emoji = "✅" if is_done else "⏳"
        created_str = created_date.strftime("%Y-%m-%d %H:%M")
        lines.append(f"{emoji} {esc(task_text)}  <i>({created_str})</i>")
        label = f"{'✅ Done' if not is_done else '↩️ Undo'}: {clip(task_text, 15)}"
        cb = f"{'complete' if not is_done else 'undo'}_{task_id}"
        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    if update.effective_user.id == user_id:
        keyboard.insert(0, [InlineKeyboardButton("➕ New Task", callback_data="add_self_task")])

    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")])
    await safe_edit_or_send(update, "\n".join(lines), InlineKeyboardMarkup(keyboard), message)

async def show_admin_users_menu(update: Update, message=None, page: int = 0, per_page: int = 8):
    total_users = await run_db(_get_users_count_sync)
    offset = page * per_page
    users = await run_db(_get_all_users_sync, offset, per_page)

    lines = ["👥 <b>User Management</b>\n"]
    keyboard = []

    for user_id, first_name, username, task_count, done_count in users:
        uname = f"@{username}" if username else "no-username"
        progress = f"{done_count}/{task_count}" if task_count > 0 else "0"
        lines.append(
            f"👤 <b>{esc(first_name) or str(user_id)}</b> ({esc(uname)})\n"
            f"   📊 Progress: {esc(progress)} | 🆔: <code>{user_id}</code>\n"
            f"   ─────────────────"
        )
        keyboard.append(
            [
                InlineKeyboardButton(f"👀 View {clip(first_name or str(user_id), 12)}", callback_data=f"view_user_{user_id}"),
                InlineKeyboardButton("➕ New Task", callback_data=f"add_task_{user_id}"),
            ]
        )

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_users:{page-1}"))
    if offset + per_page < total_users:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users:{page+1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="main_menu")])
    await safe_edit_or_send(update, "\n".join(lines), InlineKeyboardMarkup(keyboard), message)

async def show_user_detail(update: Update, user_id: int, message=None):
    tasks = await run_db(_get_user_tasks_sync, user_id)
    user_tasks = [t for t in tasks if not t[2]]

    # Admin toggle button
    is_db_admin = await run_db(_is_admin_db_sync, user_id)
    is_env_protected = user_id in ADMINS_BY_ID  # ENV admins are protected
    if is_env_protected:
        admin_btn = InlineKeyboardButton("🛡 Admin (ENV)", callback_data="noop")
    else:
        admin_btn = InlineKeyboardButton(
            "⬆️ Grant Admin" if not is_db_admin else "⬇️ Revoke Admin",
            callback_data=f"toggle_admin_{user_id}"
        )

    lines = [f"👤 <b>User Detail</b>\n", f"🆔 ID: <code>{user_id}</code>", f"📊 Active tasks: <b>{len(user_tasks)}</b>\n"]
    if user_tasks:
        lines.append("📋 <b>Pending tasks:</b>")
        for i, t in enumerate(user_tasks, 1):
            lines.append(f"{i}. {esc(t[1])}")
    else:
        lines.append("🎉 All tasks are done.")

    keyboard = [
        [InlineKeyboardButton("➕ Add Task", callback_data=f"add_task_{user_id}")],
        [InlineKeyboardButton("📊 View all tasks", callback_data=f"view_all_tasks_{user_id}")],
        [admin_btn],
        [InlineKeyboardButton("🔙 Back", callback_data="admin_users:0")],
    ]

    await safe_edit_or_send(update, "\n".join(lines), InlineKeyboardMarkup(keyboard), message)

async def show_admins_menu(update: Update, message=None):
    """Show current admins: ENV (protected) + DB (removable)."""
    db_admins = await run_db(_get_admins_db_detailed_sync)
    env_ids = sorted(list(ADMINS_BY_ID))
    env_infos = await run_db(_get_users_info_sync, env_ids)

    lines = ["🔧 <b>Admins</b>\n"]

    if env_ids:
        lines.append("🛡 <b>ENV Admins (protected)</b>")
        for uid in env_ids:
            name, un = env_infos.get(uid, (None, None))
            label = f"{esc(name) or uid} ({'@'+un if un else 'no-username'})"
            lines.append(f"• {label}")
        lines.append("")

    if db_admins:
        lines.append("🧩 <b>DB Admins</b>")
        for uid, first_name, username, added_by, added_at in db_admins:
            label = f"{esc(first_name) or uid} ({'@'+username if username else 'no-username'})"
            meta = f"added_by={added_by} at {added_at.strftime('%Y-%m-%d %H:%M') if added_at else '-'}"
            lines.append(f"• {label} — <i>{meta}</i>")
        lines.append("")
    else:
        lines.append("No DB admins yet.\n")

    keyboard = [
        [InlineKeyboardButton("➕ Add Admin by ID", callback_data="admin_add_by_id")],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    await safe_edit_or_send(update, "\n".join(lines), InlineKeyboardMarkup(keyboard), message)

async def show_stats(update: Update, message=None):
    users_cnt, tasks_cnt, done_cnt = await run_db(_get_global_stats_sync)
    pending = tasks_cnt - done_cnt
    progress = round((done_cnt / tasks_cnt) * 100, 1) if tasks_cnt > 0 else 0.0

    lines = [
        "📊 <b>Global Stats</b>\n",
        f"👥 Users: <b>{users_cnt}</b>",
        f"📝 Tasks: <b>{tasks_cnt}</b>",
        f"✅ Done: <b>{done_cnt}</b>",
        f"⏳ Pending: <b>{pending}</b>",
        f"📈 Progress: <b>{progress}%</b>\n",
    ]
    users = await run_db(_get_all_users_sync, 0, 50)
    top = []
    for uid, first_name, username, task_count, done_cnt_u in users:
        if task_count > 0:
            pct = round(done_cnt_u * 100.0 / task_count, 1)
            top.append((pct, first_name or str(uid)))
    top.sort(reverse=True)
    if top:
        lines.append("🏆 <b>Top users:</b>")
        for i, (pct, name) in enumerate(top[:5], 1):
            lines.append(f"{i}. {esc(name)} — {pct}%")

    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await safe_edit_or_send(update, "\n".join(lines), InlineKeyboardMarkup(keyboard), message)

async def show_help(update: Update, message=None):
    text = (
        "ℹ️ <b>Task Manager Bot — Help</b>\n\n"
        "🎯 <b>Users:</b>\n"
        "• ✅ My Tasks — view & toggle tasks\n"
        "• ➕ New Task — add a task for yourself\n"
        "• 📊 My Status — quick stats\n"
        "• ⚙️ Settings — mute reminders & working hours\n\n"
        "👑 <b>Admins:</b>\n"
        "• 👥 Manage Users — browse users, add tasks\n"
        "• 🔧 Admins — view/add/remove DB admins\n"
        "• 📊 Global Stats — overall metrics\n\n"
        "⌨️ <b>Commands:</b>\n"
        "/start — main menu\n"
        "/mytasks — my tasks\n"
        "/add — add a new task for yourself\n"
        "/users — user management (admins)\n"
        "/admins — admins overview (admins)\n"
        "/settings — user settings\n"
        "/whoami — show your Telegram info\n"
        "/amadmin — check admin recognition\n"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await safe_edit_or_send(update, text, InlineKeyboardMarkup(keyboard), message)

# =============================
# Settings UI
# =============================
async def show_settings_menu(update: Update, message=None):
    u = update.effective_user
    mute, start_h, end_h = await run_db(_get_user_settings_sync, u.id)
    state = "ON 🔕" if mute else "OFF 🔔"
    tz_line = f"Time zone: <code>{esc(TZ_NAME)}</code>"
    hours_line = f"Working hours: <b>{start_h:02d}:00–{end_h:02d}:00</b>" if not (start_h == 0 and end_h == 24) else "Working hours: <b>24/7</b>"
    text = (
        "⚙️ <b>User Settings</b>\n\n"
        f"{tz_line}\n"
        f"Mute reminders: <b>{state}</b>\n"
        f"{hours_line}\n\n"
        "Use the buttons to toggle mute or adjust hours."
    )
    keyboard = [
        [InlineKeyboardButton("🔕 Toggle Mute", callback_data="toggle_mute")],
        [
            InlineKeyboardButton("⏮ Start −1h", callback_data="start_dec"),
            InlineKeyboardButton("Start +1h ⏭", callback_data="start_inc"),
        ],
        [
            InlineKeyboardButton("⏮ End −1h", callback_data="end_dec"),
            InlineKeyboardButton("End +1h ⏭", callback_data="end_inc"),
        ],
        [
            InlineKeyboardButton("Preset 9–21", callback_data="preset_9_21"),
            InlineKeyboardButton("Preset 24/7", callback_data="preset_24_7"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="main_menu")],
    ]
    await safe_edit_or_send(update, text, InlineKeyboardMarkup(keyboard), message)

# =============================
# Callbacks / Messages
# =============================
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    username = query.from_user.username
    data = query.data

    await query.answer()

    # Clear state on navigation
    if data in ("main_menu", "admin_stats", "help", "settings", "admins_menu") or data.startswith("admin_users"):
        context.user_data.pop("target_user_id", None)
        context.user_data.pop("awaiting_admin_id", None)
        context.user_data.pop("adding_self_task", None)

    if data == "main_menu":
        await show_main_menu(update, context, query.message)

    elif data == "my_tasks":
        await show_user_tasks_menu(update, user_id, query.message)

    elif data == "add_self_task":
        context.user_data["adding_self_task"] = True
        await query.message.edit_text(
            "✏️ Send the task text to add it to <b>your</b> list:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="my_tasks")]]),
            disable_web_page_preview=True,
        )

    elif data.startswith("admin_users"):
        if await is_admin_async(user_id, username):
            page = 0
            if ":" in data:
                _, p = data.split(":")
                page = int(p)
            await show_admin_users_menu(update, query.message, page=page)
        else:
            await query.message.reply_text("❌ Access denied.")

    elif data == "admin_stats":
        if await is_admin_async(user_id, username):
            await show_stats(update, query.message)
        else:
            await query.message.reply_text("❌ Access denied.")

    elif data == "admins_menu":
        if await is_admin_async(user_id, username):
            await show_admins_menu(update, query.message)
        else:
            await query.message.reply_text("❌ Access denied.")

    elif data == "admin_add_by_id":
        if await is_admin_async(user_id, username):
            context.user_data["awaiting_admin_id"] = True
            await query.message.edit_text(
                "👤 Send the numeric <b>Telegram user ID</b> to grant admin.\nExample: <code>123456789</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admins_menu")]]),
                disable_web_page_preview=True,
            )
        else:
            await query.message.reply_text("❌ Access denied.")

    elif data == "my_stats":
        await show_user_tasks_menu(update, user_id, query.message)

    elif data == "help":
        await show_help(update, query.message)

    elif data == "settings":
        await show_settings_menu(update, query.message)

    elif data.startswith("complete_"):
        task_id = int(data.split("_")[1])
        await run_db(_toggle_task_sync, task_id, user_id)
        await show_user_tasks_menu(update, user_id, query.message)

    elif data.startswith("undo_"):
        task_id = int(data.split("_")[1])
        await run_db(_toggle_task_sync, task_id, user_id)
        await show_user_tasks_menu(update, user_id, query.message)

    elif data.startswith("view_user_"):
        if await is_admin_async(user_id, username):
            target_user_id = int(data.split("_")[2])
            await show_user_detail(update, target_user_id, query.message)
        else:
            await query.message.reply_text("❌ Access denied.")

    elif data.startswith("toggle_admin_"):
        if await is_admin_async(user_id, username):
            target_user_id = int(data.split("_")[2])
            if target_user_id in ADMINS_BY_ID:
                await query.message.reply_text("🛡 This admin is protected by ENV and cannot be removed.")
            else:
                if await run_db(_is_admin_db_sync, target_user_id):
                    await run_db(_remove_admin_sync, target_user_id)
                    await query.message.reply_text(f"✅ Admin revoked for <code>{target_user_id}</code>.", parse_mode=ParseMode.HTML)
                else:
                    await run_db(_add_admin_sync, target_user_id, user_id)
                    await query.message.reply_text(f"✅ Admin granted to <code>{target_user_id}</code>.", parse_mode=ParseMode.HTML)
            await show_user_detail(update, target_user_id, query.message)
        else:
            await query.message.reply_text("❌ Access denied.")

    elif data.startswith("add_task_"):
        if await is_admin_async(user_id, username):
            target_user_id = int(data.split("_")[2])
            context.user_data["target_user_id"] = target_user_id
            await query.message.edit_text(
                f"✏️ Send task text for user ID <code>{target_user_id}</code>:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_users:0")]]),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        else:
            await query.message.reply_text("❌ Access denied.")

    elif data.startswith("view_all_tasks_"):
        if await is_admin_async(user_id, username):
            target_user_id = int(data.split("_")[3])
            await show_user_tasks_menu(update, target_user_id, query.message)
        else:
            await query.message.reply_text("❌ Access denied.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free text for:
       - admin adding task to a target user
       - admin adding admin by ID
       - user adding own task
    """
    u = update.effective_user
    text = (update.message.text or "").strip()

    # Admin: add admin by ID
    if context.user_data.get("awaiting_admin_id"):
        if not await is_admin_async(u.id, u.username):
            await update.message.reply_text("❌ Access denied.")
            context.user_data.pop("awaiting_admin_id", None)
            return
        try:
            target_id = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Please send a numeric Telegram user ID.")
            return
        # Ensure user exists in users table (create stub if needed)
        await run_db(_ensure_user_and_settings_sync, target_id, None, None, None)
        await run_db(_add_admin_sync, target_id, u.id)
        context.user_data.pop("awaiting_admin_id", None)
        await update.message.reply_text(
            f"✅ Admin granted to <code>{target_id}</code>.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await show_admins_menu(update)
        return

    # Admin: add task to a target user
    if "target_user_id" in context.user_data:
        if not await is_admin_async(u.id, u.username):
            await update.message.reply_text("❌ Access denied.")
            return
        target_user_id = context.user_data["target_user_id"]
        if not text:
            await update.message.reply_text("❗ Task text is empty.")
            return
        await run_db(_add_task_sync, u.id, target_user_id, text)
        context.user_data.pop("target_user_id", None)
        await update.message.reply_text(
            f"✅ Task added for <code>{target_user_id}</code>.\n📝 {esc(text)}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    # User: add task for themselves
    if context.user_data.get("adding_self_task"):
        if not text:
            await update.message.reply_text("❗ Task text is empty.")
            return
        await run_db(_add_task_sync, u.id, u.id, text)
        context.user_data.pop("adding_self_task", None)
        await update.message.reply_text(
            f"✅ Task added to <b>your</b> list.\n📝 {esc(text)}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open My Tasks", callback_data="my_tasks")]])
        )
        return

    # Fallback
    if await is_admin_async(u.id, u.username):
        await update.message.reply_text("ℹ️ Use the menu to manage users/admins or send a task after selecting a target.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("ℹ️ Use the menu. Tip: press “➕ New Task” to add one.", parse_mode=ParseMode.HTML)

# =============================
# Jobs (asyncio fallback scheduler)
# =============================
async def job_send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Every 2 hours: ping users with pending tasks, respecting settings."""
    try:
        pending_list = await run_db(_get_pending_grouped_sync, 5)
        settings_map = await run_db(_get_all_settings_map_sync)
        bot = context.application.bot
        now_local = datetime.now(tz=TZ)

        for user_id, count_pending, samples in pending_list:
            mute, w_start, w_end = settings_map.get(user_id, (False, 9, 21))
            if mute or not _within_hours(now_local, w_start, w_end):
                continue
            try:
                lines = [
                    f"⏰ <b>Reminder</b>",
                    f"You have <b>{count_pending}</b> pending task(s).",
                ]
                if samples:
                    lines.append("Top items:")
                    for s in samples:
                        lines.append(f"• {esc(s)}")
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("Open My Tasks", callback_data="my_tasks")],
                    [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
                ])
                await bot.send_message(
                    chat_id=user_id,
                    text="\n".join(lines),
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except (Forbidden, BadRequest):
                continue
    except Exception:
        pass

async def job_midnight_rollover(context: ContextTypes.DEFAULT_TYPE):
    """After midnight: send daily performance report, then reset daily tasks."""
    try:
        report = await run_db(_collect_yesterday_report_and_reset_sync, TZ_NAME)
        bot = context.application.bot

        for user_id, total_daily, completed_y in report:
            try:
                pct = round((completed_y / total_daily) * 100, 1) if total_daily > 0 else 0.0
                now_local = datetime.now(tz=TZ)
                y_date = (now_local.date() - timedelta(days=1)).strftime("%Y-%m-%d")
                text = (
                    f"📅 <b>Daily Report — {y_date}</b>\n"
                    f"✅ Completed: <b>{completed_y}</b>\n"
                    f"📝 Total daily tasks: <b>{total_daily}</b>\n"
                    f"📈 Performance: <b>{pct}%</b>\n\n"
                    f"🔄 New day started — tasks refreshed."
                )
                await bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except (Forbidden, BadRequest):
                continue

        admins = list(ADMINS_BY_ID)
        if admins:
            total_users = await run_db(_get_users_count_sync)
            total_completed = sum(x[2] for x in report)
            total_tasks = sum(x[1] for x in report)
            pct_all = round((total_completed / total_tasks) * 100, 1) if total_tasks > 0 else 0.0
            summary = (
                f"🧾 <b>Daily Summary</b>\n"
                f"👥 Users: <b>{total_users}</b>\n"
                f"✅ Completed (yesterday): <b>{total_completed}</b>\n"
                f"📝 Total daily tasks: <b>{total_tasks}</b>\n"
                f"📈 Performance: <b>{pct_all}%</b>"
            )
            for admin_id in admins:
                try:
                    await context.application.bot.send_message(
                        chat_id=admin_id,
                        text=summary,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except (Forbidden, BadRequest):
                    continue
    except Exception:
        pass

def _seconds_until_next_even_hour(now_local: datetime) -> float:
    """Seconds until the next even-hour at minute 00 in local TZ."""
    hour = now_local.hour
    next_hour = (hour + (2 - (hour % 2))) % 24
    day = now_local.date()
    if next_hour <= hour:
        day = day + timedelta(days=1)
    candidate = datetime.combine(day, time(next_hour, 0), tzinfo=TZ)
    delta = (candidate - now_local).total_seconds()
    return max(1.0, delta)

def _seconds_until_local(h: int, m: int, now_local: Optional[datetime] = None) -> float:
    now_local = now_local or datetime.now(tz=TZ)
    candidate = datetime.combine(now_local.date(), time(h, m), tzinfo=TZ)
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return max(1.0, (candidate - now_local).total_seconds())

async def _reminders_loop(app: Application):
    """AsyncIO loop: reminders every 2 hours at :00 local time."""
    print("[SCHED] AsyncIO reminders loop active")
    ctx = SimpleNamespace(application=app)
    while True:
        now_local = datetime.now(tz=TZ)
        sleep_sec = _seconds_until_next_even_hour(now_local)
        await asyncio.sleep(sleep_sec)
        try:
            await job_send_reminders(ctx)
        except Exception:
            pass

async def _midnight_loop(app: Application):
    """AsyncIO loop: daily rollover at 00:05 local time."""
    print("[SCHED] AsyncIO midnight loop active")
    ctx = SimpleNamespace(application=app)
    while True:
        sleep_sec = _seconds_until_local(0, 5)
        await asyncio.sleep(sleep_sec)
        try:
            await job_midnight_rollover(ctx)
        except Exception:
            pass

def _schedule_asyncio_loops(app: Application):
    """Start background loops (no PTB JobQueue required)."""
    loop = asyncio.get_running_loop()
    loop.create_task(_reminders_loop(app), name="reminders_loop")
    loop.create_task(_midnight_loop(app), name="midnight_loop")

# =============================
# Error handling
# =============================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat = None
        if isinstance(update, Update):
            chat = update.effective_chat
        if chat:
            await chat.send_message(
                "⚠️ An error occurred. The team has been notified.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception:
        pass

# =============================
# App bootstrap
# =============================
async def _post_init(app: Application):
    await run_db(_init_db_sync)
    print(f"[BOOT] TZ={TZ_NAME}")
    print(f"[BOOT] Admin IDs (ENV): {sorted(ADMINS_BY_ID)}")
    print(f"[BOOT] Admin Usernames (ENV): {sorted(ADMINS_BY_USERNAME)}")
    _schedule_asyncio_loops(app)

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Initialize DB and start background schedulers after init
    application.post_init = _post_init

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mytasks", mytasks_cmd))
    application.add_handler(CommandHandler("add", add_cmd))
    application.add_handler(CommandHandler("users", users_cmd))
    application.add_handler(CommandHandler("admins", show_admins_menu))
    application.add_handler(CommandHandler("settings", settings_cmd))
    application.add_handler(CommandHandler("whoami", whoami_cmd))
    application.add_handler(CommandHandler("amadmin", amadmin_cmd))

    # Callbacks
    application.add_handler(CallbackQueryHandler(button_click))

    # Messages
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Global error handler
    application.add_error_handler(on_error)

    print("🤖 Task Manager Bot is running (polling).")
    application.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
