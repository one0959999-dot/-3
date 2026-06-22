"""backtest_trade_signals 에 신호일 기준 강세 섹터(hot_sectors) 백필.

강세 섹터는 날짜에만 의존하므로 (시장, 날짜) 조합당 1회 계산 후 일괄 UPDATE.
수집 완료 후 실행하면 전 데이터에 균일 적용됨. 재실행 시 빈 것만 채움(--all 로 전체).
"""
import sys, os, sqlite3, argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
from base.sector_strength import get_hot_sectors_for_date

ap = argparse.ArgumentParser()
ap.add_argument('--all', action='store_true', help='이미 채워진 것도 재계산')
args = ap.parse_args()

con = sqlite3.connect('lassi.db', timeout=180)
con.execute('PRAGMA busy_timeout=180000')

where = '' if args.all else "WHERE hot_sectors IS NULL OR hot_sectors=''"
pairs = con.execute(
    f"SELECT DISTINCT mode, trade_date FROM backtest_trade_signals {where}").fetchall()
print(f'대상 (시장,날짜) 조합: {len(pairs)}', flush=True)

done = 0
for mode, date in pairs:
    try:
        hot = get_hot_sectors_for_date(mode, date, top_n=4)
        hot_str = ', '.join(hot)
        con.execute('UPDATE backtest_trade_signals SET hot_sectors=? WHERE mode=? AND trade_date=?',
                    (hot_str, mode, date))
        done += 1
        if done % 500 == 0:
            con.commit()
            print(f'  {done}/{len(pairs)}', flush=True)
    except Exception as e:
        print(f'  {mode} {date} 실패: {e}', flush=True)
con.commit()

filled = con.execute("SELECT COUNT(*) FROM backtest_trade_signals WHERE hot_sectors!=''").fetchone()[0]
print(f'완료: {done}개 날짜 처리 / hot_sectors 채워진 신호 {filled:,}', flush=True)
con.close()
