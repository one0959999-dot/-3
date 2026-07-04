"""US 전체 유니버스 시세 수집 (us_ticker_cache 4,624) — KR과 동일 방식.

산출: data_cache_us_full.pkl {ticker: DataFrame[open,high,low,close,volume]} (+ 'SPY')
실행: python KR/fetch_us_full.py
"""
import sys, os, pickle, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import warnings
warnings.filterwarnings('ignore')
import pandas as pd
import yfinance as yf

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
START, END = '2014-01-01', '2026-07-04'
MIN_ROWS = 200


def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def main():
    c = sqlite3.connect(P('lassi.db'))
    tickers = [r[0] for r in c.execute('SELECT ticker FROM us_ticker_cache') if r[0]]
    c.close()
    tickers = list(dict.fromkeys(tickers + ['SPY']))
    print(f"US 티커 {len(tickers)} 수집 시작", flush=True)
    result = {}; t0 = time.time()
    for bi, batch in enumerate(chunks(tickers, 150)):
        try:
            data = yf.download(batch, start=START, end=END, progress=False, auto_adjust=True,
                               threads=True, group_by='ticker')
        except Exception:
            continue
        for t in batch:
            try:
                sub = data if len(batch) == 1 else data[t]
                sub = sub.dropna(how='all')
                if len(sub) >= MIN_ROWS and 'Close' in sub.columns:
                    df = pd.DataFrame({'open': sub['Open'], 'high': sub['High'], 'low': sub['Low'],
                                       'close': sub['Close'], 'volume': sub['Volume']}).dropna(subset=['close'])
                    if len(df) >= MIN_ROWS:
                        result[t] = df
            except Exception:
                continue
        if bi % 5 == 0:
            print(f"  배치{bi} 누적 {len(result)} ({time.time()-t0:.0f}s)", flush=True)
    pickle.dump(result, open(P('data_cache_us_full.pkl'), 'wb'))
    print(f"완료: {len(result)}/{len(tickers)} 종목, SPY={'O' if 'SPY' in result else 'X'} ({time.time()-t0:.0f}s)", flush=True)
    try:
        cc = sqlite3.connect(P('lassi.db'), timeout=30)
        r = cc.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); cc.close()
        from base.telegram_bot import TelegramNotifier
        TelegramNotifier(r[0], r[1]).send_message(f"✅ US 전체 유니버스 수집 완료: {len(result)}종목 (KR동일 검증 준비)")
    except Exception:
        pass


if __name__ == '__main__':
    main()
