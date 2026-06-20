"""
us_screener.py — 미국장 위성 종목 스크리너 (yfinance 기반, KR 동등 수준, 토스증권 API 통합)
──────────────────────────────────────────────────────────────────────
① S&P 500 동적 유니버스 (Wikipedia, 일 1회 캐싱) + 기존 고성장 목록
② 섹터 ETF 모멘텀 (GICS 기반, 20d×0.7 + 5d×0.3 듀얼 모멘텀, 4시간 캐시)
③ 모멘텀(20d/60d) + 골든크로스(SMA50>200) + 거래량 서지 + RSI
④ 52주 위치 점수 (스윗스팟 40~80%)
⑤ 백테스트 최적 전략 (walk-forward OOS 검증, 12가지 전략)
⑥ 신호 준비도 (BUY 신호까지 거리 점수화)
⑦ PyTorch LSTM 딥러닝 상승확률
⑧ 과열 패널티 (20d +15% 초과 + 거래량 서지 없음)
⑨ KIS 랭킹 보너스 (거래증가율·52주신고가·거래량·상승율 4종 교차)
⑩ 종합 스코어 → AI 최종 심사 → N개 선정
"""

import time
import datetime
import logging
import threading

import numpy as np
import yfinance as yf
import pandas as pd

logger = logging.getLogger('lassi_bot')

# ── 코어 유니버스: 이미 증명된 우량 대형주 ──────────────────────────────
# 장기 보유에 적합한 섹터 리더십 + 탄탄한 펀더멘털 보유 종목
CORE_UNIVERSE: dict[str, list[str]] = {
    "AI/반도체":   ["NVDA", "AMD", "AVGO", "AMAT", "LRCX", "QCOM"],
    "빅테크":      ["MSFT", "AAPL", "META", "GOOGL", "AMZN", "ORCL", "CRM"],
    "바이오/헬스": ["LLY", "NVO", "ABBV", "ISRG", "REGN"],
    "금융":        ["JPM", "GS", "V", "MA"],
    "소비/유통":   ["COST", "HD", "MCD", "NKE"],
    "에너지":      ["XOM", "CVX"],
}

# ── 위성 유니버스: 제2의 엔비디아·테슬라가 될 고성장 후보군 ───────────────
# 폭발적 성장 가능성이 높은 신흥 대형주 / 테마 선도주
SATELLITE_UNIVERSE: dict[str, list[str]] = {
    "AI/반도체 신흥":  ["ARM", "SMCI", "MRVL", "PLTR", "SNOW", "MU"],
    "우주/방산":       ["RKLB", "ACHR", "LMT", "NOC", "RTX", "HII"],
    "핀테크/크립토":   ["COIN", "SQ", "HOOD", "SOFI"],
    "바이오 신흥":     ["MRNA", "RXRX", "NVAX"],
    "소비/성장":       ["TSLA", "UBER", "SHOP", "CELH"],
    "클라우드/SaaS":   ["DDOG", "NET", "ZS", "GTLB"],
}

# ── 하위 호환: 기존 코드가 US_UNIVERSE 참조하는 경우 대비 ────────────────
US_UNIVERSE: dict[str, list[str]] = {
    **CORE_UNIVERSE,
    **SATELLITE_UNIVERSE,
}

# 코어 ETF — 스캔에서 제외
CORE_ETF_EXCLUDE = {"SPY", "QQQ", "IWM", "VTI", "VOO", "TQQQ", "SOXL", "UPRO", "TNA"}

# ── NASDAQ 100 폴백 (S&P 500 동적 로드 실패 시 사용) ─────────────────────
NASDAQ100_FALLBACK: list[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "COST", "NFLX",
    "AMD", "ADBE", "QCOM", "INTC", "PEP", "CSCO", "TMUS", "INTU", "TXN", "AMGN",
    "HON", "SBUX", "AMAT", "ISRG", "LRCX", "BKNG", "ADI", "MU", "VRTX", "MDLZ",
    "REGN", "PANW", "KLAC", "CDNS", "SNPS", "MRVL", "ABNB", "CEG", "CRWD", "MAR",
    "ORLY", "CTAS", "MNST", "FTNT", "WDAY", "DXCM", "PYPL", "PCAR", "CPRT", "ROST",
    "PAYX", "FAST", "ODFL", "IDXX", "EXC", "FANG", "BKR", "ON", "CHTR", "VRSK",
    "SMCI", "PLTR", "ARM", "SNOW", "NET", "DDOG", "ZS", "COIN", "SHOP", "UBER",
    "RKLB", "HOOD", "SOFI", "GTLB", "RXRX", "MRNA", "NVAX", "CELH", "SQ", "ACHR",
    "LMT", "NOC", "RTX", "CRM", "ORCL", "NOW", "VEEV", "HUBS", "RBLX", "ZM",
    "IONQ", "SOUN", "BBAI", "ACMR", "WOLF", "LSCC", "ONTO", "AMBA", "AEHR", "NPKI",
]

# ── 동적 유니버스 캐시 ─────────────────────────────────────────────────────
_dyn_cache: dict = {'universe': {}, 'date': ''}
_dyn_lock  = threading.Lock()


def _get_dynamic_satellite_universe() -> dict[str, list[str]]:
    """
    S&P 500을 Wikipedia에서 동적 로드 + 기존 SATELLITE_UNIVERSE 병합.
    일 1회 캐싱. Wikipedia 실패 시 NASDAQ100_FALLBACK 사용.
    """
    global _dyn_cache
    today = datetime.date.today().isoformat()

    with _dyn_lock:
        if _dyn_cache['universe'] and _dyn_cache['date'] == today:
            return _dyn_cache['universe']

    # 기존 고성장 목록 먼저 포함
    universe: dict[str, list[str]] = {}
    for sec, tickers in SATELLITE_UNIVERSE.items():
        universe[sec] = list(tickers)

    try:
        tables = pd.read_html('https://en.wikipedia.org/wiki/List_of_S%26P_500_companies',
                              attrs={'id': 'constituents'})
        df = tables[0]
        loaded = 0
        for _, row in df.iterrows():
            ticker = str(row['Symbol']).replace('.', '-')  # BRK.B → BRK-B
            sector = str(row.get('GICS Sector', '기타'))
            if ticker in CORE_ETF_EXCLUDE:
                continue
            universe.setdefault(sector, [])
            if ticker not in universe[sector]:
                universe[sector].append(ticker)
                loaded += 1
        logger.info(f"[US스크리너] S&P500 동적 유니버스 로드 완료: {loaded}개 추가")
    except Exception as e:
        logger.warning(f"[US스크리너] S&P500 동적 로드 실패 → NASDAQ100 폴백: {e}")
        universe.setdefault('NASDAQ100', [])
        for t in NASDAQ100_FALLBACK:
            if t not in universe['NASDAQ100']:
                universe['NASDAQ100'].append(t)

    with _dyn_lock:
        _dyn_cache = {'universe': universe, 'date': today}

    return universe

# 종목명 캐시 (yfinance info 조회 비용 절감)
_name_cache: dict[str, str] = {}
_name_lock = threading.Lock()


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / (loss + 1e-10)
    return 100 - 100 / (1 + rs)


def _get_name(ticker: str) -> str:
    """종목명 조회 (캐시 우선)"""
    with _name_lock:
        if ticker in _name_cache:
            return _name_cache[ticker]
    try:
        info = yf.Ticker(ticker).fast_info
        name = (getattr(info, 'name', '') or
                getattr(info, 'short_name', '') or ticker)
    except Exception:
        name = ticker
    with _name_lock:
        _name_cache[ticker] = name
    return name


# ══════════════════════════════════════════════════════════════════════
# ── US 섹터 모멘텀 (KR get_sector_momentum 동등)
# ══════════════════════════════════════════════════════════════════════

# GICS 섹터명 + 커스텀 위성 섹터 → 대표 ETF 매핑
_US_SECTOR_ETF_MAP: dict[str, list[str]] = {
    "Information Technology":  ["XLK", "SOXX"],
    "Health Care":             ["XLV", "IBB"],
    "Financials":              ["XLF"],
    "Consumer Discretionary":  ["XLY"],
    "Communication Services":  ["XLC"],
    "Industrials":             ["XLI"],
    "Consumer Staples":        ["XLP"],
    "Energy":                  ["XLE"],
    "Utilities":               ["XLU"],
    "Real Estate":             ["XLRE"],
    "Materials":               ["XLB"],
    # 커스텀 위성 섹터
    "AI/반도체 신흥":           ["SOXX", "XLK"],
    "우주/방산":                ["ITA"],
    "핀테크/크립토":            ["XLF"],
    "바이오 신흥":              ["IBB"],
    "소비/성장":                ["XLY"],
    "클라우드/SaaS":            ["XLK"],
    "NASDAQ100":               ["QQQ"],
    "기타":                    ["SPY"],
}
_us_sec_mom_cache: dict = {'data': {}, 'ts': 0.0}
_us_sec_mom_lock  = threading.Lock()
_US_SEC_MOM_TTL   = 4 * 3600  # 4시간 캐시


def _get_us_sector_momentum() -> dict[str, float]:
    """US 섹터 ETF 기반 듀얼 모멘텀 계산. 4시간 캐싱.
    Returns: {sector_name: blended_momentum_pct}  (20d×0.7 + 5d×0.3)
    """
    now_ts = time.time()
    with _us_sec_mom_lock:
        if now_ts - _us_sec_mom_cache['ts'] < _US_SEC_MOM_TTL and _us_sec_mom_cache['data']:
            return dict(_us_sec_mom_cache['data'])

    unique_etfs = list({e for etfs in _US_SECTOR_ETF_MAP.values() for e in etfs})
    try:
        raw = yf.download(unique_etfs, period="30d", interval="1d",
                          progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return {}
        if isinstance(raw.columns, pd.MultiIndex):
            lv0    = raw.columns.get_level_values(0).unique().tolist()
            closes = raw["Close"] if "Close" in lv0 else raw.xs("Close", axis=1, level=1, drop_level=True)
        else:
            closes = raw[["Close"]].rename(columns={"Close": unique_etfs[0]})

        etf_ret: dict[str, float] = {}
        for etf in unique_etfs:
            try:
                if etf not in closes.columns:
                    continue
                c = closes[etf].dropna()
                if len(c) < 6:
                    continue
                ret_20 = (float(c.iloc[-1]) / float(c.iloc[-min(21, len(c)-1)]) - 1) * 100
                ret_5  = (float(c.iloc[-1]) / float(c.iloc[-min(6,  len(c)-1)]) - 1) * 100
                etf_ret[etf] = ret_20 * 0.70 + ret_5 * 0.30
            except Exception:
                continue

        sector_momentum: dict[str, float] = {}
        for sector, etfs in _US_SECTOR_ETF_MAP.items():
            vals = [etf_ret[e] for e in etfs if e in etf_ret]
            if vals:
                sector_momentum[sector] = round(sum(vals) / len(vals), 2)

        with _us_sec_mom_lock:
            _us_sec_mom_cache['data'] = sector_momentum
            _us_sec_mom_cache['ts']   = time.time()

        logger.info(f"[US스크리너] 섹터 모멘텀 로드: {len(sector_momentum)}개 섹터")
        return sector_momentum
    except Exception as e:
        logger.warning(f"[US스크리너] 섹터 모멘텀 조회 실패: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════════
# ── 전략 백테스트 엔진 (KR find_best_strategy 동등)
# ══════════════════════════════════════════════════════════════════════

def _ema_us(s, n): return s.ewm(span=n, adjust=False).mean()
def _sma_us(s, n): return s.rolling(n).mean()

def _rsi_us(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-10))

def _macd_us(s, f=12, sl=26, sig=9):
    m = _ema_us(s, f) - _ema_us(s, sl)
    return m, _ema_us(m, sig)

def _bb_us(s, p=20, k=2):
    mid = _sma_us(s, p); std = s.rolling(p).std()
    return mid + k*std, mid, mid - k*std

def _stoch_us(h, l, c, kp=21, dp=5):
    lo = l.rolling(kp).min(); hi = h.rolling(kp).max()
    k  = 100 * (c - lo) / (hi - lo + 1e-10)
    return k, k.rolling(dp).mean()

def _cci_us(h, l, c, p=20):
    tp = (h + l + c) / 3; ma = _sma_us(tp, p)
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - ma) / (0.015 * md + 1e-10)

def _williams_us(h, l, c, p=14):
    return -100 * (h.rolling(p).max() - c) / (h.rolling(p).max() - l.rolling(p).min() + 1e-10)

def _cross_sig_us(fast, slow):
    s = pd.Series(0, index=fast.index)
    s[fast > slow] = 1; s[fast < slow] = -1
    t = s.diff().fillna(0)
    out = pd.Series(0, index=s.index)
    out[t > 0] = 1; out[t < 0] = -1
    return out

def _threshold_sig_us(ind, lo, hi):
    s = pd.Series(0, index=ind.index)
    s[ind < lo] = 1; s[ind > hi] = -1
    prev = 0
    for i in s.index:
        if s[i] == prev:  s[i] = 0
        elif s[i] != 0:   prev = s[i]
    return s

_STRATEGY_REGISTRY_US = {
    "RSI(9) 30/70":      lambda c, h, l: _threshold_sig_us(_rsi_us(c, 9),  30, 70),
    "RSI(14) 40/60":     lambda c, h, l: _threshold_sig_us(_rsi_us(c, 14), 40, 60),
    "EMA 5/20 크로스":    lambda c, h, l: _cross_sig_us(_ema_us(c, 5),  _ema_us(c, 20)),
    "EMA 3/10 크로스":    lambda c, h, l: _cross_sig_us(_ema_us(c, 3),  _ema_us(c, 10)),
    "SMA 5/20 크로스":    lambda c, h, l: _cross_sig_us(_sma_us(c, 5),  _sma_us(c, 20)),
    "SMA 3/10 크로스":    lambda c, h, l: _cross_sig_us(_sma_us(c, 3),  _sma_us(c, 10)),
    "SMA 3/20 크로스":    lambda c, h, l: _cross_sig_us(_sma_us(c, 3),  _sma_us(c, 20)),
    "MACD 크로스":        lambda c, h, l: _cross_sig_us(*_macd_us(c)),
    "볼린저밴드 반전":     lambda c, h, l: _threshold_sig_us(c / _bb_us(c)[1], 0.97, 1.03),
    "Stochastic 크로스":  lambda c, h, l: _cross_sig_us(*_stoch_us(h, l, c)),
    "CCI ±100":           lambda c, h, l: _threshold_sig_us(_cci_us(h, l, c), -100, 100),
    "Williams %R":        lambda c, h, l: _threshold_sig_us(_williams_us(h, l, c), -80, -20),
}


def _backtest_us(close: pd.Series, sig_series: pd.Series,
                 initial: float = 100_000.0) -> float:
    """US 종목 백테스트 — fractional shares, 수수료 0.05%."""
    fee = 0.0005
    cash, holding = float(initial), 0.0
    for date in close.index:
        price = float(close.loc[date])
        sig   = int(sig_series.get(date, 0)) if isinstance(sig_series, pd.Series) else 0
        if sig == 1 and holding == 0 and cash >= price * (1 + fee):
            holding = cash / (price * (1 + fee))
            cash    = 0.0
        elif sig == -1 and holding > 0:
            cash    += holding * price * (1 - fee)
            holding  = 0.0
    if holding > 0:
        cash += holding * float(close.iloc[-1]) * (1 - fee)
    return (cash - initial) / initial * 100


def _find_best_strategy_us(close: pd.Series,
                            high:  pd.Series,
                            low:   pd.Series):
    """Walk-forward OOS(마지막 30%) 검증으로 최적 전략 선정 (KR find_best_strategy 포팅)."""
    if len(close) < 50:
        return None, -9999.0
    split    = max(20, int(len(close) * 0.70))
    oos_close = close.iloc[split:]
    best_name, best_ret = None, -9999.0
    for name, fn in _STRATEGY_REGISTRY_US.items():
        try:
            full_sig = fn(close, high, low)
            oos_sig  = full_sig.iloc[split:]
            if (oos_sig == 1).sum() > 15:   # 과신호 전략 제외
                continue
            ret = _backtest_us(oos_close, oos_sig)
            if ret > best_ret:
                best_ret, best_name = ret, name
        except Exception:
            continue
    # OOS 전부 -30% 이하 → 전체 기간 폴백
    if best_ret < -30:
        for name, fn in _STRATEGY_REGISTRY_US.items():
            try:
                sig = fn(close, high, low)
                ret = _backtest_us(close, sig)
                if ret > best_ret:
                    best_ret, best_name = ret, name
            except Exception:
                continue
    return best_name, best_ret


def _signal_readiness_us(close: pd.Series, high: pd.Series,
                          low: pd.Series, strategy_name: str) -> float:
    """선정된 전략 기준 BUY 신호까지의 거리를 점수화 (KR calc_signal_readiness 포팅)."""
    try:
        if len(close) < 30:
            return 0.0
        if 'RSI' in strategy_name:
            period    = 9  if 'RSI(9)'  in strategy_name else 14
            threshold = 40 if '40/60'   in strategy_name else 30
            rsi_s     = _rsi_us(close, period)
            rsi_cur   = float(rsi_s.iloc[-1])
            rsi_prev  = float(rsi_s.iloc[-2])
            gap = rsi_cur - threshold
            td  = rsi_cur < rsi_prev  # 하락 중 → 신호 임박 가능성↑
            if   gap <= 5:  return 20.0
            elif gap <= 15: return 14.0 if td else 10.0
            elif gap <= 30: return  6.0 if td else  3.0
            else:           return -8.0
        elif '크로스' in strategy_name and strategy_name != 'MACD 크로스':
            use_ema = strategy_name.startswith('EMA')
            parts   = strategy_name.split()[1].split('/')
            fp, sp  = int(parts[0]), int(parts[1])
            fn      = _ema_us if use_ema else _sma_us
            fast = fn(close, fp); slow = fn(close, sp)
            f_cur, s_cur = float(fast.iloc[-1]), float(slow.iloc[-1])
            f_prv, s_prv = float(fast.iloc[-2]), float(slow.iloc[-2])
            if s_cur <= 0: return 0.0
            gap_pct   = (s_cur - f_cur) / s_cur * 100
            shrinking = (s_prv - f_prv) > (s_cur - f_cur)
            if gap_pct > 0:
                if   gap_pct <= 1.0: return 20.0
                elif gap_pct <= 3.0: return 14.0 if shrinking else 10.0
                elif gap_pct <= 6.0: return  6.0 if shrinking else  2.0
                else:                return -5.0
            else:
                return -8.0
        elif strategy_name == 'MACD 크로스':
            ml, sl_   = _macd_us(close)
            h_cur  = float((ml - sl_).iloc[-1])
            h_prev = float((ml - sl_).iloc[-2])
            if h_cur < 0:
                s_ = h_cur > h_prev
                if   h_cur > -50:  return 18.0 if s_ else  8.0
                elif h_cur > -200: return  8.0 if s_ else  2.0
                else:              return -5.0
            else:
                return -8.0
        elif strategy_name == '볼린저밴드 반전':
            _, bb_mid, bb_low = _bb_us(close)
            p    = float(close.iloc[-1])
            mid_ = float(bb_mid.iloc[-1])
            low_ = float(bb_low.iloc[-1])
            bb_1sig = mid_ - (mid_ - low_) * 0.5
            if   p <= low_ * 1.02: return 20.0
            elif p <= bb_1sig:     return 10.0
            elif p <= mid_:        return  3.0
            else:                  return -8.0
    except Exception:
        pass
    return 0.0


# ── DL 예측기 싱글턴 ──────────────────────────────────────────────────
_dl_predictor_us = None
_dl_lock_us      = threading.Lock()


def _scan_universe(universe: dict, n: int, exclude: set, score_fn) -> list[dict]:
    """공통 스캔 엔진 — 유니버스와 스코어 함수를 받아 상위 N개 반환."""
    exclude = (exclude or set()) | CORE_ETF_EXCLUDE

    all_tickers: list[str] = []
    ticker_sector: dict[str, str] = {}
    for sector, tickers in universe.items():
        for t in tickers:
            if t not in exclude:
                all_tickers.append(t)
                ticker_sector[t] = sector

    if not all_tickers:
        return []

    logger.info(f"[US스크리너] {len(all_tickers)}개 종목 다운로드 시작...")
    try:
        raw = yf.download(
            all_tickers, period="6mo", interval="1d",
            progress=False, auto_adjust=True,
        )
    except Exception as e:
        logger.error(f"[US스크리너] 배치 다운로드 실패: {e}")
        return []

    if raw is None or raw.empty:
        return []

    try:
        if isinstance(raw.columns, pd.MultiIndex):
            lv0 = raw.columns.get_level_values(0).unique().tolist()
            if "Close" in lv0:
                closes  = raw["Close"]
                volumes = raw["Volume"]
            else:
                closes  = raw.xs("Close",  axis=1, level=1, drop_level=True)
                volumes = raw.xs("Volume", axis=1, level=1, drop_level=True)
        else:
            closes  = raw[["Close"]].rename(columns={"Close": all_tickers[0]})
            volumes = raw[["Volume"]].rename(columns={"Volume": all_tickers[0]})
    except Exception as e:
        logger.error(f"[US스크리너] 컬럼 파싱 실패: {e}")
        return []

    results: list[dict] = []
    for ticker in all_tickers:
        try:
            if ticker not in closes.columns:
                continue
            close  = closes[ticker].dropna()
            volume = volumes[ticker].dropna() if ticker in volumes.columns else pd.Series(dtype=float)
            if len(close) < 60:
                continue
            price = float(close.iloc[-1])
            if price <= 0:
                continue

            mom_20  = (price / float(close.iloc[-20]) - 1) * 100 if len(close) >= 20 else 0.0
            mom_60  = (price / float(close.iloc[-60]) - 1) * 100 if len(close) >= 60 else 0.0
            sma50   = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else price
            sma200  = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else price
            golden  = price > sma50 > sma200 * 0.98
            rsi     = float(_calc_rsi(close).iloc[-1]) if len(close) >= 20 else 50.0
            if len(volume) >= 60:
                vol_ratio = float(volume.iloc[-5:].mean()) / (float(volume.iloc[-60:-5].mean()) + 1)
            else:
                vol_ratio = 1.0

            score = score_fn(mom_20, mom_60, golden, rsi, vol_ratio, close)
            if score is None:
                continue

            # macd_state 계산 (로깅 및 AI 판단용)
            macd_state = "-"
            try:
                if len(close) >= 30:
                    _ml, _sl = _macd_us(close)
                    _h_now  = float((_ml - _sl).iloc[-1])
                    _h_prev = float((_ml - _sl).iloc[-2])
                    if _h_now < 0 and _h_now > _h_prev:
                        macd_state = "눌림반등(최적)"
                    elif _h_now < 0:
                        macd_state = "눌림목"
                    else:
                        macd_state = "골든크로스"
            except Exception:
                pass

            results.append({
                "ticker":       ticker,
                "name":         ticker,
                "sector":       ticker_sector[ticker],
                "score":        round(score, 1),
                "price":        round(price, 2),
                "momentum_20d": round(mom_20, 2),
                "rsi":          round(rsi, 1),
                "golden":       golden,
                "vol_ratio":    round(vol_ratio, 2),
                "macd_state":   macd_state,
            })
        except Exception as e:
            logger.debug(f"[US스크리너] {ticker} 처리 실패: {e}")
            continue

    results.sort(key=lambda x: x["score"], reverse=True)
    # 이름 조회는 전체 대상 (AI가 전부 볼 수 있도록)
    for r in results:
        r["name"] = _get_name(r["ticker"])
        time.sleep(0.05)

    logger.info(f"[US스크리너] 스캔 완료: {len(results)}개 통과 → 상위 {n}개 선정")
    for r in results[:n]:
        logger.info(
            f"  {r['ticker']:6s}  {r['sector']:14s}  스코어:{r['score']:5.1f}"
            f"  RSI:{r['rsi']:4.1f}  20d:{r['momentum_20d']:+5.1f}%"
            f"  {'🟡골든' if r['golden'] else '  '}"
            f"  거래량:{r['vol_ratio']:.1f}x"
        )
    return results


def _core_score(mom_20, mom_60, golden, rsi, vol_ratio, close=None):
    """
    코어 스코어링 — 안정적 우상향 우선.
    과열(RSI>75) 또는 급락(20d<-8%) 제외.
    장기 모멘텀(60d)에 더 큰 가중치.
    MACD 역추세(눌림목) 보너스 — 진입게이트 아님, 가산점만.
    """
    if rsi > 75 or mom_20 < -8:
        return None
    score = 0.0
    score += min(30.0, max(0.0, mom_60 * 1.5))    # 60일 장기 모멘텀 (주요 지표)
    score += min(20.0, max(0.0, mom_20 * 1.0))    # 20일 단기 모멘텀
    if golden:          score += 25.0              # 골든크로스 (우상향 구조)
    if 40 <= rsi <= 65: score += 15.0              # RSI 적정 구간
    elif 35 <= rsi <= 70: score += 8.0
    score += min(10.0, (vol_ratio - 1) * 5.0)     # 거래량 (보조 지표)
    # ── MACD 역추세 눌림목 보너스 (최대 +8) ──────────────────────────────
    # 데드크로스 구간 = 장기 추세 살아있는데 단기 눌림 = 저점 누적 기회
    # 골든크로스 구간 = 이미 반영된 상승 = 0점
    try:
        if close is not None and len(close) >= 30:
            _ml, _sl = _macd_us(close)
            _h_now  = float((_ml - _sl).iloc[-1])
            _h_prev = float((_ml - _sl).iloc[-2])
            if _h_now < 0 and _h_now > _h_prev:
                score += 8.0   # 음수 구간 반등 중 = 눌림반등(최적)
            elif _h_now < 0:
                score += 4.0   # 단순 눌림목
            # hist > 0 (골든크로스) → 0점 유지
    except Exception:
        pass
    return score


def _satellite_score(mom_20, mom_60, golden, rsi, vol_ratio, close=None):
    """
    위성 스코어링 — 단기 폭발력 우선.
    과매수(RSI>82) 또는 급락(20d<-8%) 제외.
    단기 모멘텀 + 거래량 서지에 더 큰 가중치.
    RSI 30 근접 종목 우선 선정 — 선정 즉시 매수 신호 연결.
    ※ scan_us_satellites()는 자체 루프에서 MACD 계산 — 이 함수는 미사용.
    """
    if rsi > 82 or mom_20 < -8:
        return None
    score = 0.0
    score += min(40.0, max(0.0, mom_20 * 2.0))    # 20일 단기 모멘텀 (주요 지표)
    if golden:          score += 20.0

    # RSI 구간별 보너스 — 30 근처일수록 우선 선정 (매수 신호 임박)
    if rsi <= 32:            score += 22.0   # 30 터치·직후 — 즉시 매수 가능
    elif rsi <= 38:          score += 18.0   # 임박 — 1~2일 내 매수
    elif rsi <= 45:          score += 10.0   # 접근 중
    elif 45 < rsi <= 65:     score += 15.0   # 적정 모멘텀 구간
    elif 65 < rsi <= 70:     score += 8.0    # 허용 구간

    score += min(15.0, (vol_ratio - 1) * 10.0)    # 거래량 서지 (폭발력)
    if mom_60 > 0:      score += min(10.0, mom_60 * 0.5)
    return score


def scan_us_cores(n: int = 3, exclude: set = None) -> list[dict]:
    """
    미국 코어 종목 스캔 — 우량 대형주 유니버스에서 장기 우상향 종목 선정.
    장기 모멘텀(60d) + 골든크로스 위주 스코어링.
    """
    return _scan_universe(CORE_UNIVERSE, n, exclude or set(), _core_score)


def scan_us_satellites(n: int = 5, exclude: set = None, toss_api=None) -> list[dict]:
    """
    US 위성 종목 멀티팩터 스크리닝 (KR select_satellites와 동등 수준).

    ① S&P 500 동적 유니버스 (Wikipedia, 일 1회) + 고성장 목록
    ② 섹터 ETF 모멘텀 보너스 (GICS 기반, 4시간 캐시)
    ③ 모멘텀(20d/60d) + 골든크로스(SMA50>200) + 거래량 서지 + RSI
    ④ 52주 위치 점수 (스윗스팟 40~80% → +5점)
    ⑤ 백테스트 최적 전략 (walk-forward OOS 검증 → 수익률 반영)
    ⑥ 신호 준비도 (BUY 신호까지 거리)
    ⑦ PyTorch LSTM 딥러닝 상승확률
    ⑧ 과열 패널티 (20d +15% 초과 + 거래량 서지 없으면 패널티)
    ⑨ 토스 랭킹 보너스 (toss_api 제공 시 자동 통합)

    Returns list of dicts (score 내림차순 전체):
      ticker, name, sector, score, price, momentum_20d, momentum_60d,
      rsi, golden, vol_ratio, sector_bonus, best_strategy, backtest_return,
      signal_readiness, ai_up_prob, toss_bonus
    """
    global _dl_predictor_us

    exclude_set = (exclude or set()) | CORE_ETF_EXCLUDE

    # ① 유니버스 로드
    universe = _get_dynamic_satellite_universe()
    all_tickers: list[str] = []
    ticker_sector: dict[str, str] = {}
    for sector, tickers in universe.items():
        for t in tickers:
            if t not in exclude_set:
                all_tickers.append(t)
                ticker_sector[t] = sector
    if not all_tickers:
        return []

    # ② 섹터 모멘텀 사전 로드 (4시간 캐시)
    sector_momentum = _get_us_sector_momentum()
    sorted_sectors  = sorted(sector_momentum.items(), key=lambda x: x[1], reverse=True)
    sector_rank     = {sec: rank for rank, (sec, _) in enumerate(sorted_sectors)}
    if sector_momentum:
        top4 = [f"{s}({v:+.1f}%)" for s, v in sorted_sectors[:4]]
        logger.info(f"[US스크리너] 강세섹터 TOP4: {', '.join(top4)}")

    # ③ 토스 랭킹 보너스 수집 (선택)
    toss_bonuses: dict[str, float] = {}
    if toss_api is not None:
        try:
            toss_results = scan_us_satellites_toss(toss_api, n=50, exclude=exclude_set)
            for item in toss_results:
                toss_bonuses[item['ticker']] = item.get('score', 0.0)
            logger.info(f"[US스크리너] 토스 랭킹 보너스: {len(toss_bonuses)}개 종목")
        except Exception as e:
            logger.warning(f"[US스크리너] KIS 랭킹 수집 실패: {e}")

    # ④ DL 예측기 싱글턴 로드
    try:
        from dl_model import DeepLearningPredictor
    except ImportError:
        DeepLearningPredictor = None
    if DeepLearningPredictor is not None and _dl_predictor_us is None:
        with _dl_lock_us:
            if _dl_predictor_us is None:
                try:
                    _dl_predictor_us = DeepLearningPredictor()
                except Exception as e:
                    logger.warning(f"[US스크리너] DL 예측기 초기화 실패: {e}")
    dl_predictor = _dl_predictor_us

    # ⑤ yfinance 배치 다운로드 (1년치 — 백테스트·52주 위치용)
    logger.info(f"[US스크리너] 멀티팩터 스캔 시작: {len(all_tickers)}개 종목 다운로드...")
    try:
        raw = yf.download(all_tickers, period="1y", interval="1d",
                          progress=False, auto_adjust=True)
    except Exception as e:
        logger.error(f"[US스크리너] 배치 다운로드 실패: {e}")
        return []
    if raw is None or raw.empty:
        return []

    try:
        if isinstance(raw.columns, pd.MultiIndex):
            lv0     = raw.columns.get_level_values(0).unique().tolist()
            closes  = raw["Close"]  if "Close"  in lv0 else raw.xs("Close",  axis=1, level=1, drop_level=True)
            volumes = raw["Volume"] if "Volume" in lv0 else raw.xs("Volume", axis=1, level=1, drop_level=True)
            highs   = raw["High"]   if "High"   in lv0 else raw.xs("High",   axis=1, level=1, drop_level=True)
            lows    = raw["Low"]    if "Low"    in lv0 else raw.xs("Low",    axis=1, level=1, drop_level=True)
        else:
            closes  = raw[["Close"]].rename(columns={"Close":  all_tickers[0]})
            volumes = raw[["Volume"]].rename(columns={"Volume": all_tickers[0]})
            highs   = raw[["High"]].rename(columns={"High":    all_tickers[0]})
            lows    = raw[["Low"]].rename(columns={"Low":      all_tickers[0]})
    except Exception as e:
        logger.error(f"[US스크리너] 컬럼 파싱 실패: {e}")
        return []

    # ⑥ 종목별 멀티팩터 점수 계산
    results: list[dict] = []
    for ticker in all_tickers:
        try:
            if ticker not in closes.columns:
                continue
            close  = closes[ticker].dropna()
            volume = volumes[ticker].dropna() if ticker in volumes.columns else pd.Series(dtype=float)
            high   = highs[ticker].dropna()   if ticker in highs.columns  else close
            low    = lows[ticker].dropna()    if ticker in lows.columns   else close

            if len(close) < 60:
                continue
            price = float(close.iloc[-1])
            if price <= 0:
                continue

            # ── 기본 지표 ──────────────────────────────────────────────
            mom_20 = (price / float(close.iloc[-20]) - 1) * 100 if len(close) >= 20 else 0.0
            mom_60 = (price / float(close.iloc[-60]) - 1) * 100 if len(close) >= 60 else 0.0
            sma50  = float(close.rolling(50).mean().iloc[-1])  if len(close) >= 50  else price
            sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else price
            golden = price > sma50 > sma200 * 0.98
            rsi    = float(_calc_rsi(close).iloc[-1]) if len(close) >= 20 else 50.0
            if len(volume) >= 60:
                vol_ratio = float(volume.iloc[-5:].mean()) / (float(volume.iloc[-60:-5].mean()) + 1)
            else:
                vol_ratio = 1.0

            # 과매수(RSI>82) / 급락(20d<-8%) 제외
            if rsi > 82 or mom_20 < -8:
                continue

            # ── 기본 위성 점수 ─────────────────────────────────────────
            score = 0.0
            score += min(40.0, max(0.0, mom_20 * 2.0))
            if golden: score += 20.0
            if   rsi <= 32:          score += 22.0
            elif rsi <= 38:          score += 18.0
            elif rsi <= 45:          score += 10.0
            elif 45 < rsi <= 65:     score += 15.0
            elif 65 < rsi <= 70:     score +=  8.0
            score += min(15.0, (vol_ratio - 1) * 10.0)
            if mom_60 > 0: score += min(10.0, mom_60 * 0.5)

            # ── ④ 52주 위치 점수 ───────────────────────────────────────
            pos_52w_score = 0.0
            if len(close) >= 60:
                n52  = min(252, len(close))
                h_s  = high.reindex(close.index).dropna()
                l_s  = low.reindex(close.index).dropna()
                h52  = float(h_s.tail(n52).max()) if len(h_s) >= n52 else float(close.tail(n52).max())
                l52  = float(l_s.tail(n52).min()) if len(l_s) >= n52 else float(close.tail(n52).min())
                pos_52w = (price - l52) / (h52 - l52 + 1e-9) * 100
                if   40 <= pos_52w <= 80: pos_52w_score =  5.0  # 스윗스팟
                elif 15 <= pos_52w <  40: pos_52w_score =  3.0  # 저점 탈출 초기
                elif pos_52w > 92:        pos_52w_score = -2.0  # 극단 과열 소패널티
            score += pos_52w_score

            # ── ⑤ 섹터 모멘텀 보너스 (가산점, 필수 아님) ────────────────
            sec     = ticker_sector.get(ticker, "")
            sec_ret = sector_momentum.get(sec, 0.0)
            rank    = sector_rank.get(sec, len(sector_rank))
            _base   = max(22 - rank * 4, 10)
            if   sec_ret > 0:    _quality = 1.0
            elif sec_ret > -5:   _quality = 0.6
            else:                _quality = 0.3
            sector_bonus = int(_base * _quality)
            score += sector_bonus

            # ── ⑥ 과열 패널티 ────────────────────────────────────────
            overheated = 0.0
            if mom_20 > 15 and vol_ratio < 2.0:
                base_p    = (mom_20 - 15) * 1.5
                discount  = 0.5 if sector_bonus >= 10 else 1.0
                overheated = base_p * discount
            if mom_20 > 30:
                overheated += (mom_20 - 30) * 0.8
            score -= overheated

            # ── ⑦ 백테스트 최적 전략 ─────────────────────────────────
            best_strat, best_ret_val = None, 0.0
            if len(close) >= 50:
                try:
                    h_aligned = high.reindex(close.index).fillna(method='ffill')
                    l_aligned = low.reindex(close.index).fillna(method='ffill')
                    best_strat, best_ret_val = _find_best_strategy_us(close, h_aligned, l_aligned)
                    score += min(20.0, max(-10.0, best_ret_val * 0.3))
                except Exception:
                    pass

            # ── ⑧ 신호 준비도 ─────────────────────────────────────────
            sig_ready = 0.0
            if best_strat:
                try:
                    h_aligned = high.reindex(close.index).fillna(method='ffill')
                    l_aligned = low.reindex(close.index).fillna(method='ffill')
                    sig_ready = _signal_readiness_us(close, h_aligned, l_aligned, best_strat)
                    score += sig_ready
                except Exception:
                    pass

            # ── ⑨ 딥러닝 상승확률 ────────────────────────────────────
            ai_up_prob = 50.0
            if dl_predictor is not None:
                try:
                    dl_df = pd.DataFrame({
                        'close':  close.values,
                        'high':   high.reindex(close.index).values,
                        'low':    low.reindex(close.index).values,
                        'volume': volume.reindex(close.index).fillna(0).values,
                    })
                    ai_up_prob = dl_predictor.predict_up_probability(dl_df)
                except Exception:
                    ai_up_prob = 50.0
            score += (ai_up_prob - 50.0) * 0.2

            # ── ⑩ 토스 랭킹 보너스 ───────────────────────────────────
            toss_bonus = min(30.0, toss_bonuses.get(ticker, 0.0) * 0.15)
            score += toss_bonus

            # ── ⑪ MACD 역추세 눌림목 보너스 ──────────────────────────
            # 데드크로스 구간 = 단기 눌림 = 저점 진입 기회 → 가산
            # 골든크로스 구간 = 이미 올라간 뒤 = 0점
            macd_pullback_bonus = 0.0
            macd_state = "-"
            try:
                if len(close) >= 30:
                    _ml, _sl = _macd_us(close)
                    _h_now  = float((_ml - _sl).iloc[-1])
                    _h_prev = float((_ml - _sl).iloc[-2])
                    if _h_now < 0 and _h_now > _h_prev:
                        macd_pullback_bonus = 8.0
                        macd_state = "눌림반등(최적)"
                    elif _h_now < 0:
                        macd_pullback_bonus = 4.0
                        macd_state = "눌림목"
                    else:
                        macd_state = "골든크로스"
            except Exception:
                pass
            score += macd_pullback_bonus

            results.append({
                "ticker":               ticker,
                "name":                 ticker,
                "sector":               sec,
                "score":                round(score, 1),
                "price":                round(price, 2),
                "momentum_20d":         round(mom_20, 2),
                "momentum_60d":         round(mom_60, 2),
                "rsi":                  round(rsi, 1),
                "golden":               golden,
                "vol_ratio":            round(vol_ratio, 2),
                "sector_bonus":         sector_bonus,
                "best_strategy":        best_strat or "",
                "backtest_return":      round(best_ret_val, 1),
                "signal_readiness":     round(sig_ready, 1),
                "ai_up_prob":           round(ai_up_prob, 1),
                "toss_bonus":           round(toss_bonus, 1),
                "macd_state":           macd_state,
                "macd_pullback_bonus":  round(macd_pullback_bonus, 1),
            })
        except Exception as e:
            logger.debug(f"[US스크리너] {ticker} 처리 실패: {e}")
            continue

    results.sort(key=lambda x: x["score"], reverse=True)

    # 종목명 조회 (전체 — AI가 모두 볼 수 있도록)
    for r in results:
        r["name"] = _get_name(r["ticker"])
        time.sleep(0.05)

    logger.info(f"[US스크리너] 멀티팩터 스캔 완료: {len(results)}개 통과")
    for r in results[:n]:
        logger.info(
            f"  {r['ticker']:6s}  {r['sector']:22s}  스코어:{r['score']:6.1f}"
            f"  RSI:{r['rsi']:4.1f}  20d:{r['momentum_20d']:+5.1f}%"
            f"  {'🟡골든' if r['golden'] else '      '}"
            f"  {r['best_strategy']:<14}  DL:{r['ai_up_prob']:.0f}%"
            f"  토스:{r['toss_bonus']:.0f}pt"
        )
    return results


# ── 토스 기반 스크리너 ─────────────────────────────────────────────────

def scan_us_satellites_toss(toss_api, n: int = 5, exclude: set = None) -> list[dict]:
    """
    토스증권 해외주식 랭킹 API 기반 미국 위성 스크리너.

    yfinance 대신 토스 실시간 데이터를 사용:
    - 거래량 순위 (HHDFS76310010)
    - 거래증가율 순위 (HHDFS76330000)
    - 52주 신고가 돌파 (HHDFS76300000)
    - 상승율 순위 (HHDFS76290000)

    Returns list of dicts (scan_us_satellites 호환 포맷):
      ticker, name, sector, score, price, rate
    """
    exclude = (exclude or set()) | CORE_ETF_EXCLUDE
    # 최소 주가 필터 (페니주 제외)
    MIN_PRICE = 5.0

    exchanges = ["NAS", "NYS"]   # NASDAQ + NYSE

    # ── 4가지 랭킹 수집 ─────────────────────────────────────────────
    all_items: dict[str, dict] = {}  # ticker → aggregated dict

    def _add(items: list[dict], source: str, score_bonus: float):
        for item in items:
            ticker = item.get("ticker", "").strip()
            price  = float(item.get("price", 0))
            if not ticker or ticker in exclude or price < MIN_PRICE:
                continue
            if ticker not in all_items:
                all_items[ticker] = {
                    "ticker":  ticker,
                    "name":    item.get("name", ticker),
                    "price":   price,
                    "rate":    float(item.get("rate", 0)),
                    "score":   0.0,
                    "sources": [],
                    "sector":  "토스랭킹",
                }
            all_items[ticker]["score"]   += score_bonus
            all_items[ticker]["sources"].append(source)
            # 최신 이름·가격 갱신
            if item.get("name"):
                all_items[ticker]["name"]  = item["name"]
            if price > 0:
                all_items[ticker]["price"] = price

    try:
        for excd in exchanges:
            # ① 거래증가율 (서프라이즈 모멘텀) — 가장 중요
            _add(toss_api.scan_trade_growth(exchange=excd, n=50),
                 "trade_growth", 30.0)
            time.sleep(0.1)
            # ② 52주 신고가 (강한 추세 확인)
            _add(toss_api.scan_new_highs(exchange=excd, n=50),
                 "new_high", 25.0)
            time.sleep(0.1)
            # ③ 거래량 순위 (유동성 확인)
            _add(toss_api.scan_top_volume(exchange=excd, n=50, min_price=MIN_PRICE),
                 "top_volume", 15.0)
            time.sleep(0.1)
            # ④ 상승율 순위 (단기 가격 모멘텀)
            _add(toss_api.scan_top_gainers(exchange=excd, n=50),
                 "top_gainer", 20.0)
            time.sleep(0.1)
    except Exception as e:
        logger.warning(f"[토스스크리너] 랭킹 수집 중 오류: {e}")

    if not all_items:
        logger.warning("[KIS스크리너] 결과 없음")
        return []

    # ── 복수 소스 교차 보너스 ────────────────────────────────────────
    for item in all_items.values():
        unique_sources = len(set(item["sources"]))
        if unique_sources >= 3:
            item["score"] += 20.0   # 3개 이상 랭킹 동시 진입
        elif unique_sources >= 2:
            item["score"] += 10.0   # 2개 진입

        # 등락율 보너스 (과매수 방지: 15% 초과 상승은 감점)
        rate = item.get("rate", 0)
        if 2.0 <= rate <= 15.0:
            item["score"] += min(10.0, rate * 0.8)
        elif rate > 15.0:
            item["score"] -= 5.0   # 당일 급등 감점

    # 스코어 내림차순 정렬
    results = sorted(all_items.values(), key=lambda x: x["score"], reverse=True)

    top = results[:n * 2]
    logger.info(
        f"[KIS스크리너] 수집 {len(all_items)}개 → 상위 {len(top)}개"
    )
    for r in top[:n]:
        logger.info(
            f"  {r['ticker']:6s}  스코어:{r['score']:5.1f}"
            f"  등락:{r['rate']:+5.1f}%  소스:{set(r['sources'])}"
        )

    return top[:n]


# ── 실시간 가격 조회 ──────────────────────────────────────────────────

def get_us_prices_batch(tickers, toss_api=None) -> dict[str, float]:
    """
    복수 종목 USD 가격 배치 조회.
    toss_api 제공 시 토스 복수시세조회(HHDFS76220000) 우선 사용.
    Returns {ticker: price_usd}
    """
    tickers = list(tickers)
    if not tickers:
        return {}

    prices: dict[str, float] = {}

    # ── 토스 우선 조회 ────────────────────────────────────────────────
    if toss_api is not None:
        try:
            toss_prices = toss_api.get_prices_batch_multi(tickers)
            prices.update(toss_prices)
        except Exception as e:
            logger.debug(f"[US스크리너] 토스 배치 가격 조회 실패: {e}")

    missing = [t for t in tickers if t not in prices]
    if not missing:
        return prices

    # ── yfinance 폴백 (missing 종목만) ──────────────────────────────
    try:
        raw = yf.download(
            missing,
            period="2d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
        if raw is None or raw.empty:
            raise ValueError("empty")

        if isinstance(raw.columns, pd.MultiIndex):
            lv0 = raw.columns.get_level_values(0).unique().tolist()
            close_df = raw["Close"] if "Close" in lv0 else raw.xs("Close", axis=1, level=1, drop_level=True)
        else:
            # 단일 종목
            close_df = raw[["Close"]].rename(columns={"Close": missing[0]})

        for t in missing:
            if t in close_df.columns:
                s = close_df[t].dropna()
                if not s.empty:
                    prices[t] = float(s.iloc[-1])

    except Exception as e:
        logger.debug(f"[US스크리너] 배치 가격 조회 실패 ({e}), 개별 조회로 폴백")
        for t in missing:
            try:
                hist = yf.Ticker(t).history(period="2d")
                if not hist.empty:
                    prices[t] = float(hist["Close"].dropna().iloc[-1])
                time.sleep(0.1)
            except Exception:
                pass

    return prices


def get_futures_snapshot() -> dict:
    """
    야간선물 스냅샷 — 미국장 선행지표.

    - NQ=F  : NASDAQ 100 선물 (yfinance 직접 지원)
    - ES=F  : S&P 500 선물
    - EWY   : iShares MSCI 한국 ETF (코스피 야간 프록시)

    Returns:
        {
          "nq":  {"label", "price", "change_1h", "change_5d", "trend"},
          "es":  { ... },
          "ewy": { ... },
          "summary": "나스닥100 선물: ▲0.32% (5일:+1.8%) | ...",
        }
    """
    symbols = {
        "nq":  ("NQ=F",  "나스닥100 선물"),
        "es":  ("ES=F",  "S&P500 선물"),
        "ewy": ("EWY",   "한국(EWY) 코스피 프록시"),
    }
    result: dict = {}
    for key, (sym, label) in symbols.items():
        entry = {"label": label, "price": 0.0, "change_1h": 0.0, "change_5d": 0.0, "trend": "NEUTRAL"}
        try:
            # 5분봉 2일치 — 최신 가격 정확히 반영 + 전날 종가 기준 등락률
            df = yf.download(sym, period="5d", interval="5m",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                result[key] = entry
                continue

            cur = float(df["Close"].iloc[-1])

            # 전날 종가 기준 당일 등락률 계산 (시장 표준)
            daily = df["Close"].resample("D").last().dropna()
            if len(daily) >= 2:
                prev_close = float(daily.iloc[-2])   # 전날 종가
                chg_1h = (cur / prev_close - 1) * 100
            else:
                prev_close = float(df["Close"].iloc[-2])
                chg_1h = (cur / prev_close - 1) * 100

            # 5일 추세
            chg_5d = (cur / float(daily.iloc[0]) - 1) * 100 if len(daily) >= 2 else 0.0

            if chg_5d > 1.0:
                trend = "UPTREND"
            elif chg_5d < -1.0:
                trend = "DOWNTREND"
            else:
                trend = "NEUTRAL"

            entry = {
                "label":      label,
                "price":      round(cur, 2),
                "change_1h":  round(chg_1h, 3),
                "change_5d":  round(chg_5d, 2),
                "trend":      trend,
            }
        except Exception as e:
            logger.debug(f"[선물스냅샷] {sym} 조회 실패: {e}")
        result[key] = entry

    # 텍스트 요약
    parts = []
    for key, data in result.items():
        if data.get("price", 0) > 0:
            arrow = "▲" if data["change_1h"] > 0 else ("▼" if data["change_1h"] < 0 else "→")
            parts.append(
                f"{data['label']}: {arrow}{abs(data['change_1h']):.2f}%"
                f" (5일:{data['change_5d']:+.1f}%)"
            )
    result["summary"] = " | ".join(parts)
    logger.info(f"[선물스냅샷] {result.get('summary', 'N/A')}")
    return result


def get_sector_trends() -> dict:
    """
    NASDAQ 섹터별 추세 분석 (US_UNIVERSE 기반).

    Returns:
        {
          "sectors": [
              {"name": str, "trend": "UPTREND"|"DOWNTREND"|"NEUTRAL",
               "momentum_5d": float, "momentum_20d": float,
               "leaders": [ticker, ...]},
              ...
          ],
          "hot_sectors":  [sector_name, ...],   # 상승 섹터
          "cold_sectors": [sector_name, ...],   # 하락 섹터
        }
    """
    all_tickers  = [t for tickers in US_UNIVERSE.values() for t in tickers]
    ticker_sector = {t: s for s, tickers in US_UNIVERSE.items() for t in tickers}

    try:
        raw = yf.download(all_tickers, period="30d", interval="1d",
                          progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return {"sectors": [], "hot_sectors": [], "cold_sectors": []}

        if isinstance(raw.columns, pd.MultiIndex):
            lv0 = raw.columns.get_level_values(0).unique().tolist()
            closes = raw["Close"] if "Close" in lv0 else raw.xs("Close", axis=1, level=1, drop_level=True)
        else:
            closes = raw[["Close"]].rename(columns={"Close": all_tickers[0]})

    except Exception as e:
        logger.error(f"[섹터추세] 다운로드 실패: {e}")
        return {"sectors": [], "hot_sectors": [], "cold_sectors": []}

    # 종목별 모멘텀
    mom5:  dict[str, float] = {}
    mom20: dict[str, float] = {}
    for ticker in all_tickers:
        try:
            if ticker not in closes.columns:
                continue
            close = closes[ticker].dropna()
            if len(close) < 6:
                continue
            cur = float(close.iloc[-1])
            mom5[ticker]  = (cur / float(close.iloc[-5])  - 1) * 100 if len(close) >= 5  else 0.0
            mom20[ticker] = (cur / float(close.iloc[-20]) - 1) * 100 if len(close) >= 20 else 0.0
        except Exception:
            pass

    # 섹터별 집계
    sectors_out: list[dict] = []
    hot_sectors:  list[str] = []
    cold_sectors: list[str] = []

    for sector_name, tickers in US_UNIVERSE.items():
        m5s  = [mom5.get(t, 0.0)  for t in tickers if t in mom5]
        m20s = [mom20.get(t, 0.0) for t in tickers if t in mom20]
        if not m5s:
            continue
        avg5  = sum(m5s)  / len(m5s)
        avg20 = sum(m20s) / len(m20s) if m20s else 0.0

        if avg5 >= 2.0 and avg20 >= 0:
            trend = "UPTREND"
            hot_sectors.append(sector_name)
        elif avg5 <= -2.0:
            trend = "DOWNTREND"
            cold_sectors.append(sector_name)
        else:
            trend = "NEUTRAL"

        # 섹터 내 리더 종목 (5일 모멘텀 상위 2개)
        leaders = sorted(
            [t for t in tickers if t in mom5],
            key=lambda t: mom5[t], reverse=True
        )[:2]

        sectors_out.append({
            "name":         sector_name,
            "trend":        trend,
            "momentum_5d":  round(avg5, 2),
            "momentum_20d": round(avg20, 2),
            "leaders":      leaders,
        })

    sectors_out.sort(key=lambda x: x["momentum_5d"], reverse=True)
    logger.info(
        f"[섹터추세] 핫:{hot_sectors}  콜드:{cold_sectors}"
    )
    return {
        "sectors":     sectors_out,
        "hot_sectors": hot_sectors,
        "cold_sectors": cold_sectors,
    }


def generate_us_daily_report(claude_client=None, positions: dict = None,
                              satellite_info: list = None,
                              news_context: str = None,
                              kr_context: dict = None,
                              market_regime: str = "NEUTRAL",
                              futures_snapshot: dict = None,
                              sector_trends: list = None) -> dict:
    """
    미국장 일일 리포트 생성.
    - 주요 지수(SPY·QQQ·DIA) + 섹터 ETF 흐름 수집 (yfinance)
    - 보유 위성 포지션 손익 요약 포함
    - news_context: yfinance 뉴스 헤드라인 텍스트 (선택)
    - kr_context: KR 봇 피어 컨텍스트 dict (선택)
    - market_regime: "BULL" | "BEAR" | "NEUTRAL"
    - futures_snapshot: get_futures_snapshot() 결과 dict (선택)
    - sector_trends: get_sector_trends()["sectors"] 리스트 (선택)
    - claude_client 제공 시 Claude AI 분석, 없으면 룰 기반 리포트
    """
    from datetime import datetime, timezone, timedelta
    _ET = timezone(timedelta(hours=-4))
    today_str  = datetime.now(_ET).strftime('%Y년 %m월 %d일 (%a)')
    today_key  = datetime.now(_ET).strftime('%Y-%m-%d')

    lines: list[str] = [f"날짜: {today_str} (ET 기준)"]

    # ── 0. 시장 국면 ──────────────────────────────────────────────────
    regime_label = {"BULL": "🟢 강세장", "BEAR": "🔴 약세장", "NEUTRAL": "🟡 중립"}.get(market_regime, market_regime)
    lines.append(f"\n[현재 시장 국면] {regime_label} ({market_regime})")

    # ── 0-A. 야간선물 스냅샷 ─────────────────────────────────────────
    if futures_snapshot:
        lines.append("\n[야간선물 / 선행지표]")
        for key in ("nq", "es", "ewy"):
            data = futures_snapshot.get(key, {})
            if data.get("price", 0) > 0:
                arrow  = "▲" if data["change_1h"] > 0 else ("▼" if data["change_1h"] < 0 else "→")
                trend_str = {"UPTREND": "상승추세", "DOWNTREND": "하락추세", "NEUTRAL": "중립"}.get(
                    data.get("trend", "NEUTRAL"), data.get("trend", ""))
                lines.append(
                    f"- {data['label']}: ${data['price']:,.2f}  "
                    f"1h {arrow}{abs(data['change_1h']):.2f}%  "
                    f"5일 {data['change_5d']:+.1f}%  [{trend_str}]"
                )

    # ── 0-B. NASDAQ 섹터 추세 ────────────────────────────────────────
    if sector_trends:
        lines.append("\n[NASDAQ 섹터 추세 (5일/20일 모멘텀)]")
        trend_icons = {"UPTREND": "🟢↑", "DOWNTREND": "🔴↓", "NEUTRAL": "🟡→"}
        for s in sector_trends:
            icon    = trend_icons.get(s["trend"], "")
            leaders = "/".join(s["leaders"]) if s["leaders"] else ""
            lines.append(
                f"- {icon} {s['name']:12s}  "
                f"5일 {s['momentum_5d']:+.1f}%  20일 {s['momentum_20d']:+.1f}%"
                + (f"  주도: {leaders}" if leaders else "")
            )

    # ── 1. 주요 지수 ──────────────────────────────────────────────────
    indices = {
        "S&P 500 (SPY)":   "SPY",
        "NASDAQ 100 (QQQ)":"QQQ",
        "Dow Jones (DIA)": "DIA",
        "Russell 2000 (IWM)": "IWM",
        "VIX (공포지수)":  "^VIX",
    }
    lines.append("\n[주요 지수 데이터]")
    for name, sym in indices.items():
        try:
            df = yf.download(sym, period="35d", interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) < 2:
                continue
            close   = df["Close"]
            cur     = float(close.iloc[-1])
            prev    = float(close.iloc[-2])
            chg_pct = (cur / prev - 1) * 100
            sma5    = float(close.rolling(5).mean().iloc[-1]) if len(close) >= 5 else cur
            sma20   = float(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else cur
            # RSI
            delta = close.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss.replace(0, float('nan'))
            rsi   = float((100 - 100 / (1 + rs)).iloc[-1]) if not rs.empty else 50.0
            lines.append(
                f"- {name}: ${cur:.2f} ({chg_pct:+.2f}%)  "
                f"5일선 ${sma5:.2f} / 20일선 ${sma20:.2f}  RSI {rsi:.1f}"
            )
        except Exception:
            pass

    # ── 2. 섹터 ETF 흐름 ──────────────────────────────────────────────
    sector_etfs = {
        "기술(XLK)": "XLK", "금융(XLF)": "XLF", "에너지(XLE)": "XLE",
        "헬스케어(XLV)": "XLV", "소비재(XLY)": "XLY", "산업재(XLI)": "XLI",
    }
    lines.append("\n[섹터 ETF 5일 수익률]")
    sector_perf: list[tuple[str, float]] = []
    for name, sym in sector_etfs.items():
        try:
            df = yf.download(sym, period="10d", interval="1d",
                             progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"])
            if len(df) >= 6:
                ret = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-6]) - 1) * 100
                sector_perf.append((name, ret))
        except Exception:
            pass
    sector_perf.sort(key=lambda x: x[1], reverse=True)
    for name, ret in sector_perf:
        lines.append(f"- {name}: {ret:+.2f}%")

    # ── 3. 보유 위성 포지션 손익 ──────────────────────────────────────
    if positions:
        holding = {t: p for t, p in positions.items() if p.shares > 0}
        if holding:
            lines.append("\n[현재 보유 위성 포지션]")
            for ticker, pos in holding.items():
                try:
                    df = yf.download(ticker, period="2d", interval="1d",
                                     progress=False, auto_adjust=True)
                    if isinstance(df.columns, pd.MultiIndex):
                        df.columns = df.columns.get_level_values(0)
                    cur_p = float(df["Close"].dropna().iloc[-1]) if not df.empty else pos.avg_price_usd
                except Exception:
                    cur_p = pos.avg_price_usd
                pnl_pct = (cur_p / pos.avg_price_usd - 1) * 100 if pos.avg_price_usd > 0 else 0.0
                lines.append(
                    f"- {pos.name}({ticker}): {int(pos.shares)}주  "
                    f"평균단가 ${pos.avg_price_usd:.2f} → 현재 ${cur_p:.2f}  ({pnl_pct:+.1f}%)"
                )

    # ── 4. 스캔 후보 ──────────────────────────────────────────────────
    if satellite_info:
        lines.append("\n[오늘의 위성 후보 종목]")
        for info in satellite_info:
            lines.append(
                f"- {info['name']}({info['ticker']})  "
                f"섹터: {info['sector']}  점수: {info['score']:.0f}"
            )

    # ── 5. 미국 뉴스 헤드라인 ─────────────────────────────────────────
    if news_context:
        lines.append("\n[오늘의 주요 뉴스 헤드라인]")
        lines.append(news_context)

    # ── 6. KR 봇 교차 시장 컨텍스트 ─────────────────────────────────
    if kr_context:
        lines.append("\n[한국 시장 현황 (KR 봇)]")
        kr_regime = kr_context.get("market_regime", "N/A")
        kr_sectors = kr_context.get("hot_sectors", [])
        kr_running = kr_context.get("is_running", False)
        lines.append(f"- 한국장 국면: {kr_regime}")
        if kr_sectors:
            lines.append(f"- 주도 섹터: {', '.join(kr_sectors)}")
        lines.append(f"- KR 봇 상태: {'실행중' if kr_running else '정지'}")

    market_data_text = "\n".join(lines)

    # ── 5. AI 분석 or 룰 기반 리포트 ─────────────────────────────────
    if claude_client:
        kr_note = ""
        if kr_context:
            kr_note = (
                f"\n참고로 현재 한국 시장은 '{kr_context.get('market_regime', 'N/A')}' 국면이며, "
                f"주도 섹터는 {kr_context.get('hot_sectors', [])}입니다. "
                f"글로벌 자금 흐름 관점에서 한국-미국 교차 시장 인사이트도 간략히 포함해주세요."
            )
        prompt = (
            f"당신은 미국 주식 시장 전문 애널리스트입니다.\n"
            f"아래 데이터를 바탕으로 한국어로 오늘의 미국 시장 분석 리포트를 작성해주세요.\n"
            f"형식: 마크다운 (제목/소제목/불릿 포인트 사용)\n"
            f"포함 내용: ① 전체 시장 방향성 (현재 국면: {market_regime}) "
            f"② 야간선물이 시사하는 다음 장 방향 ③ 상승/하락 섹터 분석 "
            f"④ 보유 포지션 의견 ⑤ 오늘의 전략 제안 ⑥ 주요 뉴스 시사점{kr_note}\n\n"
            f"{market_data_text}"
        )
        try:
            report_text = claude_client.analyze_market(prompt)
        except Exception:
            report_text = market_data_text
    else:
        report_text = (
            f"### 📊 Lassi US Bot 시장 분석 리포트\n\n"
            f"```\n{market_data_text}\n```\n\n"
            f"> AI 분석 비활성화 상태입니다. Claude API 키를 등록하면 정교한 분석을 받을 수 있습니다."
        )

    return {"date": today_key, "report_markdown": report_text}


def get_us_ohlcv(ticker: str, days: int = 200) -> pd.DataFrame:
    """
    단일 종목 OHLCV (일봉, USD).
    columns: Open, High, Low, Close, Volume
    """
    try:
        period = f"{max(days // 20 + 1, 12)}mo"
        df = yf.download(ticker, period=period, interval="1d",
                         progress=False, auto_adjust=True)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"]).tail(days)
        df.columns = [c.capitalize() for c in df.columns]
        return df
    except Exception as e:
        logger.debug(f"[US스크리너] {ticker} OHLCV 조회 실패: {e}")
        return pd.DataFrame()
