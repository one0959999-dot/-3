#!/bin/bash
# EC2 최초 배포 스크립트 (Ubuntu 22.04 기준)
# 실행: bash deploy_ec2.sh

set -e

APP_DIR="/home/ubuntu/lassi_bot"
VENV="$APP_DIR/venv"
SERVICE="lassibot"

echo "=== 1. 시스템 패키지 설치 ==="
sudo apt-get update -y
sudo apt-get install -y python3.11 python3.11-venv python3-pip git

echo "=== 2. 코드 클론 (이미 있으면 pull) ==="
if [ -d "$APP_DIR/.git" ]; then
    cd "$APP_DIR" && git pull
else
    git clone https://github.com/one0959999-dot/-3.git "$APP_DIR"
    cd "$APP_DIR"
fi

echo "=== 3. 가상환경 생성 및 패키지 설치 ==="
python3.11 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== 4. systemd 서비스 등록 ==="
sudo cp "$APP_DIR/lassibot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE"
sudo systemctl restart "$SERVICE"

echo "=== 5. 상태 확인 ==="
sleep 2
sudo systemctl status "$SERVICE" --no-pager

echo ""
echo "=== 배포 완료 ==="
echo "로그 확인: tail -f $APP_DIR/lassi_bot.log"
echo "서비스 재시작: sudo systemctl restart $SERVICE"
