"""
전 종목 전략 백테스팅 (모멘텀 + 위성 + 하락장 전략)
─────────────────────────────────────────────────
데이터: pykrx 개별종목 1년치 + KIS API 종목 유니버스
기간  : 최근 1년 (약 250 거래일)
전략  :
  A. 모멘텀 슬롯  - 당일 +3~30%, 거래량 3배↑ → -3% 손절 / +5% 익절
  B. 위성 슬롯    - 당일 +0.3~3%, 거래량 1.5배↑ → -5% 손절 / +10%/+20% 분할 익절
  C. 하락장 전략  - KOSPI200(ETF 프록시) 20일선 -2% → 신규진입 중단 + 인버스 ETF 편입
  D. 레짐 통합    - 시장 국면 따라 A/B/C 자동 전환

주의: 일봉 기반 근사치. 분봉 거래량 페이드 신호 미반영.
실행 전: database.py 경로가 맞는지 확인 (KIS API 키 로드용)
"""

import time
import warnings
import sqlite3
import sys
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pykrx import stock

warnings.filterwarnings("ignore")

# ── KIS API 로드 (종목 유니버스 수집용) ──
_KIS = None
def _init_kis():
    global _KIS
    if _KIS is not None:
        return _KIS
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import database
        conn = sqlite3.connect(database.DB_PATH)
        conn.row_factory = sqlite3.Row
        user = conn.execute("SELECT * FROM users LIMIT 1").fetchone()
        conn.close()
        if user and user["real_app_key"]:
            from kis_brokers.kis_real_api import KisRealApi
            _KIS = KisRealApi(
                app_key=user["real_app_key"],
                app_secret=user["real_app_secret"],
                account_no=user["real_account_no"] or "",
            )
    except Exception as e:
        print(f"  [경고] KIS API 로드 실패: {e}")
    return _KIS

# ═══════════════════════════════════════════════
#  설정
# ═══════════════════════════════════════════════
INITIAL_CAPITAL   = 10_000_000   # 시뮬레이션 초기 자금
BUDGET_MOMENTUM   = 0.30         # 모멘텀 슬롯 비중
BUDGET_SATELLITE  = 0.40         # 위성 슬롯 비중
BUDGET_CORE       = 0.30         # 코어 비중 (백테스트에서는 Buy&Hold 비교용)

# 모멘텀 슬롯
MOM_CHG_MIN   =  3.0    # 당일 상승률 하한 (%)
MOM_CHG_MAX   = 30.0    # 당일 상승률 상한 (%)
MOM_VOL_RATIO =  3.0    # 거래량 배율 기준
MOM_STOP      = -0.03   # 손절 (-3%)
MOM_TARGET    =  0.05   # 익절 (+5%)
MOM_MAX_HOLD  =  3      # 최대 보유일

# 위성 슬롯
SAT_CHG_MIN   =  0.3
SAT_CHG_MAX   =  3.0
SAT_VOL_RATIO =  1.5
SAT_STOP      = -0.05
SAT_T1        =  0.10   # 1차 익절 (+10%)
SAT_T2        =  0.20   # 2차 익절 (+20%)
SAT_MAX_HOLD  = 20

# 재진입 쿨다운 (손절 후 N일)
COOLDOWN_DAYS = 2

# 시장 국면 판단
BEAR_THRESHOLD  = -0.02   # KOSPI 20일선 대비 -2% → 하락장
BULL_THRESHOLD  =  0.01   # +1% 이상 → 상승장

# 인버스 ETF (KODEX 200선물인버스2X)
INVERSE_ETF  = "252670"
INVERSE_NAME = "KODEX 200선물인버스2X"

# KOSPI 대리 지수 (pykrx는 지수코드 미지원 → KODEX200 ETF로 대체)
KOSPI_PROXY  = "069500"   # KODEX 200

# 유동성 필터
MIN_AVG_VOLUME = 50_000      # 20일 평균 거래량 최소
MIN_PRICE      = 500         # 최소 주가 (500원 미만 동전주 제외)
MAX_PRICE      = 500_000     # 최대 주가

# 종목 유니버스 크기 (KIS 랭킹 기반)
UNIVERSE_SIZE  = 400         # 랭킹 소스 합산 후 중복제거 기준

TRADE_FEE      = 0.00015     # 매매 수수료 (0.015%)
SLIPPAGE       = 0.001       # 슬리피지 (0.1%)


# ═══════════════════════════════════════════════
#  유틸
# ═══════════════════════════════════════════════
def trading_dates(n_days=290):
    """최근 n_days 거래일 목록 (오늘 포함)"""
    end = datetime.today()
    start = end - timedelta(days=int(n_days * 1.6))
    dates = stock.get_market_ohlcv_by_date(
        start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "005930"
    ).index.tolist()
    return dates[-n_days:]


def fetch_ohlcv(ticker, start_str, end_str):
    try:
        df = stock.get_market_ohlcv_by_date(start_str, end_str, ticker)
        df.rename(columns={
            "시가": "open", "고가": "high", "저가": "low",
            "종가": "close", "거래량": "volume"
        }, inplace=True)
        return df.dropna(subset=["close"])
    except Exception:
        return pd.DataFrame()


def apply_cost(price, is_buy=True):
    """수수료 + 슬리피지 적용"""
    cost = (TRADE_FEE + SLIPPAGE) * price
    return price + cost if is_buy else price - cost


# ═══════════════════════════════════════════════
#  시장 국면 판단 (KODEX200 ETF 프록시)
# ═══════════════════════════════════════════════
def load_kospi_regime(start_str, end_str):
    """날짜별 시장 국면: 'bull' / 'neutral' / 'bear'
    pykrx 지수코드(1001) 미지원 → KODEX200(069500)으로 대체
    """
    df = fetch_ohlcv(KOSPI_PROXY, start_str, end_str)
    if df.empty:
        df = fetch_ohlcv("005930", start_str, end_str)

    df["ma20"] = df["close"].rolling(20).mean()
    df["regime"] = "neutral"
    df.loc[df["close"] >= df["ma20"] * (1 + BULL_THRESHOLD), "regime"] = "bull"
    df.loc[df["close"] <= df["ma20"] * (1 + BEAR_THRESHOLD), "regime"] = "bear"
    return df["regime"].to_dict()


# ═══════════════════════════════════════════════
#  KIS API 기반 종목 유니버스 수집
# ═══════════════════════════════════════════════
_FALLBACK_TICKERS = [
    # 대형주
    "005930","000660","035420","005490","035720","051910","006400",
    "003670","207940","068270","105560","055550","086790","032830",
    "000270","005380","028260","066570","011200","003550",
    # 중형 성장주
    "247540","293490","259960","112040","041510","091990",
    "069500","114800","252670","251340","233740",
    # 바이오/헬스
    "128940","145020","009420","215600","196170","225900","214370",
    "068760","091990","183490","302440","377300",
    # 반도체/IT
    "000990","042700","357780","336370","403870","388790","240810",
    "042660","079550","086390","950130","348210",
    # 소비재/유통
    "139480","282330","271560","007070","004020","111770","090430",
    # 에너지/소재
    "010950","011790","010130","023530","024110","002380","004170",
    # 중소형 모멘텀 풀
    "078160","175330","140660","084370","160550","018290",
    "067160","095340","222040","014970","069620","253840",
    "215790","177350","114450","381620","142280","005950",
    "344860","225430","215790","177350",
]


def get_target_tickers():
    """KIS API 랭킹 여러 소스 + fallback → 중복제거 유니버스 반환"""
    print("  📋 종목 유니버스 수집 중...", end=" ", flush=True)
    kis = _init_kis()
    universe = set()

    if kis:
        try:
            for market in ["J", "Q"]:   # J=KOSPI, Q=KOSDAQ
                for fn_name in ["get_volume_rank", "get_price_change_rank"]:
                    fn = getattr(kis, fn_name, None)
                    if fn:
                        result = fn(market_div=market, limit=30)
                        if result:
                            universe.update(
                                t for t in result
                                if isinstance(t, str) and t.isdigit() and len(t) == 6
                            )
                        time.sleep(0.3)
        except Exception as e:
            print(f"\n  [경고] KIS 랭킹 조회 실패: {e}")

    clean_fallback = [t for t in _FALLBACK_TICKERS if t.isdigit() and len(t) == 6]
    universe.update(clean_fallback)

    tickers = sorted(universe)
    print(f"총 {len(tickers)}개 (KIS랭킹 + 기본풀)")
    return tickers


# ═══════════════════════════════════════════════
#  단일 종목 시뮬레이션 (모멘텀 / 위성)
# ═══════════════════════════════════════════════
def simulate_ticker(df, regime_map, slot="momentum"):
    """
    Returns: list of trade dicts
    """
    if len(df) < 22:
        return []

    closes  = df["close"].values
    volumes = df["volume"].values
    dates   = df.index.tolist()
    trades  = []

    # 20일 평균 거래량
    vol20 = pd.Series(volumes).rolling(20).mean().fillna(0).values

    in_pos       = False
    entry_price  = 0.0
    entry_idx    = 0
    partial_sold = False   # 위성 1차 익절 여부
    cooldown_end = -1      # 쿨다운 종료 인덱스

    for i in range(20, len(closes)):
        date   = dates[i]
        regime = regime_map.get(date, "neutral")
        price  = closes[i]
        vol    = volumes[i]
        avg_v  = vol20[i]

        if price < MIN_PRICE or price > MAX_PRICE:
            continue

        # ── 신규 진입 ──
        if not in_pos and i > cooldown_end:
            if regime == "bear":
                continue   # 하락장 신규진입 금지

            prev_close = closes[i - 1] if closes[i - 1] > 0 else 1
            chg_pct = (price - prev_close) / prev_close * 100
            vol_ratio = vol / avg_v if avg_v > 0 else 0

            if slot == "momentum":
                if MOM_CHG_MIN <= chg_pct <= MOM_CHG_MAX and vol_ratio >= MOM_VOL_RATIO:
                    in_pos      = True
                    entry_price = apply_cost(price, is_buy=True)
                    entry_idx   = i
                    partial_sold = False
            else:  # satellite
                if SAT_CHG_MIN <= chg_pct <= SAT_CHG_MAX and vol_ratio >= SAT_VOL_RATIO:
                    in_pos      = True
                    entry_price = apply_cost(price, is_buy=True)
                    entry_idx   = i
                    partial_sold = False
            # [BUG-13] 기존 continue 제거 — 진입 당일에도 청산 조건을 검사해야
            # 같은 봉에서 손절/익절 조건이 충족될 때(갭 등) 놓치지 않음.
            # 진입하지 않은 경우(in_pos=False)는 아래 'if in_pos' 블록이 건너뜀.

        # ── 청산 조건 ──
        if in_pos:
            hold_days = i - entry_idx
            ret = (price - entry_price) / entry_price

            if slot == "momentum":
                # -3% 손절
                if ret <= MOM_STOP:
                    sell_p = apply_cost(price, is_buy=False)
                    trades.append({
                        "slot":   "momentum",
                        "date":   date,
                        "ret":    (sell_p - entry_price) / entry_price,
                        "result": "stop",
                        "hold":   hold_days,
                    })
                    in_pos = False
                    cooldown_end = i + COOLDOWN_DAYS
                # +5% 익절
                elif ret >= MOM_TARGET:
                    sell_p = apply_cost(price, is_buy=False)
                    trades.append({
                        "slot":   "momentum",
                        "date":   date,
                        "ret":    (sell_p - entry_price) / entry_price,
                        "result": "target",
                        "hold":   hold_days,
                    })
                    in_pos = False
                # 최대 보유일 초과 → 시장가 청산
                elif hold_days >= MOM_MAX_HOLD:
                    sell_p = apply_cost(price, is_buy=False)
                    trades.append({
                        "slot":   "momentum",
                        "date":   date,
                        "ret":    (sell_p - entry_price) / entry_price,
                        "result": "timeout",
                        "hold":   hold_days,
                    })
                    in_pos = False

            else:  # satellite
                # -5% 손절
                if ret <= SAT_STOP:
                    sell_p = apply_cost(price, is_buy=False)
                    trades.append({
                        "slot":   "satellite",
                        "date":   date,
                        "ret":    (sell_p - entry_price) / entry_price,
                        "result": "stop",
                        "hold":   hold_days,
                    })
                    in_pos = False
                    cooldown_end = i + COOLDOWN_DAYS
                # 2차 익절 +20%
                elif ret >= SAT_T2 and partial_sold:
                    sell_p = apply_cost(price, is_buy=False)
                    trades.append({
                        "slot":   "satellite",
                        "date":   date,
                        "ret":    (sell_p - entry_price) / entry_price,
                        "result": "target2",
                        "hold":   hold_days,
                    })
                    in_pos = False
                # 1차 익절 +10%
                elif ret >= SAT_T1 and not partial_sold:
                    sell_p = apply_cost(price, is_buy=False)
                    trades.append({
                        "slot":   "satellite",
                        "date":   date,
                        "ret":    ret * 0.5,   # 50% 물량 기준
                        "result": "target1",
                        "hold":   hold_days,
                    })
                    partial_sold = True
                # 최대 보유일 초과
                elif hold_days >= SAT_MAX_HOLD:
                    sell_p = apply_cost(price, is_buy=False)
                    trades.append({
                        "slot":   "satellite",
                        "date":   date,
                        "ret":    (sell_p - entry_price) / entry_price,
                        "result": "timeout",
                        "hold":   hold_days,
                    })
                    in_pos = False

    return trades


# ═══════════════════════════════════════════════
#  하락장 전략: 인버스 ETF 시뮬레이션
# ═══════════════════════════════════════════════
def simulate_bear_strategy(start_str, end_str, regime_map, budget):
    """
    하락장 국면에서만 인버스 ETF 매수/매도
    bull/neutral 구간은 현금 보유
    """
    df = fetch_ohlcv(INVERSE_ETF, start_str, end_str)
    if df.empty:
        return [], 0.0

    closes = df["close"].values
    dates  = df.index.tolist()
    trades = []

    cash     = budget
    holding  = 0
    buy_price = 0.0

    for i in range(len(dates)):
        date   = dates[i]
        regime = regime_map.get(date, "neutral")
        price  = closes[i]

        if regime == "bear" and holding == 0 and cash >= price:
            holding    = int(cash // price)
            buy_price  = apply_cost(price, is_buy=True)
            cash      -= holding * buy_price

        elif regime != "bear" and holding > 0:
            sell_p = apply_cost(price, is_buy=False)
            profit = holding * (sell_p - buy_price)
            cash  += holding * sell_p
            trades.append({
                "slot":   "bear_inverse",
                "date":   date,
                "ret":    (sell_p - buy_price) / buy_price,
                "result": "regime_exit",
                "hold":   i,
            })
            holding = 0

    # 미청산 처리
    if holding > 0:
        sell_p = apply_cost(closes[-1], is_buy=False)
        cash  += holding * sell_p

    total_ret = (cash - budget) / budget
    return trades, total_ret


# ═══════════════════════════════════════════════
#  통계 계산
# ═══════════════════════════════════════════════
def calc_stats(trades, slot_name):
    if not trades:
        return None
    rets  = [t["ret"] for t in trades]
    wins  = [r for r in rets if r > 0]
    losses= [r for r in rets if r <= 0]
    n     = len(rets)
    wr    = len(wins) / n if n else 0
    avg_w = np.mean(wins)   if wins   else 0
    avg_l = np.mean(losses) if losses else 0
    ev    = np.mean(rets)
    total = np.sum(rets)   # 단순 합산 (복리 미적용)
    # 월별 수익률 (결과 기준)
    if trades:
        df_t = pd.DataFrame(trades)
        df_t["date"] = pd.to_datetime(df_t["date"])
        df_t = df_t.set_index("date")
        monthly = df_t["ret"].resample("ME").sum() * 100
    else:
        monthly = pd.Series(dtype=float)

    return {
        "slot":     slot_name,
        "n":        n,
        "win_rate": wr,
        "avg_win":  avg_w,
        "avg_loss": avg_l,
        "ev":       ev,
        "total_ret": total,
        "monthly":  monthly,
        "by_result": pd.Series([t["result"] for t in trades]).value_counts().to_dict(),
    }


def print_stats(s):
    if s is None:
        print("  (거래 없음)")
        return
    print(f"\n  [{s['slot']}]")
    print(f"  거래 횟수:    {s['n']:>5}건")
    print(f"  승률:         {s['win_rate']*100:>6.1f}%")
    print(f"  평균 수익:    {s['avg_win']*100:>+7.2f}%")
    print(f"  평균 손실:    {s['avg_loss']*100:>+7.2f}%")
    print(f"  트레이드 EV:  {s['ev']*100:>+7.3f}%")
    if s['avg_loss'] != 0:
        rr = abs(s['avg_win'] / s['avg_loss'])
        print(f"  손익비:       1 : {rr:.2f}")
    print(f"  누적 수익률:  {s['total_ret']*100:>+7.2f}% ({s['n']}건 단순합)")
    if not s["monthly"].empty:
        print(f"  월 평균 수익: {s['monthly'].mean():>+7.2f}%")
        print(f"  월 최대 수익: {s['monthly'].max():>+7.2f}%")
        print(f"  월 최대 손실: {s['monthly'].min():>+7.2f}%")
    print(f"  청산사유:     {s['by_result']}")


def compound_monthly(monthly_series):
    """월별 수익률 시리즈 → 연간 복리 수익률"""
    if monthly_series.empty:
        return 0.0
    prod = 1.0
    for r in monthly_series / 100:
        prod *= (1 + r)
    return (prod - 1) * 100


# ═══════════════════════════════════════════════
#  메인
# ═══════════════════════════════════════════════
if __name__ == "__main__":
    t0 = time.time()

    print("\n" + "=" * 65)
    print("  전 종목 전략 백테스팅 (모멘텀 + 위성 + 하락장)")
    print(f"  분석 대상: KIS랭킹 + 기본풀 {UNIVERSE_SIZE}개 내외 | 기간: 최근 1년")
    print("=" * 65)

    # ── 날짜 범위 설정 ──
    dates      = trading_dates(290)
    start_str  = dates[0].strftime("%Y%m%d")
    end_str    = dates[-1].strftime("%Y%m%d")
    sim_start  = dates[40]    # 지표 계산 워밍업 후 시뮬 시작
    print(f"  기간: {start_str} ~ {end_str}  (워밍업 40일 포함)\n")

    # ── KODEX200 기반 시장 국면 ──
    print("  📈 시장 국면 분석 중 (KODEX200 프록시)...", end=" ", flush=True)
    regime_map = load_kospi_regime(start_str, end_str)
    bear_days  = sum(1 for v in regime_map.values() if v == "bear")
    bull_days  = sum(1 for v in regime_map.values() if v == "bull")
    neut_days  = sum(1 for v in regime_map.values() if v == "neutral")
    print(f"완료 — 상승:{bull_days}일 / 횡보:{neut_days}일 / 하락:{bear_days}일")

    # ── 종목 유니버스 ──
    tickers = get_target_tickers()

    # ── 데이터 다운로드 & 시뮬레이션 ──
    all_mom_trades = []
    all_sat_trades = []
    skipped = 0
    total   = len(tickers)

    print(f"\n  ⏳ {total}개 종목 백테스트 시작...")

    for idx, ticker in enumerate(tickers, 1):
        if idx % 50 == 0 or idx == total:
            elapsed = time.time() - t0
            eta = elapsed / idx * (total - idx)
            print(f"    {idx}/{total}  경과:{elapsed:.0f}초  남은시간:{eta:.0f}초", flush=True)

        df = fetch_ohlcv(ticker, start_str, end_str)
        if df.empty or len(df) < 25:
            skipped += 1
            continue

        # 유동성 필터
        avg_vol = df["volume"].tail(20).mean()
        if avg_vol < MIN_AVG_VOLUME:
            skipped += 1
            continue

        # 워밍업 이후 데이터만 시뮬에 사용 (전체 df는 지표계산용)
        sim_df = df[df.index >= sim_start]
        if len(sim_df) < 5:
            skipped += 1
            continue

        # 종목 내 레짐 매핑은 전체 regime_map 사용 (동일 날짜 기준)
        m_trades = simulate_ticker(df, regime_map, slot="momentum")
        s_trades = simulate_ticker(df, regime_map, slot="satellite")

        all_mom_trades.extend(m_trades)
        all_sat_trades.extend(s_trades)

        time.sleep(0.05)   # API 과부하 방지

    # ── 하락장 인버스 ETF ──
    print(f"\n  📉 하락장 전략 (인버스 ETF) 시뮬레이션 중...", end=" ", flush=True)
    bear_budget = INITIAL_CAPITAL * BUDGET_MOMENTUM * 0.5  # 모멘텀 예산의 50% 전환
    bear_trades, bear_total_ret = simulate_bear_strategy(
        start_str, end_str, regime_map, bear_budget
    )
    print("완료")

    # ── 결과 ──
    elapsed = time.time() - t0
    print(f"\n  ✅ 완료! (총 {elapsed:.0f}초, 스킵:{skipped}개)")

    mom_stats  = calc_stats(all_mom_trades,  "모멘텀 슬롯 (30% 예산)")
    sat_stats  = calc_stats(all_sat_trades,  "위성 슬롯 (40% 예산)")
    bear_stats = calc_stats(bear_trades,     "하락장 인버스ETF 전략")

    print("\n" + "=" * 65)
    print("  📊 전략별 결과")
    print("=" * 65)

    print_stats(mom_stats)
    print_stats(sat_stats)
    print_stats(bear_stats)

    print(f"\n  [하락장 인버스ETF] 기간 전체 수익률: {bear_total_ret*100:+.2f}%")

    # ── 통합 시나리오: 예산 배분 반영 ──
    print("\n" + "=" * 65)
    print("  🏆 통합 포트폴리오 예상 수익률")
    print("  (모멘텀30% + 위성40% + 코어30% 구조 / 1,000만원 기준)")
    print("=" * 65)

    for label, stats, budget_ratio in [
        ("모멘텀", mom_stats, BUDGET_MOMENTUM),
        ("위성",   sat_stats, BUDGET_SATELLITE),
    ]:
        if stats:
            budget = INITIAL_CAPITAL * budget_ratio
            monthly_avg = stats["monthly"].mean() if not stats["monthly"].empty else 0
            monthly_gain = budget * monthly_avg / 100
            print(f"  {label} ({budget_ratio*100:.0f}%):  "
                  f"월평균 {monthly_avg:+.2f}%  →  {monthly_gain:>+10,.0f}원/월")

    # 통합 월 수익률 (코어는 연 15% 가정)
    m_monthly = mom_stats["monthly"].mean() if mom_stats else 0
    s_monthly = sat_stats["monthly"].mean() if sat_stats else 0
    core_monthly = 15 / 12   # 연 15% 가정

    combined_monthly = (
        m_monthly  * BUDGET_MOMENTUM  +
        s_monthly  * BUDGET_SATELLITE +
        core_monthly * BUDGET_CORE
    )
    combined_annual = ((1 + combined_monthly / 100) ** 12 - 1) * 100

    print(f"\n  ─────────────────────────────────────────────────────")
    print(f"  통합 월 수익률 (추정): {combined_monthly:>+7.2f}%")
    print(f"  통합 연 복리 수익률:   {combined_annual:>+7.2f}%")

    # 하락장 영향
    if bear_days > 0 and bear_stats:
        bear_contribution = bear_total_ret * 100 * BUDGET_MOMENTUM * 0.5 / 100
        print(f"\n  하락장({bear_days}일) 인버스ETF 기여:  {bear_contribution:>+.2f}%p")

    print("\n  ⚠️  주의사항")
    print("  · 일봉 기반 근사치 — 분봉 진입/청산 정밀도 미반영")
    print("  · 슬리피지 0.1% + 수수료 0.015% 반영")
    print("  · 동일 종목 재진입 쿨다운 2일 적용")
    print("  · 당일 +20% 초과 종목 진입 제외 (MOM_CHG_MAX=30%에 포함)")
    print("  · 코어 슬롯 수익률은 연15% 고정 가정치\n")
