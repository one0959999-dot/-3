"""
stock_screener.py
멀티팩터 위성 종목 자동 스크리너
─────────────────────────────────
① 섹터/테마 강세 탐지  : KRX 섹터지수 → 현재 어떤 분야가 강한지 파악
② 거래량 급등 감지      : 최근 5일 거래량 vs 60일 평균 → 테마 수급 포착
③ 모멘텀 필터          : 최근 20일 수익률 플러스 종목만
④ 전략 백테스트        : 13가지 전략 중 최고 수익 전략 & 수익률 계산
⑤ 종합 점수로 랭킹      : 섹터보너스 + 거래량 점수 + 백테스트 수익률
"""

import time
import threading
import warnings
import logging

logger = logging.getLogger('lassi_bot')  # [BUG-FIX] NameError 방지

# W-05: 싱글턴 레이스컨디션 방지용 전역 락
_dl_predictor_lock = threading.Lock()
_dl_predictor_instance = None

from pykrx import stock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

warnings.filterwarnings('ignore')

EXCLUDE_TICKERS = {"003850"}
NUM_SATELLITES  = 5
BACKTEST_DAYS   = 130

def _last_biz_day(days_back=0):
    """최근 영업일 날짜 문자열 반환"""
    d = datetime.today() - timedelta(days=days_back)
    for _ in range(10):
        s = d.strftime("%Y%m%d")
        try:
            df = stock.get_market_ohlcv_by_date(s, s, "005930")
            if not df.empty:
                return s
        except Exception:
            pass
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")

_ohlcv_cache = {}  # {(ticker, days): (date_str, DataFrame)}
_cache_lock = threading.Lock()

def fetch_ohlcv(ticker, days=200, kis=None):
    today_str = datetime.today().strftime('%Y%m%d')
    key = (ticker, days)

    # 오늘 날짜의 캐시가 있으면 그대로 반환 (Thread-Safe)
    with _cache_lock:
        if key in _ohlcv_cache and _ohlcv_cache[key][0] == today_str:
            return _ohlcv_cache[key][1]

    try:
        if kis is not None:
            time.sleep(0.25)  # 모의투자 API 초당 4회 제한 대응
            df = kis.get_ohlcv(ticker, "D")
            if df is not None and not df.empty:
                result = df.dropna(subset=['close']).tail(days)
                with _cache_lock:
                    _ohlcv_cache[key] = (today_str, result)
                return result

        # KIS API 미설정 또는 실패 시 pykrx 백업
        end   = datetime.today()
        start = end - timedelta(days=days + 90)
        time.sleep(0.05)  # API Rate limit 방어
        df = stock.get_market_ohlcv_by_date(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), ticker
        )
        df.rename(columns={
            '시가':'open','고가':'high','저가':'low',
            '종가':'close','거래량':'volume'
        }, inplace=True)
        result = df.dropna(subset=['close']).tail(days)
        
        with _cache_lock:
            _ohlcv_cache[key] = (today_str, result)  # 오늘 날짜와 함께 캐시
        return result
    except Exception:
        return pd.DataFrame()


# ──────────────────────────────────────────────
# 2. 섹터/테마 강세 탐지 (종목 수익률 기반)
# ──────────────────────────────────────────────
# 섹터별 대표 종목 목록 (pykrx 지수 API 불안정으로 직접 정의)
SECTOR_STOCKS = {
    "반도체":    ["005930","000660","042700","091990","336370","000990"],
    "2차전지":   ["373220","006400","051910","247540","096770","011790"],
    "바이오/제약":["068270","207940","000120","003850","128940","326030"],
    "자동차":    ["005380","000270","012330","204320","009150","073240"],
    "IT/소프트웨어":["035420","035720","259960","112040","047050","293490"],
    "방산/우주":  ["012450","047810","004830","272210","079550","013890"],
    "조선/중공업":["009540","010140","042660","329180","267270","138040"],
    "금융/보험":  ["055550","105560","086790","000810","316140","175330"],
    "에너지/화학":["010950","011170","096770","267250","078930","001570"],
    "건설/부동산":["000720","047040","028260","034020","006360","294870"],
    "유통/소비":  ["139480","023530","004170","282330","016360","069960"],
    "AI/로봇":   ["017670","042700","079550","108860","285490","950130"],
}

def get_sector_momentum(lookback=20, verbose=False):
    """
    섹터별 대표 종목 수익률 평균으로 강세 섹터 탐지.
    Returns: dict { sector_name: avg_return_pct }  (내림차순 정렬)
    """
    results = {}
    end   = datetime.today()
    start = end - timedelta(days=lookback + 30)

    for sector_name, tickers in SECTOR_STOCKS.items():
        rets = []
        for t in tickers:
            if not t.isdigit() or len(t) != 6:
                continue
            try:
                df = stock.get_market_ohlcv_by_date(
                    start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), t
                )
                if df is None or len(df) < 5:
                    continue
                col = '종가' if '종가' in df.columns else df.columns[3]
                series = df[col].dropna()
                if len(series) >= 5:
                    ret = (series.iloc[-1] / series.iloc[-min(lookback, len(series)-1)] - 1) * 100
                    rets.append(float(ret))
            except Exception:
                continue
        if rets:
            results[sector_name] = round(float(np.mean(rets)), 2)

    sorted_results = dict(sorted(results.items(), key=lambda x: x[1], reverse=True))
    if verbose:
        print("\n📊 현재 섹터/테마 강세 분석 (최근 20일 평균 수익률)")
        for name, ret in sorted_results.items():
            bar = "▲" if ret > 0 else "▼"
            print(f"   {bar} {name:<18} {ret:+.1f}%")
    return sorted_results


def get_sector_tickers(momentum, top_n_sectors=4):
    """
    강세 섹터 상위 N개 → 해당 섹터 대표 종목 반환.
    같은 종목이 여러 섹터에 중복 등록된 경우 먼저 등장한 섹터에만 할당 (점수 중복 방지).
    Returns: set of tickers, dict of ticker→sector_name, list of hot sector names
    """
    hot_sectors = [k for k, v in momentum.items() if v > 0][:top_n_sectors]

    sector_tickers   = set()
    ticker_to_sector = {}
    seen = set()  # 중복 종목 방지

    for sec_name in hot_sectors:
        tickers = SECTOR_STOCKS.get(sec_name, [])
        for t in tickers:
            if t.isdigit() and len(t) == 6 and t not in EXCLUDE_TICKERS and t not in seen:
                sector_tickers.add(t)
                ticker_to_sector[t] = sec_name
                seen.add(t)

    return sector_tickers, ticker_to_sector, hot_sectors


# ──────────────────────────────────────────────
# 3. 거래량 급등 감지
# ──────────────────────────────────────────────
def get_candidate_tickers(kis=None, verbose=False):
    """
    KOSPI+KOSDAQ 후보 종목 풀 생성.
    - 방법1: KIS API 동적 거래량 상위 종목 수집 (Hybrid)
    - 방법2: 알려진 주요 종목 풀 + 섹터 대표 종목 (Fallback)
    """
    # 알려진 주요 종목 풀 (KOSPI 대형주 + KOSDAQ 대형주 + 각 섹터 대표)
    BASE_POOL = [
        # KOSPI 대형주
        "005930","000660","005380","005490","028260","015760","066570","086790",
        "032830","055550","105560","012330","000270","096770","009150","010950",
        "011170","034020","078930","000810","316140","047050","033780","003550",
        "207940","068270","326030","128940","009540","010140","042660","329180",
        "373220","006400","051910","247540","011790","012450","047810","079550",
        "267270","138040","035420","035720","259960","112040","139480","023530",
        "004170","282330","016360","069960","017670","108860","285490","000720",
        "047040","034020","006360","294870","175330","000120","001570","267250",
        # KOSDAQ 대형주
        "293490","950130","263750","322000","214150","145020","091990","336370",
        "035900","041510","024060","066970","086520","357780","096530","347860",
        "272210","013890","004830","042700","035900","041510","091990",
        "196170","251270","064350","101400","236200","036540","263720",
    ]

    # 섹터별 종목도 추가
    for tickers in SECTOR_STOCKS.values():
        for t in tickers:
            if t.isdigit() and len(t) == 6:
                BASE_POOL.append(t)

    # 중복 제거
    seen = set()
    unique = []
    
    # 1. 동적 급등주 추가 (KIS API)
    dynamic_count = 0
    if kis is not None:
        if verbose:
            print("   🌐 KIS API 실시간 거래량/등락률 상위 종목 수집 중...")
        try:
            top_kospi = kis.get_volume_rank(market_div="J", limit=30)
            top_kosdaq = kis.get_volume_rank(market_div="Q", limit=30)
            top_rise_kospi = kis.get_price_change_rank(market_div="J", limit=20)
            top_rise_kosdaq = kis.get_price_change_rank(market_div="Q", limit=20)
            dynamic_pool = top_kospi + top_kosdaq + top_rise_kospi + top_rise_kosdaq
            for t in dynamic_pool:
                if t not in seen and t not in EXCLUDE_TICKERS:
                    seen.add(t)
                    unique.append(t)
                    dynamic_count += 1
            if verbose:
                print(f"   ✨ 시장 실시간 주도주 {dynamic_count}개 추가 완료!")
        except Exception as e:
            if verbose:
                print(f"   ⚠️ 실시간 종목 수집 실패, 기본 풀만 사용합니다: {e}")

    # 2. 기존 베이스 풀 추가
    for t in BASE_POOL:
        if t not in seen and t not in EXCLUDE_TICKERS:
            seen.add(t)
            unique.append(t)
            
    return unique


def get_volume_surge_tickers(kis=None,
                              market_list=("KOSPI", "KOSDAQ"),
                              surge_ratio=1.8,
                              min_cap_billion=300,
                              max_tickers=150,
                              verbose=False):
    """
    거래량 급등 종목 필터.
    - surge_ratio: 최근5일 평균거래량 / 60일 평균거래량 > surge_ratio
    Returns: dict { ticker: volume_score }
    """
    tickers   = get_candidate_tickers(kis=kis, verbose=verbose)
    candidates = {}

    if verbose:
        print(f"   후보 풀 {len(tickers)}개 종목 거래량 분석 중...")

    for ticker in tickers:
        if ticker in EXCLUDE_TICKERS:
            continue
        try:
            # 💡 days=80 대신 백테스트 기간인 BACKTEST_DAYS(130)로 일치시켜 캐시된 데이터를 재사용하게 만듭니다.
            df = fetch_ohlcv(ticker, days=BACKTEST_DAYS, kis=kis) # 🟢 kis 파라미터 추가
            if len(df) < 30 or 'volume' not in df.columns:
                continue
            if df['close'].iloc[-1] < 1000:
                continue
            # 일평균 거래대금 3억 미만 제외 — 유동성/시총 최소 기준 (min_cap_billion 프록시)
            if 'volume' in df.columns:
                avg_trading_value = (df['close'].iloc[-5:] * df['volume'].iloc[-5:]).mean()
                if avg_trading_value < 300_000_000:
                    continue
            vol_recent = df['volume'].iloc[-5:].mean()
            vol_base   = df['volume'].iloc[-75:-5].mean()
            if vol_base < 100:
                continue
            ratio = vol_recent / (vol_base + 1e-9)
            if ratio >= surge_ratio:
                candidates[ticker] = round(float(ratio), 2)
        except Exception:
            continue

    sorted_c = dict(sorted(candidates.items(), key=lambda x: x[1], reverse=True))
    if verbose:
        print(f"\n📈 거래량 급등 종목: {len(sorted_c)}개 발견 (기준 {surge_ratio}x 이상)")
        for t, r in list(sorted_c.items())[:10]:
            try:
                name = stock.get_market_ticker_name(t)
            except Exception:
                name = t
            print(f"   {name}({t}): {r:.1f}x")
    return sorted_c



# ──────────────────────────────────────────────
# 4. 기술적 지표 & 전략 (기존 유지)
# ──────────────────────────────────────────────
def ema(s, n):    return s.ewm(span=n, adjust=False).mean()
def sma(s, n):    return s.rolling(n).mean()

def calc_rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean()
    return 100 - 100 / (1 + g / (l + 1e-10))

def calc_macd(s, f=12, sl=26, sig=9):
    m = ema(s, f) - ema(s, sl)
    return m, ema(m, sig)

def calc_bb(s, p=20, k=2):
    mid = sma(s, p); sd = s.rolling(p).std()
    return mid + k*sd, mid, mid - k*sd

def calc_stoch(h, l, c, kp=21, dp=5):
    # %K 기간 14→21, %D 3→5: 신호 과다 발생(150일에 60회) 방지
    lo = l.rolling(kp).min(); hi = h.rolling(kp).max()
    k  = 100 * (c - lo) / (hi - lo + 1e-10)
    return k, k.rolling(dp).mean()

def calc_cci(h, l, c, p=20):
    tp = (h + l + c) / 3; ma = sma(tp, p)
    md = tp.rolling(p).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - ma) / (0.015 * md + 1e-10)

def calc_williams(h, l, c, p=14):
    return -100 * (h.rolling(p).max() - c) / (h.rolling(p).max() - l.rolling(p).min() + 1e-10)

def cross_sig(fast, slow):
    s = pd.Series(0, index=fast.index)
    s[fast > slow] = 1; s[fast < slow] = -1
    t = s.diff().fillna(0)
    out = pd.Series(0, index=s.index)
    out[t > 0] = 1; out[t < 0] = -1
    return out

def threshold_sig(ind, lo, hi):
    s = pd.Series(0, index=ind.index)
    s[ind < lo] = 1; s[ind > hi] = -1
    prev = 0
    for i in s.index:
        if s[i] == prev: s[i] = 0
        elif s[i] != 0: prev = s[i]
    return s

STRATEGY_REGISTRY = {
    "RSI(9) 30/70":     lambda df: threshold_sig(calc_rsi(df['close'], 9), 30, 70),
    "RSI(14) 30/70":    lambda df: threshold_sig(calc_rsi(df['close'], 14), 30, 70),
    "RSI(14) 40/60":    lambda df: threshold_sig(calc_rsi(df['close'], 14), 40, 60),
    "EMA 5/20 크로스":   lambda df: cross_sig(ema(df['close'], 5), ema(df['close'], 20)),
    "EMA 3/10 크로스":   lambda df: cross_sig(ema(df['close'], 3), ema(df['close'], 10)),
    "SMA 5/20 크로스":   lambda df: cross_sig(sma(df['close'], 5), sma(df['close'], 20)),
    "SMA 3/10 크로스":   lambda df: cross_sig(sma(df['close'], 3), sma(df['close'], 10)),
    "SMA 3/20 크로스":   lambda df: cross_sig(sma(df['close'], 3), sma(df['close'], 20)),
    "MACD 크로스":       lambda df: cross_sig(*calc_macd(df['close'])),
    "볼린저밴드 반전":    lambda df: threshold_sig(
                             df['close'] / calc_bb(df['close'])[1], 0.97, 1.03),
    "Stochastic 크로스": lambda df: cross_sig(*calc_stoch(df['high'], df['low'], df['close'])),
    "CCI ±100":          lambda df: threshold_sig(
                             calc_cci(df['high'], df['low'], df['close']), -100, 100),
    "Williams %R":       lambda df: threshold_sig(
                             calc_williams(df['high'], df['low'], df['close']), -80, -20),
}

def backtest(df, sig_series, initial=1_000_000):
    c = df['close']
    cash, holding, buy_price = float(initial), 0, 0
    fee_rate = 0.00015 # 0.015% 온라인 수수료
    tax_rate = 0.0018  # 0.18% 증권거래세
    
    for date in df.index:
        price = int(c.loc[date])
        sig   = sig_series.get(date, 0) if isinstance(sig_series, pd.Series) else 0
        
        # 매수 (수수료 가산)
        if sig == 1 and holding == 0 and cash >= price * (1 + fee_rate):
            cost_per_share = price * (1 + fee_rate)
            holding = int(cash // cost_per_share)
            if holding > 0:
                cash -= holding * cost_per_share
                buy_price = price
                
        # 매도 (수수료 및 세금 차감)
        elif sig == -1 and holding > 0:
            revenue_per_share = price * (1 - fee_rate - tax_rate)
            cash += holding * revenue_per_share
            holding = 0
            
    # 백테스트 종료 시 잔여 보유 물량 강제 청산 가치 계산
    if holding > 0:
        revenue_per_share = int(c.iloc[-1]) * (1 - fee_rate - tax_rate)
        cash += holding * revenue_per_share
        
    return (cash - initial) / initial * 100

def find_best_strategy(df):
    """
    Walk-forward 검증으로 과적합 방지.
    - 신호 생성: 전체 df (MA/RSI 이동평균 웜업을 위해 전체 필요)
    - 성능 평가: 마지막 30% 구간(OOS)만 사용
    - OOS 전 전략이 모두 손실이면 전체 기간 기준으로 폴백
    """
    if len(df) < 50:
        return None, -9999

    split = max(20, int(len(df) * 0.70))
    oos_df = df.iloc[split:]

    best_name, best_ret = None, -9999
    for name, fn in STRATEGY_REGISTRY.items():
        try:
            full_sig = fn(df)           # 전체 기간으로 신호 생성 (워밍업 포함)
            oos_sig  = full_sig.iloc[split:]
            # 과신호 전략 제외: OOS 기간(40일 내외)에 매수 신호 15개 초과 시 수수료 과다 예상
            if (oos_sig == 1).sum() > 15:
                continue
            ret = backtest(oos_df, oos_sig)
            if ret > best_ret:
                best_ret, best_name = ret, name
        except Exception:
            continue

    # OOS에서 모든 전략이 -30% 이하면 전체 기간 폴백 (데이터 부족 방어)
    if best_ret < -30:
        for name, fn in STRATEGY_REGISTRY.items():
            try:
                sig = fn(df)
                ret = backtest(df, sig)
                if ret > best_ret:
                    best_ret, best_name = ret, name
            except Exception:
                continue

    return best_name, best_ret


# ──────────────────────────────────────────────
# 5. 메인 선정 함수
# ──────────────────────────────────────────────
def select_satellites(kis=None, n=NUM_SATELLITES, verbose=True, gemini_client=None, bear_mode=False, sector_guide: str = ''):
    """
    멀티팩터 위성 종목 선정 (딥러닝 PyTorch 확률 예측 엔진 연동 완료)
    """
    if verbose:
        print("\n" + "="*60)
        print("  🔍 위성 종목 딥러닝 + 멀티팩터 스크리닝 시작")
        print("  ① 섹터/테마 강세 분석")
        print("  ② KOSPI+KOSDAQ 거래량 급등 탐지")
        print("  ③ 종목별 최적 전략 백테스트")
        print("  ④ PyTorch LSTM 인공지능 상승 확률 분석")
        print("="*60)

    sector_momentum = get_sector_momentum(lookback=20, verbose=verbose)
    sector_tickers, ticker_to_sector, hot_sectors = get_sector_tickers(sector_momentum, top_n_sectors=4)

    if verbose and hot_sectors:
        print(f"\n🔥 현재 강세 섹터 TOP4: {', '.join(hot_sectors)}")

    volume_surges = get_volume_surge_tickers(
        kis=kis, market_list=("KOSPI", "KOSDAQ"),
        surge_ratio=1.5, min_cap_billion=300, max_tickers=150, verbose=verbose
    )

    # 외인/기관 순매수 팩터 수집
    frgn_inst_tickers = set()
    if kis is not None:
        try:
            fi_kospi = kis.get_foreign_institution_rank(market_div="J", limit=30)
            fi_kosdaq = kis.get_foreign_institution_rank(market_div="Q", limit=30)
            for item in fi_kospi + fi_kosdaq:
                if (item.get("frgn_ntby_qty", 0) > 0 or item.get("orgn_ntby_qty", 0) > 0):
                    frgn_inst_tickers.add(item["ticker"])
            if verbose:
                print(f"   💼 외인/기관 순매수 종목: {len(frgn_inst_tickers)}개")
        except Exception:
            pass

    candidate_pool = set(volume_surges.keys()) | sector_tickers | frgn_inst_tickers
    candidate_pool -= EXCLUDE_TICKERS

    if verbose:
        print(f"\n📋 후보 풀: 거래량 급등 {len(volume_surges)}개 + 강세 섹터 {len(sector_tickers)}개 + 외인기관 {len(frgn_inst_tickers)}개 → 합계 {len(candidate_pool)}개")

    # 🤖 딥러닝 모델 로드 (모듈 레벨 싱글턴 — 매 호출마다 디스크 I/O 방지)
    # W-05: 락으로 멀티스레드 환경에서 중복 생성(레이스컨디션) 방지
    # BUG-06 FIX: dl_model 임포트 실패 시 무음 처리되던 것을 명시적 경고로 변경
    try:
        from dl_model import DeepLearningPredictor
    except ImportError as e:
        logger.warning(f"[스크리너] dl_model 로드 실패 — DL 예측 비활성화: {e}")
        DeepLearningPredictor = None
    global _dl_predictor_instance
    if DeepLearningPredictor is not None and _dl_predictor_instance is None:
        with _dl_predictor_lock:
            if _dl_predictor_instance is None:  # double-checked locking
                try:
                    _dl_predictor_instance = DeepLearningPredictor()
                except Exception as e:
                    logger.warning(f"[스크리너] DeepLearningPredictor 초기화 실패: {e}")
    dl_predictor = _dl_predictor_instance  # None이면 아래에서 폴백 처리

    # ── Step 4: 후보별 백테스트 + 종합 점수 ──
    results = []
    processed = 0

    for ticker in candidate_pool:
        try:
            name = stock.get_market_ticker_name(ticker)
            df   = fetch_ohlcv(ticker, days=BACKTEST_DAYS, kis=kis)
            if len(df) < 40 or 'close' not in df.columns:
                continue

            recent_ret = (df['close'].iloc[-1] / df['close'].iloc[-min(20, len(df)-1)] - 1) * 100

            # BEAR 국면에서는 기준 완화 (-25%) — 하락장에서도 반등 후보 확보
            drawdown_threshold = -25 if bear_mode else -15
            if recent_ret < drawdown_threshold:
                continue

            bb_upper, bb_mid, bb_lower = calc_bb(df['close'])
            current_price = df['close'].iloc[-1]
            bb_discount = (bb_mid.iloc[-1] - current_price) / bb_mid.iloc[-1] * 100

            best_strat, best_ret = find_best_strategy(df)

            vol_score    = volume_surges.get(ticker, 1.0)
            sector_bonus = 10 if ticker in sector_tickers else 0
            frgn_inst_bonus = 8 if ticker in frgn_inst_tickers else 0

            overheated_penalty = 0
            if recent_ret > 15 and vol_score < 2.0:
                overheated_penalty = (recent_ret - 15) * 1.5

            stat_arb_penalty = 0
            if recent_ret > 30:
                stat_arb_penalty = (recent_ret - 30) * 0.8  

            # dl_predictor가 None이면 중립 50.0으로 폴백 (DL 없이도 스크리닝 계속)
            ai_up_prob = dl_predictor.predict_up_probability(df) if dl_predictor is not None else 50.0
            ml_factor_score = (ai_up_prob - 50.0) * 0.2

            score = (best_ret
                     + (vol_score - 1) * 6
                     + sector_bonus
                     + frgn_inst_bonus
                     + (bb_discount * 1.5)
                     - overheated_penalty
                     - stat_arb_penalty
                     + ml_factor_score)

            # RSI(14) 현재값 계산 — AI 전략 검수 프롬프트에 활용
            try:
                rsi_val = round(float(calc_rsi(df['close'], 14).iloc[-1]), 1)
            except Exception:
                rsi_val = None

            results.append({
                'ticker':        ticker,
                'name':          name,
                'strategy_name': best_strat,
                'return_pct':    float(round(best_ret, 2)),
                'volume_surge':  float(round(vol_score, 2)),
                'vol_ratio':     float(round(vol_score, 2)),   # review_satellite_candidates 프롬프트용 별칭
                'rsi':           rsi_val,                      # review_satellite_candidates 프롬프트용
                'sector':        ticker_to_sector.get(ticker, '-'),
                'momentum_20d':  float(round(recent_ret, 2)),
                'dl_prob':       float(round(ai_up_prob, 1)),
                'frgn_inst':     ticker in frgn_inst_tickers,
                'score':         float(round(score, 2)),
            })
            processed += 1

            if verbose and processed % 10 == 0:
                print(f"   ... {processed}개 분석 완료")

        except Exception:
            continue

    results.sort(key=lambda x: x['score'], reverse=True)
    
    selected = None
    if gemini_client:
        if verbose:
            print("\n🤖 [AI 자율 매매] Claude AI가 최종 위성 종목과 전략을 선정 중입니다...")
        ai_result = gemini_client.ai_select_satellites(results, hot_sectors, n, sector_guide=sector_guide)
        if ai_result:
            selected = ai_result
            if verbose: print("   ✅ AI 텍스트 선정 완료!")
        else:
            if verbose: print("   ⚠️ AI 선정 실패 (폴백: 기존 득점 순으로 선정)")

    if not selected:
        selected = results[:n]

    if verbose:
        print(f"\n{'='*60}")
        print(f"  ✅ 위성 종목 선정 완료! ({len(results)}개 중 상위 {n}개)")
        print(f"{'='*60}")
        for rank, c in enumerate(selected, 1):
            vol_tag = f"📈거래량{c.get('volume_surge', 1.0):.1f}x" if c.get('volume_surge', 1.0) > 1.5 else ""
            sec_tag = f"🔥{c.get('sector', '-')}" if c.get('sector', '-') != '-' else ""
            fi_tag = "💼외인기관" if c.get('frgn_inst') else ""
            dl_tag = f" 🧠[상승확률: {c.get('dl_prob', 0):.1f}%]" if c.get('dl_prob', 0) > 0 else ""
            ai_tag = f" 🤖[AI 선정: {c.get('ai_reason', '')}]" if c.get('ai_selected') else ""
            
            print(f"\n  {rank}위. [{c['name']}] ({c['ticker']}){dl_tag}{ai_tag}")
            print(f"       전략: {c['strategy_name']}  /  6개월 수익: {c.get('return_pct', 0):+.1f}%")
            print(f"       20일 모멘텀: {c.get('momentum_20d', 0):+.1f}%  {vol_tag}  {sec_tag}  {fi_tag}")
            print(f"       종합점수: {c.get('score', 0):.1f}점")
        print(f"{'='*60}\n")

    return selected, hot_sectors



# ──────────────────────────────────────────────
# 테스트용 실행
# ──────────────────────────────────────────────
if __name__ == '__main__':
    print("섹터 강세 분석만 먼저 테스트:")
    m = get_sector_momentum(verbose=True)
    print("\n전체 스크리닝 시작 (약 3~5분 소요):")
    result = select_satellites(n=5, verbose=True)

# ──────────────────────────────────────────────
# 6. AI 코어 장기 우량주 자동 선정 (듀얼 코어용)
# ──────────────────────────────────────────────
def select_ai_core_stock(verbose=False):
    """
    미리 정의된 우량주 풀(SECTOR_STOCKS) 중에서 
    가장 안정적으로 우상향(120일 이평선 정배열 및 모멘텀)하는 1개 종목을 AI 코어로 선정.
    Returns: dict { 'ticker': '...', 'name': '...', 'strategy_name': '...', 'return_pct': ... }
    """
    if verbose:
        print("\n🔍 [AI 코어] 장기 우상향 우량주 탐색 시작...")

    best_ticker = None
    best_score = -9999
    
    candidates = set()
    for sec_tickers in SECTOR_STOCKS.values():
        for t in sec_tickers:
            if t.isdigit() and len(t) == 6 and t not in EXCLUDE_TICKERS:
                candidates.add(t)
                
    if "003850" in candidates: candidates.remove("003850")

    for ticker in list(candidates):
        try:
            df = fetch_ohlcv(ticker, days=150)
            if len(df) < 130 or 'close' not in df.columns:
                continue
                
            close = df['close']
            sma_60 = close.rolling(60).mean()
            sma_120 = close.rolling(120).mean()
            
            curr_close = close.iloc[-1]
            curr_sma60 = sma_60.iloc[-1]
            curr_sma120 = sma_120.iloc[-1]
            
            if not (curr_close > curr_sma60 > curr_sma120):
                continue
                
            momentum_120d = (curr_close / close.iloc[-120] - 1) * 100
            std_20 = close.pct_change().rolling(20).std().iloc[-1] * 100
            
            score = momentum_120d - (std_20 * 2)
            
            if score > best_score:
                best_score = score
                best_ticker = ticker
                
        except Exception:
            continue
            
    if best_ticker:
        try:
            name = stock.get_market_ticker_name(best_ticker)
        except:
            name = best_ticker
            
        return {
            'ticker': best_ticker,
            'name': name,
            'strategy_name': '정배열 장기보유',
            'return_pct': best_score
        }
        
    return None


# ──────────────────────────────────────────────
# 7. 일일 시장 분석 리포트 자동 생성
# ──────────────────────────────────────────────
def generate_daily_market_report(gemini_client=None, verbose=False, news_context=None, kis=None):
    """
    코스피/코스닥 대리 지수(ETF) 및 주도 섹터 데이터를 활용하여 텍스트 리포트를 생성합니다.
    gemini_client가 제공되면 AI 기반 분석을 수행합니다.
    """
    if verbose:
        print("\n📝 일일 시장 분석 리포트 생성 중...")

    raw_data_lines = []
    today_str = datetime.today().strftime('%Y년 %m월 %d일')
    
    # 1. 시장 방향성 데이터 수집
    indices = {
        "KOSPI (KODEX 200)": "069500",
        "KOSDAQ (KODEX KOSDAQ150)": "229200"
    }
    
    raw_data_lines.append(f"날짜: {today_str}")
    raw_data_lines.append("\n[주요 지수 데이터]")
    for name, ticker in indices.items():
        try:
            df = fetch_ohlcv(ticker, days=30)
            if len(df) < 20: continue
            
            close = df['close']
            sma_5 = close.rolling(5).mean().iloc[-1]
            sma_20 = close.rolling(20).mean().iloc[-1]
            current = close.iloc[-1]
            pct_change = (current / close.iloc[-2] - 1) * 100
            rsi_14 = calc_rsi(close, 14).iloc[-1]
            
            raw_data_lines.append(f"- {name}: 현재가 {current:,.0f}원 ({pct_change:+.2f}%), 5일평균 {sma_5:,.0f}, 20일평균 {sma_20:,.0f}, RSI {rsi_14:.1f}")
        except: pass
            
    # 2. 주도 섹터 데이터
    raw_data_lines.append("\n[섹터 모멘텀]")
    try:
        momentum = get_sector_momentum(lookback=10, verbose=False)
        for sec, val in list(momentum.items())[:5]:
            raw_data_lines.append(f"- {sec}: {val:+.1f}%")
    except: pass
        
    # 3. 거래량 급등 데이터
    try:
        volume_surges = get_volume_surge_tickers(kis=kis, surge_ratio=2.0, verbose=False)
        raw_data_lines.append(f"\n[수급 특이사항]\n- 거래량 2배 급증 종목 수: {len(volume_surges)}개")
    except: pass

    # 🚨 [신규 추가] 주요 종목의 실시간 네이버 뉴스 텍스트 컨텍스트 결합
    if news_context:
        raw_data_lines.append("\n[포트폴리오 주도주 실시간 주요 뉴스 헤드라인]")
        raw_data_lines.append(news_context)

    market_data_text = "\n".join(raw_data_lines)

    # Gemini AI 활용 여부 결정
    if gemini_client:
        if verbose: print("   🤖 Gemini AI 분석 요청 중...")
        report_text = gemini_client.analyze_market(market_data_text)
    else:
        # Fallback: 기존 룰 기반 리포트
        report_lines = [f"### 📊 Lassi Bot 시장 분석 리포트 ({today_str})"]
        report_lines.append("\n#### 📈 주요 지수 동향")
        report_lines.append(market_data_text.replace("[", "#### ").replace("-", "*"))
        report_lines.append("\n> AI 분석 기능이 비활성화되어 있습니다. Claude API 키를 등록하면 더 정교한 분석을 받을 수 있습니다.")
        report_text = "\n".join(report_lines)

    if verbose:
        print(report_text)
        
    return {
        "date": datetime.today().strftime('%Y-%m-%d'),
        "report_markdown": report_text
    }
