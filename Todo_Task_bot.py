import os
import psycopg2
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# Environment Variables
BOT_TOKEN = os.environ['BOT_TOKEN']
DATABASE_URL = os.environ['DATABASE_URL']

# Admin Config
ADMIN_IDS = os.environ.get('ADMIN_ID', '')
ADMINS = [int(admin_id.strip()) for admin_id in ADMIN_IDS.split(',') if admin_id.strip()]

# -- PostgreSQL --
def get_connection():
    """ایجاد اتصال به دیتابیس"""
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def init_db():
    """ایجاد جدول‌های لازم"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id BIGINT PRIMARY KEY, 
                  username TEXT, 
                  first_name TEXT, 
                  last_name TEXT, 
                  registered_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (task_id SERIAL PRIMARY KEY,
                  admin_id BIGINT,
                  user_id BIGINT,
                  task_text TEXT,
                  is_done BOOLEAN DEFAULT FALSE,
                  created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    conn.commit()
    conn.close()
    print("✅ دیتابیس آماده است")

def register_user(user_id, username, first_name, last_name):
    """ثبت کاربر جدید"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''INSERT INTO users (user_id, username, first_name, last_name)
                 VALUES (%s, %s, %s, %s)
                 ON CONFLICT (user_id) DO NOTHING''', 
                 (user_id, username, first_name, last_name))
    
    conn.commit()
    conn.close()

def is_admin(user_id):
    """بررسی دسترسی ادمین"""
    return user_id in ADMINS

def add_task_for_user(admin_id, user_id, task_text):
    """اضافه کردن تسک برای کاربر"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''INSERT INTO tasks (admin_id, user_id, task_text, is_done)
                 VALUES (%s, %s, %s, FALSE)''', (admin_id, user_id, task_text))
    
    conn.commit()
    conn.close()

def get_user_tasks(user_id):
    """دریافت تسک‌های کاربر"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''SELECT t.task_id, t.task_text, t.is_done, u.first_name
                 FROM tasks t
                 JOIN users u ON t.user_id = u.user_id
                 WHERE t.user_id = %s 
                 ORDER BY t.created_date DESC''', (user_id,))
    
    tasks = c.fetchall()
    conn.close()
    return tasks

def get_all_users_with_tasks():
    """دریافت همه کاربران"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''SELECT u.user_id, u.first_name, u.username,
                 COUNT(t.task_id) as task_count,
                 SUM(CASE WHEN t.is_done = TRUE THEN 1 ELSE 0 END) as done_count
                 FROM users u
                 LEFT JOIN tasks t ON u.user_id = t.user_id
                 GROUP BY u.user_id, u.first_name, u.username
                 ORDER BY task_count DESC''')
    
    users = c.fetchall()
    conn.close()
    return users

def toggle_task_status(task_id, user_id):
    """تغییر وضعیت تسک"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('''UPDATE tasks SET is_done = NOT is_done 
                 WHERE task_id = %s AND user_id = %s''', (task_id, user_id))
    
    conn.commit()
    conn.close()

def delete_task(task_id, admin_id):
    """حذف تسک"""
    conn = get_connection()
    c = conn.cursor()
    
    c.execute('DELETE FROM tasks WHERE task_id = %s AND admin_id = %s', (task_id, admin_id))
    
    conn.commit()
    conn.close()

# Bot Conf
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور شروع"""
    user = update.effective_user
    user_id = user.id
    
    register_user(user_id, user.username, user.first_name, user.last_name)
    
    if is_admin(user_id):
        text = """👑 **به پنل ادمین خوش آمدید!**

**دستورات:**
/users - مشاهده کاربران
/addtask <آیدی> <تسک> - اضافه کردن تسک

**مثال:**
/addtask 1234567 انجام پروژه"""
    else:
        text = """👋 **به ربات مدیریت تسک خوش آمدید!**

/mytasks - مشاهده تسک‌های شما"""
    
    await update.message.reply_text(text)

async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش تسک‌های کاربر"""
    user_id = update.effective_user.id
    tasks = get_user_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("📭 هیچ تسکی برای شما تعریف نشده است.")
        return
    
    keyboard = []
    task_text = "📋 **تسک‌های شما:**\n\n"
    
    for i, task in enumerate(tasks, 1):
        task_id, text, is_done, first_name = task
        status = "✅" if is_done else "◻️"
        task_text += f"{i}. {status} {text}\n"
        
        btn_text = "✅ انجام شد" if not is_done else "◻️ لغو انجام"
        toggle_btn = InlineKeyboardButton(btn_text, callback_data=f"toggle_{task_id}")
        keyboard.append([toggle_btn])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(task_text, reply_markup=reply_markup)

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """لیست کاربران برای ادمین"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ دسترسی محدود")
        return
    
    users = get_all_users_with_tasks()
    
    if not users:
        await update.message.reply_text("📭 هیچ کاربری ثبت نام نکرده است.")
        return
    
    users_text = "👥 **لیست کاربران:**\n\n"
    keyboard = []
    
    for user in users:
        user_id, first_name, username, task_count, done_count = user
        users_text += f"👤 {first_name} (@{username or 'بدون یوزرنیم'})\n"
        users_text += f"📊 تسک‌ها: {done_count}/{task_count} انجام شده\n"
        users_text += f"🆔 آیدی: {user_id}\n"
        users_text += "─" * 20 + "\n"
        
        view_btn = InlineKeyboardButton(f"مشاهده تسک‌های {first_name}", callback_data=f"view_{user_id}")
        add_btn = InlineKeyboardButton(f"اضافه کردن تسک", callback_data=f"add_{user_id}")
        keyboard.append([view_btn])
        keyboard.append([add_btn])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(users_text, reply_markup=reply_markup)

async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اضافه کردن تسک"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ دسترسی محدود")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("❌ فرمت: /addtask <آیدی> <تسک>")
        return
    
    try:
        target_user_id = int(context.args[0])
        task_text = ' '.join(context.args[1:])
        add_task_for_user(user_id, target_user_id, task_text)
        await update.message.reply_text(f"✅ تسک اضافه شد:\n{task_text}")
    except ValueError:
        await update.message.reply_text("❌ آیدی باید عدد باشد")

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک دکمه‌ها"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    await query.answer()
    
    if data.startswith("toggle_"):
        task_id = int(data.split('_')[1])
        toggle_task_status(task_id, user_id)
        await show_user_tasks(query, user_id)
    
    elif data.startswith("view_"):
        target_user_id = int(data.split('_')[1])
        await show_user_tasks_admin(query, target_user_id)
    
    elif data.startswith("add_"):
        target_user_id = int(data.split('_')[1])
        await query.edit_message_text(f"✏️ متن تسک برای کاربر {target_user_id} را بنویسید:")
    
    elif data.startswith("delete_"):
        if is_admin(user_id):
            parts = data.split('_')
            task_id = int(parts[1])
            target_user_id = int(parts[2])
            delete_task(task_id, user_id)
            await show_user_tasks_admin(query, target_user_id)

async def show_user_tasks(query, user_id):
    """نمایش تسک‌های کاربر"""
    tasks = get_user_tasks(user_id)
    
    if not tasks:
        await query.edit_message_text("📭 هیچ تسکی ندارید")
        return
    
    keyboard = []
    task_text = "📋 **تسک‌های شما:**\n\n"
    
    for i, task in enumerate(tasks, 1):
        task_id, text, is_done, first_name = task
        status = "✅" if is_done else "◻️"
        task_text += f"{i}. {status} {text}\n"
        
        btn_text = "✅ انجام شد" if not is_done else "◻️ لغو انجام"
        toggle_btn = InlineKeyboardButton(btn_text, callback_data=f"toggle_{task_id}")
        keyboard.append([toggle_btn])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(task_text, reply_markup=reply_markup)

async def show_user_tasks_admin(query, target_user_id):
    """نمایش تسک‌های کاربر برای ادمین"""
    tasks = get_user_tasks(target_user_id)
    
    if not tasks:
        await query.edit_message_text("📭 کاربر تسکی ندارد")
        return
    
    keyboard = []
    task_text = f"📋 **تسک‌های کاربر {target_user_id}:**\n\n"
    
    for i, task in enumerate(tasks, 1):
        task_id, text, is_done, first_name = task
        status = "✅" if is_done else "◻️"
        task_text += f"{i}. {status} {text}\n"
        
        delete_btn = InlineKeyboardButton("🗑️ حذف", callback_data=f"delete_{task_id}_{target_user_id}")
        keyboard.append([delete_btn])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(task_text, reply_markup=reply_markup)

def main():
    """تابع اصلی"""
    init_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mytasks", my_tasks))
    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("addtask", add_task_command))
    application.add_handler(CallbackQueryHandler(button_click))
    
    print("🤖 ربات مدیریت تسک راه‌اندازی شد...")
    application.run_polling()

if __name__ == '__main__':
    main()
