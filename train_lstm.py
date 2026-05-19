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

# 이전 단계에서 만든 딥러닝 모델 구조 불러오기
from dl_model import StockLSTM

print("🧠 [딥러닝 엔진] AI 모델 양대 시장 상위 200대 주도주 결합 대형 학습을 시작합니다...")

# 1. 동적 유니버스 생성: 실행일 기준 KOSPI + KOSDAQ 거래대금 대통합 상위 200개 추출
try:
    # 최근 정상 영업일 날짜 확인용 (주말 및 휴일 장부 백업 방어코드)
    dt = datetime.today()
    today_str = dt.strftime("%Y%m%d")
    for _ in range(10):
        df_check = stock.get_market_ohlcv_by_date(today_str, today_str, "005930")
        if not df_check.empty:
            break
        dt -= timedelta(days=1)
        today_str = dt.strftime("%Y%m%d")

    print(f"📅 데이터 스캔 타겟 기준 영업일: {today_str}")
    df_kospi = stock.get_market_price_change_by_ticker(today_str, today_str, "KOSPI")
    df_kosdaq = stock.get_market_price_change_by_ticker(today_str, today_str, "KOSDAQ")
    df_all = pd.concat([df_kospi, df_kosdaq])
    
    # 거래대금 내림차순 정렬 후 최상위 200개 골라내기
    top_200 = df_all.sort_values(by='거래대금', ascending=False).head(200)
    TRAIN_TICKERS = top_200.index.tolist()
    print(f"🔥 시장 최고 주도주 200 종목 리스트 수집 성공! (당일 거래대금 대장주: {top_200['종목명'].iloc[0]})")
except Exception as universe_err:
    print(f"⚠️ 상위 200개 실시간 추출 실패로 우량주 기본 가이드셋으로 대체합니다: {universe_err}")
    TRAIN_TICKERS = ["005930", "000660", "035420", "005380", "068270", "003850", "035720", "051910"]

SEQ_LENGTH = 20
PREDICT_DAYS = 5
EPOCHS = 40  # 200개 종목의 빅데이터이므로 과적합 방지 및 연산 가용 한도 조율차 40 에포크로 최적화

# 2. 데이터 수집 및 전처리
all_x, all_y = [], []
scaler = MinMaxScaler(feature_range=(-1, 1))

end_date = datetime.today()
start_date = end_date - timedelta(days=1200) # t2.micro 사양 최적화를 위해 과거 1200일(약 3~4년) 패턴 추출

print("📊 200개 종목의 시계열 차트 빅데이터 병렬 스캔 및 정규화 진행 중...")
for idx, ticker in enumerate(TRAIN_TICKERS, 1):
    try:
        df = stock.get_market_ohlcv_by_date(start_date.strftime("%Y%m%d"), end_date.strftime("%Y%m%d"), ticker)
        if df.empty or len(df) < (SEQ_LENGTH + PREDICT_DAYS + 10): 
            continue
        
        df.rename(columns={'시가':'open','고가':'high','저가':'low','종가':'close','거래량':'volume'}, inplace=True)
        data = df[['open', 'high', 'low', 'close', 'volume']].values
        scaled_data = scaler.fit_transform(data)
        
        for i in range(len(scaled_data) - SEQ_LENGTH - PREDICT_DAYS):
            seq = scaled_data[i : i + SEQ_LENGTH]
            current_close = df['close'].iloc[i + SEQ_LENGTH - 1]
            future_close = df['close'].iloc[i + SEQ_LENGTH + PREDICT_DAYS - 1]
            
            # 5일 뒤 종가가 현재 종가 대비 2% 넘게 상승하면 진짜 상승 패턴(1.0), 아니면 소외 패턴(0.0)
            label = 1.0 if future_close > current_close * 1.02 else 0.0
            all_x.append(seq)
            all_y.append(label)
            
        if idx % 20 == 0:
            print(f"   [진행률: {idx}/200] 차트 조각 분석 완료 (추출된 누적 학습 데이터: {len(all_x)}개)")
        
        # 🚨 [매우 중요] 거래소 서버의 요청 제한(IP Block)을 차단하기 위한 0.15초 안전 지연 버퍼
        time.sleep(0.15)
    except Exception:
        continue

if not all_x:
    print("❌ 수집된 데이터셋 파편이 없어 인공지능 훈련을 중단합니다.")
    exit()

X_tensor = torch.FloatTensor(np.array(all_x))
y_tensor = torch.FloatTensor(np.array(all_y)).unsqueeze(1)

dataset = TensorDataset(X_tensor, y_tensor)
dataloader = DataLoader(dataset, batch_size=128, shuffle=True) # 속도 극대화를 위해 대형 배치 설정

# 3. 모델 초기화 및 순방향/역방향 오차 역전파 학습
model = StockLSTM(input_size=5, hidden_layer_size=50, output_size=1)
criterion = nn.BCELoss()
optimizer = optim.Adam(model.parameters(), lr=0.001)

print(f"🚀 총 {len(all_x)}개의 시계열 매수 타점 데이터 장부 구축 완료! PyTorch 인공지능 훈련을 개시합니다.")

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
    
    if (epoch+1) % 5 == 0:
        print(f" 🎯 AI 학습 주기 [{epoch+1}/{EPOCHS}] 종합 손실도(Loss): {epoch_loss/len(dataloader):.4f}")

# 4. 모델 가중치 + 스케일러 저장 (예측 시 동일한 정규화 범위 사용하기 위해)
base_dir = os.path.dirname(os.path.abspath(__file__))
model_path = os.path.join(base_dir, "stock_lstm_model.pth")
scaler_path = os.path.join(base_dir, "stock_scaler.pkl")

torch.save(model.state_dict(), model_path)
with open(scaler_path, 'wb') as f:
    pickle.dump(scaler, f)
print(f"🎉 [대성공] 모델 저장 완료! 경로: {model_path}")
print(f"📦 스케일러 저장 완료! 경로: {scaler_path}")