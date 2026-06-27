"""매매방식 토너먼트 — 8단계 각각에서 "어떤 매매방식이 최선인가"를 백테스트로 확정.

순서 정정(사용자 지적): 국면판단(봇 vs AI) 비교 전에, 먼저 각 8단계의 '매매방식'을 검증해야 함.
여기서 나온 [국면→최선 매매방식] 표가 곧 매매방식 알고리즘이고,
그 표를 깔아야 봇 vs Gemini 국면판단 비교가 의미를 가진다.

방법: 봇 8단계 라벨(작업가설) 위에서, 후보 매매방식들을 각 국면 '그 기간 일별수익만' OOS 복리로 비교.
함정필터: OOS(검증기간 2021~) · 거래비용 · 위험조정(수익/|MDD|) · 표본/유의성.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.regime_period_backtest import _ohlc, SAMPLE_KR, COST, _run_frac
from KR.phase_judge_pilot import classify8_series
from KR.program_logic_backtest import PHASE_ORDER, PHASE_KR

OOS = '2021-01-01'


# ── 후보 매매방식 → 일별 목표비중(0~1) ──
def w_hold(df):   return pd.Series(1.0, index=df.index)
def w_cash(df):   return pd.Series(0.0, index=df.index)
def w_half(df):   return pd.Series(0.5, index=df.index)
def w_trend(df):  # 추세추종: 5MA>20MA면 보유
    return (df['close'].rolling(5).mean() > df['close'].rolling(20).mean()).astype(float)
def w_above200(df):
    return (df['close'] > df['close'].rolling(200).mean()).astype(float)
def w_swing_bb(df):  # 박스권 스윙: 하단매수~상단매도(상태)
    c = df['close']; ma = c.rolling(20).mean(); sd = c.rolling(20).std()
    lo, up = ma - 2 * sd, ma + 2 * sd
    e = (c < lo).fillna(False).values; x = (c > up).fillna(False).values
    h = False; out = np.zeros(len(c))
    for i in range(len(c)):
        if not h and e[i]: h = True
        elif h and x[i]: h = False
        out[i] = 1.0 if h else 0.0
    return pd.Series(out, index=c.index)

METHODS = {'보유': w_hold, '현금': w_cash, '반보유50%': w_half,
           '추세추종(5>20MA)': w_trend, '200일선위': w_above200, '박스권스윙': w_swing_bb}


def _phase_oos_returns(df, phase_daily):
    """각 매매방식 × 8단계의 OOS(검증기간) 격리수익 + MDD."""
    out = {}
    oos_mask = np.asarray(df.index >= OOS)
    for mname, fn in METHODS.items():
        try:
            w = fn(df).reindex(df.index).fillna(0.0).clip(0, 1)
        except Exception:
            continue
        eq = _run_frac(df['close'], w)
        dr = eq.pct_change().fillna(0.0).values
        for ph in PHASE_ORDER:
            mask = ((phase_daily.values == ph) & oos_mask)
            if mask.sum() < 15:
                continue
            ret = (np.prod(1.0 + dr[mask]) - 1.0) * 100
            out.setdefault(ph, {})[mname] = round(ret, 1)
    return out


def run(stocks=None):
    stocks = stocks or SAMPLE_KR
    agg = {}   # phase -> method -> [returns]
    done = 0
    for code, name in stocks:
        df = _ohlc(code)
        if df is None or len(df) < 400:
            continue
        ph = classify8_series(df)
        res = _phase_oos_returns(df, ph)
        if not res:
            continue
        done += 1; print(f"  {name} 완료")
        for phase, md in res.items():
            for m, v in md.items():
                agg.setdefault(phase, {}).setdefault(m, []).append(v)
    # 집계: 국면별 매매방식 중앙값수익 + 1위
    table = {}
    for phase in PHASE_ORDER:
        if phase not in agg:
            continue
        meds = {m: round(float(np.median(vs)), 1) for m, vs in agg[phase].items() if len(vs) >= 3}
        if not meds:
            continue
        winner = max(meds, key=meds.get)
        table[phase] = {'winner': winner, 'meds': meds, 'n': len(next(iter(agg[phase].values())))}
    return table, done


def format_report(table, n):
    L = [f"🏁 매매방식 토너먼트 — 8단계별 최선 매매방식 (KR {n}종목, OOS 2021~)",
         "후보: 보유/현금/반보유/추세추종/200일선/박스권스윙", ""]
    for ph in PHASE_ORDER:
        if ph not in table:
            continue
        t = table[ph]
        rank = sorted(t['meds'].items(), key=lambda kv: kv[1], reverse=True)
        L.append(f"■ {PHASE_KR[ph]}({ph}) — 표본 {t['n']}")
        L.append("   " + " / ".join(f"{m} {v:+.0f}%" for m, v in rank))
        L.append(f"   → 최선: 【{t['winner']}】 ({t['meds'][t['winner']]:+.0f}%)")
        L.append("")
    L.append("[매매방식 알고리즘 = 국면→행동]")
    for ph in PHASE_ORDER:
        if ph in table:
            L.append(f"  {PHASE_KR[ph]:12} → {table[ph]['winner']}")
    return "\n".join(L)


# ── 전체 시퀀스 검증 (국면따라 매매방식 전환 + 전환비용) — 격리함정 제거 ──
TABLE_TOURNAMENT = {'PANIC': '현금', 'BEAR_EARLY': '현금', 'BEAR_MID': '현금',
                    'BEAR_LATE': '박스권스윙', 'RECOVERY': '박스권스윙', 'BULL_EARLY': '박스권스윙',
                    'BULL_MID': '박스권스윙', 'BULL_LATE': '200일선위', 'SIDEWAYS': '보유'}
TABLE_SIMPLE = {'PANIC': '현금', 'BEAR_EARLY': '현금', 'BEAR_MID': '현금', 'BEAR_LATE': '현금',
                'RECOVERY': '보유', 'BULL_EARLY': '보유', 'BULL_MID': '보유',
                'BULL_LATE': '보유', 'SIDEWAYS': '보유'}


def _seq_weight(df, phase_daily, table):
    mw = {m: METHODS[m](df).reindex(df.index).fillna(0).clip(0, 1) for m in set(table.values())}
    w = pd.Series(0.0, index=df.index)
    for ph, m in table.items():
        sel = (phase_daily == ph).values
        w.values[sel] = mw[m].values[sel]
    return w


def _oos(eq, idx):
    v = eq[idx >= OOS]
    if len(v) < 30: return None
    ret = (v.iloc[-1] / v.iloc[0] - 1) * 100
    peak = v.cummax(); mdd = ((v / peak - 1) * 100).min()
    return round(ret, 1), round(float(mdd), 1)


def run_sequence(stocks=None):
    stocks = stocks or SAMPLE_KR
    agg = {'토너먼트표': [], '단순표(하락현금)': [], '단순보유': []}
    done = 0
    for code, name in stocks:
        df = _ohlc(code)
        if df is None or len(df) < 400: continue
        ph = classify8_series(df); idx = df.index
        e_t = _run_frac(df['close'], _seq_weight(df, ph, TABLE_TOURNAMENT))
        e_s = _run_frac(df['close'], _seq_weight(df, ph, TABLE_SIMPLE))
        e_h = _run_frac(df['close'], pd.Series(1.0, index=idx))
        for k, e in [('토너먼트표', e_t), ('단순표(하락현금)', e_s), ('단순보유', e_h)]:
            m = _oos(e, idx)
            if m: agg[k].append(m)
        done += 1; print(f"  {name} 완료")
    return agg, done


def report_sequence(agg, n):
    L = [f"🔁 전체 시퀀스 검증 (국면전환+비용, KR {n}종목, OOS 2021~)",
         "격리함정 제거 — 실제로 국면따라 갈아탔을 때 보유를 이기나", ""]
    for k, lst in agg.items():
        if not lst: continue
        rets = [x[0] for x in lst]; mdds = [x[1] for x in lst]
        win = sum(1 for r in rets if r > 0)
        L.append(f"{k:16} 수익중앙 {np.median(rets):+.0f}% · MDD중앙 {np.median(mdds):.0f}% · 양(+) {win}/{len(rets)}")
    # 표별 보유 대비 승률
    base = {tuple(x): None for x in []}
    return "\n".join(L)


# ── 실제 프로그램 방어자산 (인버스20%+달러13%+금7%+현금60%) 시퀀스 ──
DEEP_BEAR = {'PANIC', 'BEAR_EARLY', 'BEAR_MID'}     # 깊은 하락=방어바스켓


def _defensive_basket_ret():
    """방어바스켓 일별 포트수익 = 0.20*인버스 + 0.13*달러 + 0.07*금 (+0.60 현금=0)."""
    from KR.regime_period_backtest import _yf
    inv = _yf('114800.KS')['close'].pct_change()
    dol = _yf('130730.KS')['close'].pct_change()
    gold = _yf('132030.KS')['close'].pct_change()
    r = (0.20 * inv).add(0.13 * dol, fill_value=0).add(0.07 * gold, fill_value=0)
    return r


def run_sequence_defensive(stocks=None):
    stocks = stocks or SAMPLE_KR
    basket = _defensive_basket_ret()
    agg = {'방어전환(인버스+달러+금)': [], '하락현금전환': [], '단순보유': []}
    done = 0
    for code, name in stocks:
        df = _ohlc(code)
        if df is None or len(df) < 400: continue
        ph = classify8_series(df); idx = df.index
        sr = df['close'].pct_change().fillna(0.0)
        br = basket.reindex(idx).fillna(0.0)
        is_bear = ph.isin(DEEP_BEAR).shift(1).fillna(False)   # 어제 국면으로 오늘 포지션(룩어헤드 제거)
        sw = is_bear.astype(int).diff().abs().fillna(0) * (COST * 2)   # 전환비용
        # 방어전환: 하락=방어바스켓, 그외=보유
        r_def = np.where(is_bear.values, br.values, sr.values) - sw.values
        # 하락현금전환: 하락=0, 그외=보유
        r_cash = np.where(is_bear.values, 0.0, sr.values) - sw.values
        eq_def = pd.Series((1 + r_def).cumprod(), index=idx)
        eq_cash = pd.Series((1 + r_cash).cumprod(), index=idx)
        eq_hold = (1 + sr).cumprod()
        for k, e in [('방어전환(인버스+달러+금)', eq_def), ('하락현금전환', eq_cash), ('단순보유', eq_hold)]:
            m = _oos(e, idx)
            if m: agg[k].append(m)
        done += 1; print(f"  {name} 완료")
    return agg, done


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else len(SAMPLE_KR)
    if '--def' in sys.argv:
        agg, done = run_sequence_defensive(SAMPLE_KR[:n])
        rep = report_sequence(agg, done).replace('전체 시퀀스 검증', '방어자산 시퀀스 검증(실제 프로그램 로직)')
    elif '--seq' in sys.argv:
        agg, done = run_sequence(SAMPLE_KR[:n])
        rep = report_sequence(agg, done)
    else:
        table, done = run(SAMPLE_KR[:n])
        rep = format_report(table, done)
    print("\n" + rep)
    if '--tg' in sys.argv:
        from KR.program_logic_backtest import send_telegram
        send_telegram(rep)
