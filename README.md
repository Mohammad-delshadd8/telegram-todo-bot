# Task Manager Telegram Bot

A minimal, production-ready task manager bot for Telegram with:
- **Admins UI** (add/remove DB-admins, ENV-admins are protected)  
- **User self-service** (users can add their own tasks)  
- **Smart reminders** every 2 hours (respecting per-user **mute** and **working hours**)  
- **Daily reset + performance report** at **00:05** local time  
- **No JobQueue dependency** — uses an **asyncio** scheduler by default  
- **Safe DB migrations** on boot (idempotent `ALTER ... IF NOT EXISTS`)  
- **Default TZ:** `Asia/Tehran` (override with `TZ`)  

> Built with `python-telegram-bot` (async) + PostgreSQL. UI is in English with emoji, HTML parse mode, and safe escaping.

---

## Table of Contents
- [Features](#features)  
- [Tech Stack](#tech-stack)  
- [Requirements](#requirements)  
- [Environment Variables](#environment-variables)  
- [Quick Start (Local)](#quick-start-local)  
- [Deploy on Railway](#deploy-on-railway)  
- [Commands](#commands)  
- [Admin Roles](#admin-roles)  
- [Scheduling](#scheduling)  
- [Working Hours & Mute](#working-hours--mute)  
- [Database Schema](#database-schema)  
- [Security Notes](#security-notes)  
- [Troubleshooting](#troubleshooting)  
- [FAQ](#faq)  
- [License](#license)

---

## Features
- **Task management**
  - Users: view tasks, toggle done/undo, **add tasks for themselves**.
  - Admins: browse users, add tasks for any user, view per-user details.
- **Admin management**
  - **ENV-admins** from `ADMIN_IDS`/`ADMIN_USERNAMES` are **protected**.
  - **DB-admins** can be added/removed in the bot UI (`🔧 Admins` menu) or per-user page (`Grant/Revoke Admin`).
- **Reminders & reports**
  - **Every 2 hours** reminders (00, 02, 04, …) — delivered only if user isn’t muted and it’s within their working hours.
  - **Daily reset** of daily tasks + **personal performance report** at **00:05** local time.
- **Settings per user**
  - **Mute reminders** toggle
  - **Working hours** window (e.g., `09–21` or `24/7`)
- **Infra-friendly**
  - No PTB JobQueue dependency; **asyncio-based scheduler** avoids warnings and runs anywhere.
  - Idempotent DB init/migrations at startup.

---

## Tech Stack
- **Python** (async)
- **python-telegram-bot** (async)
- **PostgreSQL** (with `sslmode=require` by default, compatible with Railway)
- **asyncio** for scheduling (cron-like loops)

---

## Requirements
- Python 3.11+
- PostgreSQL 12+ (or managed Postgres, e.g., Railway)
- A Telegram Bot Token (from @BotFather)

**Recommended `requirements.txt`:**
```txt
python-telegram-bot>=21.0
psycopg2-binary>=2.9
python-dotenv>=1.0  # optional, if you want to load .env locally
```

---

## Environment Variables
| Name                 | Required | Example / Notes                                             |
|----------------------|----------|-------------------------------------------------------------|
| `BOT_TOKEN`          | ✅       | `123456:ABC-...` BotFather token                            |
| `DATABASE_URL`       | ✅       | `postgres://user:pass@host:port/dbname`                     |
| `TZ`                 | ❌       | Default: `Asia/Tehran` (e.g., `Europe/Paris`)               |
| `ADMIN_IDS`          | ❌       | Comma-separated numeric IDs, e.g., `111,222` (protected)    |
| `ADMIN_USERNAMES`    | ❌       | Comma-separated usernames, e.g., `alice,bob` (protected)    |

> **Note:** ENV-admins are **protected** (cannot be removed via UI). DB-admins are managed in the bot itself.

### Sample `.env`
```env
BOT_TOKEN=123456:ABCDEF_your_token_here
DATABASE_URL=postgres://user:pass@host:5432/mydb
TZ=Asia/Tehran
ADMIN_IDS=123456789,987654321
ADMIN_USERNAMES=yourusername,teammate
```

---

## Quick Start (Local)
1. **Clone** and create a virtual environment
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scriptsctivate
   pip install -r requirements.txt
   ```
2. **Set envs** (either export or use a `.env` loader)
3. **Run**
   ```bash
   python bot.py
   ```
4. DM your bot on Telegram and `/start`.

> DB schema/migrations run automatically at boot. No manual SQL needed.

---

## Deploy on Railway
1. Create a new project and add a **PostgreSQL** addon (or use an existing DB).
2. Add the env vars from the table above.
3. Use the default start command:
   ```bash
   python bot.py
   ```
4. Ensure the service stays **always-on** so the asyncio scheduler can run reminders and daily resets.

---

## Commands
- `/start` — Main menu  
- `/mytasks` — Your tasks  
- `/add` — Add a task for **yourself** (prompts for text)  
- `/users` — User management (admins only)  
- `/admins` — Admins overview (admins only)  
- `/settings` — Mute + working hours  
- `/whoami` — Your Telegram info + TZ + admin flags  
- `/amadmin` — Quick admin check  

> The UI also includes buttons like **➕ New Task**, **⚙️ Settings**, **🔧 Admins**, etc.

---

## Admin Roles
- **ENV-admins** (`ADMIN_IDS`, `ADMIN_USERNAMES`)  
  - Always recognized as admins  
  - **Not removable** via UI  
- **DB-admins** (stored in `admins` table)  
  - Can be added/removed from **🔧 Admins** menu or **User Detail** page  
  - Persist in DB (no redeploy needed)

---

## Scheduling
- **Reminders:** every **2 hours** at minute `00` (local TZ).  
  Uses an **asyncio** loop — no PTB JobQueue needed.
- **Daily reset/report:** **00:05** local time  
  - Sends each user a **Yesterday Performance** recap  
  - Resets all `is_daily` tasks to pending state

> The scheduler runs **in-process**. If the process sleeps/stops, jobs won’t fire (obvious but worth stating).

---

## Working Hours & Mute
- Per-user **mute** toggle (**🔕 Toggle Mute**).
- Per-user **working hours** range; reminders only deliver **inside** that window.  
  Supports wrap-around windows (e.g., `21–06`) and **24/7** preset.

---

## Database Schema
Tables created/updated automatically:

- `users(user_id PK, username, first_name, last_name, registered_date)`
- `tasks(task_id PK, admin_id, user_id FK, task_text, is_done, created_date, is_daily, last_reset, completed_at)`
- `user_settings(user_id PK, mute_reminders, work_start, work_end)`
- `admins(user_id PK, added_by, added_at)`

Indexes:
- `idx_tasks_user` on `tasks(user_id)`
- `idx_tasks_pending` on `tasks(user_id, is_done)`
- `idx_tasks_completed_at` on `tasks(completed_at)`

> Migrations are **idempotent**: new columns are added with `ALTER TABLE ... IF NOT EXISTS`.

---

## Security Notes
- UI uses **HTML parse mode** with proper escaping to avoid Telegram entity parsing issues.
- Admin mutation is restricted to admins; ENV-admins are immutable via UI.
- DB is accessed with SSL (`sslmode=require`) by default; ensure your `DATABASE_URL` supports it (Railway does).

---

## Troubleshooting

**“No JobQueue set up” warnings**  
Not applicable here — we removed JobQueue and use asyncio. If you ever switch back to PTB JobQueue, install:
```bash
pip install "python-telegram-bot[job-queue]"
```

**“UndefinedColumn” on startup**  
You ran an old schema earlier. This bot adds columns with `ALTER TABLE ... IF NOT EXISTS`.  
Just run the new version once; it fixes itself.

**“Can’t parse entities”**  
We use **HTML** with escaping. If you change templates, keep `ParseMode.HTML` and escape dynamic text.

**Reminders not arriving**  
- Check user **Mute** and **Working Hours** in **⚙️ Settings**  
- Ensure the bot process is **running continuously**  
- Confirm `TZ` if timing feels off

**Admin not recognized**  
- Check `ADMIN_IDS`/`ADMIN_USERNAMES` (for protected admins)  
- Or add as DB-admin from **🔧 Admins** → “Add Admin by ID”  

---

## FAQ

**Q: Do I need PTB JobQueue?**  
A: No. The bot uses **asyncio** loops. It’s simpler and avoids extra dependencies.

**Q: Can users add their own tasks?**  
A: Yes — via **➕ New Task** button or `/add`.

**Q: Can admins promote/demote other admins?**  
A: Yes — DB-admins can be added/removed. ENV-admins are protected.

**Q: Does it support overnight working hours (e.g., 21–06)?**  
A: Yup. Reminder delivery checks the hour window and supports wrap-around.

**Q: What timezone does it use?**  
A: Default `Asia/Tehran`. Override with `TZ`.



---

### Screenshots (optional)
You can add a few screenshots/GIFs of:
- Main menu (user vs admin)
- Settings (mute + working hours)
- Admins menu
- User detail with Grant/Revoke Admin

---

**Heads-up:** Keep your Railway service always-on so the scheduler can do its thing. If you scale to multiple instances, convert schedulers to a single-leader pattern or externalize them (e.g., a separate worker).
