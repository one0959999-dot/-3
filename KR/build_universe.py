"""확대 유니버스 빌더 — DB 2,768종목서 거래대금 상위 N개 추려 전체기간 캐시.

목적: 분석 표본을 ~61 → ~250(유동성 상위)로 키워 통계 신뢰성 확보.
소형 잡주는 모멘텀 왜곡(상한가/유동성)하므로 '거래대금(close×volume) 상위'로 필터.
2단계: (1)최근 6개월 거래대금 스크리닝 → 상위 N (2)그 N개 전체기간(2014~) 수집.
출력: data_cache_big.pkl  (구조: {'KOSPI':{code:(name,df)}, 'KOSDAQ':{...}, 'index':{'KOSPI':df}, 'vix':s})

실행: python KR/build_universe.py [N]    # N=종목수(기본 250)
"""
import sys, os, pickle, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
import yfinance as yf

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_big.pkl')
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lassi.db')


def all_tickers():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT ticker, name FROM kr_ticker_cache").fetchall()
    c.close()
    return [(r['ticker'], r['name']) for r in rows if str(r['ticker']).isdigit() and len(str(r['ticker'])) == 6]


def _batch(symbols, **kw):
    out = {}
    for i in range(0, len(symbols), 120):
        chunk = symbols[i:i + 120]
        try:
            df = yf.download(chunk, progress=False, auto_adjust=True, group_by='ticker',
                             threads=True, **kw)
        except Exception:
            continue
        for s in chunk:
            try:
                sub = df[s] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(subset=['Close'])
                if len(sub) > 0:
                    out[s] = sub
            except Exception:
                pass
        print(f"    ...{min(i+120,len(symbols))}/{len(symbols)}")
    return out


def screen_liquidity(tickers):
    """최근 6개월 거래대금(close×volume 중앙값)으로 시장판별+유동성 점수."""
    codes = [c for c, n in tickers]
    ks = {c: c + '.KS' for c in codes}
    # 1) 전부 .KS 시도
    ks_data = _batch([ks[c] for c in codes], period='6mo')
    market = {}; turnover = {}
    for c in codes:
        s = ks[c]
        if s in ks_data and len(ks_data[s]) > 30:
            market[c] = ('KOSPI', s)
    # 2) .KS 실패분만 .KQ 시도
    miss = [c for c in codes if c not in market]
    kq_data = _batch([c + '.KQ' for c in miss], period='6mo')
    for c in miss:
        s = c + '.KQ'
        if s in kq_data and len(kq_data[s]) > 30:
            market[c] = ('KOSDAQ', s)
    # 거래대금
    src = {**ks_data, **kq_data}
    for c, (mkt, s) in market.items():
        d = src.get(s)
        if d is not None and 'Volume' in d:
            turnover[c] = float((d['Close'] * d['Volume']).median())
    return market, turnover


def full_fetch(codes_market):
    """선정 종목 전체기간(2014~) 수집."""
    by_sym = {s: (c, mkt) for c, (mkt, s) in codes_market.items()}
    data = _batch(list(by_sym.keys()), start='2014-01-01')
    out = {'KOSPI': {}, 'KOSDAQ': {}}
    namemap = dict(all_tickers())
    for s, d in data.items():
        c, mkt = by_sym[s]
        if len(d) < 400:
            continue
        df = d.rename(columns=str.lower)[['open', 'high', 'low', 'close', 'volume']].astype(float).dropna(subset=['close'])
        out[mkt][c] = (namemap.get(c, c), df)
    return out


def main(N=250):
    print(f"▶ 확대 유니버스 빌드 — 거래대금 상위 {N}")
    tk = all_tickers()
    print(f"  전체 종목 {len(tk)}")
    print("  [1단계] 유동성 스크리닝(최근 6개월)...")
    market, turnover = screen_liquidity(tk)
    print(f"    가격데이터 확보 {len(market)} · 거래대금 산출 {len(turnover)}")
    top = sorted(turnover.items(), key=lambda kv: kv[1], reverse=True)[:N]
    sel = {c: market[c] for c, _ in top}
    print(f"  [2단계] 상위 {len(sel)}종목 전체기간 수집...")
    stocks = full_fetch(sel)
    print(f"    KOSPI {len(stocks['KOSPI'])} · KOSDAQ {len(stocks['KOSDAQ'])}")
    # 중간 저장(지수/VIX 실패해도 종목은 보존)
    pickle.dump({'KOSPI': stocks['KOSPI'], 'KOSDAQ': stocks['KOSDAQ']}, open(OUT + '.stocks', 'wb'))
    print("  지수/VIX 수집...")

    def _flat(df):
        df = df.copy()
        df.columns = [str(x[0]).lower() if isinstance(x, tuple) else str(x).lower() for x in df.columns]
        return df
    idx = _flat(yf.download('^KS11', start='2014-01-01', progress=False, auto_adjust=True))
    vix = _flat(yf.download('^VIX', start='2014-01-01', progress=False, auto_adjust=True))
    vixs = pd.Series(np.asarray(vix['close']).ravel(), index=pd.to_datetime(vix.index))
    data = {'KOSPI': stocks['KOSPI'], 'KOSDAQ': stocks['KOSDAQ'],
            'index': {'KOSPI': idx[['open', 'high', 'low', 'close', 'volume']].astype(float)},
            'vix': vixs}
    pickle.dump(data, open(OUT, 'wb'))
    tot = len(stocks['KOSPI']) + len(stocks['KOSDAQ'])
    print(f"✅ 저장: 총 {tot}종목 → {OUT}")


if __name__ == '__main__':
    N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 250
    main(N)
