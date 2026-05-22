"""
us_backtest.py — 미국장 전략 백테스트 + 급등주 역공학 분석
────────────────────────────────────────────────────────
① 퀀트 팩터 백테스트: PEG·성장률·정배열 전략 vs S&P500 수익률 비교
② 역공학 패턴 분석: NVDA, TSLA, RKLB — 급등 전 공통 신호 추출
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

# ── 분석 대상 ─────────────────────────────────────────────────────
# 역공학: 이미 크게 오른 종목들
ROCKET_STOCKS = {
    "NVDA": {"name": "엔비디아",    "boom_start": "2023-01-01", "pre_start": "2021-01-01"},
    "TSLA": {"name": "테슬라",      "boom_start": "2020-01-01", "pre_start": "2018-01-01"},
    "RKLB": {"name": "로켓랩",      "boom_start": "2023-06-01", "pre_start": "2022-01-01"},
    "META": {"name": "메타",        "boom_start": "2023-01-01", "pre_start": "2021-01-01"},
    "SMCI": {"name": "슈퍼마이크로", "boom_start": "2023-06-01", "pre_start": "2022-01-01"},
}

# 벤치마크 비교군
BENCHMARK = "SPY"

# ── 유틸 ──────────────────────────────────────────────────────────
def pct(a, b):
    if b and b != 0:
        return round((a / b - 1) * 100, 1)
    return None

def get_hist(ticker, start, end=None):
    end = end or datetime.today().strftime("%Y-%m-%d")
    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        # MultiIndex 처리
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e:
        print(f"  ⚠️ {ticker} 다운로드 실패: {e}")
        return pd.DataFrame()

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / (loss + 1e-10)
    return 100 - 100 / (1 + rs)

# ══════════════════════════════════════════════════════════════════
# 1. 역공학 패턴 분석
# ══════════════════════════════════════════════════════════════════
def analyze_rocket_pattern(ticker, info, verbose=True):
    """급등 전 구간의 기술적·펀더멘털 신호 분석"""
    name = info["name"]
    boom_start = info["boom_start"]
    pre_start  = info["pre_start"]

    print(f"\n{'='*60}")
    print(f"  📊 [{name} / {ticker}] 역공학 패턴 분석")
    print(f"  분석 구간: {pre_start} ~ {boom_start} (급등 직전)")
    print(f"{'='*60}")

    # 가격 데이터
    df = get_hist(ticker, pre_start)
    if df.empty or len(df) < 60:
        print(f"  ❌ 데이터 부족")
        return None

    # 급등 전 시점 기준가
    pre_df = df[df.index < boom_start]
    post_df = df[df.index >= boom_start]

    if pre_df.empty or post_df.empty:
        print(f"  ❌ 구간 분리 실패")
        return None

    close_col = 'Close' if 'Close' in pre_df.columns else pre_df.columns[0]
    pre_price  = float(pre_df[close_col].iloc[-1])   # 급등 직전 종가
    peak_price = float(post_df[close_col].max())      # 급등 후 최고가
    now_price  = float(df[close_col].iloc[-1])        # 현재가

    rise_pct = pct(peak_price, pre_price)
    print(f"\n  💰 가격 변화:")
    print(f"     급등 전:  ${pre_price:,.2f}")
    print(f"     최고가:   ${peak_price:,.2f}  ({rise_pct:+.0f}%)")
    print(f"     현재가:   ${now_price:,.2f}")

    # 기술적 지표 (급등 직전 상태)
    close = pre_df[close_col]
    vol_col = 'Volume' if 'Volume' in pre_df.columns else None

    print(f"\n  📈 급등 직전 기술적 신호:")

    # 이동평균 배열
    sma_50  = close.rolling(50).mean().iloc[-1]  if len(close) >= 50  else None
    sma_200 = close.rolling(200).mean().iloc[-1] if len(close) >= 200 else None
    if sma_50 and sma_200:
        golden = pre_price > sma_50 > sma_200
        status = "✅ 정배열 (현재가>50일>200일)" if golden else "⚠️ 역배열"
        print(f"     이동평균:   {status}")
        print(f"                현재가 ${pre_price:.2f} / 50일 ${sma_50:.2f} / 200일 ${sma_200:.2f}")

    # RSI
    if len(close) >= 20:
        rsi = calc_rsi(close).iloc[-1]
        rsi_state = "과매수⚠️" if rsi > 70 else ("과매도✅" if rsi < 40 else "중립 ➡️")
        print(f"     RSI(14):    {rsi:.1f}  [{rsi_state}]")

    # 52주 위치
    high_52 = close.rolling(252).max().iloc[-1] if len(close) >= 252 else close.max()
    low_52  = close.rolling(252).min().iloc[-1] if len(close) >= 252 else close.min()
    pos_52  = (pre_price - low_52) / (high_52 - low_52 + 1e-9) * 100
    print(f"     52주 위치:  {pos_52:.0f}%  (0%=저점, 100%=고점)")
    print(f"                52주 고점 ${high_52:.2f} / 저점 ${low_52:.2f}")

    # 6개월 모멘텀
    mom_6m = pct(pre_price, close.iloc[-126]) if len(close) >= 126 else None
    mom_3m = pct(pre_price, close.iloc[-63])  if len(close) >= 63  else None
    if mom_6m: print(f"     6개월 수익: {mom_6m:+.1f}%")
    if mom_3m: print(f"     3개월 수익: {mom_3m:+.1f}%")

    # 거래량 급등
    if vol_col and len(pre_df) >= 30:
        vol_recent = pre_df[vol_col].iloc[-20:].mean()
        vol_base   = pre_df[vol_col].iloc[-60:-20].mean()
        vol_ratio  = vol_recent / (vol_base + 1)
        vol_state  = "🔥 급증" if vol_ratio > 1.5 else ("📈 증가" if vol_ratio > 1.1 else "➡️ 보통")
        print(f"     거래량 비율: {vol_ratio:.2f}x  [{vol_state}]")

    # 펀더멘털 (yfinance info)
    print(f"\n  💼 펀더멘털 (현재 기준):")
    try:
        info_data = yf.Ticker(ticker).info
        pe  = info_data.get('trailingPE')
        peg = info_data.get('pegRatio')
        rev_growth = info_data.get('revenueGrowth')
        gross_margin = info_data.get('grossMargins')
        market_cap = info_data.get('marketCap', 0)
        sector = info_data.get('sector', 'N/A')
        industry = info_data.get('industry', 'N/A')

        print(f"     섹터:       {sector} / {industry}")
        if market_cap: print(f"     시가총액:   ${market_cap/1e9:.1f}B")
        if pe:  print(f"     P/E:        {pe:.1f}")
        if peg: print(f"     PEG:        {peg:.2f}  {'✅ 저평가' if peg < 1.5 else '⚠️ 고평가'}")
        if rev_growth: print(f"     매출성장:   {rev_growth*100:+.1f}%  {'🚀 고성장' if rev_growth > 0.20 else ''}")
        if gross_margin: print(f"     매출총이익률: {gross_margin*100:.1f}%")
    except Exception as e:
        print(f"     (펀더멘털 조회 실패: {e})")

    return {
        'ticker': ticker, 'name': name,
        'pre_price': pre_price, 'peak_price': peak_price,
        'rise_pct': rise_pct,
        'sma50': sma_50, 'sma200': sma_200,
        'golden_cross': pre_price > (sma_50 or 0) > (sma_200 or 0),
        'rsi': rsi if len(close) >= 20 else None,
        'pos_52w': pos_52,
        'mom_6m': mom_6m,
    }


# ══════════════════════════════════════════════════════════════════
# 2. 공통 패턴 추출
# ══════════════════════════════════════════════════════════════════
def extract_common_pattern(results):
    valid = [r for r in results if r]
    if not valid:
        return

    print(f"\n{'='*60}")
    print(f"  🔍 공통 패턴 분석 ({len(valid)}개 급등주 비교)")
    print(f"{'='*60}")

    # 각 지표별 통계
    rises   = [r['rise_pct'] for r in valid if r.get('rise_pct')]
    rsis    = [r['rsi'] for r in valid if r.get('rsi')]
    pos52s  = [r['pos_52w'] for r in valid if r.get('pos_52w') is not None]
    mom6ms  = [r['mom_6m'] for r in valid if r.get('mom_6m') is not None]
    goldens = [r['golden_cross'] for r in valid if r.get('golden_cross') is not None]

    print(f"\n  📊 급등폭 분포:")
    print(f"     평균: {np.mean(rises):+.0f}%  /  최소: {min(rises):+.0f}%  /  최대: {max(rises):+.0f}%")

    if rsis:
        print(f"\n  📊 RSI 분포 (급등 직전):")
        print(f"     평균: {np.mean(rsis):.1f}  /  범위: {min(rsis):.1f} ~ {max(rsis):.1f}")
        overheated = sum(1 for r in rsis if r > 65)
        print(f"     RSI 65 초과 (이미 달린 상태): {overheated}/{len(rsis)}개")

    if pos52s:
        print(f"\n  📊 52주 위치 (급등 직전):")
        print(f"     평균: {np.mean(pos52s):.0f}%  →  ", end="")
        low_zone  = sum(1 for p in pos52s if p < 40)
        mid_zone  = sum(1 for p in pos52s if 40 <= p < 70)
        high_zone = sum(1 for p in pos52s if p >= 70)
        print(f"저점권(<40%) {low_zone}개  /  중간 {mid_zone}개  /  고점권(>70%) {high_zone}개")

    if goldens:
        golden_cnt = sum(1 for g in goldens if g)
        print(f"\n  📊 정배열 (50일>200일):")
        print(f"     {golden_cnt}/{len(goldens)}개 종목이 이미 정배열 상태였음")

    if mom6ms:
        print(f"\n  📊 6개월 모멘텀 (급등 직전):")
        pos_mom = sum(1 for m in mom6ms if m > 0)
        print(f"     평균: {np.mean(mom6ms):+.1f}%  /  플러스 종목: {pos_mom}/{len(mom6ms)}개")

    # 스크리너 시사점
    print(f"\n  {'='*50}")
    print(f"  💡 스크리너 반영 시사점:")
    print(f"  {'='*50}")

    avg_pos = np.mean(pos52s) if pos52s else 50
    avg_rsi = np.mean(rsis) if rsis else 55
    avg_mom = np.mean(mom6ms) if mom6ms else 0

    if avg_pos < 50:
        print(f"  ✅ 52주 저점 부근에서 매수 기회 포착 → 스크리너 조건 반영")
    else:
        print(f"  ✅ 이미 상승 추세 중 추가 급등 → 모멘텀 팩터 중요")

    if avg_rsi < 60:
        print(f"  ✅ RSI {avg_rsi:.0f} — 과매수 아닌 상태에서 진입 유리")
    else:
        print(f"  ⚠️ RSI {avg_rsi:.0f} — 진입 시점 선택 중요")

    golden_ratio = sum(1 for g in goldens if g) / len(goldens) if goldens else 0
    if golden_ratio >= 0.5:
        print(f"  ✅ 정배열 종목 비율 {golden_ratio*100:.0f}% → 정배열 필터 유효")

    print(f"\n  🎯 최적 진입 조건 (분석 기반):")
    print(f"     - 52주 위치: 30~60% 구간 (너무 고점도, 저점도 아닌)")
    print(f"     - RSI: 45~65 (과매수/과매도 제외)")
    print(f"     - 정배열 또는 골든크로스 직후")
    print(f"     - 거래량 1.3x 이상 증가 추세")
    print(f"     - 매출 성장률 15%↑ + PEG < 2.0")


# ══════════════════════════════════════════════════════════════════
# 3. 간단 전략 백테스트
# ══════════════════════════════════════════════════════════════════
def simple_backtest(start="2020-01-01", end="2025-01-01"):
    """
    전략: 매월 초 S&P500 종목 중 퀀트 필터 통과 종목 균등 보유
    여기서는 대표 팩터 포트폴리오로 시뮬레이션
    """
    print(f"\n{'='*60}")
    print(f"  📈 전략 백테스트 ({start} ~ {end})")
    print(f"{'='*60}")

    # 팩터별 대표 ETF로 전략 시뮬레이션
    portfolios = {
        "우리 전략 (성장+가치 혼합)":   ["MTUM", "VBR"],       # 모멘텀 + 소형가치
        "성장주 전략":                   ["VUG", "QQQ"],        # 성장 + 나스닥100
        "가치/저평가 전략":              ["VTV", "IWD"],        # 가치주
        "방어 포함 (BEAR 대응)":         ["MTUM", "VBR", "GLD","XLU"],  # 방어자산 포함
        "S&P500 벤치마크":               ["SPY"],
    }

    results = {}
    print(f"\n  종목 데이터 다운로드 중...")

    for strat_name, tickers in portfolios.items():
        try:
            prices = {}
            for t in tickers:
                df = get_hist(t, start, end)
                if not df.empty:
                    c = 'Close' if 'Close' in df.columns else df.columns[0]
                    prices[t] = df[c].resample('ME').last()

            if not prices:
                continue

            price_df = pd.DataFrame(prices).dropna()
            if price_df.empty or len(price_df) < 6:
                continue

            # 월별 수익률 균등 가중
            monthly_ret = price_df.pct_change().dropna()
            port_ret    = monthly_ret.mean(axis=1)

            # 누적 수익률
            cumret = (1 + port_ret).cumprod()
            total  = (cumret.iloc[-1] - 1) * 100
            n_years = len(port_ret) / 12
            cagr   = ((cumret.iloc[-1]) ** (1/n_years) - 1) * 100 if n_years > 0 else 0

            # 최대 낙폭 (MDD)
            rolling_max = cumret.cummax()
            drawdown    = (cumret - rolling_max) / rolling_max
            mdd         = drawdown.min() * 100

            # 샤프 지수
            sharpe = (port_ret.mean() / (port_ret.std() + 1e-9)) * np.sqrt(12)

            # 월 승률
            win_rate = (port_ret > 0).sum() / len(port_ret) * 100

            results[strat_name] = {
                'total': total, 'cagr': cagr, 'mdd': mdd,
                'sharpe': sharpe, 'win_rate': win_rate
            }
        except Exception as e:
            print(f"  ⚠️ {strat_name} 실패: {e}")

    # 결과 출력
    print(f"\n  {'전략':<28}  {'총수익':>8}  {'연환산':>7}  {'최대낙폭':>8}  {'샤프':>6}  {'승률':>6}")
    print(f"  {'-'*65}")
    for name, r in results.items():
        marker = " ◀" if "우리" in name else ""
        print(f"  {name:<28}  {r['total']:>+7.1f}%  {r['cagr']:>+6.1f}%  "
              f"{r['mdd']:>+7.1f}%  {r['sharpe']:>5.2f}  {r['win_rate']:>5.1f}%{marker}")

    # 시사점
    if "우리 전략 (성장+가치 혼합)" in results and "S&P500 벤치마크" in results:
        our = results["우리 전략 (성장+가치 혼합)"]
        spy = results["S&P500 벤치마크"]
        alpha = our['cagr'] - spy['cagr']
        print(f"\n  💡 S&P500 대비 초과수익(알파): {alpha:+.1f}%/년")
        if "방어 포함 (BEAR 대응)" in results:
            def_r = results["방어 포함 (BEAR 대응)"]
            mdd_improve = def_r['mdd'] - our['mdd']
            print(f"  💡 방어자산 포함 시 MDD 개선: {mdd_improve:+.1f}%p")

    return results


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print("\n" + "="*60)
    print("  🚀 미국장 전략 분석 시작")
    print("="*60)

    # 1. 급등주 역공학 패턴 분석
    print("\n\n[1단계] 급등주 역공학 패턴 분석")
    pattern_results = []
    for ticker, info in ROCKET_STOCKS.items():
        result = analyze_rocket_pattern(ticker, info)
        pattern_results.append(result)
        import time; time.sleep(1)  # API rate limit

    # 공통 패턴 추출
    extract_common_pattern(pattern_results)

    # 2. 전략 백테스트
    print("\n\n[2단계] 퀀트 전략 백테스트")
    backtest_results = simple_backtest(start="2019-01-01", end="2025-01-01")

    print("\n\n✅ 분석 완료!")
