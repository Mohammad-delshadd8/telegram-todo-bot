#!/bin/bash

echo "🤖 شروع راه‌اندازی ربات تلگرام..."
echo "📁 بررسی ساختار فایل‌ها:"
ls -la

echo "🔍 بررسی وجود فایل bot.py:"
if [ -f "Todo_Task_bot.py" ]; then
    echo "✅ فایل Todo_Task_bot.py پیدا شد"
else
    echo "❌ فایل Todo_Task_bot.py پیدا نشد"
    echo "📋 لیست کامل فایل‌ها:"
    find . -type f -name "*.py" | head -20
    exit 1
fi

echo "🐍 بررسی پایتون:"
python --version

echo "📦 نصب dependencies:"
pip install -r requirements.txt

echo "🚀 راه‌اندازی ربات..."
python Todo_Task_bot.py
