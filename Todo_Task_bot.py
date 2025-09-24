import os
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

BOT_TOKEN = os.environ['AAEcP39PX6mLgngUsJrdUR2J2KYO93D9Z0g']

# admin lists 
ADMINS = [780949018]  

# DB
def init_db():
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    c = conn.cursor()
    
    # users 
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, 
                  last_name TEXT, registered_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # tables 
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  admin_id INTEGER,
                  user_id INTEGER,
                  task_text TEXT,
                  is_done INTEGER DEFAULT 0,
                  created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY (user_id) REFERENCES users (user_id))''')
    
    conn.commit()
    conn.close()

def register_user(user_id, username, first_name, last_name):
    """ثبت کاربر جدید در دیتابیس"""
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    c = conn.cursor()
    
    c.execute('''INSERT OR IGNORE INTO users (user_id, username, first_name, last_name)
                 VALUES (?, ?, ?, ?)''', (user_id, username, first_name, last_name))
    
    conn.commit()
    conn.close()

def is_admin(user_id):
    """بررسی آیا کاربر ادمین است"""
    return user_id in ADMINS

def add_task_for_user(admin_id, user_id, task_text):
    """اضافه کردن تسک برای کاربر خاص"""
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    c = conn.cursor()
    
    c.execute('''INSERT INTO tasks (admin_id, user_id, task_text, is_done)
                 VALUES (?, ?, ?, 0)''', (admin_id, user_id, task_text))
    
    conn.commit()
    conn.close()

def get_user_tasks(user_id):
    """دریافت تسک‌های یک کاربر"""
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    c = conn.cursor()
    
    c.execute('''SELECT t.task_id, t.task_text, t.is_done, u.first_name, u.username
                 FROM tasks t
                 JOIN users u ON t.user_id = u.user_id
                 WHERE t.user_id = ? 
                 ORDER BY t.created_date DESC''', (user_id,))
    
    tasks = c.fetchall()
    conn.close()
    return tasks

def get_all_users_with_tasks():
    """دریافت همه کاربران به همراه تعداد تسک‌هایشان"""
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    c = conn.cursor()
    
    c.execute('''SELECT u.user_id, u.first_name, u.username,
                 COUNT(t.task_id) as task_count,
                 SUM(CASE WHEN t.is_done = 1 THEN 1 ELSE 0 END) as done_count
                 FROM users u
                 LEFT JOIN tasks t ON u.user_id = t.user_id
                 GROUP BY u.user_id
                 ORDER BY task_count DESC''')
    
    users = c.fetchall()
    conn.close()
    return users

def toggle_task_status(task_id, user_id):
    """تغییر وضعیت تسک"""
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    c = conn.cursor()
    
    c.execute('''UPDATE tasks SET is_done = NOT is_done 
                 WHERE task_id = ? AND user_id = ?''', (task_id, user_id))
    
    conn.commit()
    conn.close()

def delete_task(task_id, admin_id):
    """حذف تسک (فقط توسط ادمین)"""
    conn = sqlite3.connect('tasks.db', check_same_thread=False)
    c = conn.cursor()
    
    c.execute('DELETE FROM tasks WHERE task_id = ? AND admin_id = ?', (task_id, admin_id))
    
    conn.commit()
    conn.close()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستور /start برای همه کاربران"""
    user = update.effective_user
    user_id = user.id
    
    # new user 
    register_user(user_id, user.username, user.first_name, user.last_name)
    
    if is_admin(user_id):
        welcome_text = """👑 **به پنل ادمین خوش آمدید!**

**دستورات ادمین:**
/users - مشاهده همه کاربران
/addtask <آیدی کاربر> <تسک> - اضافه کردن تسک برای کاربر
/mytasks - مشاهده تسک‌های خودتان

**مثال:**
/addtask 1234567 انجام پروژه پایتون
"""
    else:
        welcome_text = """👋 **به ربات مدیریت تسک خوش آمدید!**

**دستورات شما:**
/mytasks - مشاهده تسک‌های محول شده به شما

✅ می‌توانید با کلیک روی دکمه‌ها، وضعیت تسک‌ها را تغییر دهید.
"""
    
    await update.message.reply_text(welcome_text)

async def my_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش تسک‌های کاربر"""
    user_id = update.effective_user.id
    tasks = get_user_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("📭 هیچ تسکی برای شما تعریف نشده است.")
        return
    
    keyboard = []
    task_text = f"📋 **تسک‌های شما ({len(tasks)} مورد):**\n\n"
    
    for i, task in enumerate(tasks, 1):
        task_id, text, is_done, first_name, username = task
        status = "✅" if is_done else "◻️"
        task_text += f"{i}. {status} {text}\n"
        
        toggle_btn = InlineKeyboardButton(
            "✅ انجام شد" if not is_done else "◻️ لغو انجام",
            callback_data=f"usertoggle_{task_id}"
        )
        keyboard.append([toggle_btn])
    
    refresh_btn = InlineKeyboardButton("🔄 بروزرسانی", callback_data="userrefresh")
    keyboard.append([refresh_btn])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(task_text, reply_markup=reply_markup)

# admin part 
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش لیست همه کاربران (فقط ادمین)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ شما دسترسی به این بخش را ندارید.")
        return
    
    users = get_all_users_with_tasks()
    
    if not users:
        await update.message.reply_text("📭 هیچ کاربری ثبت نام نکرده است.")
        return
    
    users_text = "👥 **لیست کاربران:**\n\n"
    keyboard = []
    
    for user in users:
        user_id, first_name, username, task_count, done_count = user
        username_display = f"@{username}" if username else "بدون یوزرنیم"
        
        users_text += f"👤 {first_name} ({username_display})\n"
        users_text += f"📊 تسک‌ها: {done_count}/{task_count} انجام شده\n"
        users_text += f"🆔 آیدی: `{user_id}`\n"
        users_text += "─" * 30 + "\n"
        
        # buttons 
        view_btn = InlineKeyboardButton(
            f"👀 مشاهده تسک‌های {first_name}",
            callback_data=f"adminview_{user_id}"
        )
        add_btn = InlineKeyboardButton(
            f"➕ اضافه کردن تسک",
            callback_data=f"adminadd_{user_id}"
        )
        keyboard.append([view_btn])
        keyboard.append([add_btn])
        keyboard.append([])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(users_text, reply_markup=reply_markup)

async def add_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """اضافه کردن تسک برای کاربر (فقط ادمین)"""
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        await update.message.reply_text("❌ شما دسترسی به این بخش را ندارید.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("""❌ فرمت دستور نادرست است.

✅ فرمت صحیح:
/addtask <آیدی کاربر> <متن تسک>

📌 مثال:
/addtask 1234567 انجام تمرینات فصل ۴
/addtask 1234567 تحویل پروژه تا پایان هفته""")
        return
    
    try:
        target_user_id = int(context.args[0])
        task_text = ' '.join(context.args[1:])
        
        add_task_for_user(user_id, target_user_id, task_text)
        
        await update.message.reply_text(f"✅ تسک جدید برای کاربر {target_user_id} اضافه شد:\n\n📝 {task_text}")
        
    except ValueError:
        await update.message.reply_text("❌ آیدی کاربر باید عددی باشد.")

# buttons management 
async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت کلیک روی دکمه‌های اینلاین"""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    await query.answer()
    
    if data.startswith("usertoggle_"):
        # check task 
        task_id = int(data.split('_')[1])
        toggle_task_status(task_id, user_id)
        await show_user_tasks(query, user_id)
    
    elif data == "userrefresh":
        # update lists 
        await show_user_tasks(query, user_id)
    
    elif data.startswith("adminview_"):
        # user tasks 
        target_user_id = int(data.split('_')[1])
        await show_admin_user_tasks(query, user_id, target_user_id)
    
    elif data.startswith("adminadd_"):
        # new task 
        target_user_id = int(data.split('_')[1])
        await ask_for_task_text(query, target_user_id)
    
    elif data.startswith("admindelete_"):
        # delete task 
        if is_admin(user_id):
            task_id = int(data.split('_')[1])
            target_user_id = int(data.split('_')[2])
            delete_task(task_id, user_id)
            await show_admin_user_tasks(query, user_id, target_user_id)

async def show_user_tasks(query, user_id):
    """نمایش تسک‌های کاربر عادی"""
    tasks = get_user_tasks(user_id)
    
    if not tasks:
        await query.edit_message_text("📭 هیچ تسکی برای شما تعریف نشده است.")
        return
    
    keyboard = []
    task_text = f"📋 **تسک‌های شما ({len(tasks)} مورد):**\n\n"
    
    for i, task in enumerate(tasks, 1):
        task_id, text, is_done, first_name, username = task
        status = "✅" if is_done else "◻️"
        task_text += f"{i}. {status} {text}\n"
        
        toggle_btn = InlineKeyboardButton(
            "✅ انجام شد" if not is_done else "◻️ لغو انجام",
            callback_data=f"usertoggle_{task_id}"
        )
        keyboard.append([toggle_btn])
    
    refresh_btn = InlineKeyboardButton("🔄 بروزرسانی", callback_data="userrefresh")
    keyboard.append([refresh_btn])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(task_text, reply_markup=reply_markup)

async def show_admin_user_tasks(query, admin_id, target_user_id):
    """نمایش تسک‌های یک کاربر خاص برای ادمین"""
    tasks = get_user_tasks(target_user_id)
    
    if not tasks:
        await query.edit_message_text("📭 این کاربر هیچ تسکی ندارد.")
        return
    
    keyboard = []
    task_text = f"📋 **تسک‌های کاربر (آیدی: {target_user_id}):**\n\n"
    
    for i, task in enumerate(tasks, 1):
        task_id, text, is_done, first_name, username = task
        status = "✅" if is_done else "◻️"
        task_text += f"{i}. {status} {text}\n"
        
        delete_btn = InlineKeyboardButton(
            "🗑️ حذف این تسک",
            callback_data=f"admindelete_{task_id}_{target_user_id}"
        )
        keyboard.append([delete_btn])
    
    back_btn = InlineKeyboardButton("🔙 بازگشت به لیست کاربران", callback_data="backtousers")
    add_btn = InlineKeyboardButton("➕ اضافه کردن تسک جدید", callback_data=f"adminadd_{target_user_id}")
    keyboard.append([add_btn])
    keyboard.append([back_btn])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(task_text, reply_markup=reply_markup)

async def ask_for_task_text(query, target_user_id):
    """درخواست متن تسک از ادمین"""
    text = f"✏️ لطفاً متن تسک را برای کاربر (آیدی: {target_user_id}) ارسال کنید:"
    await query.edit_message_text(text)

# main 
def main():
    init_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("mytasks", my_tasks))
    

    application.add_handler(CommandHandler("users", list_users))
    application.add_handler(CommandHandler("addtask", add_task_command))


    application.add_handler(CallbackQueryHandler(button_click))
    
    print("🤖 ربات مدیریت تسک (نسخه ادمین) راه‌اندازی شد...")
    application.run_polling()

if __name__ == '__main__':
    main()
