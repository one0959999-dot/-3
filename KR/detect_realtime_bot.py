"""2단계: 봇(규칙기반) 실시간 9국면 판단 → 정답지와 괴리 + 차트 오버레이.

봇은 정답지를 모름. 매일 '그 시점까지 데이터'만으로 9국면 판단(워크포워드, 룩어헤드 없음).
규칙: classify_phase 트리(MA200·기울기·모멘텀·ADX·52주고점, 종목엔 VIX대신 급락).
괴리율: 정답지(사후) 일별 국면 vs 봇 실시간 국면 — 정확일치% / 3분류일치%.
차트: 가격 + 봇 실시간 국면색 + 정답지 전환점(▲▼) → 봇이 어디서 늦나/틀리나 시각화.

실행: python KR/detect_realtime_bot.py
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.family'] = 'Malgun Gothic'
plt.rcParams['axes.unicode_minus'] = False
from KR.answer_sheet import zigzag, label_phases, PHASE_ORDER

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
SCRATCH = r"C:\Users\신동호\AppData\Local\Temp\claude\C--Users------gemini-antigravity-scratch-lassi-bot\557fef11-84ae-4595-9213-189a3f93567e\scratchpad"
END = '2025-12-31'
COL = {'패닉': '#7B1010', '하락초기': '#F4A6A6', '하락중반': '#E33', '하락말기': '#F5A623',
       '회복초입': '#9ACD32', '상승초입': '#9BE89B', '상승중반': '#1F9D55', '상승말기': '#F2C200', '횡보': '#D9D9D9'}
GROUP = {'패닉': 'B', '하락초기': 'B', '하락중반': 'B', '하락말기': 'B',
         '회복초입': 'U', '상승초입': 'U', '상승중반': 'U', '상승말기': 'U', '횡보': 'N'}


def _adx(df, n=14):
    h, l, c = df.get('high', df['close']), df.get('low', df['close']), df['close']
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    up, dn = h.diff(), -l.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0); mdm = dn.where((dn > up) & (dn > 0), 0.0)
    atr = tr.rolling(n).mean()
    pdi = 100 * pdm.rolling(n).mean() / (atr + 1e-9); mdi = 100 * mdm.rolling(n).mean() / (atr + 1e-9)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi + 1e-9)
    return dx.rolling(n).mean()


def bot_realtime_phase(df):
    """봇 실시간 9국면(워크포워드: 각일 과거데이터만). classify_phase 트리."""
    c = df['close']
    ma200 = c.rolling(200).mean(); ma60 = c.rolling(60).mean(); ma120 = c.rolling(120).mean()
    mom20 = (c / c.shift(20) - 1) * 100; mom60 = (c / c.shift(60) - 1) * 100
    vs200 = (c / ma200 - 1) * 100; slope = (ma200 / ma200.shift(20) - 1) * 100
    vs52 = (c / c.rolling(252).max() - 1) * 100; adx = _adx(df)
    out = []
    for i in range(len(c)):
        if i < 200 or pd.isna(ma200.iloc[i]):
            out.append('횡보'); continue
        cur = c.iloc[i]; m200 = ma200.iloc[i]; m20 = mom20.iloc[i]; m60 = mom60.iloc[i]
        ax = adx.iloc[i] if not pd.isna(adx.iloc[i]) else 20; sl = slope.iloc[i]; v52 = vs52.iloc[i]
        if m20 < -12: ph = '패닉'
        elif cur < m200 * 0.92 and m20 < -5: ph = '하락중반'
        elif cur < m200 and m60 < -15: ph = '하락중반'
        elif cur < m200 and m20 < -3: ph = '하락초기'
        elif cur < m200 and ax < 18: ph = '하락말기'
        elif cur > m200 and m20 > 3 and m60 < -10: ph = '회복초입'
        elif cur > m200 and sl > 0:
            if v52 > -5: ph = '상승말기'
            elif cur > ma60.iloc[i] > ma120.iloc[i] and ax > 25:
                ph = '상승초입' if (m20 > 0 and m60 < 15) else '상승중반'
            else: ph = '상승중반'
        else: ph = '횡보'
        out.append(ph)
    return pd.Series(out, index=c.index)


def gap(ans_ph, bot_ph):
    common = ans_ph.index.intersection(bot_ph.index)
    a = ans_ph.reindex(common); b = bot_ph.reindex(common)
    exact = (a.values == b.values).mean() * 100
    g3 = (a.map(GROUP).values == b.map(GROUP).values).mean() * 100
    return exact, g3


def overlay_chart(name, df, ans_pts, bot_ph, fname, exact, g3):
    c = df['close'][df['close'].index <= END]
    fig, ax = plt.subplots(figsize=(15, 6))
    ax.plot(c.index, c.values, color='#222', lw=1.1, zorder=3)
    runs = (bot_ph != bot_ph.shift()).cumsum(); seen = set()
    for _, g in pd.DataFrame({'ph': bot_ph}).groupby(runs):
        p = g['ph'].iloc[0]
        ax.axvspan(g.index[0], g.index[-1], color=COL.get(p, '#fff'), alpha=0.32, zorder=1,
                   label=p if p not in seen else None); seen.add(p)
    for d, t, pr in ans_pts:
        if d > pd.Timestamp(END): continue
        ax.scatter([d], [pr], marker='^' if t == 'L' else 'v', color='blue' if t == 'L' else 'red',
                   s=90, zorder=5, edgecolor='white')
    ax.set_title(f"{name} — 봇 실시간 국면(색) vs 정답지 전환점(▲▼)  |  정확일치 {exact:.0f}% · 3분류일치 {g3:.0f}%", fontsize=12)
    ax.legend(loc='upper left', ncol=5, fontsize=8, framealpha=0.9); ax.grid(alpha=0.2)
    path = os.path.join(SCRATCH, fname); fig.tight_layout(); fig.savefig(path, dpi=90); plt.close(fig)
    return path


def main():
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb')); big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
    dfs = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                dfs.setdefault(c, (n, df))
    idx_df = wf['index']['KOSPI']
    samples = [('지수', 'KOSPI지수', idx_df), ('005930', None, None), ('247540', None, None)]
    print("🤖 2단계 봇 실시간 국면 vs 정답지 괴리")
    allg = []
    for code, nm, df in samples:
        if df is None and code in dfs:
            nm, df = dfs[code]
        if df is None: continue
        d = df[df.index <= END]
        ans_pts = zigzag(d['close'], 0.20); ans_ph = label_phases(d['close'], ans_pts)
        bot_ph = bot_realtime_phase(d)
        ex, g3 = gap(ans_ph, bot_ph)
        p = overlay_chart(nm, df, ans_pts, bot_ph, f"bot_{code}.png", ex, g3)
        print(f"  {nm}: 정확일치 {ex:.0f}% · 3분류일치 {g3:.0f}% → {p}")
    # 전체 집계
    for code, (nm, df) in list(dfs.items()):
        d = df[df.index <= END]
        if len(d) < 250: continue
        ans_pts = zigzag(d['close'], 0.20)
        if len(ans_pts) < 3: continue
        ans_ph = label_phases(d['close'], ans_pts); bot_ph = bot_realtime_phase(d)
        allg.append(gap(ans_ph, bot_ph))
    if allg:
        print(f"\n[전체 {len(allg)}종목 평균] 정확일치 {np.mean([x[0] for x in allg]):.0f}% · 3분류일치 {np.mean([x[1] for x in allg]):.0f}%")
    print("판독: 정확일치=봇 9국면이 정답지와 같은 비율. 3분류=상승/하락/횡보 방향 맞춘 비율. (봇은 사후정답 모름·실시간)")


if __name__ == '__main__':
    main()
