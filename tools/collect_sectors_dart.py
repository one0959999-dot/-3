"""DART 업종코드(KSIC) 수집 — 섹터 로테이션용 종목→섹터 분류.

유니버스(현재 유동성 + 상폐)의 induty_code(KSIC)를 DART company API로 수집.
저장: ticker_sector(ticker, induty_code). 큰 섹터 분류는 분석단계에서 KSIC prefix로 매핑.
이어받기 지원. DART 키는 DB users.dart_api_key.

실행: python tools/collect_sectors_dart.py [max]
"""
import sys, os, sqlite3, time, io, zipfile, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import requests
import xml.etree.ElementTree as ET

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lassi.db')
BIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data_cache_big.pkl')
DEL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data_cache_delisted.pkl')


def dart_key():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    k = c.execute("SELECT dart_api_key FROM users WHERE dart_api_key IS NOT NULL AND dart_api_key!='' LIMIT 1").fetchone()['dart_api_key']
    c.close(); return k


def stockcode_to_corp(key):
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
    if os.path.exists(BIG):
        b = pickle.load(open(BIG, 'rb')); codes |= set(b['KOSPI']) | set(b['KOSDAQ'])
    if os.path.exists(DEL):
        d = pickle.load(open(DEL, 'rb')); codes |= {c for c, v in d.items() if v['close'].index.max().year >= 2015}
    return sorted(codes)


def main(maxn=99999):
    key = dart_key()
    print("corpCode 매핑...")
    s2c = stockcode_to_corp(key)
    uni = [c for c in universe() if c in s2c][:maxn]
    con = sqlite3.connect(DB, timeout=120); con.execute('PRAGMA busy_timeout=120000')
    con.execute("CREATE TABLE IF NOT EXISTS ticker_sector (ticker TEXT PRIMARY KEY, induty_code TEXT)")
    have = {r[0] for r in con.execute("SELECT ticker FROM ticker_sector").fetchall()}
    n = 0
    for i, code in enumerate(uni, 1):
        if code in have:
            continue
        try:
            r = requests.get('https://opendart.fss.or.kr/api/company.json',
                             params={'crtfc_key': key, 'corp_code': s2c[code]}, timeout=15).json()
            ind = r.get('induty_code') if r.get('status') == '000' else None
        except Exception:
            ind = None
        con.execute("INSERT OR REPLACE INTO ticker_sector VALUES (?,?)", (code, ind))
        n += 1
        if n % 50 == 0:
            con.commit(); print(f"  [{i}/{len(uni)}] {n}건")
        time.sleep(0.05)
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM ticker_sector WHERE induty_code IS NOT NULL").fetchone()[0]
    con.close()
    print(f"✅ 업종코드 수집: 신규 {n} · 유효 {tot}")


if __name__ == '__main__':
    m = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 99999
    main(m)
