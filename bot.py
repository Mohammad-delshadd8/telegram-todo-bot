import os
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from datetime import datetime

BOT_TOKEN = os.environ['BOT_TOKEN']
DATABASE_URL = os.environ.get('DATABASE_URL')
ADMIN_IDS = os.environ.get('ADMIN_ID', '')
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS.split(',') if admin_id.strip()]

#  DB Conf 
def get_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, 
                  last_name TEXT, registered_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (task_id SERIAL PRIMARY KEY, admin_id BIGINT, user_id BIGINT,
                  task_text TEXT, is_done BOOLEAN DEFAULT FALSE,
                  created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def register_user(user_id, username, first_name, last_name):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''INSERT INTO users (user_id, username, first_name, last_name)
                 VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO NOTHING''', 
                 (user_id, username, first_name, last_name))
    conn.commit()
    conn.close()

def is_admin(user_id):
    return user_id in ADMINS

def add_task_for_user(admin_id, user_id, task_text):
    conn = get_connection()
    c = conn.cursor()
    c.execute('INSERT INTO tasks (admin_id, user_id, task_text) VALUES (%s, %s, %s)', 
              (admin_id, user_id, task_text))
    conn.commit()
    conn.close()

def get_user_tasks(user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('''SELECT task_id, task_text, is_done, created_date 
                 FROM tasks WHERE user_id = %s ORDER BY created_date DESC''', (user_id,))
    tasks = c.fetchall()
    conn.close()
    return tasks

def get_all_users():
    conn = get_connection()
    c = conn.cursor()
    c.execute('''SELECT u.user_id, u.first_name, u.username,
                 COUNT(t.task_id) as task_count,
                 SUM(CASE WHEN t.is_done THEN 1 ELSE 0 END) as done_count
                 FROM users u LEFT JOIN tasks t ON u.user_id = t.user_id
                 GROUP BY u.user_id, u.first_name, u.username
                 ORDER BY task_count DESC''')
    users = c.fetchall()
    conn.close()
    return users

def toggle_task(task_id, user_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('UPDATE tasks SET is_done = NOT is_done WHERE task_id = %s AND user_id = %s', 
              (task_id, user_id))
    conn.commit()
    conn.close()

def delete_task(task_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute('DELETE FROM tasks WHERE task_id = %s', (task_id,))
    conn.commit()
    conn.close()

# UI 
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message=None):
    """منوی اصلی زیبا"""
    user = update.effective_user
    user_id = user.id
    register_user(user_id, user.username, user.first_name, user.last_name)
    
    if is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton("👥 مدیریت کاربران", callback_data="admin_users")],
            [InlineKeyboardButton("📊 آمار کلی", callback_data="admin_stats")],
            [InlineKeyboardButton("✅ تسک‌های من", callback_data="my_tasks")],
            [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")]
        ]
        text = "👑 **پنل مدیریت - منوی اصلی**\n\nسلام ادمین عزیز! چه کاری می‌خواهید انجام دهید؟"
    else:
        tasks = get_user_tasks(user_id)
        pending_tasks = len([t for t in tasks if not t[2]])
        
        keyboard = [
            [InlineKeyboardButton("✅ تسک‌های من", callback_data="my_tasks")],
            [InlineKeyboardButton("📊 وضعیت من", callback_data="my_stats")],
            [InlineKeyboardButton("ℹ️ راهنما", callback_data="help")]
        ]
        text = f"👋 **سلام {user.first_name}!**\n\n📊 شما **{pending_tasks}** تسک در انتظار دارید."

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if message:
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """شروع با منوی زیبا"""
    await show_main_menu(update, context)

async def show_user_tasks_menu(update: Update, user_id: int, message=None):
    """منوی تسک‌های کاربر"""
    tasks = get_user_tasks(user_id)
    
    if not tasks:
        text = "🎉 **هیچ تسکی ندارید!**\n\nهمه کارهایتان را انجام داده‌اید!"
        keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]
    else:
        pending = len([t for t in tasks if not t[2]])
        done = len([t for t in tasks if t[2]])
        
        text = f"📋 **تسک‌های شما**\n\n"
        text += f"📊 وضعیت: ✅ {done} انجام شده | ⏳ {pending} در انتظار\n\n"
        
        keyboard = []
        for task in tasks:
            task_id, task_text, is_done, created_date = task
            emoji = "✅" if is_done else "⏳"
            text += f"{emoji} {task_text}\n"
            
            if not is_done:
                btn = InlineKeyboardButton(f"✅ انجام شد: {task_text[:15]}...", 
                                         callback_data=f"complete_{task_id}")
            else:
                btn = InlineKeyboardButton(f"↩️ برگشت: {task_text[:15]}...", 
                                         callback_data=f"undo_{task_id}")
            keyboard.append([btn])
    
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منو", callback_data="main_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if message:
        await message.edit_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

async def show_admin_users_menu(update: Update, message=None):
    """منوی مدیریت کاربران برای ادمین"""
    users = get_all_users()
    
    text = "👥 **مدیریت کاربران**\n\n"
    keyboard = []
    
    for user in users:
        user_id, first_name, username, task_count, done_count = user
        username_display = f"@{username}" if username else "بدون یوزرنیم"
        progress = f"{done_count}/{task_count}" if task_count > 0 else "۰"
        
        text += f"👤 **{first_name}** ({username_display})\n"
        text += f"   📊 پیشرفت: {progress} | 🆔: `{user_id}`\n"
        text += "   ─────────────────\n"
        
        user_btn = InlineKeyboardButton(
            f"👀 مشاهده {first_name}",
            callback_data=f"view_user_{user_id}"
        )
        add_task_btn = InlineKeyboardButton(
            f"➕ تسک جدید",
            callback_data=f"add_task_{user_id}"
        )
        keyboard.append([user_btn])
        keyboard.append([add_task_btn])
        keyboard.append([])
    
    keyboard.append([InlineKeyboardButton("➕ کاربر جدید", callback_data="add_new_user")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if message:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_user_detail(update: Update, user_id: int, message=None):
    """جزئیات کاربر خاص"""
    tasks = get_user_tasks(user_id)
    user_tasks = [t for t in tasks if not t[2]]  # تسک‌های انجام نشده
    
    text = f"👤 **جزئیات کاربر**\n\n"
    text += f"🆔 آیدی: `{user_id}`\n"
    text += f"📊 تسک‌های فعال: **{len(user_tasks)}**\n\n"
    
    if user_tasks:
        text += "📋 **تسک‌های در انتظار:**\n"
        for i, task in enumerate(user_tasks, 1):
            text += f"{i}. {task[1]}\n"
    else:
        text += "🎉 کاربر همه تسک‌ها را انجام داده است!"
    
    keyboard = [
        [InlineKeyboardButton("➕ اضافه کردن تسک", callback_data=f"add_task_{user_id}")],
        [InlineKeyboardButton("📊 مشاهده همه تسک‌ها", callback_data=f"view_all_tasks_{user_id}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="admin_users")]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if message:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_stats(update: Update, user_id: int, message=None):
    """آمار و گزارشات"""
    users = get_all_users()
    total_tasks = sum([user[3] for user in users])  # task_count
    completed_tasks = sum([user[4] for user in users])  # done_count
    
    text = "📊 **آمار کلی سیستم**\n\n"
    text += f"👥 تعداد کاربران: **{len(users)}**\n"
    text += f"📝 کل تسک‌ها: **{total_tasks}**\n"
    text += f"✅ انجام شده: **{completed_tasks}**\n"
    text += f"⏳ در انتظار: **{total_tasks - completed_tasks}**\n"
    text += f"📈 درصد پیشرفت: **{round((completed_tasks/total_tasks)*100 if total_tasks > 0 else 0, 1)}%**\n\n"
    
    text += "🏆 **برترین کاربران:**\n"
    for i, user in enumerate(users[:5], 1):  # 5 کاربر برتر
        if user[3] > 0:  # اگر تسک دارد
            progress = round((user[4]/user[3])*100, 1)
            text += f"{i}. {user[1]} - {progress}%\n"
    
    keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if message:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_help(update: Update, message=None):
    """منوی راهنما"""
    text = """ℹ️ **راهنمای ربات مدیریت تسک**

🎯 **برای کاربران عادی:**
- ✅ تسک‌های من: مشاهده و مدیریت تسک‌ها
- 📊 وضعیت من: آمار شخصی

👑 **برای ادمین‌ها:**
- 👥 مدیریت کاربران: مشاهده و مدیریت همه کاربران
- 📊 آمار کلی: گزارش کامل سیستم
- ➕ اضافه کردن تسک: برای کاربران مختلف

🔧 **دستورات متنی:**
/start - نمایش منوی اصلی
/mytasks - تسک‌های من (برای کاربران)
/users - مدیریت کاربران (فقط ادمین)

📞 **پشتیبانی:** در صورت مشکل با ادمین تماس بگیرید."""

    keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if message:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode='Markdown')

# Buttons 
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    await query.answer()
    
    if data == "main_menu":
        await show_main_menu(update, context, query.message)
    
    elif data == "my_tasks":
        await show_user_tasks_menu(update, user_id, query.message)
    
    elif data == "admin_users":
        if is_admin(user_id):
            await show_admin_users_menu(update, query.message)
    
    elif data == "admin_stats":
        if is_admin(user_id):
            await show_stats(update, user_id, query.message)
    
    elif data == "my_stats":
        await show_stats(update, user_id, query.message)
    
    elif data == "help":
        await show_help(update, query.message)
    
    elif data.startswith("complete_"):
        task_id = int(data.split('_')[1])
        toggle_task(task_id, user_id)
        await show_user_tasks_menu(update, user_id, query.message)
    
    elif data.startswith("undo_"):
        task_id = int(data.split('_')[1])
        toggle_task(task_id, user_id)
        await show_user_tasks_menu(update, user_id, query.message)
    
    elif data.startswith("view_user_"):
        if is_admin(user_id):
            target_user_id = int(data.split('_')[2])
            await show_user_detail(update, target_user_id, query.message)
    
    elif data.startswith("add_task_"):
        if is_admin(user_id):
            target_user_id = int(data.split('_')[2])
            # ذخیره آیدی کاربر در context برای استفاده بعدی
            context.user_data['target_user_id'] = target_user_id
            await query.message.edit_text(
                f"✏️ لطفاً متن تسک را برای کاربر (آیدی: {target_user_id}) ارسال کنید:",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 لغو", callback_data="admin_users")]])
            )

# UI
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت پیام‌های متنی برای اضافه کردن تسک"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ دسترسی محدود")
        return
    
    if 'target_user_id' in context.user_data:
        target_user_id = context.user_data['target_user_id']
        task_text = update.message.text
        
        add_task_for_user(user_id, target_user_id, task_text)
        
        # پاک کردن داده‌های موقت
        del context.user_data['target_user_id']
        
        await update.message.reply_text(
            f"✅ تسک با موفقیت اضافه شد!\n\n"
            f"👤 کاربر: {target_user_id}\n"
            f"📝 تسک: {task_text}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 منوی اصلی", callback_data="main_menu")]])
        )

def main():
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Functions 
    application.add_handler(CommandHandler("start", start))
    
    # Buttons 
    application.add_handler(CallbackQueryHandler(button_click))
    
    # Messages 
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("🤖 ربات پیشرفته مدیریت تسک راه‌اندازی شد!")
    application.run_polling()

if __name__ == '__main__':
    main()
