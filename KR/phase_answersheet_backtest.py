"""답안지 vs 실제(봇/AI) — 국면판단 괴리율 평가 (2015~현재).

사용자 프레임:
- 답안지 = 사후(hindsight)로 본 '진짜 국면'(기간 고정). 국면을 완벽히 알았을 때의 수익 = 천장.
- 실제 = 봇/AI가 그때그때 국면을 모르고 실시간 판단(서술형 풀듯, 룩어헤드 없음).
- 괴리율 = (답안지수익 - 실제수익)/답안지수익. 가장 작은 쪽이 국면을 제일 잘 잡는 것.

매매규칙(국면→매매, 공통): 하락계열(패닉/하락초/중/말)=현금, 그외(상승계열+횡보)=보유(코스피EW).
 → 국면을 완벽히 알면 하락을 다 피해 천장수익. 실시간은 못 피한 만큼 괴리.
대상자산: 코스피 종목 동일가중(EW). 기간 2015-01~.

실행: python KR/phase_answersheet_backtest.py [--telegram]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.walkforward_backtest import classify_phase_walkforward, _adx_series, send_telegram
from KR.phase_source_backtest import gemini_index_phase

START = '2015-01-01'
COST = 0.0021
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_wf.pkl')
BEAR = {'PANIC', 'BEAR_EARLY', 'BEAR_MID', 'BEAR_LATE'}
PHASE_ORDER = ['PANIC', 'BEAR_EARLY', 'BEAR_MID', 'BEAR_LATE', 'RECOVERY',
               'BULL_EARLY', 'BULL_MID', 'BULL_LATE', 'SIDEWAYS']
PHASE_KR = {'PANIC': '패닉', 'BEAR_EARLY': '하락초기', 'BEAR_MID': '하락중반',
            'BEAR_LATE': '하락말기', 'RECOVERY': '회복초입', 'BULL_EARLY': '상승초입',
            'BULL_MID': '상승중반', 'BULL_LATE': '상승말기', 'SIDEWAYS': '횡보'}


def classify_hindsight(idx_df):
    """답안지 = 사후(미래정보 허용) 9국면. classify_phase 트리에 forward/centered 지표 투입."""
    c = idx_df['close']
    ma200 = c.rolling(200, center=True).mean()
    ma60 = c.rolling(60, center=True).mean()
    ma120 = c.rolling(120, center=True).mean()
    mom20 = (c.shift(-20) / c - 1) * 100          # 미래 20일(사후)
    mom60 = (c.shift(-60) / c - 1) * 100          # 미래 60일
    slope = (ma200.shift(-20) / ma200 - 1) * 100
    hi52 = c.rolling(252, center=True).max()
    vs52 = (c / hi52 - 1) * 100
    adx = _adx_series(idx_df)                       # ADX는 인과(추세강도)
    out = []
    for i in range(len(c)):
        m200 = ma200.iloc[i]
        if pd.isna(m200) or pd.isna(mom60.iloc[i]):
            out.append('SIDEWAYS'); continue
        cur = c.iloc[i]; m20 = mom20.iloc[i]; m60 = mom60.iloc[i]
        ax = adx.iloc[i] if not pd.isna(adx.iloc[i]) else 20
        sl = slope.iloc[i] if not pd.isna(slope.iloc[i]) else 0
        v52 = vs52.iloc[i]; m60v, m120v = ma60.iloc[i], ma120.iloc[i]
        if m20 < -10:                                ph = 'PANIC'
        elif cur < m200 * 0.92 and m20 < -5:         ph = 'BEAR_MID'
        elif cur < m200 and m60 < -8:                ph = 'BEAR_MID'
        elif cur < m200 and m20 < -3:                ph = 'BEAR_EARLY'
        elif cur < m200 and m20 > 3:                 ph = 'BEAR_LATE'   # 바닥서 반등시작
        elif cur < m200:                             ph = 'BEAR_EARLY'
        elif cur > m200 and m20 > 3 and m60 < 0:     ph = 'RECOVERY'
        elif cur > m200 and sl > 0:
            if v52 > -5:                             ph = 'BULL_LATE'
            elif cur > m60v > m120v and ax > 25:     ph = 'BULL_EARLY' if m20 > 0 else 'BULL_MID'
            else:                                    ph = 'BULL_MID'
        else:                                        ph = 'SIDEWAYS'
        out.append(ph)
    return pd.Series(out, index=c.index)


def sim(ew, phase, shift):
    """국면→비중(하락=현금0/그외=보유1). shift=True면 실시간(어제판단→오늘). 누적 equity."""
    p = phase.reindex(ew.index).ffill()
    if shift:
        p = p.shift(1)
    expo = (~p.isin(BEAR)).astype(float).fillna(1.0)
    turn = expo.diff().abs().fillna(expo)
    r = expo * ew - turn * COST
    return (1 + r).cumprod(), expo


def metrics(eq):
    ret = (eq.iloc[-1] / eq.iloc[0] - 1) * 100
    mdd = float(((eq / eq.cummax() - 1) * 100).min())
    return ret, mdd


def run(telegram=False):
    data = pickle.load(open(CACHE, 'rb'))
    stocks = {**data['KOSPI'], **data['KOSDAQ']}
    idx_df = data['index']['KOSPI']
    idx_df = idx_df[idx_df.index >= '2014-06-01']
    cal = idx_df.index[idx_df.index >= START]
    ew = pd.DataFrame({c: df['close'].pct_change().reindex(cal)
                       for c, (n, df) in stocks.items()}).mean(axis=1).fillna(0.0)

    ans_phase = classify_hindsight(idx_df)
    bot_phase = classify_phase_walkforward(idx_df, data.get('vix'))
    # AI: gemini가 코스피 지수 월별 실시간 판단(날짜익명=사후지식 차단)
    try:
        from base.database import get_db_connection
        from ai.gemini_api import GeminiApi
        conn = get_db_connection()
        k = conn.execute("SELECT gemini_api_key FROM users WHERE gemini_api_key IS NOT NULL AND gemini_api_key!='' LIMIT 1").fetchone()
        conn.close()
        gem = GeminiApi(k['gemini_api_key'] if k else '')
        ai_phase, nlab, npts = gemini_index_phase('KOSPI', idx_df, gem, anonymize=True)
    except Exception as e:
        print("AI 실패:", e); ai_phase = bot_phase; nlab = npts = 0

    eq_ans, _ = sim(ew, ans_phase, shift=False)     # 답안지(천장)
    eq_bot, _ = sim(ew, bot_phase, shift=True)      # 봇 실시간
    eq_ai, _ = sim(ew, ai_phase, shift=True)        # AI 실시간
    eq_hold = (1 + ew).cumprod()

    ra, ma = metrics(eq_ans); rb, mb = metrics(eq_bot); ri, mi = metrics(eq_ai); rh, mh = metrics(eq_hold)
    gap_bot = (ra - rb) / abs(ra) * 100 if ra else 0
    gap_ai = (ra - ri) / abs(ra) * 100 if ra else 0

    L = ["📋 답안지 vs 실제(봇/AI) — 국면판단 괴리율 (2015~)",
         "규칙: 하락계열=현금/그외=보유(코스피EW) · 답안지=사후국면(천장), 실제=실시간판단", "=" * 62]
    # 답안지 9국면 기간(일수)
    L.append("[답안지 9국면 분포(사후 확정)]")
    vc = ans_phase.reindex(cal).value_counts()
    for ph in PHASE_ORDER:
        d = int(vc.get(ph, 0))
        if d:
            L.append(f"  {PHASE_KR[ph]:10} {d:>5}일")
    L.append("")
    L.append(f"{'주체':16}{'수익':>10}{'MDD':>8}{'답안지대비 괴리율':>16}")
    L.append("-" * 62)
    L.append(f"{'답안지(천장)':16}{ra:>+9.0f}%{ma:>+7.0f}%{'기준':>16}")
    L.append(f"{'봇 실시간':16}{rb:>+9.0f}%{mb:>+7.0f}%{gap_bot:>+15.0f}%")
    L.append(f"{'AI 실시간':16}{ri:>+9.0f}%{mi:>+7.0f}%{gap_ai:>+15.0f}%")
    L.append(f"{'(참고)단순보유':16}{rh:>+9.0f}%{mh:>+7.0f}%")
    L.append("-" * 62)
    winner = '봇' if gap_bot < gap_ai else 'AI'
    L.append(f"📌 괴리율 낮은 쪽(국면 더 잘 잡음): {winner}  (봇 {gap_bot:+.0f}% vs AI {gap_ai:+.0f}%)")
    L.append(f"   AI 라벨 {nlab}/{npts}월(익명)")
    L.append("⚠️ 답안지는 미래정보 사용한 '천장'(실현불가). 괴리율=실시간 감지의 한계치. 생존편향 잔존.")
    return "\n".join(L)


if __name__ == '__main__':
    rep = run(telegram='--telegram' in sys.argv)
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
