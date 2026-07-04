"""2026 YTD 시세 수집 — 진짜 OOS용 (전체 KR 유니버스 + KOSPI지수).

산출: data_cache_kr_2026.pkl {ticker: close Series, '__INDEX__': KOSPI close}
실행: python KR/fetch_2026.py
"""
import sys, os, pickle, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings
warnings.filterwarnings('ignore')
import pandas as pd
import yfinance as yf

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
START, END = '2025-12-01', '2026-07-04'  # 12월 겹침 = 연속성 검증용


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def fetch(tickers, suffix):
    out = {}
    syms = [t + suffix for t in tickers]
    try:
        data = yf.download(syms, start=START, end=END, progress=False, auto_adjust=True, threads=True, group_by='ticker')
    except Exception:
        return out
    for t in tickers:
        try:
            sub = data if len(syms) == 1 else data[t + suffix]
            c = sub['Close'].dropna()
            if len(c) >= 20:
                out[t] = c
        except Exception:
            continue
    return out


def main():
    full = pickle.load(open(P('data_cache_kr_full.pkl'), 'rb'))
    tickers = list(full.keys())
    print(f"2026 수집 대상 {len(tickers)}종목", flush=True)
    res = {}
    t0 = time.time()
    for bi, b in enumerate(chunks(tickers, 120)):
        res.update(fetch(b, '.KS'))
        if bi % 5 == 0:
            print(f"  .KS 배치{bi} 누적 {len(res)} ({time.time()-t0:.0f}s)", flush=True)
    rem = [t for t in tickers if t not in res]
    for b in chunks(rem, 120):
        res.update(fetch(b, '.KQ'))
    print(f"종목 수집 {len(res)}/{len(tickers)}", flush=True)
    idx = yf.download('^KS11', start=START, end=END, progress=False, auto_adjust=True)
    res['__INDEX__'] = idx['Close'].dropna() if hasattr(idx['Close'], 'dropna') else idx['Close'].iloc[:, 0].dropna()
    pickle.dump(res, open(P('data_cache_kr_2026.pkl'), 'wb'))
    ix = res['__INDEX__']
    if hasattr(ix, 'columns'):
        ix = ix.iloc[:, 0]
    print(f"지수 {len(ix)}일: {float(ix.iloc[0]):,.0f} → {float(ix.iloc[-1]):,.0f}", flush=True)
    print("저장 완료", flush=True)


if __name__ == '__main__':
    main()
