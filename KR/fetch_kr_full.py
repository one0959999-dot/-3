"""전체 KOSPI+KOSDAQ 생존주 시세 수집 (yfinance) — 유니버스 편향 제거용.

kr_ticker_cache 전체(~2768) → .KS 먼저, 실패분 .KQ 재시도 → close/high/low/volume 저장.
기존 241 큐레이팅 대신 전체 유니버스로 재검증 가능케 함. 상폐 911은 별도 유지.
산출: data_cache_kr_full.pkl  {ticker: DataFrame[open,high,low,close,volume]}

실행: python KR/fetch_kr_full.py
"""
import sys, os, pickle, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import warnings
warnings.filterwarnings('ignore')
import pandas as pd
import yfinance as yf

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
START, END = '2014-01-01', '2025-12-31'
MIN_ROWS = 200


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def fetch_batch(tickers, suffix):
    """배치 다운로드 → {원본티커: df}."""
    syms = [t + suffix for t in tickers]
    out = {}
    try:
        data = yf.download(syms, start=START, end=END, progress=False, auto_adjust=True, threads=True, group_by='ticker')
    except Exception:
        return out
    for t in tickers:
        sym = t + suffix
        try:
            if len(syms) == 1:
                sub = data
            else:
                sub = data[sym]
            sub = sub.dropna(how='all')
            if len(sub) >= MIN_ROWS and 'Close' in sub.columns:
                df = pd.DataFrame({
                    'open': sub['Open'], 'high': sub['High'], 'low': sub['Low'],
                    'close': sub['Close'], 'volume': sub['Volume']}).dropna(subset=['close'])
                if len(df) >= MIN_ROWS:
                    out[t] = df
        except Exception:
            continue
    return out


def main():
    c = sqlite3.connect(P('lassi.db'))
    tickers = [r[0] for r in c.execute('SELECT ticker FROM kr_ticker_cache')]
    c.close()
    tickers = [t for t in tickers if t and len(t) == 6 and t.isdigit()]
    print(f"전체 KR 티커 {len(tickers)}개 수집 시작", flush=True)
    result = {}
    t0 = time.time()
    # 1) .KS
    for bi, batch in enumerate(chunks(tickers, 120)):
        got = fetch_batch(batch, '.KS')
        result.update(got)
        if bi % 3 == 0:
            print(f"  [.KS] 배치{bi} 누적 {len(result)}종목 ({time.time()-t0:.0f}s)", flush=True)
    print(f".KS 완료: {len(result)}종목", flush=True)
    # 2) .KQ (KS 실패분)
    remaining = [t for t in tickers if t not in result]
    print(f".KQ 재시도 대상 {len(remaining)}개", flush=True)
    for bi, batch in enumerate(chunks(remaining, 120)):
        got = fetch_batch(batch, '.KQ')
        result.update(got)
        if bi % 3 == 0:
            print(f"  [.KQ] 배치{bi} 누적 {len(result)}종목 ({time.time()-t0:.0f}s)", flush=True)
    print(f"\n총 수집 {len(result)}종목 / {len(tickers)} ({time.time()-t0:.0f}s)", flush=True)
    pickle.dump(result, open(P('data_cache_kr_full.pkl'), 'wb'))
    print("저장 완료: data_cache_kr_full.pkl", flush=True)
    # 텔레그램 알림
    try:
        cc = sqlite3.connect(P('lassi.db'), timeout=30)
        r = cc.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); cc.close()
        from base.telegram_bot import TelegramNotifier
        TelegramNotifier(r[0], r[1]).send_message(f"✅ 전체 KR 유니버스 수집 완료: {len(result)}종목 (편향제거 재검증 준비)")
    except Exception:
        pass


if __name__ == '__main__':
    main()
