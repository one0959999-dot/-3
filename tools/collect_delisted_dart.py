"""상폐 종목 대량 수집 — DART(목록) + pykrx(시세) + 사유분류. 생존편향 교정용.

이 환경은 KRX 종목리스트가 막혀 기존 add_delisted_kr.py가 안 됨.
→ DART corpCode(상폐 포함 전체기업)에서 주식코드 보유분 중 '현재 미상장'을 상폐 후보로,
  pykrx로 시세 채우고(상폐종목도 OHLCV 됨), delisting_reason으로 사유(부실/피인수/자진) 분류.

저장: ticker_delisting(DB) + data_cache_delisted.pkl({ticker:(name,reason,close_series)}).
사유: 부실상폐(0수렴=자본잠식/감사거절류) / 피인수상폐 / 자진상폐.

실행: python tools/collect_delisted_dart.py [max]   # max=처리 후보수(기본 전체)
"""
import sys, os, sqlite3, time, io, zipfile, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import requests
import xml.etree.ElementTree as ET
from pykrx import stock
from base.delisting_reason import infer_from_price

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lassi.db')
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data_cache_delisted.pkl')


def dart_stockcodes():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    k = c.execute("SELECT dart_api_key FROM users WHERE dart_api_key IS NOT NULL AND dart_api_key!='' LIMIT 1").fetchone()['dart_api_key']
    c.close()
    r = requests.get('https://opendart.fss.or.kr/api/corpCode.xml', params={'crtfc_key': k}, timeout=40)
    z = zipfile.ZipFile(io.BytesIO(r.content)); root = ET.fromstring(z.read(z.namelist()[0]))
    out = {}
    for x in root.findall('list'):
        sc = (x.findtext('stock_code') or '').strip()
        if sc and len(sc) == 6 and sc.isdigit():
            out[sc] = x.findtext('corp_name')
    return out


def current_listed():
    c = sqlite3.connect(DB)
    rows = {r[0] for r in c.execute("SELECT ticker FROM kr_ticker_cache").fetchall()}
    c.close()
    return rows


def fetch_close(code):
    try:
        df = stock.get_market_ohlcv_by_date('20100101', time.strftime('%Y%m%d'), code)
        if df is None or len(df) < 20 or '종가' not in df.columns:
            return None
        import pandas as pd
        s = df['종가'].astype(float); s.index = pd.to_datetime(s.index)
        return s[s > 0]
    except Exception:
        return None


def main(maxn=99999):
    print("DART corpCode 로딩...")
    dart = dart_stockcodes()
    cur = current_listed()
    cand = [c for c in dart if c not in cur]
    print(f"DART 주식코드 {len(dart)} · 현재상장 {len(cur)} · 상폐후보 {len(cand)}")
    cand = sorted(cand)[:maxn]

    store = {}
    if os.path.exists(OUT):
        store = pickle.load(open(OUT, 'rb'))
    con = sqlite3.connect(DB, timeout=120); con.execute('PRAGMA busy_timeout=120000')
    reasons = {}
    done = 0; got = 0
    for i, code in enumerate(cand, 1):
        if code in store:
            got += 1; continue
        s = fetch_close(code)
        done += 1
        if s is None or len(s) < 20:
            continue
        last_date = s.index.max()
        # 최근까지 거래중이면 상폐 아님(스킵)
        if last_date.year >= 2026 and last_date.month >= 5:
            continue
        import pandas as pd
        info, _ = None, None
        reason, detail = infer_from_price(pd.DataFrame({'close': s})), None
        rtype, det = reason
        store[code] = {'name': dart.get(code, code), 'reason': rtype, 'close': s,
                       'last_date': last_date.strftime('%Y-%m-%d'), **det}
        reasons[rtype] = reasons.get(rtype, 0) + 1
        got += 1
        con.execute("INSERT OR REPLACE INTO ticker_delisting(ticker,mode,reason,last_date,last_price,peak_1y,last_vs_peak_pct) VALUES (?,?,?,?,?,?,?)",
                    (code, 'KR', rtype, last_date.strftime('%Y-%m-%d'),
                     det.get('last_price'), det.get('peak_1y'), det.get('last_vs_peak_pct')))
        if got % 50 == 0:
            con.commit(); pickle.dump(store, open(OUT, 'wb'))
            print(f"  [{i}/{len(cand)}] 수집 {got} · {reasons}")
        time.sleep(0.15)
    con.commit(); con.close()
    pickle.dump(store, open(OUT, 'wb'))
    # 2015+ 시세 있는 것(백테스트 유효)
    valid2015 = sum(1 for v in store.values() if v['close'].index.max().year >= 2015)
    print(f"\n✅ 상폐 수집 총 {len(store)} (이번 {got}) · 사유 {reasons}")
    print(f"   2015년 이후 시세 보유(백테스트 유효): {valid2015}")
    print(f"   → DB ticker_delisting + {OUT}")


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 99999
    main(n)
