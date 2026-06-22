"""백테스트 진행률 1시간마다 텔레그램 발송.
KR/US 완료율 + 데이터 품질(KR 소수점 오염 여부)을 보고.
독립 실행:
  Start-Process python -ArgumentList '-B','progress_reporter.py' -WindowStyle Hidden
"""
import sys, os, time, sqlite3
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import logging
logging.disable(logging.CRITICAL)

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lassi.db')


def _creds():
    c = sqlite3.connect(DB, timeout=30)
    r = c.execute("SELECT telegram_token, telegram_chat_id FROM users "
                  "WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone()
    c.close()
    return (r[0], r[1]) if r else (None, None)


def _report():
    c = sqlite3.connect(DB, timeout=60)
    kr_tot = c.execute('SELECT COUNT(*) FROM kr_ticker_cache').fetchone()[0] or 2768
    try:
        us_tot = c.execute('SELECT COUNT(*) FROM us_ticker_cache').fetchone()[0] or 9982
    except Exception:
        us_tot = 9982
    krd = c.execute("SELECT COUNT(*) FROM backtest_full_progress WHERE mode='KR'").fetchone()[0]
    usd = c.execute("SELECT COUNT(*) FROM backtest_full_progress WHERE mode='US'").fetchone()[0]
    sig = c.execute('SELECT COUNT(*) FROM backtest_trade_signals').fetchone()[0]
    dec = c.execute("SELECT COUNT(*) FROM backtest_trade_signals WHERE mode='KR' AND price!=CAST(price AS INTEGER)").fetchone()[0]
    c.close()
    kr_pct = krd / max(kr_tot, 1) * 100
    us_pct = usd / max(us_tot, 1) * 100
    quality = '✅ 정상' if dec == 0 else f'⚠️ 소수점오염 {dec}건'
    return (f"📊 백테스트 진행률\n"
            f"━━━━━━━━━━━━━\n"
            f"🇰🇷 KR: {kr_pct:.1f}%  ({krd:,}/{kr_tot:,})\n"
            f"🇺🇸 US: {us_pct:.1f}%  ({usd:,}/{us_tot:,})\n"
            f"📈 누적 신호: {sig:,}\n"
            f"🔍 데이터 품질: {quality}")


def main():
    token, chat = _creds()
    if not token:
        return
    from base.telegram_bot import TelegramNotifier
    tg = TelegramNotifier(token, chat)
    while True:
        try:
            tg.send_message(_report())
        except Exception:
            pass
        time.sleep(3600)   # 1시간


if __name__ == '__main__':
    main()
