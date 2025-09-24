#!/bin/bash

mkdir app 
cp * /app 
pip install "python-telegram-bot[job-queue]
echo "📂 Current directory: $(pwd)"
echo "📄 Files in directory:"
ls -la
echo "🚀 Starting bot..."
python bot.py
