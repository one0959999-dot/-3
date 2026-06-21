"""기존 backtest_trade_signals 의 KR 신호에 DART 공시(news_summary) 백필.

가격/지표/국면은 그대로 두고, 비어 있는 news_summary 만 채운다.
corp_code 버그 수정(corpCode.xml 매핑) 이후 1회성으로 실행.

사용법:
    python tools/backfill_disclosures.py            # user 1, 빈 공시만
    python tools/backfill_disclosures.py --all      # 모든 KR 행 덮어쓰기
"""
import argparse
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai.news_monitor import NewsMonitor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default='lassi.db')
    ap.add_argument('--user', type=int, default=1)
    ap.add_argument('--all', action='store_true', help='이미 채워진 공시도 덮어쓰기')
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    c = con.cursor()

    # DART 키
    row = c.execute('SELECT dart_api_key FROM users WHERE id=?', (args.user,)).fetchone()
    dart_key = (row['dart_api_key'] or '') if row else ''
    if not dart_key:
        print(f'❌ user {args.user} DART 키 없음')
        return
    nm = NewsMonitor(dart_key, '', '')

    where = "mode='KR'"
    if not args.all:
        where += " AND (news_summary IS NULL OR news_summary='')"
    rows = c.execute(
        f'SELECT id, ticker, trade_date FROM backtest_trade_signals WHERE {where}'
    ).fetchall()
    print(f'대상 KR 신호: {len(rows)}건')

    # (ticker, date) 캐시 — 같은 종목·날짜 중복 조회 방지
    cache = {}
    updated = 0
    filled = 0
    for i, r in enumerate(rows, 1):
        key = (r['ticker'], r['trade_date'])
        if key not in cache:
            try:
                cache[key] = nm.get_disclosure_summary_for_date(
                    r['ticker'], r['trade_date'], days=5) or ''
            except Exception as e:
                cache[key] = ''
                print(f'  조회 실패 {key}: {e}')
        summary = cache[key]
        c.execute('UPDATE backtest_trade_signals SET news_summary=? WHERE id=?',
                  (summary, r['id']))
        updated += 1
        if summary:
            filled += 1
        if i % 50 == 0:
            con.commit()
            print(f'  진행 {i}/{len(rows)} | 공시있음 {filled}건')

    con.commit()
    con.close()
    print(f'✅ 완료: {updated}건 UPDATE, 공시 채워진 행 {filled}건')


if __name__ == '__main__':
    main()
