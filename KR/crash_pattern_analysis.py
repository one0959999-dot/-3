"""
crash_pattern_analysis.py — 테마주 급락 직전 패턴 역공학 분석
──────────────────────────────────────────────────────────────
급등 후 급락한 종목들의 고점 직전 신호를 분석해서
청산 로직에 반영할 패턴 추출
"""

import pandas as pd
import numpy as np
from datetime import datetime
import warnings
import time
warnings.filterwarnings('ignore')

from pykrx import stock as pykrx_stock
import yfinance as yf

# ── 분석 대상: 급등 후 급락한 종목 ────────────────────────────────
CRASH_STOCKS_KR = {
    "086520": {"name": "에코프로",   "peak": "2023-08-01", "crash_end": "2024-06-01"},
    "028300": {"name": "HLB",        "peak": "2024-05-01", "crash_end": "2024-12-01"},
    "196170": {"name": "알테오젠",   "peak": "2024-07-01", "crash_end": "2024-12-01"},
    "277810": {"name": "레인보우로보틱스", "peak": "2023-09-01", "crash_end": "2024-06-01"},
}

CRASH_STOCKS_US = {
    "SMCI": {"name": "슈퍼마이크로", "peak": "2024-03-01", "crash_end": "2024-12-01"},
    "NVDA": {"name": "엔비디아(조정)", "peak": "2024-06-01", "crash_end": "2024-09-01"},
}

def get_ohlcv_kr(ticker, start, end=None):
    end = end or datetime.today().strftime("%Y-%m-%d")
    try:
        time.sleep(0.3)
        df = pykrx_stock.get_market_ohlcv_by_date(
            start.replace("-",""), end.replace("-",""), ticker)
        if df is None or df.empty: return pd.DataFrame()
        df.rename(columns={'시가':'Open','고가':'High','저가':'Low','종가':'Close','거래량':'Volume'}, inplace=True)
        df.index = pd.to_datetime(df.index)
        return df
    except: return pd.DataFrame()

def get_ohlcv_us(ticker, start, end=None):
    end = end or datetime.today().strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty: return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index)
        return df
    except: return pd.DataFrame()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

def detect_rsi_divergence(close, rsi, lookback=20):
    """RSI 베어리시 다이버전스 감지: 가격 신고가 but RSI 낮아짐"""
    if len(close) < lookback * 2: return False, 0
    recent_close = close.iloc[-lookback:]
    recent_rsi   = rsi.iloc[-lookback:]
    prev_close   = close.iloc[-lookback*2:-lookback]
    prev_rsi     = rsi.iloc[-lookback*2:-lookback]
    price_higher = recent_close.max() > prev_close.max()
    rsi_lower    = recent_rsi.max() < prev_rsi.max()
    divergence_gap = prev_rsi.max() - recent_rsi.max()
    return (price_higher and rsi_lower), round(divergence_gap, 1)

# ══════════════════════════════════════════════════════════════════
# 핵심 분석: 고점 N일 전 신호 측정
# ══════════════════════════════════════════════════════════════════
def analyze_crash_signals(ticker, info, get_fn, pre_days=90):
    name     = info["name"]
    peak_dt  = info["peak"]
    crash_dt = info["crash_end"]

    # 고점 전 90일 ~ 고점 후 crash_end 까지 데이터
    start = (pd.Timestamp(peak_dt) - pd.Timedelta(days=pre_days+30)).strftime("%Y-%m-%d")
    df = get_fn(ticker, start, crash_dt)
    if df.empty or len(df) < 40:
        print(f"  ❌ {name} 데이터 부족")
        return None

    close  = df['Close']
    volume = df['Volume'] if 'Volume' in df.columns else None
    rsi    = calc_rsi(close)

    # 고점 기준 인덱스
    peak_idx  = df.index.searchsorted(pd.Timestamp(peak_dt))
    peak_idx  = min(peak_idx, len(df)-1)
    peak_price = float(close.iloc[peak_idx])

    # 고점 이후 최대 낙폭
    post_df = df.iloc[peak_idx:]
    if len(post_df) < 5:
        crash_pct = 0
    else:
        crash_pct = float((post_df['Close'].min() - peak_price) / peak_price * 100)

    print(f"\n{'='*58}")
    print(f"  💥 [{name} / {ticker}] 급락 패턴 분석")
    print(f"  고점: {peak_dt}  {peak_price:,.0f}  →  낙폭: {crash_pct:+.1f}%")
    print(f"{'='*58}")

    signals = {}

    # ── 고점 전 20일 vs 20~40일 비교 ──────────────────────────────
    w1 = slice(max(0, peak_idx-20), peak_idx)     # 고점 직전 20일
    w2 = slice(max(0, peak_idx-40), peak_idx-20)  # 그 이전 20일

    print(f"\n  [고점 직전 20일 vs 이전 20일 비교]")

    # 거래량 소멸 체크
    if volume is not None and peak_idx >= 40:
        vol_w1 = float(volume.iloc[w1].mean())
        vol_w2 = float(volume.iloc[w2].mean())
        vol_fade = vol_w1 / (vol_w2 + 1e-9)
        signals['vol_fade'] = round(vol_fade, 2)
        state = "🚨 소멸" if vol_fade < 0.7 else ("⚠️ 감소" if vol_fade < 0.9 else "✅ 유지")
        print(f"  거래량 변화:  {vol_fade:.2f}x  [{state}]")
        print(f"             직전 20일 평균 {vol_w1:,.0f}  vs  이전 {vol_w2:,.0f}")

    # RSI 고점 대비 하락
    if peak_idx >= 40:
        rsi_w1_max = float(rsi.iloc[w1].max())
        rsi_w2_max = float(rsi.iloc[w2].max())
        rsi_drop   = rsi_w1_max - rsi_w2_max
        signals['rsi_divergence'] = rsi_drop < -5
        signals['rsi_drop']       = round(rsi_drop, 1)
        state = "🚨 다이버전스" if rsi_drop < -5 else ("⚠️ 약화" if rsi_drop < 0 else "✅ 유지")
        print(f"  RSI 변화:    직전최고 {rsi_w1_max:.1f}  이전최고 {rsi_w2_max:.1f}  ({rsi_drop:+.1f})  [{state}]")

    # 고점 당시 RSI
    if peak_idx > 0:
        rsi_at_peak = float(rsi.iloc[peak_idx])
        signals['rsi_at_peak'] = round(rsi_at_peak, 1)
        state = "🚨 극과매수" if rsi_at_peak > 80 else ("⚠️ 과매수" if rsi_at_peak > 70 else "➡️ 보통")
        print(f"  고점 RSI:    {rsi_at_peak:.1f}  [{state}]")

    # 52주 위치
    n52 = min(252, peak_idx)
    if n52 >= 20:
        h52 = df['High'].iloc[max(0,peak_idx-n52):peak_idx].max() if 'High' in df.columns else close.iloc[max(0,peak_idx-n52):peak_idx].max()
        l52 = df['Low'].iloc[max(0,peak_idx-n52):peak_idx].min()  if 'Low'  in df.columns else close.iloc[max(0,peak_idx-n52):peak_idx].min()
        pos = float((peak_price - l52) / (h52 - l52 + 1e-9) * 100)
        signals['pos_52w_at_peak'] = round(pos, 1)
        print(f"  고점 52주 위치: {pos:.0f}%")

    # 이동평균 이격
    if peak_idx >= 20:
        ma20  = float(close.iloc[max(0,peak_idx-20):peak_idx].mean())
        gap20 = (peak_price - ma20) / ma20 * 100
        signals['ma20_gap'] = round(gap20, 1)
        state = "🚨 극이격" if gap20 > 40 else ("⚠️ 이격" if gap20 > 20 else "➡️ 정상")
        print(f"  20일선 이격:  +{gap20:.1f}%  [{state}]")

    if peak_idx >= 60:
        ma60  = float(close.iloc[max(0,peak_idx-60):peak_idx].mean())
        gap60 = (peak_price - ma60) / ma60 * 100
        signals['ma60_gap'] = round(gap60, 1)
        state = "🚨 극이격" if gap60 > 60 else ("⚠️ 이격" if gap60 > 30 else "➡️ 정상")
        print(f"  60일선 이격:  +{gap60:.1f}%  [{state}]")

    signals['crash_pct']  = round(crash_pct, 1)
    signals['name']       = name
    signals['ticker']     = ticker
    return signals


# ══════════════════════════════════════════════════════════════════
# 공통 패턴 + 청산 조건 도출
# ══════════════════════════════════════════════════════════════════
def derive_exit_rules(all_signals):
    valid = [s for s in all_signals if s]
    if not valid: return

    print(f"\n{'='*58}")
    print(f"  📊 급락 전 공통 패턴 요약 ({len(valid)}개 종목)")
    print(f"{'='*58}")

    crashes    = [s['crash_pct']         for s in valid]
    rsi_peaks  = [s['rsi_at_peak']       for s in valid if 'rsi_at_peak'       in s]
    pos_peaks  = [s['pos_52w_at_peak']   for s in valid if 'pos_52w_at_peak'   in s]
    ma20_gaps  = [s['ma20_gap']          for s in valid if 'ma20_gap'          in s]
    ma60_gaps  = [s['ma60_gap']          for s in valid if 'ma60_gap'          in s]
    vol_fades  = [s['vol_fade']          for s in valid if 'vol_fade'          in s]
    rsi_divs   = [s['rsi_divergence']    for s in valid if 'rsi_divergence'    in s]

    print(f"\n  낙폭:          평균 {np.mean(crashes):.1f}%  /  최대 {min(crashes):.1f}%")
    if rsi_peaks:  print(f"  고점 RSI:      평균 {np.mean(rsi_peaks):.1f}  →  80 이상 {sum(1 for r in rsi_peaks if r>80)}/{len(rsi_peaks)}개")
    if pos_peaks:  print(f"  고점 52주위치: 평균 {np.mean(pos_peaks):.0f}%  →  95% 이상 {sum(1 for p in pos_peaks if p>95)}/{len(pos_peaks)}개")
    if ma20_gaps:  print(f"  20일선 이격:   평균 +{np.mean(ma20_gaps):.1f}%  →  30% 이상 {sum(1 for g in ma20_gaps if g>30)}/{len(ma20_gaps)}개")
    if ma60_gaps:  print(f"  60일선 이격:   평균 +{np.mean(ma60_gaps):.1f}%  →  50% 이상 {sum(1 for g in ma60_gaps if g>50)}/{len(ma60_gaps)}개")
    if vol_fades:  print(f"  거래량 소멸:   평균 {np.mean(vol_fades):.2f}x  →  0.8 미만 {sum(1 for v in vol_fades if v<0.8)}/{len(vol_fades)}개")
    if rsi_divs:   print(f"  RSI 다이버전스: {sum(rsi_divs)}/{len(rsi_divs)}개 종목에서 발생")

    # 임계값 계산
    rsi_thresh  = np.percentile(rsi_peaks,  25) if rsi_peaks  else 75
    ma20_thresh = np.percentile(ma20_gaps,  25) if ma20_gaps  else 30
    ma60_thresh = np.percentile(ma60_gaps,  25) if ma60_gaps  else 50
    vol_thresh  = np.percentile(vol_fades,  75) if vol_fades  else 0.8

    print(f"\n{'='*58}")
    print(f"  🚨 청산 트리거 조건 (분석 기반 임계값)")
    print(f"{'='*58}")
    print(f"""
  [단일 조건 트리거]
  ① RSI {rsi_thresh:.0f} 이상  AND  거래량 감소 추세  → 청산 검토
  ② 20일 이동평균 이격 +{ma20_thresh:.0f}% 이상              → 청산 검토
  ③ 60일 이동평균 이격 +{ma60_thresh:.0f}% 이상              → 청산 검토

  [복합 조건 트리거 (더 신뢰도 높음)]
  ④ RSI 다이버전스 감지  +  거래량 0.8x 미만       → 강력 청산 신호
  ⑤ 52주 위치 95% 이상  +  20일 이격 {ma20_thresh:.0f}%↑      → 강력 청산 신호

  [점진적 청산 조건]
  ⑥ RSI 75↑  →  보유량 30% 익절
     RSI 85↑  →  추가 30% 익절
     RSI 90↑  →  전량 청산 준비
    """)

    return {
        'rsi_exit_threshold':  round(rsi_thresh, 0),
        'ma20_gap_threshold':  round(ma20_thresh, 1),
        'ma60_gap_threshold':  round(ma60_thresh, 1),
        'vol_fade_threshold':  round(vol_thresh, 2),
    }


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print("\n" + "="*58)
    print("  💥 테마주 급락 패턴 분석")
    print("="*58)

    all_signals = []

    print("\n[한국 종목]")
    for ticker, info in CRASH_STOCKS_KR.items():
        s = analyze_crash_signals(ticker, info, get_ohlcv_kr)
        all_signals.append(s)
        time.sleep(0.5)

    print("\n\n[미국 종목]")
    for ticker, info in CRASH_STOCKS_US.items():
        s = analyze_crash_signals(ticker, info, get_ohlcv_us)
        all_signals.append(s)

    rules = derive_exit_rules(all_signals)

    print("\n\n✅ 분석 완료 — 다음 단계: strategy.py 청산 로직 반영")
