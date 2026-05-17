import json
import sqlite3
import sys

conn = sqlite3.connect('/home/ubuntu/lassi_bot/lassi.db')
c = conn.cursor()

# Get the last log message
import logging
import traceback
sys.path.append('/home/ubuntu/lassi_bot')
from bot_controller import manager
from app import app
with app.app_context():
    bot = manager.get_bot(1)
    if bot:
        print("Bot 1 in memory logs:")
        for log in bot.logs:
            if "상태 저장 실패" in log['message'] or "Exception" in log['message'] or "오류" in log['message']:
                print(log['message'])
    else:
        print("Bot 1 not loaded.")
