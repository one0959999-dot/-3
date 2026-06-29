"""2단계: 봇 매매기법 vs AI 매매 — 정답지(이상 바닥/천장)에 얼마나 가까이 사고팔았나 100점 채점.

같은 15종목(국면 선정때 쓴 것). 정답지=zigzag 전환점(바닥=매수정답/천장=매도정답).
봇: 매매기법(RSI<30·볼린저하단·이격·낙폭·거래량·MACD골든 → 2개+ 매수 / 반대 매도) 신호.
AI: 알아서(가격+코스피정세+금리, 날짜익명) BUY/SELL/HOLD 판단.
채점: 각 전환점에 최근접 신호의 가격슬리피지 → 100-슬리피지%*5 (미포착 0). 종목평균 → 봇/AI 종합점수.
완료시 텔레그램 전송.

실행: python KR/detect_trade_score.py
"""
import sys, os, re, pickle, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.answer_sheet import zigzag
from KR.detect_bot import indicators, bottom_signals, top_signals, signal_days
from KR.detect_realtime_ai import rate_series, gemini

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
END = '2025-12-31'; WINDOW = 90
STOCKS = ['지수', '005930', '247540', '000660', '035420', '051910', '005380', '068270',
          '105560', '042660', '034020', '196170', '028300', '011200', '012450']


def score(answer_pts, buy_days, sell_days, close):
    """전환점별 최근접 신호 슬리피지 → 100점. 반환 (score, 포착수, 총점수, 평균슬리피지)."""
    sc = []; slips = []; caught = 0
    for d, t, price in answer_pts:
        if d > pd.Timestamp(END):
            continue
        sigs = buy_days if t == 'L' else sell_days
        cand = [s for s in sigs if abs((s - d).days) <= WINDOW]
        if not cand:
            sc.append(0); continue
        near = min(cand, key=lambda s: abs((s - d).days))
        sp = float(close.reindex([near]).ffill().iloc[0])
        slip = max(0.0, (sp / price - 1) * 100) if t == 'L' else max(0.0, (price / sp - 1) * 100)
        slips.append(slip); caught += 1
        sc.append(max(0.0, 100 - slip * 5))
    return (np.mean(sc) if sc else 0), caught, len(sc), (np.mean(slips) if slips else 0)


def ai_trade(name, df, idxc, rate, gem):
    """AI BUY/SELL/HOLD 월별(날짜익명) → 매수/매도 신호일."""
    c = df['close'][df['close'].index <= END]
    months = pd.Series(c.index, index=c.index).groupby([c.index.year, c.index.month]).last().values
    pts = [pd.Timestamp(d) for d in months if c.index.get_loc(pd.Timestamp(d)) >= 200]
    rows = []; tags = []
    g = c.diff().clip(lower=0).rolling(14).mean(); l = (-c.diff().clip(upper=0)).rolling(14).mean()
    rsi_s = 100 - 100 / (1 + g / (l + 1e-9))
    ic = idxc.reindex(c.index).ffill()
    for j, d in enumerate(pts):
        i = c.index.get_loc(d)
        r1 = (c.iloc[i]/c.iloc[i-21]-1)*100; r3 = (c.iloc[i]/c.iloc[i-63]-1)*100; r12 = (c.iloc[i]/c.iloc[i-252]-1)*100
        rsi = float(rsi_s.iloc[i]); vs200 = (c.iloc[i]/c.iloc[max(0,i-200):i+1].mean()-1)*100
        m3 = (ic.iloc[i]/ic.iloc[i-63]-1)*100 if i>=63 else 0
        rt = float(rate.reindex([d]).ffill().iloc[0])
        tag = f"P{j+1:03d}"; tags.append((tag, d))
        rows.append(f"{tag}: 1M{r1:+.0f} 3M{r3:+.0f} 12M{r12:+.0f} RSI{rsi:.0f} 200MA{vs200:+.0f}% 코스피3M{m3:+.0f} 금리{rt:.1f}%")
    prompt = ("너는 트레이더다. 각 구간서 '바닥이라 매수할 때'면 BUY, '천장이라 매도할 때'면 SELL, 아니면 HOLD."
              " 미래정보 없음, 추세·정세·금리만. 최저점 매수·최고점 매도가 목표.\n"
              "구간(시간순, 날짜익명):\n" + "\n".join(rows) + "\n\n형식대로 모든구간(설명금지):\nP###=BUY/SELL/HOLD")
    txt = gem.generate_content(prompt, temperature=0.2)
    t2d = {t: d for t, d in tags}; buys, sells = [], []
    for m in re.finditer(r'(P\d{3})\s*=\s*(BUY|SELL|HOLD)', txt):
        if m.group(1) in t2d:
            if m.group(2) == 'BUY': buys.append(t2d[m.group(1)])
            elif m.group(2) == 'SELL': sells.append(t2d[m.group(1)])
    return buys, sells


def tg(msg):
    c = sqlite3.connect(P('lassi.db'), timeout=30)
    r = c.execute("SELECT telegram_token, telegram_chat_id FROM users WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone(); c.close()
    if r:
        from base.telegram_bot import TelegramNotifier
        TelegramNotifier(r[0], r[1]).send_message(msg)


def main():
    wf = pickle.load(open(P('data_cache_wf.pkl'), 'rb')); big = pickle.load(open(P('data_cache_big.pkl'), 'rb'))
    dfs = {}
    for d in (big, wf):
        for mk in ('KOSPI', 'KOSDAQ'):
            for c, (n, df) in d.get(mk, {}).items():
                dfs.setdefault(c, (n, df))
    idxc = wf['index']['KOSPI']['close']; rate = rate_series(); gem = gemini()
    L = ["🎯 2단계: 봇 매매기법 vs AI — 정답지 매수/매도 근접도 100점", "(같은 15종목, 정답지=zigzag 바닥/천장, 100점=정답가에 정확)", ""]
    bot_all, ai_all = [], []
    for code in STOCKS:
        if code == '지수':
            nm, df = 'KOSPI지수', wf['index']['KOSPI']
        elif code in dfs:
            nm, df = dfs[code]
        else:
            continue
        d = df[df.index <= END]
        ans = zigzag(d['close'], 0.20)
        if len(ans) < 2:
            continue
        ind = indicators(d)
        sb, _ = bottom_signals(ind); st, _ = top_signals(ind)
        bot_buy = signal_days(sb, 2); bot_sell = signal_days(st, 2)
        bs, bc, bn, bsl = score(ans, bot_buy, bot_sell, d['close'])
        ai_buy, ai_sell = ai_trade(nm, d, idxc, rate, gem)
        as_, ac, an, asl = score(ans, ai_buy, ai_sell, d['close'])
        bot_all.append(bs); ai_all.append(as_)
        L.append(f"{nm:10} 봇 {bs:4.0f}점(슬립{bsl:.0f}%) · AI {as_:4.0f}점(슬립{asl:.0f}%)")
    L.append("")
    L.append(f"━━ 종합(15종목 평균) ━━")
    L.append(f"  봇  {np.mean(bot_all):.0f}점 / 100")
    L.append(f"  AI  {np.mean(ai_all):.0f}점 / 100")
    L.append(f"\n승자: {'봇' if np.mean(bot_all)>np.mean(ai_all) else 'AI'} (100점=정답 바닥/천장에 정확히 매매)")
    rep = "\n".join(L)
    print(rep); tg(rep)


if __name__ == '__main__':
    main()
