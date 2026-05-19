import torch
import torch.nn as pd_nn
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
import pickle
import os

SCALER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_scaler.pkl")


class StockLSTM(pd_nn.Module):
    def __init__(self, input_size=5, hidden_layer_size=50, output_size=1):
        super(StockLSTM, self).__init__()
        self.hidden_layer_size = hidden_layer_size
        self.lstm = pd_nn.LSTM(input_size, hidden_layer_size, batch_first=True)
        self.linear = pd_nn.Linear(hidden_layer_size, output_size)
        self.sigmoid = pd_nn.Sigmoid()

    def forward(self, input_seq):
        lstm_out, _ = self.lstm(input_seq)
        predictions = self.linear(lstm_out[:, -1, :])
        return self.sigmoid(predictions)


class DeepLearningPredictor:
    def __init__(self, model_path=None):
        if model_path is None:
            model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_lstm_model.pth")
        self.model_path = model_path
        self.model = StockLSTM()
        self.is_trained = os.path.exists(self.model_path)

        # 훈련 시 저장된 스케일러 로드 — 없으면 fallback 스케일러 사용
        if os.path.exists(SCALER_PATH):
            with open(SCALER_PATH, 'rb') as f:
                self.scaler = pickle.load(f)
            self._scaler_fitted = True
        else:
            self.scaler = MinMaxScaler(feature_range=(-1, 1))
            self._scaler_fitted = False

        if self.is_trained:
            self.model.load_state_dict(torch.load(self.model_path, weights_only=True))
            self.model.eval()

    def preprocess_data(self, df):
        """OHLCV → 정규화 텐서 변환. 훈련 스케일러 있으면 transform만, 없으면 fit_transform."""
        data = df[['open', 'high', 'low', 'close', 'volume']].values.astype(float)
        if self._scaler_fitted:
            return self.scaler.transform(data)   # 훈련 분포 그대로 유지
        return self.scaler.fit_transform(data)    # 스케일러 없을 때 폴백

    def predict_up_probability(self, df):
        """과거 20일치 OHLCV로 5일 후 +2% 상승 확률(%) 반환.
        모델/데이터 부족 시 50.0 (중립) 반환 — 전 종목 패널티 방지."""
        if not self.is_trained or len(df) < 20:
            return 50.0  # 중립값: ml_factor_score = (50-50)*0.2 = 0

        try:
            scaled_data = self.preprocess_data(df.tail(20))
            tensor_data = torch.FloatTensor(scaled_data).unsqueeze(0)
            with torch.no_grad():
                prob = self.model(tensor_data).item()
            return prob * 100.0
        except Exception:
            return 50.0