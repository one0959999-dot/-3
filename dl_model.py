"""
dl_model.py — 고도화 LSTM 예측 모델
입력 피처: OHLCV 5개 + RSI/MACD/볼린저밴드/거래량비율/모멘텀 5개 = 총 10개
구조: 2-Layer Bidirectional LSTM + Dropout + FC
"""
import torch
import torch.nn as pd_nn
import numpy as np
import pandas as pd
from sklearn.preprocessing import MinMaxScaler
import pickle
import os

SCALER_PATH  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_scaler.pkl")
INPUT_SIZE   = 10   # OHLCV(5) + 기술지표(5)
SEQ_LEN      = 30   # 과거 30일 시퀀스 (기존 20일 → 확장)


def _add_features(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame에 기술지표 5개를 추가해 10피처 DataFrame 반환."""
    df = df.copy()
    c = df['close']

    # RSI(14)
    d  = c.diff()
    g  = d.clip(lower=0).rolling(14, min_periods=1).mean()
    lo = (-d.clip(upper=0)).rolling(14, min_periods=1).mean()
    df['rsi'] = 100 - 100 / (1 + g / (lo + 1e-10))

    # MACD histogram (12/26/9)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd  = ema12 - ema26
    sig   = macd.ewm(span=9, adjust=False).mean()
    df['macd_hist'] = macd - sig

    # 볼린저밴드 %B
    mid  = c.rolling(20, min_periods=1).mean()
    std  = c.rolling(20, min_periods=1).std().fillna(0)
    df['bb_pct'] = (c - (mid - 2 * std)) / (4 * std + 1e-9)

    # 거래량 비율 (오늘 / 20일 평균)
    vol_avg = df['volume'].rolling(20, min_periods=1).mean()
    df['vol_ratio'] = df['volume'] / (vol_avg + 1e-9)

    # 5일 모멘텀 (수익률)
    df['momentum'] = c.pct_change(5).fillna(0)

    df = df.fillna(0)
    return df


class StockLSTM(pd_nn.Module):
    """
    2-Layer Bidirectional LSTM + Dropout.
    기존 단층 단방향 50유닛 → 2층 양방향 128유닛으로 확장.
    """
    def __init__(self, input_size=INPUT_SIZE, hidden_size=128, num_layers=2,
                 dropout=0.3, output_size=1):
        super().__init__()
        self.lstm = pd_nn.LSTM(
            input_size, hidden_size,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=True,
            dropout=dropout if num_layers > 1 else 0.0
        )
        self.dropout = pd_nn.Dropout(dropout)
        self.bn      = pd_nn.BatchNorm1d(hidden_size * 2)
        self.fc1     = pd_nn.Linear(hidden_size * 2, 64)
        self.relu    = pd_nn.ReLU()
        self.fc2     = pd_nn.Linear(64, output_size)
        self.sigmoid = pd_nn.Sigmoid()

    def forward(self, x):
        out, _ = self.lstm(x)          # (batch, seq, hidden*2)
        out    = out[:, -1, :]         # 마지막 타임스텝
        out    = self.bn(out)
        out    = self.dropout(out)
        out    = self.relu(self.fc1(out))
        return self.sigmoid(self.fc2(out))


class DeepLearningPredictor:
    def __init__(self, model_path=None):
        if model_path is None:
            model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_lstm_model.pth")
        self.model_path = model_path
        self.model      = StockLSTM()
        self.is_trained = os.path.exists(self.model_path)

        # 훈련 시 저장된 스케일러 로드
        if os.path.exists(SCALER_PATH):
            with open(SCALER_PATH, 'rb') as f:
                self.scaler = pickle.load(f)
            self._scaler_fitted = True
        else:
            self.scaler = MinMaxScaler(feature_range=(-1, 1))
            self._scaler_fitted = False

        if self.is_trained:
            try:
                self.model.load_state_dict(
                    torch.load(self.model_path, weights_only=True, map_location='cpu')
                )
                self.model.eval()
            except Exception:
                # 구조 변경으로 기존 모델 로드 실패 시 재훈련 필요 플래그
                self.is_trained = False

    def preprocess_data(self, df: pd.DataFrame) -> np.ndarray:
        """OHLCV+기술지표 10피처 → 정규화 배열 반환."""
        df_feat = _add_features(df)
        cols = ['open', 'high', 'low', 'close', 'volume',
                'rsi', 'macd_hist', 'bb_pct', 'vol_ratio', 'momentum']
        data = df_feat[cols].values.astype(float)
        if self._scaler_fitted:
            return self.scaler.transform(data)
        return self.scaler.fit_transform(data)

    def predict_up_probability(self, df: pd.DataFrame) -> float:
        """
        과거 30일치 데이터로 5일 후 +2% 상승 확률(%) 반환.
        모델 미훈련 또는 데이터 부족 시 50.0(중립) 반환.
        """
        if not self.is_trained or len(df) < SEQ_LEN:
            return 50.0

        try:
            scaled = self.preprocess_data(df.tail(SEQ_LEN))
            tensor = torch.FloatTensor(scaled).unsqueeze(0)  # (1, seq, features)
            with torch.no_grad():
                prob = self.model(tensor).item()
            return round(prob * 100.0, 2)
        except Exception:
            return 50.0
