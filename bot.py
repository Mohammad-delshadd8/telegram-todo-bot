import os
import html
import psycopg2
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Tuple, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# -----------------------------
# Config
# -----------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_IDS_ENV = os.environ.get("ADMIN_IDS", "")  # e.g. "123,456"
ADMINS: List[int] = [int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip().isdigit()]

# Thread pool for blocking DB calls
EXECUTOR = ThreadPoolExecutor(max_workers=8)


# -----------------------------
# Utilities
# -----------------------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def esc(s: Optional[str]) -> str:
    """HTML-escape user-provided text (None-safe)."""
    return html.escape(s or "")


def clip(s: str, n: int) -> str:
    """Clip string to n chars and add ellipsis if needed."""
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


async def safe_edit_or_send(
    update: Update,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    message=None,
):
    """
    Edit the existing message if provided; otherwise send a new message.
    Avoids failing on 'message is not modified'.
    """
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
    except BadRequest as e:
        # Fallback to sending a new message (covers 'message is not modified' and odd edit errors)
        await update.effective_chat.send_message(
            text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# -----------------------------
# DB (sync) -> run in executor
# -----------------------------
def _get_conn():
    # Railway Postgres typically requires SSL; keep require. Change if your DB doesn't.
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def _init_db_sync():
    with _get_conn() as conn, conn.cursor() as c:
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);")


def _register_user_sync(user_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]):
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


def _add_task_sync(admin_id: int, user_id: int, task_text: str):
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("INSERT INTO tasks (admin_id, user_id, task_text) VALUES (%s, %s, %s)", (admin_id, user_id, task_text))


def _toggle_task_sync(task_id: int, user_id: int):
    with _get_conn() as conn, conn.cursor() as c:
        c.execute("UPDATE tasks SET is_done = NOT is_done WHERE task_id = %s AND user_id = %s", (task_id, user_id))


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


async def run_db(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(EXECUTOR, func, *args)


# -----------------------------
# UI / Menus (HTML parse mode)
# -----------------------------
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message=None):
    user = update.effective_user
    await run_db(_register_user_sync, user.id, user.username, user.first_name, user.last_name)

    if is_admin(user.id):
        keyboard = [
            [InlineKeyboardButton("👥 Manage Users", callback_data="admin_users:0")],
            [InlineKeyboardButton("📊 Global Stats", callback_data="admin_stats")],
            [InlineKeyboardButton("✅ My Tasks", callback_data="my_tasks")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
        ]
        text = "<b>👑 Admin Panel — Main Menu</b>\n\nWhat do you want to do?"
    else:
        tasks = await run_db(_get_user_tasks_sync, user.id)
        pending = sum(1 for t in tasks if not t[2])
        keyboard = [
            [InlineKeyboardButton("✅ My Tasks", callback_data="my_tasks")],
            [InlineKeyboardButton("📊 My Status", callback_data="my_stats")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
        ]
        text = f"👋 <b>Hello {esc(user.first_name)}</b>\n\n📊 You have <b>{pending}</b> pending task(s)."

    await safe_edit_or_send(update, text, InlineKeyboardMarkup(keyboard), message)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)


async def mytasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_user_tasks_menu(update, update.effective_user.id)


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Access denied.")
        return
    await show_admin_users_menu(update, page=0)


async def show_user_tasks_menu(update: Update, user_id: int, message=None):
    tasks = await run_db(_get_user_tasks_sync, user_id)

    if not tasks:
        text = "🎉 <b>No tasks!</b>\n\nYou’re all caught up."
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await safe_edit_or_send(update, text, InlineKeyboardMarkup(keyboard), message)
        return

    pending = sum(1 for t in tasks if not t[2])
    done = sum(1 for t in tasks if t[2])

    lines = [f"📋 <b>Your Tasks</b>\n", f"📊 Status: ✅ {done} done | ⏳ {pending} pending\n"]
    keyboard = []

    # Soft-cap to avoid Telegram 4096-character limit
    for task_id, task_text, is_done, created_date in tasks[:40]:
        emoji = "✅" if is_done else "⏳"
        created_str = created_date.strftime("%Y-%m-%d %H:%M")
        lines.append(f"{emoji} {esc(task_text)}  <i>({created_str})</i>")
        label = f"{'✅ Done' if not is_done else '↩️ Undo'}: {clip(task_text, 15)}"
        cb = f"{'complete' if not is_done else 'undo'}_{task_id}"
        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

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
        [InlineKeyboardButton("🔙 Back", callback_data="admin_users:0")],
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
    for uid, first_name, username, task_count, done_count_ in users:
        if task_count > 0:
            pct = round(done_count_ * 100.0 / task_count, 1)
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
        "• 📊 My Status — quick stats\n\n"
        "👑 <b>Admins:</b>\n"
        "• 👥 Manage Users — browse users, add tasks\n"
        "• 📊 Global Stats — overall metrics\n\n"
        "⌨️ <b>Commands:</b>\n"
        "/start — main menu\n"
        "/mytasks — my tasks\n"
        "/users — user management (admins)\n"
    )
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
    await safe_edit_or_send(update, text, InlineKeyboardMarkup(keyboard), message)


# -----------------------------
# Callbacks / Messages
# -----------------------------
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    await query.answer()

    # Clear add-task state if navigating away
    if data in ("main_menu", "admin_stats", "help") or data.startswith("admin_users"):
        context.user_data.pop("target_user_id", None)

    if data == "main_menu":
        await show_main_menu(update, context, query.message)

    elif data == "my_tasks":
        await show_user_tasks_menu(update, user_id, query.message)

    elif data.startswith("admin_users"):
        if is_admin(user_id):
            page = 0
            if ":" in data:
                _, p = data.split(":")
                page = int(p)
            await show_admin_users_menu(update, query.message, page=page)

    elif data == "admin_stats":
        if is_admin(user_id):
            await show_stats(update, query.message)

    elif data == "my_stats":
        await show_user_tasks_menu(update, user_id, query.message)

    elif data == "help":
        await show_help(update, query.message)

    elif data.startswith("complete_"):
        task_id = int(data.split("_")[1])
        await run_db(_toggle_task_sync, task_id, user_id)
        await show_user_tasks_menu(update, user_id, query.message)

    elif data.startswith("undo_"):
        task_id = int(data.split("_")[1])
        await run_db(_toggle_task_sync, task_id, user_id)
        await show_user_tasks_menu(update, user_id, query.message)

    elif data.startswith("view_user_"):
        if is_admin(user_id):
            target_user_id = int(data.split("_")[2])
            await show_user_detail(update, target_user_id, query.message)

    elif data.startswith("add_task_"):
        if is_admin(user_id):
            target_user_id = int(data.split("_")[2])
            context.user_data["target_user_id"] = target_user_id
            await query.message.edit_text(
                f"✏️ Send task text for user ID <code>{target_user_id}</code>:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="admin_users:0")]]),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )

    elif data.startswith("view_all_tasks_"):
        if is_admin(user_id):
            target_user_id = int(data.split("_")[3])
            await show_user_tasks_menu(update, target_user_id, query.message)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free text when in add-task mode."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Access denied.")
        return

    if "target_user_id" in context.user_data:
        target_user_id = context.user_data["target_user_id"]
        task_text = (update.message.text or "").strip()
        if not task_text:
            await update.message.reply_text("❗ Task text is empty.")
            return

        await run_db(_add_task_sync, user_id, target_user_id, task_text)
        context.user_data.pop("target_user_id", None)

        await update.message.reply_text(
            f"✅ Task added.\n\n👤 User: <code>{target_user_id}</code>\n📝 Task: {esc(task_text)}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]]),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text("ℹ️ Use menu buttons to add tasks.", parse_mode=ParseMode.HTML)


# -----------------------------
# Error handling
# -----------------------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    # Log the exception; on Railway logs are visible in dashboard.
    err = context.error
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
        pass  # Avoid raising from the error handler


# -----------------------------
# App bootstrap
# -----------------------------
async def _init_db_once(app: Application):
    await run_db(_init_db_sync)


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Initialize DB once the bot starts
    application.post_init = _init_db_once

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mytasks", mytasks_cmd))
    application.add_handler(CommandHandler("users", users_cmd))

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
