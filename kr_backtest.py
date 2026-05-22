"""
kr_backtest.py — 한국장 급등주 역공학 패턴 분석 + 전략 백테스트
──────────────────────────────────────────────────────────────
① 급등주 역공학: 에코프로, HLB, 알테오젠 등 — 급등 직전 공통 신호 추출
② 전략 백테스트: 현재 스크리너 로직 vs KOSPI 수익률 비교
③ 개선 포인트: 미국장 분석과 비교해 한국장 스크리너에 반영할 조건 도출
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings
import time
warnings.filterwarnings('ignore')

try:
    from pykrx import stock as pykrx_stock
except ImportError:
    print("❌ pykrx 없음. pip install pykrx")
    exit(1)

# ── 분석 대상 급등주 ──────────────────────────────────────────────
ROCKET_STOCKS_KR = {
    "086520": {"name": "에코프로",        "boom_start": "2023-01-01", "pre_start": "2021-06-01"},
    "028300": {"name": "HLB",             "boom_start": "2024-01-01", "pre_start": "2022-06-01"},
    "196170": {"name": "알테오젠",        "boom_start": "2024-01-01", "pre_start": "2022-06-01"},
    "277810": {"name": "레인보우로보틱스", "boom_start": "2023-01-01", "pre_start": "2021-06-01"},
    "022100": {"name": "포스코DX",        "boom_start": "2023-01-01", "pre_start": "2021-06-01"},
}

BENCHMARK_TICKER = "069500"  # KODEX200 (KOSPI 벤치마크)
BENCHMARK_NAME   = "KOSPI (KODEX200)"

# ── 유틸 ──────────────────────────────────────────────────────────
def get_ohlcv_kr(ticker: str, start: str, end: str = None) -> pd.DataFrame:
    """pykrx로 OHLCV 조회 (날짜 문자열 YYYY-MM-DD)"""
    end = end or datetime.today().strftime("%Y-%m-%d")
    s   = start.replace("-", "")
    e   = end.replace("-", "")
    try:
        time.sleep(0.3)
        df = pykrx_stock.get_market_ohlcv_by_date(s, e, ticker)
        if df is None or df.empty:
            return pd.DataFrame()
        df.rename(columns={
            '시가': 'Open', '고가': 'High', '저가': 'Low',
            '종가': 'Close', '거래량': 'Volume'
        }, inplace=True)
        df.index = pd.to_datetime(df.index)
        return df
    except Exception as e_:
        print(f"  ⚠️ {ticker} 조회 실패: {e_}")
        return pd.DataFrame()

def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / (loss + 1e-10))

def pct(a, b):
    if b and b != 0:
        return round((a / b - 1) * 100, 1)
    return None


# ══════════════════════════════════════════════════════════════════
# 1. 역공학 패턴 분석
# ══════════════════════════════════════════════════════════════════
def analyze_rocket_pattern_kr(ticker: str, info: dict) -> dict | None:
    name       = info["name"]
    boom_start = info["boom_start"]
    pre_start  = info["pre_start"]

    print(f"\n{'='*60}")
    print(f"  📊 [{name} / {ticker}] 역공학 패턴 분석")
    print(f"  분석 구간: {pre_start} ~ {boom_start} (급등 직전)")
    print(f"{'='*60}")

    df = get_ohlcv_kr(ticker, pre_start)
    if df.empty or len(df) < 60:
        print("  ❌ 데이터 부족")
        return None

    pre_df  = df[df.index < boom_start]
    post_df = df[df.index >= boom_start]
    if pre_df.empty or post_df.empty:
        print("  ❌ 구간 분리 실패")
        return None

    pre_price  = float(pre_df['Close'].iloc[-1])
    peak_price = float(post_df['Close'].max())
    now_price  = float(df['Close'].iloc[-1])
    rise_pct   = pct(peak_price, pre_price)

    print(f"\n  💰 가격 변화:")
    print(f"     급등 전:  {pre_price:,.0f}원")
    print(f"     최고가:   {peak_price:,.0f}원  ({rise_pct:+.0f}%)")
    print(f"     현재가:   {now_price:,.0f}원")

    close = pre_df['Close']

    # 이동평균 배열
    sma_20  = close.rolling(20).mean().iloc[-1]  if len(close) >= 20  else None
    sma_60  = close.rolling(60).mean().iloc[-1]  if len(close) >= 60  else None
    sma_120 = close.rolling(120).mean().iloc[-1] if len(close) >= 120 else None

    print(f"\n  📈 급등 직전 기술적 신호:")
    if sma_20 and sma_60:
        golden = pre_price > sma_20 > sma_60
        status = "✅ 정배열 (현재가>20일>60일)" if golden else "⚠️ 역배열"
        print(f"     이동평균:   {status}")
        print(f"                현재가 {pre_price:,.0f} / 20일 {sma_20:,.0f} / 60일 {sma_60:,.0f}")
        if sma_120:
            print(f"                120일 {sma_120:,.0f}  ({'위✅' if pre_price > sma_120 else '아래⚠️'})")

    rsi_val = None
    if len(close) >= 20:
        rsi_val = float(calc_rsi(close).iloc[-1])
        rsi_state = "과매수⚠️" if rsi_val > 70 else ("과매도✅" if rsi_val < 40 else "중립 ➡️")
        print(f"     RSI(14):    {rsi_val:.1f}  [{rsi_state}]")

    # 52주 위치
    high_52 = close.rolling(252).max().iloc[-1] if len(close) >= 252 else close.max()
    low_52  = close.rolling(252).min().iloc[-1] if len(close) >= 252 else close.min()
    pos_52  = (pre_price - low_52) / (high_52 - low_52 + 1e-9) * 100
    print(f"     52주 위치:  {pos_52:.0f}%  (0%=저점, 100%=고점)")

    # 모멘텀
    mom_6m = pct(pre_price, close.iloc[-126]) if len(close) >= 126 else None
    mom_3m = pct(pre_price, close.iloc[-63])  if len(close) >= 63  else None
    mom_1m = pct(pre_price, close.iloc[-20])  if len(close) >= 20  else None
    if mom_6m: print(f"     6개월 수익: {mom_6m:+.1f}%")
    if mom_3m: print(f"     3개월 수익: {mom_3m:+.1f}%")
    if mom_1m: print(f"     1개월 수익: {mom_1m:+.1f}%")

    # 거래량
    vol_ratio = None
    if 'Volume' in pre_df.columns and len(pre_df) >= 30:
        vol_recent = pre_df['Volume'].iloc[-20:].mean()
        vol_base   = pre_df['Volume'].iloc[-60:-20].mean()
        vol_ratio  = float(vol_recent / (vol_base + 1))
        vol_state  = "🔥 급증" if vol_ratio > 1.5 else ("📈 증가" if vol_ratio > 1.1 else "➡️ 보통")
        print(f"     거래량 비율: {vol_ratio:.2f}x  [{vol_state}]")

    # 20일 변동성
    if len(close) >= 20:
        std_20 = float(close.pct_change().rolling(20).std().iloc[-1]) * 100
        print(f"     20일 변동성: {std_20:.1f}%  ({'고변동⚠️' if std_20 > 3 else '저변동✅'})")

    return {
        'ticker': ticker, 'name': name,
        'pre_price': pre_price, 'peak_price': peak_price, 'rise_pct': rise_pct,
        'sma20': sma_20, 'sma60': sma_60,
        'golden': pre_price > (sma_20 or 0) > (sma_60 or 0),
        'rsi': rsi_val,
        'pos_52w': pos_52,
        'mom_6m': mom_6m,
        'mom_1m': mom_1m,
        'vol_ratio': vol_ratio,
    }


# ══════════════════════════════════════════════════════════════════
# 2. 공통 패턴 + 미국장 비교
# ══════════════════════════════════════════════════════════════════
def extract_and_compare(kr_results: list):
    valid = [r for r in kr_results if r]
    if not valid:
        return

    print(f"\n{'='*60}")
    print(f"  🔍 한국 급등주 공통 패턴 ({len(valid)}개)")
    print(f"{'='*60}")

    rises   = [r['rise_pct']  for r in valid if r.get('rise_pct')]
    rsis    = [r['rsi']       for r in valid if r.get('rsi')]
    pos52s  = [r['pos_52w']   for r in valid if r.get('pos_52w') is not None]
    mom6ms  = [r['mom_6m']    for r in valid if r.get('mom_6m')  is not None]
    mom1ms  = [r['mom_1m']    for r in valid if r.get('mom_1m')  is not None]
    vol_rs  = [r['vol_ratio'] for r in valid if r.get('vol_ratio') is not None]
    goldens = [r['golden']    for r in valid]

    print(f"\n  📊 급등폭:    평균 {np.mean(rises):+.0f}%  /  최소 {min(rises):+.0f}%  /  최대 {max(rises):+.0f}%")
    if rsis:
        print(f"  📊 RSI:       평균 {np.mean(rsis):.1f}  /  범위 {min(rsis):.1f} ~ {max(rsis):.1f}")
    if pos52s:
        low_z = sum(1 for p in pos52s if p < 40)
        mid_z = sum(1 for p in pos52s if 40 <= p < 70)
        hi_z  = sum(1 for p in pos52s if p >= 70)
        print(f"  📊 52주 위치: 평균 {np.mean(pos52s):.0f}%  →  저점권 {low_z}개 / 중간 {mid_z}개 / 고점권 {hi_z}개")
    if goldens:
        gc = sum(1 for g in goldens if g)
        print(f"  📊 정배열:    {gc}/{len(goldens)}개 이미 정배열")
    if vol_rs:
        print(f"  📊 거래량:    평균 {np.mean(vol_rs):.2f}x  (1.5x↑ {sum(1 for v in vol_rs if v>1.5)}개)")
    if mom1ms:
        print(f"  📊 1개월 모멘텀: 평균 {np.mean(mom1ms):+.1f}%")

    # 한국 vs 미국 비교
    print(f"\n{'='*60}")
    print(f"  🌏 한국장 vs 미국장 패턴 비교")
    print(f"{'='*60}")
    print(f"""
  항목             한국 급등주               미국 급등주
  ─────────────────────────────────────────────────────
  급등폭           평균 {np.mean(rises):+.0f}%               평균 +1399%
  52주 위치        평균 {np.mean(pos52s):.0f}%               평균 50%
  RSI              평균 {np.mean(rsis):.1f}                평균 63.8
  패턴 특징        테마 수급 중심              섹터 전환 + 실적
  진입 타이밍      테마 초기 거래량 폭발       저평가 발굴 or 모멘텀
  핵심 신호        거래량 급증 + 테마 키워드   PEG + 매출 성장률
    """)

    # 스크리너 개선 제안
    avg_pos  = np.mean(pos52s)  if pos52s  else 50
    avg_rsi  = np.mean(rsis)    if rsis    else 55
    avg_vol  = np.mean(vol_rs)  if vol_rs  else 1.2
    avg_mom1 = np.mean(mom1ms)  if mom1ms  else 0

    print(f"  💡 현재 스크리너 개선 포인트:")
    print(f"  {'='*50}")

    if avg_pos < 50:
        print(f"  ✅ 52주 저점 부근 ({avg_pos:.0f}%) → 저점 탈출 초기 포착 조건 강화")
    if avg_vol > 1.3:
        print(f"  ✅ 거래량 비율 평균 {avg_vol:.1f}x → 거래량 1.3x 이상 필터 유지·강화")
    if avg_rsi < 65:
        print(f"  ✅ RSI {avg_rsi:.1f} → 과매수 제외 조건 유효 (현재 RSI<80 필터 적절)")
    if avg_mom1 > 5:
        print(f"  ✅ 1개월 모멘텀 +{avg_mom1:.1f}% → 단기 상승 시작 신호 조건 추가 고려")

    print(f"\n  🎯 한국장 스크리너 추가 조건 제안:")
    print(f"     ① 52주 위치 15~60% 구간 우대 (저점 탈출 초기)")
    print(f"     ② 거래량 비율 1.3x 이상 (수급 유입 확인)")
    print(f"     ③ 1개월 모멘텀 +3~20% (막 상승 시작)")
    print(f"     ④ 테마 키워드 보너스 점수 현행 유지 (한국은 테마 드리븐)")
    print(f"     ⑤ 외인/기관 순매수 동반 여부 (현행 유지)")


# ══════════════════════════════════════════════════════════════════
# 3. 전략 백테스트
# ══════════════════════════════════════════════════════════════════
def kr_strategy_backtest(start: str = "2020-01-01", end: str = "2025-01-01"):
    """
    현재 스크리너 로직과 유사한 팩터 전략을 KOSPI 섹터 ETF로 시뮬레이션
    """
    print(f"\n{'='*60}")
    print(f"  📈 한국장 전략 백테스트 ({start} ~ {end})")
    print(f"{'='*60}")

    # 팩터 전략 대리 ETF (한국 상장)
    portfolios = {
        "모멘텀+거래량 전략 (위성 유사)":  ["091160", "139290", "091170"],  # KODEX 반도체, TIGER 2차전지, KODEX IT
        "섹터 분산 전략 (코어 유사)":       ["069500", "229200", "139290"],  # KOSPI+KOSDAQ+2차전지
        "방어 포함 전략":                   ["069500", "229200", "114800", "132030"],  # + 인버스 + 골드
        "KOSPI 벤치마크":                   ["069500"],
        "KOSDAQ 벤치마크":                  ["229200"],
    }

    results = {}
    print(f"\n  데이터 수집 중...")

    for strat_name, tickers in portfolios.items():
        try:
            prices = {}
            for t in tickers:
                df = get_ohlcv_kr(t, start, end)
                if not df.empty and 'Close' in df.columns:
                    prices[t] = df['Close'].resample('ME').last()

            if not prices:
                continue

            price_df = pd.DataFrame(prices).dropna()
            if price_df.empty or len(price_df) < 6:
                continue

            monthly_ret = price_df.pct_change().dropna()
            port_ret    = monthly_ret.mean(axis=1)
            cumret      = (1 + port_ret).cumprod()

            total   = (cumret.iloc[-1] - 1) * 100
            n_years = len(port_ret) / 12
            cagr    = ((cumret.iloc[-1]) ** (1/n_years) - 1) * 100 if n_years > 0 else 0

            rolling_max = cumret.cummax()
            mdd         = ((cumret - rolling_max) / rolling_max).min() * 100

            sharpe   = (port_ret.mean() / (port_ret.std() + 1e-9)) * np.sqrt(12)
            win_rate = (port_ret > 0).sum() / len(port_ret) * 100

            results[strat_name] = {
                'total': total, 'cagr': cagr, 'mdd': mdd,
                'sharpe': sharpe, 'win_rate': win_rate
            }
        except Exception as e:
            print(f"  ⚠️ {strat_name} 오류: {e}")

    if not results:
        print("  ❌ 백테스트 데이터 없음")
        return {}

    print(f"\n  {'전략':<30}  {'총수익':>8}  {'연환산':>7}  {'최대낙폭':>8}  {'샤프':>6}  {'승률':>6}")
    print(f"  {'-'*68}")
    for name, r in results.items():
        marker = " ◀" if "위성" in name or "코어" in name else ""
        print(f"  {name:<30}  {r['total']:>+7.1f}%  {r['cagr']:>+6.1f}%  "
              f"{r['mdd']:>+7.1f}%  {r['sharpe']:>5.2f}  {r['win_rate']:>5.1f}%{marker}")

    # 한국 vs 미국 비교
    kospi = results.get("KOSPI 벤치마크", {})
    if kospi:
        print(f"\n  💡 한국 vs 미국 시장 비교:")
        print(f"     KOSPI 연환산:   {kospi.get('cagr',0):+.1f}%  MDD: {kospi.get('mdd',0):+.1f}%")
        print(f"     S&P500 연환산:  +15.8%  MDD: -23.9%  (2019~2025 백테스트 기준)")

    return results


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
if __name__ == '__main__':
    print("\n" + "="*60)
    print("  🇰🇷 한국장 전략 분석 시작")
    print("="*60)

    # 1. 급등주 역공학
    print("\n[1단계] 한국 급등주 역공학 패턴 분석")
    kr_results = []
    for ticker, info in ROCKET_STOCKS_KR.items():
        r = analyze_rocket_pattern_kr(ticker, info)
        kr_results.append(r)
        time.sleep(0.5)

    # 2. 공통 패턴 + 미국 비교
    extract_and_compare(kr_results)

    # 3. 전략 백테스트
    print("\n\n[2단계] 한국장 전략 백테스트")
    kr_strategy_backtest(start="2020-01-01", end="2025-01-01")

    print("\n\n✅ 한국장 분석 완료!")
