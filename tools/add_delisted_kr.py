"""상폐 종목을 KR 종목 풀(kr_ticker_cache)에 추가 — 생존자 편향 교정.

과거 시점(연도별) 종목 리스트를 union해서, 현재 없는 종목 = 상폐 추정.
kr_ticker_cache 에 추가하면 백테스트가 pending 으로 같이 수집한다.
※ pykrx 사용 — 실행 중 백테스트와 충돌(rate limit) 시 0개 나옴. 백테스트 한가할 때 실행.

사용: python tools/add_delisted_kr.py
"""
import sys, os, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
from pykrx import stock

# 연도별 스냅샷 (반기 단위로 촘촘히 — 짧게 상장했다 상폐된 것도 포착)
SNAP_DATES = ['20140102', '20150102', '20160104', '20170102', '20180102',
              '20190102', '20200102', '20210104', '20220103', '20230102',
              '20240102', '20250102']


def main():
    today = set()
    for m in ('KOSPI', 'KOSDAQ'):
        try:
            today |= set(stock.get_market_ticker_list('20260620', m))
        except Exception:
            pass
    if not today:
        print('현재 상장 조회 실패 — pykrx rate limit (백테스트 멈추고 재시도)')
        return

    allt = set(today)
    for d in SNAP_DATES:
        for m in ('KOSPI', 'KOSDAQ'):
            try:
                allt |= set(stock.get_market_ticker_list(d, m))
            except Exception:
                pass
        time.sleep(0.5)

    delisted = sorted(allt - today)
    print(f'현재상장 {len(today)} / 과거포함 union {len(allt)} / 상폐추정 {len(delisted)}')

    # kr_ticker_cache 에 추가 (이름은 가능하면, 안되면 티커)
    con = sqlite3.connect('lassi.db', timeout=120)
    con.execute('PRAGMA busy_timeout=120000')
    con.execute('CREATE TABLE IF NOT EXISTS kr_ticker_cache (ticker TEXT PRIMARY KEY, name TEXT)')
    existing = {r[0] for r in con.execute('SELECT ticker FROM kr_ticker_cache').fetchall()}
    added = 0
    for tk in delisted:
        if tk in existing:
            continue
        try:
            nm = stock.get_market_ticker_name(tk)
        except Exception:
            nm = tk
        con.execute('INSERT OR IGNORE INTO kr_ticker_cache VALUES (?,?)', (tk, nm or tk))
        added += 1
    con.commit()
    total = con.execute('SELECT COUNT(*) FROM kr_ticker_cache').fetchone()[0]
    con.close()
    print(f'상폐 {added}개 추가 → kr_ticker_cache 총 {total}개 (백테스트가 pending으로 수집)')


if __name__ == '__main__':
    main()
