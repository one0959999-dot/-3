"""★알고리즘 v1.0 — 완성본 (종목선정 = 매매 통합, 단일 소스).

동결규칙 v3 (검증: 아티팩트 제거·파라미터 9/9·롤링 5/6·연도 7/11·2026 OOS·미국 메커니즘 확인):
  [유니버스] KOSPI+KOSDAQ 전체
  [제외] 자본잠식·2년연속적자(재무랙+1년) / 정체가격(126일 무변동일>20%) / 거래일<100/126
  [필수] 가격 > 상승중인 200일선 (추세 = 핵심 엔진이자 부실 방어막)
  [선호] 원시수익률 126일 변동성 낮은 순
  [보유] 상위 50종목 동일비중 / 분기(63거래일) 리밸런스 / 신호 다음날(t+1) 체결
  [금지] 시장타이밍·손절·리스크오프 오버레이 (검증에서 전부 수익 파괴)

사용:
  python KR/algo_v1.py            # 최신 데이터 기준 50종목 선정 출력
  python KR/algo_v1.py --telegram # + 텔레그램 전송
백테스트·라이브가 같은 select() 함수를 쓰는 것이 원칙 (재구현 금지).
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
N_PICK = 50; STALE_MAX = 0.20; MIN_DAYS = 100; REBAL_DAYS = 63


def load_financial_flags():
    """부실 첫 해(자본잠식 or 2년연속적자) + 품질(최근 흑자) — 재무랙 반영은 호출부에서."""
    c = sqlite3.connect(P('lassi.db'))
    rows = {}
    for t, y, cap, pi, ni in c.execute('SELECT ticker,year,capital,paidin,netincome FROM financials_dart'):
        rows.setdefault(t, {})[y] = (cap, pi, ni)
    c.close()
    return rows, {t: sorted(d) for t, d in rows.items()}


def is_bad(fin, fy, t, year):
    """year 시점에 공시로 알 수 있는 부실 여부 (재무랙: year-2까지 확정공시 + 공시지연 1년)."""
    run = 0
    for y in fy.get(t, []):
        if y > year - 2:
            break
        cap, pi, ni = fin[t][y]
        imp = (cap is not None and cap < 0) or (cap is not None and pi not in (None, 0) and 0 <= cap < pi)
        run = run + 1 if (ni is not None and ni < 0) else 0
        if imp or run >= 2:
            return True
    return False


def is_quality(fin, fy, t, year):
    ys = [y for y in fy.get(t, []) if y <= year - 1]
    if not ys:
        return True  # 재무 없음 = 통과 (커버리지 편향 방지)
    cap, pi, ni = fin[t][ys[-1]]
    return ni is not None and ni > 0 and cap not in (None, 0)


def select(panel, fin, fy, as_of=None):
    """동결규칙 v3 선정 — panel: DataFrame[date x ticker] close (원시, ffill 금지).
    반환: 저변동성 순 50종목 리스트."""
    if as_of is not None:
        panel = panel[panel.index <= as_of]
    if len(panel) < 260:
        raise ValueError('데이터 부족 (260일+ 필요)')
    yr = panel.index[-1].year
    c = panel
    ma = c.rolling(200, min_periods=120).mean()
    trend_ok = (c.iloc[-1] > ma.iloc[-1]) & (ma.iloc[-1] > ma.iloc[-21])
    ret = c.pct_change()
    vol = ret.iloc[-126:].std()
    zero = ((ret.iloc[-126:] == 0) & c.iloc[-126:].notna()).sum()
    days = c.iloc[-126:].notna().sum()
    stale_ok = (zero / days.replace(0, np.nan)) < STALE_MAX
    active_ok = days >= MIN_DAYS
    live_ok = c.iloc[-5:].notna().any()  # 최근 거래 존재
    cand = [t for t in c.columns
            if bool(trend_ok.get(t, False)) and bool(stale_ok.get(t, False))
            and bool(active_ok.get(t, False)) and bool(live_ok.get(t, False))
            and not np.isnan(vol.get(t, np.nan))
            and not is_bad(fin, fy, t, yr) and is_quality(fin, fy, t, yr)]
    cand.sort(key=lambda t: vol[t])
    return cand[:N_PICK]


def latest_panel():
    full = pickle.load(open(P('data_cache_kr_full.pkl'), 'rb'))
    cl = {t: df['close'] for t, df in full.items()}
    panel = pd.DataFrame(cl).sort_index()
    f26 = P('data_cache_kr_2026.pkl')
    if os.path.exists(f26):
        d26 = pickle.load(open(f26, 'rb')); d26.pop('__INDEX__', None)
        ext = {}
        for t, s in d26.items():
            if t in panel.columns:
                old = panel[t].dropna()
                common = old.index.intersection(s.dropna().index)
                if len(common):
                    f = float(old[common[-1]]) / float(s[common[-1]])
                    ext[t] = s[s.index > old.index[-1]] * f
        if ext:
            panel = pd.concat([panel, pd.DataFrame(ext).reindex(columns=panel.columns).sort_index()])
    return panel


def main(telegram=False):
    fin, fy = load_financial_flags()
    panel = latest_panel()
    picks = select(panel, fin, fy)
    names = {}
    try:
        c = sqlite3.connect(P('lassi.db'))
        names = {t: n for t, n in c.execute('SELECT ticker,name FROM kr_ticker_cache')}
        c.close()
    except Exception:
        pass
    L = [f"📌 알고리즘 v1.0 선정 50종목 (기준일 {panel.index[-1].date()}, 동일비중·분기리밸)", ""]
    for i, t in enumerate(picks, 1):
        L.append(f"{i:3}. {t} {names.get(t, '')}")
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
