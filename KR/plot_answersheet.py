"""정답지 시각화 — 가격 차트에 9국면(색칠) + 전환점(바닥/천장) 오버레이.

각 종목 차트에 정답지(9국면 구간 색칠 + 매수/매도 전환점)를 씌워 '왜 그 국면인지' 눈으로 검증.
실행: python KR/plot_answersheet.py
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
from KR.answer_sheet import zigzag, label_phases

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
SCRATCH = r"C:\Users\신동호\AppData\Local\Temp\claude\C--Users------gemini-antigravity-scratch-lassi-bot\557fef11-84ae-4595-9213-189a3f93567e\scratchpad"
END = '2025-12-31'
COL = {'패닉': '#7B1010', '하락초기': '#F4A6A6', '하락중반': '#E33', '하락말기': '#F5A623',
       '회복초입': '#9ACD32', '상승초입': '#9BE89B', '상승중반': '#1F9D55', '상승말기': '#F2C200',
       '횡보': '#D9D9D9'}


def plot_one(name, close, fname):
    c = close[close.index <= END].dropna()
    if len(c) < 200:
        return None
    pts = zigzag(c, 0.20)
    ph = label_phases(c, pts)
    fig, ax = plt.subplots(figsize=(15, 6))
    ax.plot(c.index, c.values, color='#222', lw=1.1, zorder=3)
    # 9국면 색칠
    runs = (ph != ph.shift()).cumsum()
    seen = set()
    for _, g in pd.DataFrame({'ph': ph}).groupby(runs):
        p = g['ph'].iloc[0]
        ax.axvspan(g.index[0], g.index[-1], color=COL.get(p, '#fff'), alpha=0.35, zorder=1,
                   label=p if p not in seen else None)
        seen.add(p)
    # 전환점
    for d, t, pr in pts:
        if d > pd.Timestamp(END):
            continue
        ax.scatter([d], [pr], marker='^' if t == 'L' else 'v',
                   color='blue' if t == 'L' else 'red', s=90, zorder=5, edgecolor='white')
        ax.annotate('매수' if t == 'L' else '매도', (d, pr), zorder=6, fontsize=8,
                    xytext=(0, -14 if t == 'L' else 8), textcoords='offset points',
                    ha='center', color='blue' if t == 'L' else 'red', weight='bold')
    ax.set_title(f"{name} — 9국면 정답지 (색=국면, ▲매수바닥/▼매도천장)", fontsize=13)
    ax.legend(loc='upper left', ncol=5, fontsize=8, framealpha=0.9)
    ax.grid(alpha=0.2)
    path = os.path.join(SCRATCH, fname)
    fig.tight_layout(); fig.savefig(path, dpi=90); plt.close(fig)
    return path


def main():
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))
    big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
    closes = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                closes.setdefault(c, (n, df['close']))
    targets = [('지수', 'KOSPI지수', wf['index']['KOSPI']['close']),
               ('005930', None, None), ('247540', None, None)]
    out = []
    for code, nm, ser in targets:
        if ser is None and code in closes:
            nm, ser = closes[code]
        if ser is None:
            continue
        path = plot_one(nm, ser, f"answersheet_{code}.png")
        if path:
            out.append((nm, path))
            print(f"저장: {nm} → {path}")
    print("DONE", len(out))


if __name__ == '__main__':
    main()
