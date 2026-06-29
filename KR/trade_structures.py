"""정답지에 더 가까워지는 매매구조 비교 — 1000만 한 종목, 15종목.

구조:
 A 단순보유   : 첫날 매수 후 끝까지 보유(손 안댐).
 B 순수스윙   : 바닥 전액매수/천장 전액매도 (=기존 봇, 승자 통째 팔아 패배).
 D 국면리스크오프: 평소 풀투자, 봇 '하락국면'에서만 현금화, 비하락이면 재진입. (③ 리스크관리)
 E 부분트림   : 천장에 50%만 매도(50% 코어유지), 바닥에 재매수.
 F 코어70스윙30: 70% 절대보유 + 30%만 바닥매수/천장매도 스윙.
1주단위·실거래비용. 목적: 승자 상승은 먹고 하락만 피해 보유를 이기는 구조가 있나?

실행: python KR/trade_structures.py [--telegram]
"""
import sys, os, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.detect_trade_score_v2 import feats, bottom_score, top_score, vol_of
from KR.detect_realtime_bot import bot_realtime_phase, GROUP
from KR.trade_sim_allin import sim_bot, sim_hold, STOCKS, BUY_COST, SELL_COST, END, INIT


def _sig(df):
    d = df[df.index <= END]; c = d['close']; F = feats(d); vol = vol_of(c)
    return d, c, bottom_score(F, vol), top_score(F, vol)


def sim_riskoff(df):
    d = df[df.index <= END]; c = d['close']
    try:
        ph = bot_realtime_phase(d); grp = ph.reindex(c.index).map(lambda p: GROUP.get(p, '횡보'))
    except Exception:
        grp = pd.Series('상승', index=c.index)
    cash = float(INIT); sh = 0
    for i in range(len(c)):
        p = float(c.iloc[i])
        if i < 60 or p <= 0 or np.isnan(p):
            continue
        bear = grp.iloc[i] == '하락'
        if bear and sh > 0:
            cash += sh * p * (1 - SELL_COST); sh = 0
        elif not bear and sh == 0:
            q = int(cash // (p * (1 + BUY_COST)))
            if q > 0:
                cash -= q * p * (1 + BUY_COST); sh = q
    return cash + sh * float(c.iloc[-1])


def sim_trim(df, sell_frac=0.5):
    d, c, bs, ts = _sig(df)
    p0 = float(c.iloc[0]); sh = int(INIT // (p0 * (1 + BUY_COST))); cash = INIT - sh * p0 * (1 + BUY_COST)
    trimmed = False
    for i in range(len(c)):
        p = float(c.iloc[i])
        if i < 60 or p <= 0 or np.isnan(p):
            continue
        if not trimmed and ts.iloc[i] >= 2 and sh > 0:
            q = int(sh * sell_frac); cash += q * p * (1 - SELL_COST); sh -= q; trimmed = True
        elif trimmed and bs.iloc[i] >= 2:
            q = int(cash // (p * (1 + BUY_COST)))
            if q > 0:
                cash -= q * p * (1 + BUY_COST); sh += q; trimmed = False
    return cash + sh * float(c.iloc[-1])


def sim_core_swing(df, core=0.7):
    """core 비율은 첫날 사서 절대보유, 나머지로만 스윙."""
    d, c, bs, ts = _sig(df)
    p0 = float(c.iloc[0])
    core_cash = INIT * core; swing_cash = INIT - core_cash
    core_sh = int(core_cash // (p0 * (1 + BUY_COST)))
    cash = INIT - core_sh * p0 * (1 + BUY_COST)  # 남은 현금(코어잔돈+스윙몫)
    swing_sh = 0; deploy = swing_cash
    cash = cash  # 스윙은 deploy 한도 내에서
    sc = swing_cash
    for i in range(len(c)):
        p = float(c.iloc[i])
        if i < 60 or p <= 0 or np.isnan(p):
            continue
        if swing_sh == 0 and bs.iloc[i] >= 2:
            q = int(sc // (p * (1 + BUY_COST)))
            if q > 0:
                sc -= q * p * (1 + BUY_COST); swing_sh = q
        elif swing_sh > 0 and ts.iloc[i] >= 2:
            sc += swing_sh * p * (1 - SELL_COST); swing_sh = 0
    last = float(c.iloc[-1])
    return core_sh * last + swing_sh * last + sc + (cash - swing_cash)


def main(telegram=False):
    P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb')); big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
    dfs = {}
    for dd in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in dd.get(mk, {}).items():
                dfs.setdefault(c, (n, df))
    idx_df = wf['index']['KOSPI']
    cols = ['보유', '순수스윙', '리스크오프', '부분트림', '코어70']
    L = ["🏗️ 정답지에 가까워지는 매매구조 — 1000만 한종목 (15종목, 1주·실비용)", ""]
    L.append(f"{'종목':11}" + "".join(f"{c:>10}" for c in cols))
    L.append("-" * 62)
    tot = {c: 0 for c in cols}; winbeat = {c: 0 for c in cols}; n = 0
    for code in STOCKS:
        if code == '지수':
            nm, df = 'KOSPI지수', idx_df
        elif code in dfs:
            nm, df = dfs[code]
        else:
            continue
        if len(df[df.index <= END]) < 250:
            continue
        v = {'보유': sim_hold(df), '순수스윙': sim_bot(df), '리스크오프': sim_riskoff(df),
             '부분트림': sim_trim(df), '코어70': sim_core_swing(df)}
        n += 1
        for c in cols:
            tot[c] += v[c]
            if v[c] >= v['보유']:
                winbeat[c] += 1
        L.append(f"{nm:11}" + "".join(f"{v[c]/1e4:>9,.0f}만" for c in cols))
    L.append("-" * 62)
    L.append(f"{'평균':11}" + "".join(f"{tot[c]/n/1e4:>9,.0f}만" for c in cols))
    L.append(f"\n[보유 이긴 종목수 / {n}]")
    for c in cols:
        if c == '보유':
            continue
        L.append(f"  {c:8}: {winbeat[c]}/{n} 종목")
    L.append("\n※ 정답지(완벽)=평균 수조원(환상). 현실 구조 중 보유를 이기는 게 있나 확인.")
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
