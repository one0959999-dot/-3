"""2단계: 봇 vs AI vs 봇+AI 실시간 9국면 판단 → 정답지 괴리 + 차트.

AI(Gemini)는 정답지를 모름. 가격 + 시장정세(코스피) + 금리(FRED)를 근거로 시점별 9국면 판단(날짜익명=사후지식 차단).
봇=규칙기반(detect_realtime_bot), 봇+AI=앙상블(불일치시 AI 채택).
괴리: 정답지(사후) 일별국면 vs 각 탐지기 — 정확일치% / 3분류(상승·하락·횡보)일치%.
차트 3종목(지수·삼성전자·에코프로비엠) AI/봇+AI 오버레이 + 집계.

실행: python KR/detect_realtime_ai.py
"""
import sys, os, re, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.rcParams['font.family'] = 'Malgun Gothic'; plt.rcParams['axes.unicode_minus'] = False
from KR.answer_sheet import zigzag, label_phases
from KR.detect_realtime_bot import bot_realtime_phase, gap, COL, GROUP, overlay_chart

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
SCRATCH = r"C:\Users\신동호\AppData\Local\Temp\claude\C--Users------gemini-antigravity-scratch-lassi-bot\557fef11-84ae-4595-9213-189a3f93567e\scratchpad"
END = '2025-12-31'
CODE2KR = {'PANIC': '패닉', 'BEAR_EARLY': '하락초기', 'BEAR_MID': '하락중반', 'BEAR_LATE': '하락말기',
           'RECOVERY': '회복초입', 'BULL_EARLY': '상승초입', 'BULL_MID': '상승중반', 'BULL_LATE': '상승말기', 'SIDEWAYS': '횡보'}


def rate_series():
    c = sqlite3.connect(P('lassi.db')); c.row_factory = sqlite3.Row
    k = c.execute("SELECT fred_api_key FROM users WHERE fred_api_key IS NOT NULL AND fred_api_key!='' LIMIT 1").fetchone()['fred_api_key']; c.close()
    j = requests.get('https://api.stlouisfed.org/fred/series/observations',
                     params={'series_id': 'IR3TIB01KRM156N', 'api_key': k, 'file_type': 'json', 'observation_start': '2013-01-01'}, timeout=20).json()
    s = pd.Series({pd.Timestamp(o['date']): float(o['value']) for o in j['observations'] if o['value'] != '.'})
    return s.sort_index()


def gemini():
    from base.database import get_db_connection
    from ai.gemini_api import GeminiApi
    conn = get_db_connection()
    k = conn.execute("SELECT gemini_api_key FROM users WHERE gemini_api_key IS NOT NULL AND gemini_api_key!='' LIMIT 1").fetchone(); conn.close()
    return GeminiApi(k['gemini_api_key'])


def ai_phase(name, df, idxc, rate, gem):
    """Gemini 시점별 9국면(날짜익명, 가격+코스피정세+금리). 월별 → 일별."""
    c = df['close'][df['close'].index <= END]
    months = pd.Series(c.index, index=c.index).groupby([c.index.year, c.index.month]).last().values
    pts = [pd.Timestamp(d) for d in months if c.index.get_loc(pd.Timestamp(d)) >= 200]
    rows = []; tags = []
    for j, d in enumerate(pts):
        i = c.index.get_loc(d)
        r1 = (c.iloc[i]/c.iloc[i-21]-1)*100; r3 = (c.iloc[i]/c.iloc[i-63]-1)*100
        r6 = (c.iloc[i]/c.iloc[i-126]-1)*100; r12 = (c.iloc[i]/c.iloc[i-252]-1)*100
        g = c.diff().clip(lower=0).rolling(14).mean(); l = (-c.diff().clip(upper=0)).rolling(14).mean()
        rsi = float((100-100/(1+g/(l+1e-9))).iloc[i]); vs200 = (c.iloc[i]/c.iloc[max(0,i-200):i+1].mean()-1)*100
        # 시장정세
        ic = idxc.reindex(c.index).ffill(); mi = ic.index.get_loc(d) if d in ic.index else i
        m3 = (ic.iloc[i]/ic.iloc[i-63]-1)*100 if i>=63 else 0; m12 = (ic.iloc[i]/ic.iloc[i-252]-1)*100 if i>=252 else 0
        mvs200 = (ic.iloc[i]/ic.iloc[max(0,i-200):i+1].mean()-1)*100
        # 금리
        rt = float(rate.reindex([d]).ffill().iloc[0]); rt6 = float(rate.reindex([d]).ffill().iloc[0] - rate.reindex([d - pd.Timedelta(days=180)]).ffill().iloc[0])
        tag = f"P{j+1:03d}"; tags.append((tag, d))
        rows.append(f"{tag}: [종목]1M{r1:+.0f} 3M{r3:+.0f} 6M{r6:+.0f} 12M{r12:+.0f} RSI{rsi:.0f} 200MA{vs200:+.0f}% "
                    f"[코스피]3M{m3:+.0f} 12M{m12:+.0f} 200MA{mvs200:+.0f}% [금리]{rt:.1f}%(6개월{rt6:+.1f})")
    prompt = (
        "너는 시장 국면 분석가다. 각 구간을 9국면 중 하나로 판단하라(미래정보 없음, 추세·정세·금리만).\n"
        "9국면 코드: PANIC BEAR_EARLY BEAR_MID BEAR_LATE RECOVERY BULL_EARLY BULL_MID BULL_LATE SIDEWAYS\n"
        "각 구간(시간순, 날짜익명): 종목추세 + 코스피 시장정세 + 한국금리.\n" + "\n".join(rows) +
        "\n\n반드시 형식대로 모든 구간 한줄씩(설명금지):\nP###=CODE")
    txt = gem.generate_content(prompt, temperature=0.2)
    t2d = {t: d for t, d in tags}; lab = {}
    for m in re.finditer(r'(P\d{3})\s*=\s*([A-Z_]+)', txt):
        if m.group(2) in CODE2KR and m.group(1) in t2d:
            lab[t2d[m.group(1)]] = CODE2KR[m.group(2)]
    if not lab:
        return pd.Series('횡보', index=c.index)
    s = pd.Series(lab).sort_index().reindex(c.index, method='ffill').fillna('횡보')
    return s


def combined_chart(name, close, ans_pts, ans_ph, bot_ph, ai_ph, botai_ph, gb, ga, gba, fname):
    """한 종목 4단 비교: 정답지 / 봇 / AI / 봇+AI (같은 가격·전환점 위에 각자 국면색)."""
    c = close[close.index <= END]
    panels = [('정답지(사후)', ans_ph, ''), (f'봇 {gb[0]:.0f}/{gb[1]:.0f}%', bot_ph, ''),
              (f'AI {ga[0]:.0f}/{ga[1]:.0f}%', ai_ph, ''), (f'봇+AI {gba[0]:.0f}/{gba[1]:.0f}%', botai_ph, '')]
    fig, axes = plt.subplots(4, 1, figsize=(15, 12), sharex=True)
    for ax, (title, ph, _) in zip(axes, panels):
        ax.plot(c.index, c.values, color='#222', lw=0.9, zorder=3)
        runs = (ph != ph.shift()).cumsum()
        for _, g in pd.DataFrame({'ph': ph}).groupby(runs):
            p = g['ph'].iloc[0]
            ax.axvspan(g.index[0], g.index[-1], color=COL.get(p, '#fff'), alpha=0.33, zorder=1)
        for d, t, pr in ans_pts:
            if d <= pd.Timestamp(END):
                ax.scatter([d], [pr], marker='^' if t == 'L' else 'v',
                           color='blue' if t == 'L' else 'red', s=55, zorder=5, edgecolor='white')
        ax.set_ylabel(title, fontsize=10); ax.grid(alpha=0.15)
    axes[0].set_title(f"{name} — 정답지 vs 봇 vs AI vs 봇+AI (색=국면, ▲매수/▼매도=정답전환점, %=정확/3분류일치)", fontsize=12)
    path = os.path.join(SCRATCH, fname); fig.tight_layout(); fig.savefig(path, dpi=85); plt.close(fig)
    return path


def ensemble(bot_ph, ai_ph):
    """봇+AI: 3분류 불일치시 AI(매크로 인지) 채택, 일치시 봇의 상세 국면 유지."""
    common = bot_ph.index.intersection(ai_ph.index)
    out = bot_ph.reindex(common).copy()
    for d in common:
        if GROUP.get(bot_ph[d]) != GROUP.get(ai_ph[d]):
            out[d] = ai_ph[d]
    return out


def main():
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb')); big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
    dfs = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                dfs.setdefault(c, (n, df))
    idxc = wf['index']['KOSPI']['close']
    rate = rate_series(); gem = gemini()
    chart = [('지수', 'KOSPI지수', wf['index']['KOSPI']), ('005930', None, None), ('247540', None, None)]
    extra = ['000660', '035420', '051910', '005380', '068270', '105560', '042660', '034020',
             '196170', '028300', '011200', '012450']
    print("🤖 2단계: 봇 vs AI vs 봇+AI (같은 종목세트, 가격+코스피정세+금리, 날짜익명)")
    agg = {'봇': [], 'AI': [], '봇+AI': []}; results = {}

    def run_stock(code, nm, df):
        d = df[df.index <= END]
        ans_pts = zigzag(d['close'], 0.20); ans_ph = label_phases(d['close'], ans_pts)
        bot_ph = bot_realtime_phase(d)
        ai_ph = ai_phase(nm, d, idxc, rate, gem)
        ba = ensemble(bot_ph, ai_ph)
        gb, ga, gba = gap(ans_ph, bot_ph), gap(ans_ph, ai_ph), gap(ans_ph, ba)
        agg['봇'].append(gb); agg['AI'].append(ga); agg['봇+AI'].append(gba)
        results[code] = dict(name=nm, ans_pts=ans_pts, ans_ph=ans_ph, bot_ph=bot_ph, ai_ph=ai_ph,
                             botai_ph=ba, gb=gb, ga=ga, gba=gba)
        print(f"  {nm:10} 봇 {gb[0]:.0f}/{gb[1]:.0f}% · AI {ga[0]:.0f}/{ga[1]:.0f}% · 봇+AI {gba[0]:.0f}/{gba[1]:.0f}%  (정확/3분류)")
        combined_chart(nm, d['close'], ans_pts, ans_ph, bot_ph, ai_ph, ba, gb, ga, gba, f"cmp_{code}.png")

    seen = set()
    for code, nm, df in chart:
        if df is None and code in dfs: nm, df = dfs[code]
        if df is not None: run_stock(code, nm, df); seen.add(code)
    for code in extra:
        if code in dfs and code not in seen:
            nm, df = dfs[code]; run_stock(code, nm, df); seen.add(code)
    pickle.dump(results, open(P('data_detect_results.pkl'), 'wb'))

    print(f"\n[집계 {len(agg['봇'])}종목 (전부 동일 세트) 평균 — 정확일치 / 3분류일치]")
    for k in ('봇', 'AI', '봇+AI'):
        ex = np.mean([x[0] for x in agg[k]]); g3 = np.mean([x[1] for x in agg[k]])
        print(f"  {k:6} 정확 {ex:.0f}% · 3분류 {g3:.0f}%")
    print("⚠️ AI에 금리·정세 넣어 시대유추 누수 일부 가능(날짜는 익명화). 차트: scratchpad/ai_*.png, botai_*.png")


if __name__ == '__main__':
    main()
