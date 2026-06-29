"""상폐 원인 분석 → 종목선정(A) 부실회피 필터 도출.

데이터: data_cache_delisted.pkl(911, 사유+가격) + financials_dart(자본/자본금/순이익).
재무 적신호:
 - 완전자본잠식: 자본총계<0
 - 부분자본잠식: 0<자본총계<자본금 (자본잠식)
 - 적자: 순이익<0,  연속적자: 2년+ 연속
가격 적신호(상폐 전 사전경고):
 - 종착가/고점 (얼마나 무너졌나), 상폐 6개월전 이미 -50%였나(조기경고 가능?)
비교: 부실상폐 vs 피인수 vs 자진 vs 생존주 — 각 신호 출현율.
→ 부실상폐를 사전에 거르는 필터 후보 산출.

실행: python KR/analyze_delisting.py [--telegram]
"""
import sys, os, sqlite3, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)


def fin_flags(rows):
    """rows: [(year, capital, paidin, netincome)] → 종목 적신호 dict."""
    rows = sorted(rows)
    full_imp = any(cap is not None and cap < 0 for _, cap, _, _ in rows)
    part_imp = any(cap is not None and pi not in (None, 0) and 0 <= cap < pi for _, cap, pi, _ in rows)
    nis = [(y, ni) for y, _, _, ni in rows if ni is not None]
    loss = any(ni < 0 for _, ni in nis)
    cons = False
    run = 0
    for _, ni in nis:
        run = run + 1 if ni < 0 else 0
        if run >= 2:
            cons = True
    return dict(완전잠식=full_imp, 부분잠식=part_imp, 자본잠식=(full_imp or part_imp), 적자=loss, 연속적자=cons)


def main(telegram=False):
    deli = pickle.load(open(P('data_cache_delisted.pkl'), 'rb'))
    c = sqlite3.connect(P('lassi.db'))
    fin = {}
    for t, y, cap, pi, ni in c.execute('SELECT ticker, year, capital, paidin, netincome FROM financials_dart'):
        fin.setdefault(t, []).append((y, cap, pi, ni))
    c.close()

    # 그룹: 부실/피인수/자진 + 생존주(재무 있으나 상폐목록에 없음)
    reason = {k: v['reason'] for k, v in deli.items()}
    groups = {'부실상폐': [], '피인수상폐': [], '자진상폐': [], '생존주': []}
    for t in fin:
        g = reason.get(t, '생존주')
        if g in groups:
            groups[g].append(t)

    L = ["🔬 상폐 원인 분석 → 종목선정 부실회피 필터", ""]
    L.append("[1] 재무 적신호 출현율 (재무보유 종목 기준)")
    L.append(f"{'그룹':9}{'n':>4}{'자본잠식':>8}{'완전잠식':>8}{'적자':>7}{'연속적자':>8}")
    L.append("-" * 44)
    stat = {}
    for g, tickers in groups.items():
        fl = [fin_flags(fin[t]) for t in tickers if t in fin]
        if not fl:
            continue
        n = len(fl)
        row = {k: 100 * np.mean([f[k] for f in fl]) for k in ('자본잠식', '완전잠식', '적자', '연속적자')}
        stat[g] = (n, row)
        L.append(f"{g:9}{n:>4}{row['자본잠식']:>7.0f}%{row['완전잠식']:>7.0f}%{row['적자']:>6.0f}%{row['연속적자']:>7.0f}%")

    # [2] 가격 사전경고: 부실상폐 무너짐 정도 + 조기경고
    L.append("\n[2] 가격 패턴 (상폐사유별)")
    L.append(f"{'그룹':9}{'n':>4}{'종착/고점':>9}{'상폐前30일':>10}{'6개월前-50%↓':>12}")
    L.append("-" * 46)
    for g in ('부실상폐', '피인수상폐', '자진상폐'):
        items = [(k, v) for k, v in deli.items() if v['reason'] == g]
        lvp, f30, early = [], [], []
        for k, v in items:
            lvp.append(v['last_vs_peak_pct']); f30.append(v['final_30d_pct'])
            cl = v['close']
            if len(cl) > 130:
                ld = cl.index[-1]
                p6 = cl[cl.index <= ld - pd.Timedelta(days=180)]
                if len(p6):
                    pk = cl[cl.index <= ld - pd.Timedelta(days=180)].max()
                    early.append(p6.iloc[-1] / pk - 1 <= -0.5)
        L.append(f"{g:9}{len(items):>4}{np.median(lvp):>8.0f}%{np.median(f30):>9.0f}%{100*np.mean(early) if early else 0:>10.0f}%")

    # [3] 결론: 필터 후보
    bs = stat.get('부실상폐', (0, {}))[1]; sv = stat.get('생존주', (0, {}))[1]
    L.append("\n[3] 부실상폐 vs 생존주 — 필터 변별력")
    for k in ('자본잠식', '완전잠식', '연속적자', '적자'):
        L.append(f"  {k:6}: 부실 {bs.get(k,0):.0f}% vs 생존 {sv.get(k,0):.0f}%  (차이 {bs.get(k,0)-sv.get(k,0):+.0f}%p)")
    L.append("\n→ 종목선정 부실회피 필터(제안):")
    L.append("  ① 자본잠식(자본총계<자본금) 종목 제외 — 최강 신호")
    L.append("  ② 2년 연속 순손실 제외")
    L.append("  ③ 고점대비 -50%+ & 하락하는 200일선 = 위험(리스크오프 트리거)")
    L.append("  + AI 정성: 감사의견 거절·횡령·관리종목 공시 뉴스 스캔")
    rep = "\n".join(L)
    print(rep)
    if telegram:
        try:
            cc = sqlite3.connect(P('lassi.db'), timeout=30)
            r = cc.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); cc.close()
            from base.telegram_bot import TelegramNotifier
            TelegramNotifier(r[0], r[1]).send_message(rep); print("텔레그램 ✓")
        except Exception as e:
            print("텔레그램 실패", e)


if __name__ == '__main__':
    main('--telegram' in sys.argv)
