#!/bin/bash
# 라씨 매매비서 봇 서버 시작 스크립트 (Ubuntu Linux 기준)

echo "Starting Lassi Trading Bot..."
cd "$(dirname "$0")"

# 파이썬 가상환경 활성화 (존재하는 경우)
if [ -d "venv" ]; then
    source venv/bin/activate
fi

# 봇 실행
export FLASK_APP=base/app.py
export FLASK_ENV=production
nohup python base/app.py > bot_stdout.log 2> bot_stderr.log &

echo "Bot is running in the background! (PID: $!)"
