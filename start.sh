#!/bin/bash

mkdir app 
cp * /app 

echo "📂 Current directory: $(pwd)"
echo "📄 Files in directory:"
ls -la
echo "🚀 Starting bot..."
python bot.py
