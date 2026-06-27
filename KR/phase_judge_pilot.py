"""국면 판단 주체 비교 파일럿 — 봇(classify_phase 8단계) vs Gemini, 그 라벨로 백테스트.

파이프라인(BACKTEST_TODO 사용자 5단계):
 1. 같은 종목들에 봇 8단계 판단 + Gemini 8단계 판단 (월별, 룩어헤드 없음)
 2. 각자 라벨 위에 국면별 전략(program_logic) 백테스트
 3. OOS(검증기간)로 누구 판단이 더 좋은 매매결과를 내나 측정 → 국면판단 알고리즘
함정 거르기: 룩어헤드 제거(과거데이터만) · OOS 분리 · 거래비용 · 표본/유의성 경고.
종목수는 점차 확대(5→15→30→40). Gemini만 사용(배칭 1종목=1호출).
"""
import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from KR.regime_period_backtest import _ohlc, _yf, COST, _run_frac
from KR.program_logic_backtest import (_simulate, PHASE_ORDER, PHASE_KR, PHASE2REG3,
                                       send_telegram, SAMPLE_KR)
from base.market_phase import _adx

OOS_SPLIT = '2021-01-01'
PILOT = SAMPLE_KR[:5]


# ── 봇 8단계: 임의 종목 시계열에 classify_phase 트리 적용 (시장/종목/섹터 공용) ──
def classify8_series(df):
    c, h, l = df['close'].astype(float), df['high'].astype(float), df['low'].astype(float)
    ma60 = c.rolling(60).mean(); ma120 = c.rolling(120).mean(); ma200 = c.rolling(200).mean()
    mom20 = (c / c.shift(20) - 1) * 100; mom60 = (c / c.shift(60) - 1) * 100
    vs52 = (c / c.rolling(252).max() - 1) * 100
    adx = _adx(h, l, c); slope = (ma200 / ma200.shift(20) - 1) * 100
    out = []
    for i in range(len(c)):
        m200 = ma200.iloc[i]
        if pd.isna(m200) or i < 200:
            out.append('UNKNOWN'); continue
        cur = c.iloc[i]; m20s = mom20.iloc[i]; m60s = mom60.iloc[i]
        ax = adx.iloc[i]; sl = slope.iloc[i]; v52 = vs52.iloc[i]
        if m20s < -12: ph = 'PANIC'
        elif cur < m200 * 0.92 and m20s < -5: ph = 'BEAR_MID'
        elif cur < m200 and m60s < -15: ph = 'BEAR_MID'
        elif cur < m200 and m20s < -3: ph = 'BEAR_EARLY'
        elif cur < m200 and ax < 18: ph = 'BEAR_LATE'
        elif cur > m200 and m20s > 3 and m60s < -10: ph = 'RECOVERY'
        elif cur > m200 and sl > 0:
            if v52 > -5: ph = 'BULL_LATE'
            elif cur > ma60.iloc[i] > ma120.iloc[i] and ax > 25:
                ph = 'BULL_EARLY' if (m20s > 0 and m60s < 15) else 'BULL_MID'
            else: ph = 'BULL_MID'
        else: ph = 'SIDEWAYS'
        out.append(ph)
    return pd.Series(out, index=c.index)


def monthly_points(df):
    """월말 시점(과거 200일 이상 확보된 것만)."""
    idx = df.index
    months = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).last().values
    pts = [pd.Timestamp(d) for d in months]
    return [d for d in pts if df.index.get_loc(d) >= 200]


VALID_PHASES = set(PHASE_ORDER) | {'RECOVERY'}


def gemini_judge(name, df, points, gem):
    """Gemini가 각 월시점 8단계 판단 — 트레일링(과거) 지표만 제공(룩어헤드 없음). 1종목=1호출."""
    c = df['close'].astype(float)
    rows = []
    for d in points:
        i = df.index.get_loc(d)
        w = c.iloc[max(0, i - 252):i + 1]
        r1 = (c.iloc[i] / c.iloc[i - 21] - 1) * 100 if i >= 21 else 0
        r3 = (c.iloc[i] / c.iloc[i - 63] - 1) * 100 if i >= 63 else 0
        r6 = (c.iloc[i] / c.iloc[i - 126] - 1) * 100 if i >= 126 else 0
        r12 = (c.iloc[i] / c.iloc[i - 252] - 1) * 100 if i >= 252 else 0
        vs52 = (c.iloc[i] / w.max() - 1) * 100
        vsma200 = (c.iloc[i] / c.iloc[max(0, i - 200):i + 1].mean() - 1) * 100
        rows.append(f"{d.strftime('%Y-%m')}: 1M{r1:+.0f}% 3M{r3:+.0f}% 6M{r6:+.0f}% 12M{r12:+.0f}% "
                    f"52주고점대비{vs52:+.0f}% 200MA대비{vsma200:+.0f}%")
    prompt = (
        f"너는 시장 국면 분석가다. 종목 '{name}'의 월별 추세 데이터를 보고 각 월의 국면을 8단계 중 하나로 판단하라.\n"
        "8단계: PANIC(공포급락) BEAR_EARLY(하락초기) BEAR_MID(하락중반) BEAR_LATE(하락말기/바닥) "
        "RECOVERY(회복초입) BULL_EARLY(상승초입) BULL_MID(상승중반) BULL_LATE(상승말기/고점) SIDEWAYS(횡보)\n"
        "각 월의 데이터(해당 월까지의 과거 추세만, 미래정보 없음):\n" + "\n".join(rows) +
        "\n\n반드시 아래 형식으로 모든 월에 대해 한 줄씩만 출력(설명 금지):\nYYYY-MM=PHASE")
    txt = gem.generate_content(prompt, temperature=0.2)
    labels = {}
    for m in re.finditer(r'(\d{4}-\d{2})\s*[=:]\s*([A-Z_]+)', txt):
        ph = m.group(2)
        if ph in VALID_PHASES:
            labels[m.group(1)] = ph
    return labels


def _label_series_to_daily(df, monthly_labels):
    """월 라벨 dict('YYYY-MM'->phase) → 일별 phase 시리즈(ffill)."""
    ser = pd.Series('UNKNOWN', index=df.index)
    for d in df.index:
        key = d.strftime('%Y-%m')
        if key in monthly_labels:
            ser.loc[d] = monthly_labels[key]
    ser = ser.replace('UNKNOWN', np.nan).ffill().fillna('SIDEWAYS')
    return ser


def _oos_metrics(eq, df):
    """검증기간(OOS) 수익률 + MDD."""
    v = eq[df.index >= OOS_SPLIT]
    if len(v) < 30:
        return None
    ret = (v.iloc[-1] / v.iloc[0] - 1) * 100
    peak = v.cummax(); mdd = ((v / peak - 1) * 100).min()
    return round(ret, 1), round(float(mdd), 1)


def run_pilot(stocks=None, send=False):
    from base.database import get_db_connection
    from ai.gemini_api import GeminiApi
    conn = get_db_connection()
    key = conn.execute("SELECT gemini_api_key FROM users WHERE gemini_api_key IS NOT NULL "
                       "AND gemini_api_key!='' LIMIT 1").fetchone()
    conn.close()
    gem = GeminiApi(key['gemini_api_key'] if key else '')
    stocks = stocks or PILOT

    agree_all, results, calls = [], [], 0
    for code, name in stocks:
        df = _ohlc(code)
        if df is None or len(df) < 300:
            print(f"  {name} 데이터부족"); continue
        bot_daily = classify8_series(df)
        pts = monthly_points(df)
        bot_m = {d.strftime('%Y-%m'): bot_daily.loc[d] for d in pts if bot_daily.loc[d] != 'UNKNOWN'}
        gem_m = gemini_judge(name, df, pts, gem); calls += 1
        # 일치율 (공통 월)
        common = [k for k in bot_m if k in gem_m]
        agree = sum(1 for k in common if PHASE2REG3.get(bot_m[k]) == PHASE2REG3.get(gem_m[k]))
        agree_pct = round(100 * agree / len(common), 0) if common else 0
        agree_all.append(agree_pct)
        # 각 라벨로 백테스트 (OOS)
        bot_ser = _label_series_to_daily(df, bot_m)
        gem_ser = _label_series_to_daily(df, gem_m)
        eq_bot = _simulate(df, bot_ser, 'phase')
        eq_gem = _simulate(df, gem_ser, 'phase')
        eq_hold = _run_frac(df['close'], pd.Series(1.0, index=df.index))
        mb, mg, mh = _oos_metrics(eq_bot, df), _oos_metrics(eq_gem, df), _oos_metrics(eq_hold, df)
        results.append({'name': name, 'agree': agree_pct, 'n': len(common),
                        'bot': mb, 'gem': mg, 'hold': mh,
                        'gem_labels': len(gem_m)})
        print(f"  {name}: 일치율 {agree_pct}% (공통 {len(common)}월) / Gemini라벨 {len(gem_m)}개")
    return results, agree_all, calls


def format_report(results, agree_all, calls):
    L = ["🤖 국면판단 파일럿: 봇(classify_phase) vs Gemini",
         f"종목 {len(results)} · Gemini호출 {calls}회 · OOS검증기간 {OOS_SPLIT}~", ""]
    if agree_all:
        L.append(f"평균 국면일치율(3분류 기준): {round(np.mean(agree_all),0)}%")
    L.append("")
    L.append("[종목별 OOS 수익률(MDD) — 국면전용 전략]")
    L.append(f"{'종목':10} 일치% | 봇라벨        Gemini라벨     단순보유")
    win_bot = win_gem = 0
    for r in results:
        def fmt(m): return f"{m[0]:+.0f}%({m[1]:+.0f})" if m else "  -  "
        L.append(f"{r['name']:10} {r['agree']:>4.0f} | {fmt(r['bot']):14} {fmt(r['gem']):14} {fmt(r['hold'])}")
        if r['bot'] and r['gem']:
            # 위험조정(수익/|MDD|)으로 승자
            sb = r['bot'][0] / (abs(r['bot'][1]) + 1); sg = r['gem'][0] / (abs(r['gem'][1]) + 1)
            if sb > sg: win_bot += 1
            elif sg > sb: win_gem += 1
    L.append("")
    L.append(f"[국면판단 승자(위험조정 OOS)] 봇 {win_bot}종목 vs Gemini {win_gem}종목")
    L.append("⚠️ 파일럿(소표본) — 유의성 낮음. 확대(15→30→40)로 검증 필요.")
    return "\n".join(L)


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5
    results, agree_all, calls = run_pilot(SAMPLE_KR[:n])
    rep = format_report(results, agree_all, calls)
    print("\n" + rep)
    if '--tg' in sys.argv:
        send_telegram(rep)
