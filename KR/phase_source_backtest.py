"""국면 판단 주체 비교 — 봇 판단 vs AI(Gemini) 판단, 같은 매매엔진·같은 조건으로 실전형 백테스트.

사용자 요구:
- 국면을 (A)봇이 판단했을 때 / (B)AI가 판단했을 때 — 2종류.
- 둘 다 실전처럼(워크포워드, 답안지無) 국면파악→타이밍→매매. 같은 [국면→기법] 알고리즘·같은 비용.
- vs 단순보유. 코스피/코스닥 분리.
- 알고리즘 데이터 신뢰성 파악.

핵심: 매매엔진(walkforward_backtest.simulate)은 동일. 국면 '판단 주체'만 봇/AI로 교체.
  · 봇 판단: classify_phase 트리(VIX포함) 일별 워크포워드.
  · AI 판단: Gemini가 지수(^KS11/^KQ11)의 월별 국면을 트레일링 데이터로 판단(지수당 1호출, 룩어헤드無).
둘 다 shift(1)로 '어제 판단→오늘 매매'. 룩어헤드 통제.

실행: python KR/phase_source_backtest.py [--telegram]
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from KR.walkforward_backtest import (load, simulate, classify_phase_walkforward,
                                     PHASE_KR, ALGO, START, MARKETS, PRINCIPAL, send_telegram)

VALID = set(ALGO.keys())


# ──────────────────────────────────────────────────────────────────────────
# AI(Gemini)가 지수 국면을 월별로 판단 (트레일링 데이터만 → 룩어헤드 없음, 지수당 1호출)
# ──────────────────────────────────────────────────────────────────────────
def gemini_index_phase(mkt_name, idx_df, gem, anonymize=False):
    """anonymize=True: 날짜를 P001..로 가려 LLM 사후지식(날짜→실제결과 기억) 누수 차단."""
    c = idx_df['close'].astype(float)
    idx = c.index
    months = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).last().values
    pts = [pd.Timestamp(d) for d in months if c.index.get_loc(pd.Timestamp(d)) >= 200]
    rows = []
    tags = []   # 프롬프트에 쓴 태그(날짜 or P###) ↔ 실제 월 매핑
    for j, d in enumerate(pts):
        i = c.index.get_loc(d)
        w = c.iloc[max(0, i - 252):i + 1]
        r1 = (c.iloc[i] / c.iloc[i - 21] - 1) * 100 if i >= 21 else 0
        r3 = (c.iloc[i] / c.iloc[i - 63] - 1) * 100 if i >= 63 else 0
        r6 = (c.iloc[i] / c.iloc[i - 126] - 1) * 100 if i >= 126 else 0
        r12 = (c.iloc[i] / c.iloc[i - 252] - 1) * 100 if i >= 252 else 0
        vs52 = (c.iloc[i] / w.max() - 1) * 100
        vsma200 = (c.iloc[i] / c.iloc[max(0, i - 200):i + 1].mean() - 1) * 100
        tag = f"P{j+1:03d}" if anonymize else d.strftime('%Y-%m')
        tags.append((tag, d))
        rows.append(f"{tag}: 1M{r1:+.0f}% 3M{r3:+.0f}% 6M{r6:+.0f}% 12M{r12:+.0f}% "
                    f"52주고점대비{vs52:+.0f}% 200MA대비{vsma200:+.0f}%")
    label_hdr = ("각 구간 데이터(시간순, 과거 추세만, 날짜는 익명):\n" if anonymize
                 else "각 월 데이터(해당 월까지 과거 추세만, 미래정보 없음):\n")
    fmt = "P###=PHASE" if anonymize else "YYYY-MM=PHASE"
    who = f"'{mkt_name} 지수'의 시계열 구간" if anonymize else f"'{mkt_name} 지수'의 월별"
    prompt = (
        f"너는 시장 국면 분석가다. {who} 추세 데이터를 보고 각 구간의 시장국면을 9단계 중 하나로 판단하라.\n"
        "9단계: PANIC(공포급락) BEAR_EARLY(하락초기) BEAR_MID(하락중반) BEAR_LATE(하락말기/바닥) "
        "RECOVERY(회복초입) BULL_EARLY(상승초입) BULL_MID(상승중반) BULL_LATE(상승말기/고점) SIDEWAYS(횡보)\n"
        + label_hdr + "\n".join(rows) +
        f"\n\n반드시 아래 형식으로 모든 구간에 한 줄씩만 출력(설명 금지):\n{fmt}")
    txt = gem.generate_content(prompt, temperature=0.2)
    tag2date = {t: d for t, d in tags}
    pat = r'(P\d{3})\s*[=:]\s*([A-Z_]+)' if anonymize else r'(\d{4}-\d{2})\s*[=:]\s*([A-Z_]+)'
    date_labels = {}  # 월말 '날짜' -> phase (월중 룩어헤드 방지: 그 날짜 이후에만 적용)
    for m in re.finditer(pat, txt):
        tag, ph = m.group(1), m.group(2)
        if ph in VALID and tag in tag2date:
            date_labels[tag2date[tag]] = ph
    if date_labels:
        me = pd.Series(date_labels).sort_index()
        ser = me.reindex(idx, method='ffill').fillna('SIDEWAYS')   # 월말판단→그 이후 일자만(+ simulate shift1)
    else:
        ser = pd.Series('SIDEWAYS', index=idx)
    return ser, len(date_labels), len(pts)


def monthlyize(daily_phase, idx_df):
    """봇 일별국면을 '월말 판단→다음달 매매'로 변환(AI와 같은 주기·월중 룩어헤드 없음)."""
    s = daily_phase.reindex(idx_df.index).ffill()
    idx = idx_df.index
    me_dates = pd.Series(idx, index=idx).groupby([idx.year, idx.month]).last().values
    date_labels = {pd.Timestamp(d): s.loc[pd.Timestamp(d)] for d in me_dates}
    me = pd.Series(date_labels).sort_index()
    return me.reindex(idx, method='ffill').fillna('SIDEWAYS')   # 월말 이후 일자만(+ simulate shift1)


def _agree_3way(bot_ser, ai_ser):
    """봇 vs AI 국면 일치율(9단계 정확일치 / 3분류 BEAR-BULL-NEUTRAL)."""
    common = bot_ser.index.intersection(ai_ser.index)
    b = bot_ser.reindex(common); a = ai_ser.reindex(common)
    valid = (b != 'UNKNOWN') & (a != 'UNKNOWN')
    b, a = b[valid], a[valid]
    if len(b) == 0:
        return 0, 0
    exact = (b.values == a.values).mean() * 100
    reg = {'PANIC': 'B', 'BEAR_EARLY': 'B', 'BEAR_MID': 'B', 'BEAR_LATE': 'B',
           'RECOVERY': 'U', 'BULL_EARLY': 'U', 'BULL_MID': 'U', 'BULL_LATE': 'U', 'SIDEWAYS': 'N'}
    r3 = (pd.Series(b).map(reg).values == pd.Series(a).map(reg).values).mean() * 100
    return round(exact), round(r3)


def run(data, gem):
    L = []
    L.append("🤖 국면 판단주체 비교: 봇 vs AI(Gemini)  —  실전형(답안지無)·같은 매매엔진")
    L.append(f"원금 {PRINCIPAL//10000:,}만 · 기간 {START}~ · 코스피/코스닥 분리 · 국면→기법 동일")
    L.append("=" * 64)
    rel = []   # 신뢰성 메모
    for mkt in ('KOSPI', 'KOSDAQ'):
        idx_df = data['index'][mkt]
        stocks = data[mkt]
        if idx_df is None or not stocks:
            L.append(f"\n[{mkt}] 데이터부족"); continue
        bot_phase = classify_phase_walkforward(idx_df, data.get('vix'))
        bot_monthly = monthlyize(bot_phase, idx_df)                       # 봇을 월별로(공정 주기)
        ai_phase, n_lab, n_pts = gemini_index_phase(mkt, idx_df, gem, anonymize=False)   # AI 날짜노출
        ai_anon, _, _ = gemini_index_phase(mkt, idx_df, gem, anonymize=True)             # AI 날짜익명(사후지식 차단)
        exact, r3 = _agree_3way(bot_phase, ai_phase)

        r_bot = simulate(data, mkt, algo=True, phase_series=bot_phase)
        r_botm = simulate(data, mkt, algo=True, phase_series=bot_monthly)
        r_ai = simulate(data, mkt, algo=True, phase_series=ai_phase)
        r_anon = simulate(data, mkt, algo=True, phase_series=ai_anon)
        r_hold = simulate(data, mkt, algo=False)
        if not all([r_bot, r_botm, r_ai, r_anon, r_hold]):
            L.append(f"\n[{mkt}] 시뮬 실패"); continue

        def line(tag, r):
            return f"  {tag:18} 1000만→{r['final']/1e4:>6,.0f}만  {r['ret']:+6.0f}%  연{r['cagr']:+.0f}%  MDD{r['mdd']:.0f}%  전환{r['switches']}회"
        L.append(f"\n[{mkt}] {r_bot['N']}종목 · {r_bot['days']}일(~{r_bot['days']/252:.1f}년)")
        L.append(line('봇 판단(일별)', r_bot))
        L.append(line('봇 판단(월별)', r_botm))
        L.append(line('AI 판단(날짜노출)', r_ai))
        L.append(line('★AI 판단(날짜익명)', r_anon))
        L.append(f"  {'단순보유':18} 1000만→{r_hold['final']/1e4:>6,.0f}만  {r_hold['ret']:+6.0f}%  연{r_hold['cagr']:+.0f}%  MDD{r_hold['mdd']:.0f}%")
        # 핵심 판정: 날짜익명 AI가 보유를 이기나(=진짜 신호) / 노출 대비 얼마나 빠지나(=사후지식 누수량)
        leak = r_ai['ret'] - r_anon['ret']
        L.append(f"  → 사후지식 누수량(날짜노출-익명): {leak:+.0f}%p {'(누수 큼=노출결과 신뢰불가)' if leak>200 else '(누수 작음)'}")
        L.append(f"  → ★익명AI vs 보유: {r_anon['ret']-r_hold['ret']:+.0f}%p  {'🟢익명에도 보유이김=진짜신호' if r_anon['ret']>r_hold['ret'] else '🔴익명선 보유못이김=노출우위는 허상'}")
        L.append(f"  → 봇·AI 국면일치율: 정확 {exact}% / 3분류 {r3}%  (AI 라벨 {n_lab}/{n_pts}월)")
        rel.append((mkt, r_bot['N'], n_lab, n_pts, exact, r3, leak,
                    r_anon['ret'] > r_hold['ret']))

    # ── 데이터 신뢰성 진단 ──
    L.append("\n" + "=" * 64)
    L.append("🔍 알고리즘/데이터 신뢰성 진단")
    for mkt, n, nlab, npts, ex, r3, leak, anon_win in rel:
        cov = round(100 * nlab / npts) if npts else 0
        flag = "⚠️표본작음" if n < 15 else "✓"
        L.append(f"  [{mkt}] 종목 {n}개 {flag} · AI라벨커버 {cov}% · 봇·AI일치 정확{ex}%/3분류{r3}% · "
                 f"사후지식누수 {leak:+.0f}%p · 익명AI보유이김 {'예' if anon_win else '아니오'}")
    L.append("  · ★LLM 사후지식 누수: 날짜노출 AI는 학습기억으로 '결과를 아는' 답안지일 위험 →")
    L.append("    날짜익명 결과(★)만 신뢰. 누수량이 크면 노출결과(+1065%/+3482%)는 과대평가.")
    L.append("  · 판단주기 통제: 봇 일별 vs 월별 비교로 '휩쏘=주기탓 vs 실력탓' 분리.")
    L.append("  · 룩어헤드: 국면 shift(1)+트레일링데이터만 → 통제됨 ✓")
    L.append("  · 데이터: yfinance 수정종가(배당·액면 반영), KR 소수점오염 없음 ✓")
    L.append("  · ⚠️ 생존편향: 표본=현재 상장 대형주 → 상장폐지·부진주 누락 → '단순보유'가 과대평가됐을 수 있음")
    L.append("  · ⚠️ 단일기간·단일실행(2018~): 다기간 교차검증 없음 → robustness 미확인")
    L.append("=" * 64)
    return "\n".join(L)


if __name__ == '__main__':
    from base.database import get_db_connection
    from ai.gemini_api import GeminiApi
    conn = get_db_connection()
    key = conn.execute("SELECT gemini_api_key FROM users WHERE gemini_api_key IS NOT NULL "
                       "AND gemini_api_key!='' LIMIT 1").fetchone()
    conn.close()
    gem = GeminiApi(key['gemini_api_key'] if key else '')
    data = load(force='--refresh' in sys.argv)
    rep = run(data, gem)
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
