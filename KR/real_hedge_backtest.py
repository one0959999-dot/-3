"""실제 봇 헤지 검증 — 봇의 '진짜' BEAR 감지기(get_market_regime_detail)로 다시 돌린다.

사용자 지적 2건:
1. 횡보=가만히 보유 말고, 박스권 스윙으로 주식수 늘려 상승장 대비.
2. 하락 국면 봇 코드의 헤지 전략(실제 로직)을 비교한 게 맞나?

저니 문서의 기존 결론("헤지<보유, 차이는 감지정확도")은 classify8(종목별 8단계) 감지기 기준이었음.
봇의 *실제* 헤지는 strategy.get_market_regime_detail = KODEX200 지수 7신호 점수(-7~+7)
+ ADX/연속일/RSI 필터로 BEAR/BULL/NEUTRAL 판정 → BEAR면 방어바스켓(인20%+달13%+금7%)+현금60%.
이 '진짜 감지기'로 돌리면 결론이 바뀌는지 본다.

비교:
  A) 단순보유
  B) 봇실제헤지   : 지수 실제 BEAR 감지 → 방어바스켓40%+현금60% (전환비용+룩어헤드제거)
  C) B+횡보스윙   : NEUTRAL 구간에 박스권스윙(주식수 누적), BULL=보유, BEAR=방어
  D) classify8헤지: 기존 저니 결론 재현용(종목별 deep-bear → 방어바스켓) — B와 차이=감지기 효과

원칙: OOS 2021~ · 전환비용 · 룩어헤드 제거(어제 국면으로 오늘 포지션) · 표본 천천히 확대.
실행: python KR/real_hedge_backtest.py [N]      # N=종목수(기본 5)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from KR.regime_period_backtest import _ohlc, _yf, SAMPLE_KR, COST
from KR.phase_judge_pilot import classify8_series
from KR.range_accumulation_backtest import accumulation_swing

OOS = '2021-01-01'
INDEX_TICKER = '069500'          # 봇이 쓰는 KODEX200 (strategy.get_market_regime_detail)
DEEP_BEAR8 = {'PANIC', 'BEAR_EARLY', 'BEAR_MID'}   # classify8 깊은하락(참고 D용)

# 방어바스켓 = 인버스20% + 달러13% + 금7% (+현금60%) — strategy.DEFENSIVE_ASSETS와 동일
DEF_WEIGHTS = [('114800.KS', 0.20), ('130730.KS', 0.13), ('132030.KS', 0.07)]
DEF_CASH = 0.60


# ──────────────────────────────────────────────────────────────────────────
# 1. 봇의 '진짜' BEAR 감지기를 일별 시계열로 재현 (strategy.py 공식 그대로 벡터화)
# ──────────────────────────────────────────────────────────────────────────
def _rsi_series(c, n=14):
    """strategy._calc_regime_score의 RSI(단순이동평균 방식) 그대로."""
    d = c.diff()
    g = d.clip(lower=0).rolling(n).mean()
    lo = (-d.clip(upper=0)).rolling(n).mean()
    return 100 - 100 / (1 + g / (lo + 1e-10))


def _regime_score_series(c):
    """strategy._calc_regime_score 7신호 점수를 일별 시계열로(룩어헤드 없음: t까지만 사용)."""
    ma5, ma20, ma60 = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(60).mean()
    rsi = _rsi_series(c)
    ret22 = (c / c.shift(22) - 1) * 100
    s = pd.Series(0.0, index=c.index)
    s += np.where(c > ma5, 1, -1)                       # S1
    s += np.where(ma5 > ma5.shift(3), 1, -1)            # S2
    s += np.where(c > ma20, 1, -1)                      # M1
    s += np.where(ma20 > ma20.shift(5), 1, -1)          # M2
    s += np.where(rsi > 55, 1, np.where(rsi < 45, -1, 0))   # M3
    s += np.where(ma20 > ma60, 1, -1)                   # L1
    s += np.where(ret22 > 3.0, 1, np.where(ret22 < -3.0, -1, 0))  # L2
    return s, rsi, ret22


def _adx_series(df, period=14):
    """strategy._calc_adx 공식 그대로(단순이동평균 ADX) 시계열로."""
    high, low, close = df['high'], df['low'], df['close']
    tr = pd.concat([high - low, (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs()], axis=1).max(axis=1)
    up_move, down_move = high.diff(), -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr = tr.rolling(period).mean()
    plus_di = 100 * plus_dm.rolling(period).mean() / (atr + 1e-10)
    minus_di = 100 * minus_dm.rolling(period).mean() / (atr + 1e-10)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    return dx.rolling(period).mean(), plus_di, minus_di


def _up_streak_series(c):
    """연속 상승일 수 — strategy._get_up_streak 시계열판."""
    up = (c.diff() > 0)
    return up * (up.groupby((~up).cumsum()).cumcount() + 1)


def bot_regime_series():
    """봇 실제 BEAR/BULL/NEUTRAL 일별 판정(KODEX200). strategy.get_market_regime_detail 규칙층 재현.
    (AI 오버레이는 백테스트 불가라 규칙층만 — 봇도 claude 없으면 이 경로)"""
    df = _ohlc(INDEX_TICKER)
    if df is None:
        df = _yf('^KS11')      # 폴백: 코스피지수
        if df is None:
            return pd.Series(dtype=object)
    score, rsi, _ = _regime_score_series(df['close'])
    adx, plus_di, minus_di = _adx_series(df)
    streak = _up_streak_series(df['close'])

    reg = pd.Series('NEUTRAL', index=df.index)
    bear = (score <= -4) & (adx >= 20) & ~((adx >= 50) & (minus_di > 40))
    bull = (score >= 5) & (streak < 8) & (rsi < 80)
    reg[bear] = 'BEAR'
    reg[bull] = 'BULL'
    return reg


# ──────────────────────────────────────────────────────────────────────────
# 2. 방어바스켓 일별 수익 (인20%+달13%+금7%, 현금60%=0)
# ──────────────────────────────────────────────────────────────────────────
def defensive_basket_ret():
    r = None
    for sym, w in DEF_WEIGHTS:
        d = _yf(sym)
        if d is None:
            continue
        part = w * d['close'].pct_change()
        r = part if r is None else r.add(part, fill_value=0)
    return r if r is not None else pd.Series(dtype=float)


# ──────────────────────────────────────────────────────────────────────────
# 3. 매매방식별 일별 포트 수익 → OOS 복리/MDD
# ──────────────────────────────────────────────────────────────────────────
def _box_swing_weight(df):
    """박스권 스윙 비중(0/1): 볼린저 하단 진입 ~ 상단 이탈. (phase_method_tournament.w_swing_bb)
    NEUTRAL 구간에 적용 시 주식수 누적효과를 비중타이밍으로 근사."""
    c = df['close']; ma = c.rolling(20).mean(); sd = c.rolling(20).std()
    lo, up = ma - 2 * sd, ma + 2 * sd
    e = (c < lo).fillna(False).values; x = (c > up).fillna(False).values
    h = False; out = np.zeros(len(c))
    for i in range(len(c)):
        if not h and e[i]: h = True
        elif h and x[i]: h = False
        out[i] = 1.0 if h else 0.0
    return pd.Series(out, index=c.index)


def _oos(eq, idx):
    v = eq[idx >= OOS]
    if len(v) < 30:
        return None
    ret = (v.iloc[-1] / v.iloc[0] - 1) * 100
    peak = v.cummax(); mdd = ((v / peak - 1) * 100).min()
    return round(float(ret), 1), round(float(mdd), 1)


def _sim(stock_ret, w_stock, w_basket, basket_ret):
    """일별 포트수익 = w_stock*종목 + w_basket*방어바스켓 - 비중변동분 전환비용. 누적 equity."""
    idx = stock_ret.index
    ws = w_stock.reindex(idx).ffill().fillna(0.0)
    wb = w_basket.reindex(idx).ffill().fillna(0.0)
    br = basket_ret.reindex(idx).fillna(0.0)
    turn = ws.diff().abs().fillna(0) + wb.diff().abs().fillna(0)
    r = ws * stock_ret + wb * br - turn * COST
    return (1 + r).cumprod()


def run(stocks):
    basket = defensive_basket_ret()
    reg_idx = bot_regime_series()
    agg = {'A_단순보유': [], 'B_봇실제헤지': [], 'C_봇헤지+누적스윙': [], 'D_classify8헤지': []}
    rows = []
    done = 0
    for code, name in stocks:
        df = _ohlc(code)
        if df is None or len(df) < 400:
            print(f"  - {name}: 데이터부족 건너뜀"); continue
        idx = df.index
        sr = df['close'].pct_change().fillna(0.0)

        # 봇 지수국면(어제 기준 → 룩어헤드 제거)
        reg = reg_idx.reindex(idx).ffill().shift(1)
        is_bear = (reg == 'BEAR')
        is_bull = (reg == 'BULL')
        is_neut = ~is_bear & ~is_bull

        # A) 단순보유
        eq_a = (1 + sr).cumprod()
        # B) 봇실제헤지: BEAR→방어40%/현금60%, 그외 보유
        ws_b = (~is_bear).astype(float)
        wb_b = is_bear.astype(float) * 0.40
        eq_b = _sim(sr, ws_b, wb_b, basket)
        # C) B + 진짜 누적스윙: BEAR→방어바스켓, 그외→accumulation_swing(주식수 누적·돌파시 전량보유)
        #    accumulation_swing은 이미 ADX<gate(박스권)만 트림/추가, 돌파시 전량보유 → 사용자 의도 그대로
        acc_eq, _sh, _cs, _lp = accumulation_swing(df)
        acc_ret = acc_eq.pct_change().fillna(0.0)
        br_c = basket.reindex(idx).fillna(0.0)
        bear_today = is_bear.reindex(idx).fillna(False)
        turn_c = bear_today.astype(int).diff().abs().fillna(0)
        r_c = np.where(bear_today.values, 0.40 * br_c.values, acc_ret.values) - turn_c.values * COST
        eq_c = pd.Series((1 + r_c).cumprod(), index=idx)
        # D) classify8 헤지(참고): 종목별 deep-bear → 방어바스켓
        ph8 = classify8_series(df)
        is_bear8 = ph8.isin(DEEP_BEAR8).reindex(idx).fillna(False).shift(1).fillna(False)
        ws_d = (~is_bear8).astype(float)
        wb_d = is_bear8.astype(float) * 0.40
        eq_d = _sim(sr, ws_d, wb_d, basket)

        row = [name]
        for k, e in [('A_단순보유', eq_a), ('B_봇실제헤지', eq_b),
                     ('C_봇헤지+누적스윙', eq_c), ('D_classify8헤지', eq_d)]:
            m = _oos(e, idx)
            if m:
                agg[k].append(m); row.append(f"{m[0]:+.0f}%/{m[1]:.0f}")
            else:
                row.append("-")
        rows.append(row)
        done += 1
        print(f"  ✓ {name}")
    return agg, rows, done


def report(agg, rows, n):
    L = [""]
    L.append(f"🔬 실제 봇 헤지 검증 (KR {n}종목, OOS {OOS}~, 룩어헤드제거+전환비용)")
    L.append("   봇 진짜 BEAR감지기(KODEX200 7신호) + 방어바스켓(인20/달13/금7)+현금60")
    L.append("=" * 64)
    L.append(f"{'전략':18} {'수익중앙':>9} {'MDD중앙':>9} {'양(+)':>7} {'보유이김':>8}")
    L.append("-" * 64)
    hold = agg['A_단순보유']
    hold_med = np.median([x[0] for x in hold]) if hold else 0
    for k, lst in agg.items():
        if not lst:
            L.append(f"{k:18} {'표본부족':>9}"); continue
        rets = [x[0] for x in lst]; mdds = [x[1] for x in lst]
        win = sum(1 for r in rets if r > 0)
        beat = sum(1 for r in rets if r > hold_med) if k != 'A_단순보유' else '-'
        L.append(f"{k:18} {np.median(rets):>+8.0f}% {np.median(mdds):>+8.0f}% "
                 f"{win:>4}/{len(rets):<2} {str(beat):>8}")
    L.append("-" * 64)
    L.append("종목별 (수익%/MDD):")
    L.append(f"  {'종목':10} {'보유':>11} {'봇헤지':>11} {'누적스윙':>11} {'cls8':>11}")
    for r in rows:
        L.append(f"  {r[0]:10} {r[1]:>11} {r[2]:>11} {r[3]:>11} {r[4]:>11}")
    L.append("=" * 64)
    L.append("판독: B>A면 봇 실제감지기로는 헤지가 통함(기존 classify8 결론 반전).")
    L.append("      C>B면 횡보스윙(주식수누적)이 추가가치. D는 기존 저니 결론 재현용.")
    return "\n".join(L)


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
    stocks = SAMPLE_KR[:n]
    print(f"▶ 실제 봇 헤지 백테스트 시작 — {n}종목 (천천히 확대)")
    agg, rows, done = run(stocks)
    print(report(agg, rows, done))
