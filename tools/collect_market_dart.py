"""DART corp_cls 수집 — 종목별 시장구분(KOSPI/KOSDAQ). 코어/위성 분리용.

⚠️ 기존 ticker_sector(yfinance 국가/섹터 테이블)는 건드리지 않음. 새 테이블 ticker_market_dart 사용.
섹터는 이미 ticker_sector(sector 컬럼)에 있으므로 여기선 corp_cls(시장)만.
유니버스 = 캐시(big+wf+delisted)의 KR 티커. DART company.json corp_cls.

실행: python tools/collect_market_dart.py [max]
"""
import sys, os, sqlite3, time, io, zipfile, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import requests
import xml.etree.ElementTree as ET

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lassi.db')
P = lambda f: os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), f)


def dart_key():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    k = c.execute("SELECT dart_api_key FROM users WHERE dart_api_key IS NOT NULL AND dart_api_key!='' LIMIT 1").fetchone()['dart_api_key']
    c.close(); return k


def s2c(key):
    r = requests.get('https://opendart.fss.or.kr/api/corpCode.xml', params={'crtfc_key': key}, timeout=40)
    z = zipfile.ZipFile(io.BytesIO(r.content)); root = ET.fromstring(z.read(z.namelist()[0]))
    out = {}
    for x in root.findall('list'):
        sc = (x.findtext('stock_code') or '').strip()
        if sc and len(sc) == 6:
            out[sc] = x.findtext('corp_code')
    return out


def universe():
    codes = set()
    for f in ('data_cache_big.pkl', 'data_cache_wf.pkl'):
        if os.path.exists(P(f)):
            d = pickle.load(open(P(f), 'rb'))
            for mk in ('KOSPI', 'KOSDAQ'):
                codes |= set(d.get(mk, {}))
    if os.path.exists(P('data_cache_delisted.pkl')):
        codes |= set(pickle.load(open(P('data_cache_delisted.pkl'), 'rb')))
    return sorted(codes)


def main(maxn=99999):
    key = dart_key()
    print("corpCode 매핑..."); mp = s2c(key)
    uni = [c for c in universe() if c in mp][:maxn]
    con = sqlite3.connect(DB, timeout=120); con.execute('PRAGMA busy_timeout=120000')
    con.execute("CREATE TABLE IF NOT EXISTS ticker_market_dart (ticker TEXT PRIMARY KEY, market TEXT)")
    have = {r[0] for r in con.execute("SELECT ticker FROM ticker_market_dart").fetchall()}
    n = 0
    for i, code in enumerate(uni, 1):
        if code in have:
            continue
        mkt = None
        try:
            r = requests.get('https://opendart.fss.or.kr/api/company.json',
                             params={'crtfc_key': key, 'corp_code': mp[code]}, timeout=15).json()
            if r.get('status') == '000':
                mkt = {'Y': 'KOSPI', 'K': 'KOSDAQ', 'N': 'KONEX'}.get(r.get('corp_cls'), r.get('corp_cls'))
        except Exception:
            pass
        con.execute("INSERT OR REPLACE INTO ticker_market_dart VALUES (?,?)", (code, mkt))
        n += 1
        if n % 100 == 0:
            con.commit(); print(f"  [{i}/{len(uni)}] {n}건")
        time.sleep(0.04)
    con.commit()
    import collections
    dist = collections.Counter(r[0] for r in con.execute("SELECT market FROM ticker_market_dart").fetchall())
    con.close()
    print(f"✅ 시장구분 수집: 신규 {n} · 분포 {dict(dist)}")


if __name__ == '__main__':
    m = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 99999
    main(m)
