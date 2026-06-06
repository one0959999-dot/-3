import yfinance as yf
import pandas as pd
from pykrx import stock
from datetime import datetime, timedelta
import json
import time

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

dataset = []
print("🧠 1. 미국 시장 (나스닥 QQQ) 데이터 수집 중...")
df_us = yf.download("QQQ", start="2000-01-01", end="2024-05-01")
if isinstance(df_us.columns, pd.MultiIndex):
    df_us.columns = df_us.columns.droplevel(1)

print("🧠 2. 한국 시장 (KOSPI & KOSDAQ) 데이터 수집 중...")
end_date = datetime.today()
start_date = datetime(2000, 1, 1)# 2000년부터 국장 상장 이후 전체 데이터

# 분석 대상: 미국(QQQ), 한국 코스피(KODEX 200), 한국 코스닥(KODEX 코스닥150)
market_data = [
    {"name": "NASDAQ(QQQ)", "df": df_us},
]

# 한국 ETF 데이터 다운로드 및 병합
kr_tickers = {"069500": "KOSPI(KODEX 200)", "229200": "KOSDAQ(KODEX 코스닥150)"}
for ticker, name in kr_tickers.items():
    kr_df = stock.get_market_ohlcv_by_date(start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"), ticker)
    kr_df.rename(columns={'종가':'Close'}, inplace=True)
    market_data.append({"name": name, "df": kr_df})
    time.sleep(1)

print("📈 글로벌 양방향 차트 교육 데이터 생성 중...")
for market in market_data:
    df = market["df"]
    name = market["name"]
    
    df['RSI'] = calc_rsi(df['Close'], 14)
    df['SMA_20'] = df['Close'].rolling(window=20).mean()
    df['SMA_120'] = df['Close'].rolling(window=120).mean()
    df = df.dropna()

    for i in range(len(df) - 20):
        current_date = df.index[i].strftime('%Y-%m-%d')
        close_price = float(df['Close'].iloc[i])
        rsi = float(df['RSI'].iloc[i])
        sma120 = float(df['SMA_120'].iloc[i])
        
        # 과매도 구간(가짜 반등 위험 구역)만 집중 추출
        if rsi <= 35:
            future_20_days = df['Close'].iloc[i+1 : i+21]
            max_future = float(future_20_days.max())
            min_future = float(future_20_days.min())
            
            profit_pct = (max_future - close_price) / close_price * 100
            loss_pct = (min_future - close_price) / close_price * 100
            
            is_bear_market = bool(close_price < sma120)
            market_status = "대세 하락장(120일선 붕괴)" if is_bear_market else "상승/조정장"
            
            text_input = f"종목: {name}, 시기: {current_date}, 상태: {market_status}, RSI: {rsi:.1f}, 현재가: {close_price:.2f}. 이 매매 신호를 승인하시겠습니까?"
            
            if is_bear_market and loss_pct < -10:
                output = f"REJECT. 이유: 120일선 아래의 역배열 하락장이며, {name} 시장 특유의 '떨어지는 칼날(가짜 반등)'입니다. 추가 폭락 위험이 높으므로 매수를 거절합니다."
            elif profit_pct >= 5 and loss_pct >= -5:
                output = f"CONFIRM. 이유: 충분한 과매도 구간이며 {name} 시장에서도 단기 기술적 반등 확률이 높은 안전한 자리입니다."
            else:
                output = "REJECT. 이유: 변동성이 너무 크고 확실한 반등 시그널이 부족합니다. 횡보장에서는 관망하세요."

            dataset.append({"text_input": text_input, "output": output})

with open("global_market_training.jsonl", "w", encoding="utf-8") as f:
    for data in dataset:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")

print(f"✅ 총 {len(dataset)}개의 미장+국장 통합 차트/하락장 방어 교육 데이터가 저장되었습니다!")