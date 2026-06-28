"""종목별 9국면 정답지 (~50종목) — 각 종목을 9국면 구간으로 나누고 '왜 그 국면인지' 근거 첨부.

사용자: 지금은 국면만. 지수(코스피/코스닥)는 맥락, 메인은 종목별 9국면 정답지(30~60종목, 종목별).
방법: 종목별 zigzag 전환점 → 9국면 라벨(answer_sheet.label_phases) → 구간별 [기간·국면·가격변화·진입근거].
근거 = 그 구간 진입일의 관찰지표(RSI·MA200이격·20/60일모멘텀·거래량) → 검증가능한 상세 대답지.
저장: data_answersheet_stocks.pkl. 예시 3종목 상세 + 전체 커버리지 요약 출력.

실행: python KR/answer_sheet_stocks.py
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.answer_sheet import zigzag, label_phases, PHASE_ORDER, IDEAL

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
END = '2025-12-31'

# 다양한 ~50종목 (코스피 대형/중형 + 코스닥 + 큰 사이클)
UNIVERSE = [
    ('005930','삼성전자'),('000660','SK하이닉스'),('035420','NAVER'),('035720','카카오'),
    ('051910','LG화학'),('006400','삼성SDI'),('005380','현대차'),('000270','기아'),
    ('005490','POSCO홀딩스'),('012330','현대모비스'),('105560','KB금융'),('055550','신한지주'),
    ('086790','하나금융'),('068270','셀트리온'),('207940','삼성바이오로직스'),('066570','LG전자'),
    ('017670','SK텔레콤'),('033780','KT&G'),('015760','한국전력'),('010130','고려아연'),
    ('009150','삼성전기'),('011070','LG이노텍'),('042700','한미반도체'),('012450','한화에어로스페이스'),
    ('010140','삼성중공업'),('009540','HD한국조선해양'),('034020','두산에너빌리티'),('042660','한화오션'),
    ('051900','LG생활건강'),('090430','아모레퍼시픽'),('032830','삼성생명'),('259960','크래프톤'),
    ('011200','HMM'),('112610','씨에스윈드'),('064350','현대로템'),('010120','LS일렉트릭'),
    ('267260','HD현대일렉트릭'),
    # 코스닥
    ('247540','에코프로비엠'),('086520','에코프로'),('196170','알테오젠'),('028300','HLB'),
    ('058470','리노공업'),('263750','펄어비스'),('293490','카카오게임즈'),('068760','셀트리온제약'),
    ('357780','솔브레인'),('240810','원익IPS'),('000250','삼천당제약'),
]


def load_closes():
    closes, names = {}, {}
    for f in ('data_cache_big.pkl', 'data_cache_wf.pkl'):
        d = pickle.load(open(P(f), 'rb'))
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                closes.setdefault(c, df); names.setdefault(c, n)
    if os.path.exists(P('data_cache_delisted.pkl')):
        for c, v in pickle.load(open(P('data_cache_delisted.pkl'), 'rb')).items():
            if c not in closes:
                closes[c] = pd.DataFrame({'close': v['close']})
    return closes, names


def evidence_at(df, d):
    c = df['close']; i = c.index.get_loc(d)
    if i < 60:
        return ""
    g = c.diff().clip(lower=0).rolling(14).mean(); l = (-c.diff().clip(upper=0)).rolling(14).mean()
    rsi = float((100 - 100 / (1 + g / (l + 1e-9))).iloc[i])
    ma200 = float(c.iloc[max(0, i - 200):i + 1].mean()); vs200 = (c.iloc[i] / ma200 - 1) * 100
    m20 = (c.iloc[i] / c.iloc[i - 20] - 1) * 100 if i >= 20 else 0
    m60 = (c.iloc[i] / c.iloc[i - 60] - 1) * 100 if i >= 60 else 0
    vr = (df['volume'].iloc[i] / df['volume'].iloc[max(0, i - 20):i].mean()) if 'volume' in df.columns and df['volume'].iloc[max(0, i - 20):i].mean() > 0 else None
    s = f"RSI{rsi:.0f}·200MA{vs200:+.0f}%·20일{m20:+.0f}%·60일{m60:+.0f}%"
    if vr: s += f"·거래량{vr:.1f}배"
    return s


def segments(df, ph):
    """연속 같은 국면 = 구간. [(start,end,phase,days,price_chg,evidence)]."""
    c = df['close'].dropna().reindex(ph.index)
    runs = (ph != ph.shift()).cumsum()
    segs = []
    for _, g in pd.DataFrame({'ph': ph, 'c': c}).groupby(runs):
        p = g['ph'].iloc[0]; sd, ed = g.index[0], g.index[-1]; days = len(g)
        if days < 5 or pd.isna(g['c'].iloc[0]):
            continue
        chg = (g['c'].iloc[-1] / g['c'].iloc[0] - 1) * 100 if g['c'].iloc[0] > 0 else 0
        segs.append((sd, ed, p, days, chg, evidence_at(df, sd)))
    return segs


def main():
    closes, names = load_closes()
    out = {}; cover = {p: 0 for p in PHASE_ORDER}
    done = 0
    for code, nm in UNIVERSE:
        if code not in closes:
            continue
        df = closes[code]; df = df[df.index <= END]
        if len(df) < 250:
            continue
        pts = zigzag(df['close'], 0.20)
        if len(pts) < 3:
            continue
        ph = label_phases(df['close'], pts)
        segs = segments(df, ph)
        out[code] = {'name': nm, 'segments': segs}
        for s in segs:
            cover[s[2]] = cover.get(s[2], 0) + 1
        done += 1
    pickle.dump(out, open(P('data_answersheet_stocks.pkl'), 'wb'))

    L = [f"📒 종목별 9국면 정답지 — {done}종목 (각 구간 + 판단근거)", "=" * 72]
    # 예시 3종목 상세
    for code in ['005930', '247540', '042660']:
        if code not in out:
            continue
        o = out[code]
        L.append(f"\n━━ {o['name']}({code}) ━━")
        for sd, ed, p, days, chg, ev in o['segments']:
            L.append(f"  {sd.strftime('%y.%m')}~{ed.strftime('%y.%m')} [{p:6}] {days:4}일 {chg:+5.0f}% | {ev} → {IDEAL[p]}")
    # 커버리지 요약
    L.append("\n" + "=" * 72)
    L.append(f"[국면 커버리지: {done}종목 전체에서 각 국면 등장 구간 수]")
    for p in PHASE_ORDER:
        L.append(f"  {p:8} {cover.get(p,0):4}구간   ({IDEAL[p]})")
    L.append("=" * 72)
    L.append("저장: data_answersheet_stocks.pkl · 각 구간 진입근거(RSI·이격·모멘텀·거래량) 검증가능.")
    print("\n".join(L))


if __name__ == '__main__':
    main()
