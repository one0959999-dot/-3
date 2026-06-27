"""보유 초과수익(beat-hold) 자동 탐색기 — '무슨 수를 써서라도 보유를 이겨라'.

방향 전환: 방어/현금/박스권스윙은 노출을 줄여 상승을 놓침 → 보유 못 이김(검증완료).
보유를 이기려면 반대로: (1)좋은 국면에 노출 키우기(레버리지), (2)이기는 종목 집중(모멘텀 로테이션).

자동 탐색 대상(전부 워크포워드·룩어헤드 없음: 월말판단→다음달매매 + shift1):
 A. 레버리지 타이밍: 추세(MA)·봇국면·AI국면 신호로 노출 0~2배 조절
 B. 모멘텀 로테이션: 매월 트레일링 모멘텀 상위 K종목만 보유(+절대추세 필터)
 C. AI 자유노출: Gemini가 각 구간 목표노출(0/0.5/1/1.5/2배)을 직접 추천 → AI 최대능력
비교축: 봇 vs AI 누가 타이밍 잘 잡나. 과최적화 필터: 기간 2분할(전반/후반) 일관성.

실행: python KR/strategy_search.py [--telegram]
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from KR.walkforward_backtest import load, classify_phase_walkforward, START, PRINCIPAL, send_telegram

COST = 0.0021          # 편도 거래비용+세금
BORROW = 0.045         # 레버리지 차입 연이율(초과노출분)
SPLIT = '2022-01-01'   # 기간 2분할(robustness)
BULL = {'RECOVERY', 'BULL_EARLY', 'BULL_MID', 'BULL_LATE'}
DEEPBEAR = {'PANIC', 'BEAR_EARLY', 'BEAR_MID'}


# ──────────────────────────────────────────────────────────────────────────
# 공통: 시장 데이터 → EW 일별수익 / 종목수익 행렬 / 지수
# ──────────────────────────────────────────────────────────────────────────
def market_frames(data, mkt):
    stocks = data[mkt]; idx_df = data['index'][mkt]
    cal = idx_df.index[idx_df.index >= START]
    rets = {}
    for code, (name, df) in stocks.items():
        rets[code] = df['close'].pct_change().reindex(cal)
    R = pd.DataFrame(rets).reindex(cal)          # 종목 일별수익(NaN=상장전)
    ew = R.mean(axis=1).fillna(0.0)              # 동일가중 시장(일별)
    idx_close = idx_df['close'].reindex(cal).ffill()
    return cal, R, ew, idx_close, idx_df


def month_ends(cal):
    s = pd.Series(cal, index=cal)
    return [pd.Timestamp(d) for d in s.groupby([cal.year, cal.month]).last().values]


# ──────────────────────────────────────────────────────────────────────────
# 시뮬레이터
# ──────────────────────────────────────────────────────────────────────────
def sim_exposure(ew, expo_daily):
    """EW시장에 목표노출(0~2) 적용. expo_daily는 '그날 적용할 노출'(이미 룩어헤드 제거됨)."""
    e = expo_daily.reindex(ew.index).fillna(0.0)
    turn = e.diff().abs().fillna(e)
    borrow = np.maximum(e - 1.0, 0.0) * (BORROW / 252)
    daily = e * ew - turn * COST - borrow
    return (1 + daily).cumprod() * PRINCIPAL


def sim_rotation(cal, R, idx_close, K, lookL, abs_filter=False):
    """모멘텀 로테이션: 매 월말 트레일링 lookL개월 수익 상위 K종목 → 다음달 보유. abs_filter: 지수<MA200면 현금."""
    me = month_ends(cal)
    ma200 = idx_close.rolling(200).mean()
    # 월별 선정(월말 t 기준, 다음달 적용)
    sel_by_month = {}
    Lwin = lookL * 21
    for t in me:
        i = cal.get_loc(t)
        if i < Lwin:
            sel_by_month[t] = None; continue
        if abs_filter and not (idx_close.iloc[i] > ma200.iloc[i]):
            sel_by_month[t] = []          # 현금
            continue
        mom = (R.iloc[i - Lwin:i + 1] + 1).prod() - 1   # 트레일링 누적수익(룩어헤드 없음)
        mom = mom.dropna()
        if len(mom) == 0:
            sel_by_month[t] = None; continue
        sel_by_month[t] = list(mom.sort_values(ascending=False).head(K).index)
    # 일별 적용(다음달): 각 거래일 d → 직전 월말의 선정 사용
    me_sorted = sorted(sel_by_month.keys())
    daily_ret = pd.Series(0.0, index=cal)
    prev_sel = None; cur = None
    me_ptr = 0
    cost_acc = pd.Series(0.0, index=cal)
    for d in cal:
        # d 이전의 가장 최근 월말 선정 사용
        while me_ptr < len(me_sorted) and me_sorted[me_ptr] < d:
            cur = sel_by_month[me_sorted[me_ptr]]; me_ptr += 1
        if cur is None:
            daily_ret.loc[d] = 0.0; continue
        if cur == []:
            daily_ret.loc[d] = 0.0
        else:
            daily_ret.loc[d] = R.loc[d, cur].mean()
        # 리밸런싱 비용(선정 바뀐 날)
        if cur is not prev_sel:
            if prev_sel and cur and prev_sel != []:
                changed = len(set(cur) ^ set(prev_sel)) / max(len(cur), 1)
            else:
                changed = 1.0
            daily_ret.loc[d] -= changed * COST
            prev_sel = cur
    return (1 + daily_ret.fillna(0.0)).cumprod() * PRINCIPAL


# ── 노출 신호 생성기(월말→다음달, 룩어헤드 제거) ──
def expo_from_phase(phase_daily, cal, lev_bull, lev_neutral, lev_bear):
    """봇 국면 → 노출. 월말 국면을 다음달 적용."""
    me = month_ends(cal)
    p = phase_daily.reindex(cal).ffill()
    lab = {t: p.loc[t] for t in me}
    s = pd.Series(lab).sort_index().reindex(cal, method='ffill')
    def mp(ph):
        if ph in BULL: return lev_bull
        if ph in DEEPBEAR: return lev_bear
        return lev_neutral
    return s.map(mp).shift(1).fillna(0.0)


def expo_from_ma(idx_close, cal, maN, lev_up, lev_dn):
    ma = idx_close.rolling(maN).mean()
    e = pd.Series(np.where(idx_close > ma, lev_up, lev_dn), index=cal)
    # 월말→다음달
    me = month_ends(cal); lab = {t: e.loc[t] for t in me}
    return pd.Series(lab).sort_index().reindex(cal, method='ffill').shift(1).fillna(0.0)


# ── 지표 ──
def metrics(eq, cal):
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    yrs = len(eq) / 252
    cagr = ((eq.iloc[-1] / eq.iloc[0]) ** (1 / yrs) - 1) * 100 if yrs > 0 else 0
    mdd = float(((eq / eq.cummax() - 1) * 100).min())
    # 기간 2분할 수익
    h1 = eq[cal < SPLIT]; h2 = eq[cal >= SPLIT]
    r1 = (h1.iloc[-1] / h1.iloc[0] - 1) * 100 if len(h1) > 20 else 0
    r2 = (h2.iloc[-1] / h2.iloc[0] - 1) * 100 if len(h2) > 20 else 0
    return {'ret': ret, 'cagr': cagr, 'mdd': mdd, 'calmar': cagr / (abs(mdd) + 1e-9),
            'r1': r1, 'r2': r2, 'final': eq.iloc[-1]}


# ──────────────────────────────────────────────────────────────────────────
# AI 자유노출 (Gemini가 각 구간 목표노출 0~2배 추천, 날짜익명=사후지식 차단)
# ──────────────────────────────────────────────────────────────────────────
def ai_exposure(mkt, idx_close, cal, gem):
    me = [t for t in month_ends(cal) if cal.get_loc(t) >= 200]
    rows = []; tags = []
    for j, t in enumerate(me):
        i = cal.get_loc(t)
        c = idx_close
        r1 = (c.iloc[i] / c.iloc[i-21] - 1)*100 if i>=21 else 0
        r3 = (c.iloc[i] / c.iloc[i-63] - 1)*100 if i>=63 else 0
        r6 = (c.iloc[i] / c.iloc[i-126] - 1)*100 if i>=126 else 0
        r12 = (c.iloc[i] / c.iloc[i-252] - 1)*100 if i>=252 else 0
        vsma200 = (c.iloc[i] / c.iloc[max(0,i-200):i+1].mean() - 1)*100
        tag = f"P{j+1:03d}"; tags.append((tag, t))
        rows.append(f"{tag}: 1M{r1:+.0f}% 3M{r3:+.0f}% 6M{r6:+.0f}% 12M{r12:+.0f}% 200MA대비{vsma200:+.0f}%")
    prompt = (
        "너는 공격적 포트폴리오 매니저다. 장기 누적수익을 '최대화'하는 게 목표다.\n"
        "각 구간의 추세를 보고 주식 목표노출을 정하라. 강한 상승추세=레버리지(최대 2.0), "
        "약세/하락=축소(0.0~0.5), 보통=1.0. (미래정보 없음, 추세만으로 판단)\n"
        "노출 선택지: 0.0 / 0.5 / 1.0 / 1.5 / 2.0\n"
        "구간 데이터(시간순, 날짜익명):\n" + "\n".join(rows) +
        "\n\n반드시 형식대로 모든 구간 한 줄씩(설명금지):\nP###=EXPOSURE")
    txt = gem.generate_content(prompt, temperature=0.2)
    t2d = {t: d for t, d in tags}
    lab = {}
    for m in re.finditer(r'(P\d{3})\s*[=:]\s*([0-9.]+)', txt):
        tag, v = m.group(1), m.group(2)
        try:
            fv = float(v)
        except Exception:
            continue
        if tag in t2d and fv in (0.0, 0.5, 1.0, 1.5, 2.0):
            lab[t2d[tag]] = fv
    if not lab:
        return pd.Series(1.0, index=cal), 0
    s = pd.Series(lab).sort_index().reindex(cal, method='ffill').shift(1).fillna(0.0)
    return s, len(lab)


# ──────────────────────────────────────────────────────────────────────────
# 탐색 실행
# ──────────────────────────────────────────────────────────────────────────
def search_market(data, mkt, gem=None):
    cal, R, ew, idx_close, idx_df = market_frames(data, mkt)
    bot_phase = classify_phase_walkforward(idx_df, data.get('vix'))
    results = {}

    # 벤치마크
    results['보유(1x)'] = metrics(sim_exposure(ew, pd.Series(1.0, index=cal)), cal)
    # A. 레버리지 타이밍
    results['항상2x'] = metrics(sim_exposure(ew, pd.Series(2.0, index=cal)), cal)
    for maN in (120, 150, 200):
        for L in (1.5, 2.0):
            e = expo_from_ma(idx_close, cal, maN, L, 0.0)
            results[f'추세MA{maN}>→{L}x/현금'] = metrics(sim_exposure(ew, e), cal)
        e = expo_from_ma(idx_close, cal, maN, 2.0, 1.0)
        results[f'추세MA{maN}>→2x/유지1x'] = metrics(sim_exposure(ew, e), cal)
    # 봇 국면 레버리지
    for tag, (lb, ln, lbear) in {
        '봇:불2x/중1x/하현금': (2.0, 1.0, 0.0),
        '봇:불1.5x/중1x/하현금': (1.5, 1.0, 0.0),
        '봇:불2x/중1x/하1x': (2.0, 1.0, 1.0),
    }.items():
        e = expo_from_phase(bot_phase, cal, lb, ln, lbear)
        results[tag] = metrics(sim_exposure(ew, e), cal)
    # B. 모멘텀 로테이션
    for K in (3, 5, 10):
        for L in (3, 6, 12):
            results[f'모멘텀 top{K}/{L}M'] = metrics(sim_rotation(cal, R, idx_close, K, L), cal)
            results[f'모멘텀 top{K}/{L}M+추세필터'] = metrics(sim_rotation(cal, R, idx_close, K, L, abs_filter=True), cal)
    # C. AI 자유노출
    ai_calls = 0
    if gem is not None:
        e_ai, n = ai_exposure(mkt, idx_close, cal, gem); ai_calls = 1
        results[f'★AI자유노출(라벨{n})'] = metrics(sim_exposure(ew, e_ai), cal)
    return results, ai_calls


def report(data, gem):
    L = []
    L.append("🔎 보유 초과수익 자동탐색 (레버리지·모멘텀·봇/AI타이밍·AI자유노출)")
    L.append(f"원금 {PRINCIPAL//10000:,}만 · {START}~ · 코스피/코스닥 · 룩어헤드제거 · 기간2분할 robustness")
    L.append("=" * 70)
    for mkt in ('KOSPI', 'KOSDAQ'):
        if not data[mkt] or data['index'][mkt] is None:
            L.append(f"\n[{mkt}] 데이터부족"); continue
        res, _ = search_market(data, mkt, gem)
        hold = res['보유(1x)']
        rows = sorted(res.items(), key=lambda kv: kv[1]['cagr'], reverse=True)
        L.append(f"\n[{mkt}]  (보유 기준: 누적 {hold['ret']:+.0f}% · 연{hold['cagr']:+.0f}% · MDD{hold['mdd']:.0f}%)")
        L.append(f"  {'전략':24} {'누적%':>8} {'연%':>6} {'MDD':>6} {'Calmar':>6} {'전반/후반':>14} {'vs보유':>7}")
        for name, m in rows:
            beat = m['ret'] - hold['ret']
            robust = '✓' if (m['r1'] > hold['r1'] and m['r2'] > hold['r2']) else ('반' if (m['r1'] > hold['r1'] or m['r2'] > hold['r2']) else '✗')
            star = '🟢' if beat > 0 else '  '
            L.append(f"{star}{name:24} {m['ret']:>+8.0f} {m['cagr']:>+6.0f} {m['mdd']:>+6.0f} {m['calmar']:>6.1f} "
                     f"{m['r1']:>+6.0f}/{m['r2']:>+6.0f} {beat:>+7.0f}[{robust}]")
        # 봇 vs AI 타이밍 요약
        bot_best = max((m['ret'] for n, m in res.items() if n.startswith('봇:')), default=0)
        ai_item = [(n, m) for n, m in res.items() if n.startswith('★AI')]
        if ai_item:
            ai_ret = ai_item[0][1]['ret']
            L.append(f"  → 타이밍 우열: 봇레버리지 최고 {bot_best:+.0f}% vs AI자유노출 {ai_ret:+.0f}% · "
                     f"{'AI 우세' if ai_ret>bot_best else '봇 우세'}")
    L.append("\n" + "=" * 70)
    L.append("판독: 🟢=보유초과 · [✓]=전·후반 모두 보유초과(robust, 신뢰) · [반]=한 기간만 · [✗]=둘다 미달")
    L.append("⚠️ 과최적화 경계: [✓]아닌 초과는 한 기간 운빨 가능. 생존편향(보유 과대평가)·단일국가 한계.")
    return "\n".join(L)


if __name__ == '__main__':
    gem = None
    if '--ai' in sys.argv or '--telegram' in sys.argv:
        try:
            from base.database import get_db_connection
            from ai.gemini_api import GeminiApi
            conn = get_db_connection()
            key = conn.execute("SELECT gemini_api_key FROM users WHERE gemini_api_key IS NOT NULL "
                               "AND gemini_api_key!='' LIMIT 1").fetchone()
            conn.close()
            gem = GeminiApi(key['gemini_api_key'] if key else '')
        except Exception as e:
            print("AI 비활성:", e)
    data = load()
    rep = report(data, gem)
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
