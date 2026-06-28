"""DART 연도별 재무 수집 — 종목선정 건전성 필터(자본잠식·적자)용. 시점별 백테스트 기반.

유니버스(현재 유동성 + 상폐)의 연도별(사업보고서) 자본총계·자본금·당기순이익을 모은다.
→ 자본잠식(자본총계<0 or <자본금×0.5)·적자(순이익<0) 시점별 판정 가능.
저장: financials_dart 테이블 (ticker, year, capital(자본총계), paidin(자본금), netincome(순이익), fs).
이어받기 지원. DART 키는 DB users.dart_api_key.

실행: python tools/collect_financials_dart.py [max_tickers]
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
YEARS = list(range(2014, 2025))


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
        b = pickle.load(open(BIG, 'rb'))
        codes |= set(b['KOSPI']) | set(b['KOSDAQ'])
    if os.path.exists(DEL):
        d = pickle.load(open(DEL, 'rb'))
        codes |= {c for c, v in d.items() if v['close'].index.max().year >= 2015}
    return sorted(codes)


def _num(s):
    try:
        return float(str(s).replace(',', ''))
    except Exception:
        return None


def fetch_year(key, corp, year):
    """반환 {capital, paidin, netincome, fs} or None."""
    try:
        r = requests.get('https://opendart.fss.or.kr/api/fnlttSinglAcnt.json',
                         params={'crtfc_key': key, 'corp_code': corp, 'bsns_year': str(year), 'reprt_code': '11011'},
                         timeout=20).json()
    except Exception:
        return None
    if r.get('status') != '000':
        return None
    want = {'자본총계': 'capital', '자본금': 'paidin', '당기순이익': 'netincome'}
    res = {}
    for fs in ('CFS', 'OFS'):                      # 연결 우선, 없으면 별도
        got = {}
        for it in r['list']:
            if it.get('fs_div') != fs:
                continue
            nm = it.get('account_nm', '')
            for w, key2 in want.items():
                if nm == w or (w == '당기순이익' and nm.startswith('당기순이익')):
                    v = _num(it.get('thstrm_amount'))
                    if v is not None:
                        got[key2] = v
        if 'capital' in got:
            got['fs'] = fs
            return got
    return None


def main(maxn=99999):
    key = dart_key()
    print("corpCode 매핑...")
    s2c = stockcode_to_corp(key)
    uni = [c for c in universe() if c in s2c][:maxn]
    print(f"유니버스 {len(uni)}종목 × {len(YEARS)}년 = 최대 {len(uni)*len(YEARS)} 조회")
    con = sqlite3.connect(DB, timeout=120); con.execute('PRAGMA busy_timeout=120000')
    con.execute("""CREATE TABLE IF NOT EXISTS financials_dart
                   (ticker TEXT, year INT, capital REAL, paidin REAL, netincome REAL, fs TEXT,
                    PRIMARY KEY(ticker, year))""")
    have = {(r[0], r[1]) for r in con.execute("SELECT ticker, year FROM financials_dart").fetchall()}
    n_new = 0
    for i, code in enumerate(uni, 1):
        corp = s2c[code]
        for y in YEARS:
            if (code, y) in have:
                continue
            f = fetch_year(key, corp, y)
            if f:
                con.execute("INSERT OR REPLACE INTO financials_dart VALUES (?,?,?,?,?,?)",
                            (code, y, f.get('capital'), f.get('paidin'), f.get('netincome'), f.get('fs')))
                n_new += 1
            time.sleep(0.05)
        if i % 25 == 0:
            con.commit(); print(f"  [{i}/{len(uni)}] 신규 {n_new}건")
    con.commit()
    tot = con.execute("SELECT COUNT(*) FROM financials_dart").fetchone()[0]
    # 자본잠식/적자 통계
    impaired = con.execute("SELECT COUNT(DISTINCT ticker) FROM financials_dart WHERE capital<0").fetchone()[0]
    con.close()
    print(f"✅ 재무 수집: 신규 {n_new} · 총 {tot}건 · 자본잠식 경험종목 {impaired}")


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 99999
    main(n)
