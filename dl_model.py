import torch
import torch.nn as pd_nn
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
import os

# PyTorch LSTM 딥러닝 신경망 모델 정의
class StockLSTM(pd_nn.Module):
    def __init__(self, input_size=5, hidden_layer_size=50, output_size=1):
        super(StockLSTM, self).__init__()
        self.hidden_layer_size = hidden_layer_size
        self.lstm = pd_nn.LSTM(input_size, hidden_layer_size, batch_first=True)
        self.linear = pd_nn.Linear(hidden_layer_size, output_size)
        self.sigmoid = pd_nn.Sigmoid() # 0~1 사이의 확률값 반환

    def forward(self, input_seq):
        lstm_out, _ = self.lstm(input_seq)
        predictions = self.linear(lstm_out[:, -1, :])
        return self.sigmoid(predictions)

class DeepLearningPredictor:
    def __init__(self, model_path="stock_lstm_model.pth"):
        self.model_path = model_path
        self.scaler = MinMaxScaler(feature_range=(-1, 1))
        self.model = StockLSTM()
        self.is_trained = os.path.exists(self.model_path)
        
        if self.is_trained:
            self.model.load_state_dict(torch.load(self.model_path, weights_only=True))
            self.model.eval()

    def preprocess_data(self, df):
        """OHLCV 데이터를 텐서로 변환"""
        data = df[['open', 'high', 'low', 'close', 'volume']].values
        return self.scaler.fit_transform(data)

    def predict_up_probability(self, df):
        """과거 20일치 차트 데이터를 넣고 내일 주가가 오를 확률(%) 계산"""
        if not self.is_trained or len(df) < 20:
            return 0.0 # 모델이 없거나 데이터가 부족하면 0점 처리
        
        scaled_data = self.preprocess_data(df.tail(20))
        # 파이토치 텐서로 변환 (Batch=1, Seq=20, Features=5)
        tensor_data = torch.FloatTensor(scaled_data).unsqueeze(0)
        
        with torch.no_grad():
            prob = self.model(tensor_data).item()
            
        return prob * 100.0 # 0~100% 확률로 변환