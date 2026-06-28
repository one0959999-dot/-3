"""상세 대답지 — 정답지의 각 전환점을 '왜·어떻게 그 순간으로 찍었나' 근거 전체 공개(검증용).

정답지는 사후(zigzag) 기준. 신뢰 확보 위해 전환점별로:
 - 확정근거(사후): 진입레그(여기로 오기까지 며칠간 몇%), 확정레그(여기서 다음까지 몇% → 이게 '진짜 전환'임을 증명)
 - 그 순간 관찰가능 지표(실시간이라면 이걸로 알아챘을): RSI14·MA200이격·20/60일모멘텀·52주고점대비·낙폭·VIX·거래량
 - 한줄 판정근거
지수(KOSPI) 전체 전환점 + (옵션) 종목 샘플.

실행: python KR/answer_key_detail.py [pct]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.answer_sheet import zigzag

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
END = '2025-12-31'


def indicators_at(df, vix, d):
    c = df['close']; i = c.index.get_loc(d)
    if i < 60:
        return {}
    win = c.iloc[max(0, i - 252):i + 1]
    g = c.diff().clip(lower=0).rolling(14).mean(); l = (-c.diff().clip(upper=0)).rolling(14).mean()
    rsi = float((100 - 100 / (1 + g / (l + 1e-9))).iloc[i])
    ma200 = float(c.iloc[max(0, i - 200):i + 1].mean())
    vs200 = (c.iloc[i] / ma200 - 1) * 100
    mom20 = (c.iloc[i] / c.iloc[i - 20] - 1) * 100 if i >= 20 else 0
    mom60 = (c.iloc[i] / c.iloc[i - 60] - 1) * 100 if i >= 60 else 0
    vs52 = (c.iloc[i] / win.max() - 1) * 100
    dd = (c.iloc[i] / c.iloc[:i + 1].cummax().iloc[-1] - 1) * 100
    vx = float(vix.reindex([d]).ffill().iloc[0]) if vix is not None else None
    vol = df['volume'].iloc[i] / df['volume'].iloc[max(0, i - 20):i].mean() if 'volume' in df.columns and df['volume'].iloc[max(0, i - 20):i].mean() > 0 else None
    return dict(rsi=rsi, vs200=vs200, mom20=mom20, mom60=mom60, vs52=vs52, dd=dd, vix=vx, volr=vol)


def reason_line(typ, ind):
    if typ == 'L':  # 바닥
        cues = []
        if ind.get('rsi', 100) < 35: cues.append(f"RSI {ind['rsi']:.0f}(과매도)")
        if ind.get('vs200', 0) < -10: cues.append(f"200MA {ind['vs200']:+.0f}%(이격)")
        if ind.get('mom20', 0) < -8: cues.append(f"20일 {ind['mom20']:+.0f}%(급락)")
        if ind.get('dd', 0) < -20: cues.append(f"낙폭 {ind['dd']:.0f}%")
        if ind.get('vix') and ind['vix'] > 30: cues.append(f"VIX {ind['vix']:.0f}(공포)")
        if ind.get('volr') and ind['volr'] > 1.8: cues.append(f"거래량 {ind['volr']:.1f}배")
        return "바닥근거: " + (", ".join(cues) if cues else "뚜렷한 과매도신호 약함")
    else:  # 천장
        cues = []
        if ind.get('rsi', 0) > 65: cues.append(f"RSI {ind['rsi']:.0f}(과열)")
        if ind.get('vs200', 0) > 10: cues.append(f"200MA {ind['vs200']:+.0f}%(상방이격)")
        if ind.get('vs52', -99) > -3: cues.append(f"52주고점 {ind['vs52']:+.0f}%(고점권)")
        if ind.get('mom60', 0) > 20: cues.append(f"60일 {ind['mom60']:+.0f}%(과속)")
        if ind.get('volr') and ind['volr'] > 1.8: cues.append(f"거래량 {ind['volr']:.1f}배")
        return "천장근거: " + (", ".join(cues) if cues else "뚜렷한 과열신호 약함")


def detail(name, df, vix, pts):
    c = df['close']
    L = [f"\n━━ [{name}] 전환점 {len(pts)}개 상세 대답지 ━━"]
    for k, (d, t, p) in enumerate(pts):
        i = c.index.get_loc(d)
        # 진입레그(이전전환점→여기)
        if k > 0:
            d0, t0, p0 = pts[k - 1]
            in_chg = (p / p0 - 1) * 100; in_days = (d - d0).days
            leg_in = f"진입: {d0.strftime('%y.%m')}({p0:,.0f})→여기 {in_days}일간 {in_chg:+.0f}%"
        else:
            leg_in = "진입: (시작)"
        # 확정레그(여기→다음전환점) = 사후 확정근거
        if k < len(pts) - 1:
            d1, t1, p1 = pts[k + 1]
            out_chg = (p1 / p - 1) * 100; out_days = (d1 - d).days
            leg_out = f"확정: 이후 {out_days}일간 {out_chg:+.0f}% → {'반등' if t=='L' else '하락'} 실현(이게 전환점 증거)"
        else:
            leg_out = "확정: (미완·진행중)"
        ind = indicators_at(df, vix, d)
        typ_kr = '🟢바닥(매수정답)' if t == 'L' else '🔴천장(매도정답)'
        L.append(f"\n{d.strftime('%Y-%m-%d')} {typ_kr}  지수 {p:,.0f}")
        L.append(f"  {leg_in}")
        L.append(f"  {leg_out}")
        if ind:
            L.append(f"  그날 지표: RSI {ind['rsi']:.0f} · 200MA {ind['vs200']:+.0f}% · 20일 {ind['mom20']:+.0f}% · "
                     f"60일 {ind['mom60']:+.0f}% · 52주고점 {ind['vs52']:+.0f}%"
                     + (f" · VIX {ind['vix']:.0f}" if ind.get('vix') else "")
                     + (f" · 거래량 {ind['volr']:.1f}배" if ind.get('volr') else ""))
            L.append(f"  ➤ {reason_line(t, ind)}")
    return "\n".join(L)


def main(pct=0.20):
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb'))
    idx_df = wf['index']['KOSPI']; idx_df = idx_df[idx_df.index <= END]
    vix = wf.get('vix')
    pts = zigzag(idx_df['close'], pct)
    print("📒 상세 대답지 — 각 전환점을 '어떻게/왜 그 순간으로 찍었나' (검증용)")
    print("=" * 70)
    print("원리: 정답지는 사후 zigzag. '확정'레그(이후 실제 반대방향 20%+ 이동)가 그 점이 진짜 전환점인 증거.")
    print("'그날 지표'는 실시간이라면 그걸로 알아챘을 단서(2단계 봇/AI가 쓸 것).")
    print(detail("KOSPI지수", idx_df, vix, pts))


if __name__ == '__main__':
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 0.20)
