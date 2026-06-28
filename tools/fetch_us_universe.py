"""US 유니버스 시세 수집(yfinance) — 코어(대형)/위성(성장)/지수. data_cache_us.pkl.
⚠️ US 상폐데이터 없음 → 생존편향 낌(낙관적). KR(교정본)과 직접비교 불가, 방향성 참고용.
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import pandas as pd, yfinance as yf

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data_cache_us.pkl')

CORE = ['AAPL','MSFT','NVDA','AMZN','GOOGL','META','AVGO','LLY','JPM','V','XOM','UNH','MA','COST','HD',
        'PG','JNJ','WMT','ABBV','NFLX','BAC','KO','CVX','MRK','ADBE','PEP','CRM','TMO','ORCL','ACN',
        'MCD','CSCO','ABT','DHR','INTC','QCOM','TXN','AMAT','IBM','GE']
SAT = ['TSLA','AMD','PLTR','SNOW','NET','CRWD','DDOG','SHOP','UBER','ABNB','COIN','RBLX','MELI','PANW',
       'SMCI','MRVL','ON','ENPH','FSLR','ROKU','SOFI','DKNG','TTD','ZS','MDB','U','AFRM','RIVN','LCID',
       'HOOD','PYPL','ARM','CELH','DASH','SNAP']
ETF = ['SPY','QQQ']


def fetch(tickers):
    raw = yf.download(tickers, start='2014-06-01', progress=False, auto_adjust=True, group_by='ticker', threads=True)
    out = {}
    for t in tickers:
        try:
            sub = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            sub = sub.dropna(subset=['Close'])
            if len(sub) > 400:
                df = sub.rename(columns=str.lower)[['open','high','low','close','volume']].astype(float)
                out[t] = (t, df)
        except Exception:
            pass
    return out


def main():
    print("US 코어 수집..."); core = fetch(CORE); print(f"  {len(core)}")
    print("US 위성 수집..."); sat = fetch(SAT); print(f"  {len(sat)}")
    print("ETF 수집..."); etf = fetch(ETF)
    spy = etf.get('SPY');
    data = {'CORE': core, 'SAT': sat,
            'index': {'US': spy[1] if spy else None},
            'etf': {t: etf[t][1] for t in etf}}
    pickle.dump(data, open(OUT, 'wb'))
    print(f"✅ US 저장: 코어 {len(core)} + 위성 {len(sat)} + ETF {len(etf)} → {OUT}")


if __name__ == '__main__':
    main()
