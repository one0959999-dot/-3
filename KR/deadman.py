"""Dead-man 감시 — 봇이 정상 실행 안 되면(heartbeat 오래됨) 경보.

봇(live_v1/auto_order)이 돌 때마다 heartbeat.txt를 갱신함. 이 스크립트를 매일 크론으로 돌려
마지막 정상실행이 STALE_DAYS일 넘으면 "봇이 멈췄다" 텔레그램 경보.
독립 실행(봇과 별개)이라 봇 자체가 죽어도 이건 살아서 알림.

크론(매일 09:00 KST): 0 0 * * * cd /home/ubuntu/lassi_bot && venv/bin/python KR/deadman.py
"""
import sys, os, sqlite3, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
HEARTBEAT = P('heartbeat.txt')
STALE_DAYS = 8  # 주간 봇이 8일 넘게 안 뛰면 이상


def tg(msg):
    try:
        c = sqlite3.connect(P('lassi.db'), timeout=30)
        r = c.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); c.close()
        from base.telegram_bot import TelegramNotifier
        TelegramNotifier(r[0], r[1]).send_message(msg); return True
    except Exception:
        return False


def main():
    if not os.path.exists(HEARTBEAT):
        tg("⚠️ [Dead-man] heartbeat 파일 없음 — 봇이 한 번도 정상실행 안 됨. 점검 필요.")
        print("heartbeat 없음"); return 1
    raw = open(HEARTBEAT).read().strip()
    ts_str = raw.split('|')[0]
    try:
        last = datetime.datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
    except Exception:
        tg(f"⚠️ [Dead-man] heartbeat 형식 오류: {raw}"); return 1
    age = (datetime.datetime.now() - last)
    age_days = age.total_seconds() / 86400
    if age_days > STALE_DAYS:
        tg(f"🚨 [Dead-man 경보] 봇이 {age_days:.1f}일째 정상실행 안 됨!\n"
           f"마지막 실행: {raw}\n→ EC2/크론/데이터 점검 필요.")
        print(f"STALE: {age_days:.1f}일"); return 2
    print(f"정상: 마지막 실행 {age_days:.1f}일 전 ({raw})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
