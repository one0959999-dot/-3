"""
train_lstm.py — 고도화 LSTM 주간 재훈련 스크립트
입력 피처: OHLCV(5) + RSI/MACD/볼린저밴드/거래량비율/모멘텀(5) = 10개
모델: 2-Layer Bidirectional LSTM + Dropout + BatchNorm
"""
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, TensorDataset
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
from pykrx import stock
from datetime import datetime, timedelta
import os
import pickle
import time

from dl_model import StockLSTM, _add_features, INPUT_SIZE, SEQ_LEN

print("🧠 [딥러닝 엔진 v2] 10피처 Bidirectional LSTM 훈련을 시작합니다...")

# ── 1. 유니버스 생성: 당일 거래대금 상위 200개 ─────────────────────────────
try:
    dt = datetime.today()
    today_str = dt.strftime("%Y%m%d")
    for _ in range(10):
        df_check = stock.get_market_ohlcv_by_date(today_str, today_str, "005930")
        if not df_check.empty:
            break
        dt -= timedelta(days=1)
        today_str = dt.strftime("%Y%m%d")

    print(f"📅 기준 영업일: {today_str}")
    df_kospi  = stock.get_market_price_change_by_ticker(today_str, today_str, "KOSPI")
    df_kosdaq = stock.get_market_price_change_by_ticker(today_str, today_str, "KOSDAQ")
    df_all    = pd.concat([df_kospi, df_kosdaq])
    top_200   = df_all.sort_values(by='거래대금', ascending=False).head(200)
    TRAIN_TICKERS = top_200.index.tolist()
    print(f"🔥 상위 200 종목 수집 완료 (대장주: {top_200['종목명'].iloc[0]})")
except Exception as e:
    print(f"⚠️ 상위 200 추출 실패 → 기본 종목으로 대체: {e}")
    TRAIN_TICKERS = ["005930", "000660", "035420", "005380", "068270",
                     "003850", "035720", "051910", "207940", "006400"]

PREDICT_DAYS = 5
EPOCHS       = 50   # 피처 증가에 따른 학습 에포크 증가

# ── 2. 데이터 수집 및 10피처 전처리 ──────────────────────────────────────────
all_x, all_y = [], []
scaler = MinMaxScaler(feature_range=(-1, 1))

end_date   = datetime.today()
start_date = end_date - timedelta(days=1500)  # 약 4년치

FEATURE_COLS = ['open', 'high', 'low', 'close', 'volume',
                'rsi', 'macd_hist', 'bb_pct', 'vol_ratio', 'momentum']

print("📊 200개 종목 빅데이터 스캔 중...")
raw_segments = []

for idx, ticker in enumerate(TRAIN_TICKERS, 1):
    try:
        df = stock.get_market_ohlcv_by_date(
            start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"), ticker
        )
        if df.empty or len(df) < (SEQ_LEN + PREDICT_DAYS + 30):
            continue

        df.rename(columns={'시가': 'open', '고가': 'high', '저가': 'low',
                            '종가': 'close', '거래량': 'volume'}, inplace=True)

        # 10피처 추가
        df_feat = _add_features(df)
        data = df_feat[FEATURE_COLS].values.astype(float)
        raw_segments.append((data, df['close'].values))

        if idx % 20 == 0:
            print(f"   [진행률: {idx}/200] 누적 세그먼트: {len(raw_segments)}개")
        time.sleep(0.15)
    except Exception:
        continue

if not raw_segments:
    print("❌ 수집된 데이터 없음. 훈련 중단.")
    exit()

# 전체 데이터 합쳐서 스케일러 fit
all_data = np.vstack([seg[0] for seg in raw_segments])
scaler.fit(all_data)

for data, closes in raw_segments:
    scaled = scaler.transform(data)
    for i in range(len(scaled) - SEQ_LEN - PREDICT_DAYS):
        seq          = scaled[i: i + SEQ_LEN]
        cur_close    = closes[i + SEQ_LEN - 1]
        future_close = closes[i + SEQ_LEN + PREDICT_DAYS - 1]
        label = 1.0 if future_close > cur_close * 1.02 else 0.0
        all_x.append(seq)
        all_y.append(label)

print(f"✅ 총 {len(all_x)}개 시계열 샘플 구축 완료")

X_tensor = torch.FloatTensor(np.array(all_x))
y_tensor = torch.FloatTensor(np.array(all_y)).unsqueeze(1)

# 클래스 불균형 보정 (상승 샘플이 적을 수 있음)
pos_weight = torch.tensor([(y_tensor == 0).sum() / (y_tensor == 1).sum() + 1e-9])

dataset    = TensorDataset(X_tensor, y_tensor)
dataloader = DataLoader(dataset, batch_size=256, shuffle=True)

# ── 3. 모델 훈련 ──────────────────────────────────────────────────────────────
model     = StockLSTM(input_size=INPUT_SIZE)
criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
optimizer = optim.AdamW(model.parameters(), lr=0.001, weight_decay=1e-4)
scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=EPOCHS)

print(f"🚀 PyTorch 훈련 개시 (에포크: {EPOCHS}, 배치: 256, 피처: {INPUT_SIZE}개)")

for epoch in range(EPOCHS):
    model.train()
    epoch_loss = 0.0
    for batch_x, batch_y in dataloader:
        optimizer.zero_grad()
        outputs    = model(batch_x)
        loss       = criterion(outputs, batch_y)
        loss.backward()
        nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
        optimizer.step()
        epoch_loss += loss.item()
    scheduler.step()

    if (epoch + 1) % 10 == 0:
        print(f"   🎯 에포크 [{epoch+1}/{EPOCHS}] Loss: {epoch_loss/len(dataloader):.4f}")

# ── 4. 모델 및 스케일러 저장 ──────────────────────────────────────────────────
base_dir    = os.path.dirname(os.path.abspath(__file__))
model_path  = os.path.join(base_dir, "stock_lstm_model.pth")
scaler_path = os.path.join(base_dir, "stock_scaler.pkl")

torch.save(model.state_dict(), model_path)
with open(scaler_path, 'wb') as f:
    pickle.dump(scaler, f)

print(f"🎉 모델 저장 완료: {model_path}")
print(f"📦 스케일러 저장 완료: {scaler_path}")
print("✅ 고도화 LSTM v2 훈련 완료!")
