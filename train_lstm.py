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

# 이전 단계에서 만든 딥러닝 모델 구조 불러오기
from dl_model import StockLSTM

print("🧠 [딥러닝 엔진] AI 모델 학습(Training) 스크립트를 시작합니다...")

# 1. 학습용 우량주 및 주도주 유니버스 (데이터 수집용)
TRAIN_TICKERS = ["005930", "000660", "035420", "005380", "068270", "003850", "035720", "051910"]
SEQ_LENGTH = 20
PREDICT_DAYS = 5
EPOCHS = 50

# 2. 데이터 수집 및 전처리
all_x, all_y = [], []
scaler = MinMaxScaler(feature_range=(-1, 1))

end_date = datetime.today()
start_date = end_date - timedelta(days=1500) # 약 4~5년 치 과거 데이터

print("📊 과거 차트 데이터를 수집하고 텐서(Tensor)로 변환 중...")
for ticker in TRAIN_TICKERS:
    try:
        df = stock.get_market_ohlcv_by_date(start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"), ticker)
        if df.empty: continue
        
        df.rename(columns={'시가':'open','고가':'high','저가':'low','종가':'close','거래량':'volume'}, inplace=True)
        data = df[['open', 'high', 'low', 'close', 'volume']].values
        scaled_data = scaler.fit_transform(data)
        
        # 시계열 시퀀스(20일)와 정답지(5일 뒤 주가 상승 여부) 생성
        for i in range(len(scaled_data) - SEQ_LENGTH - PREDICT_DAYS):
            seq = scaled_data[i : i + SEQ_LENGTH]
            current_close = df['close'].iloc[i + SEQ_LENGTH - 1]
            future_close = df['close'].iloc[i + SEQ_LENGTH + PREDICT_DAYS - 1]
            
            # 5일 뒤 주가가 현재보다 2% 이상 오르면 정답(1), 아니면 오답(0)
            label = 1.0 if future_close > current_close * 1.02 else 0.0
            
            all_x.append(seq)
            all_y.append(label)
    except Exception as e:
        print(f"[{ticker}] 데이터 수집 오류: {e}")

# 파이토치 텐서로 변환
X_tensor = torch.FloatTensor(np.array(all_x))
y_tensor = torch.FloatTensor(np.array(all_y)).unsqueeze(1)

# 데이터로더 생성
dataset = TensorDataset(X_tensor, y_tensor)
dataloader = DataLoader(dataset, batch_size=64, shuffle=True)

# 3. 모델 초기화 및 학습(Training)
model = StockLSTM(input_size=5, hidden_layer_size=50, output_size=1)
criterion = nn.BCELoss() # 확률(0~1) 예측용 오차 함수
optimizer = optim.Adam(model.parameters(), lr=0.001)

print(f"🔥 총 {len(all_x)}개의 패턴 데이터를 학습합니다. (시간이 다소 소요됩니다)")

for epoch in range(EPOCHS):
    model.train()
    epoch_loss = 0
    for batch_x, batch_y in dataloader:
        optimizer.zero_grad()
        outputs = model(batch_x)
        loss = criterion(outputs, batch_y)
        loss.backward()
        optimizer.step()
        epoch_loss += loss.item()
    
    if (epoch+1) % 10 == 0:
        print(f" - Epoch [{epoch+1}/{EPOCHS}] Loss: {epoch_loss/len(dataloader):.4f}")

# 4. 뇌(가중치 파일) 저장
model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_lstm_model.pth")
torch.save(model.state_dict(), model_path)
print(f"🎉 학습 완료! 딥러닝 뇌 파일이 저장되었습니다: {model_path}")
print("   이제 봇을 켜면 딥러닝 예측 확률(%)이 스크리너 점수에 자동 반영됩니다!")