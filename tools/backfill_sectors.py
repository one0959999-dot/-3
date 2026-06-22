"""기존 backtest_trade_signals 의 sector='기타' 종목을 실제 섹터로 백필.

종목당 1회 yfinance 조회(ticker_sector 캐시 공유) 후 해당 종목 전체 신호 UPDATE.
라이브 백테스트와 동시 실행 가능하나 yfinance 부담 줄이려 약간의 sleep 포함.
"""
import sys, os, time, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from base.sector_lookup import get_sector

con = sqlite3.connect('lassi.db', timeout=120)
con.execute('PRAGMA busy_timeout=120000')
rows = con.execute(
    "SELECT DISTINCT mode, ticker FROM backtest_trade_signals "
    "WHERE sector='기타' OR sector IS NULL").fetchall()
print(f'백필 대상 종목: {len(rows)}', flush=True)

done = 0
for mode, ticker in rows:
    try:
        sector, _ = get_sector(ticker, mode)
        if sector and sector != '기타':
            con.execute('UPDATE backtest_trade_signals SET sector=? WHERE mode=? AND ticker=?',
                        (sector, mode, ticker))
            con.commit()
        done += 1
        if done % 50 == 0:
            print(f'  {done}/{len(rows)} 처리', flush=True)
    except Exception as e:
        print(f'  {ticker} 실패: {e}', flush=True)
    time.sleep(0.2)

# 결과 요약
dist = con.execute("SELECT sector, COUNT(*) FROM backtest_trade_signals GROUP BY sector ORDER BY 2 DESC LIMIT 15").fetchall()
print('=== 섹터 분포 ===', flush=True)
for s, n in dist:
    print(f'  {s}: {n:,}', flush=True)
con.close()
print('완료', flush=True)
