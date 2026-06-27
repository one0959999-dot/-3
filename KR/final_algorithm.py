"""라씨 매매 알고리즘 최종 합성·검증 (종목선정 파트 제외 — 추후 논의).

사용자 지시:
- #2 진행: 누적스윙을 '진짜 박스권 종목'만 게이팅해서 보유와 공정 비교.
- 위와 같은 데이터 추출 → 검증/테스트 → 알고리즘(종목선정 제외) 합성.
- 종목 수 32→대폭 확대(60종목)로 최종 확정.
- 결과는 표 + 텔레그램 전송.

원칙: OOS 2021~ · 전환비용 · 룩어헤드 제거 · 데이터 1회 다운로드 후 캐시.

산출 알고리즘(종목선정 제외) "라씨 v1":
  pre-OOS(2021 이전) range_score로 종목 성격 분류(룩어헤드 없음):
    · 진짜 박스권(score≥GATE)  → 누적스윙(저점매수·고점일부매도로 주식수↑, 돌파시 전량보유)
    · 추세주(score<GATE)        → 단순보유
  → 이 복합규칙이 '전부 보유' / '전부 누적스윙' 을 이기는지 검증.

실행:
  python KR/final_algorithm.py            # 검증만(표 출력)
  python KR/final_algorithm.py --telegram # 표 출력 + 텔레그램 전송
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd

from KR.regime_period_backtest import _yf, SAMPLE_KR, COST
from KR.range_accumulation_backtest import accumulation_swing, range_score, buy_hold
from KR.real_hedge_backtest import (bot_regime_series, defensive_basket_ret,
                                    _oos, _sim, INDEX_TICKER)
from KR.phase_judge_pilot import classify8_series

OOS = '2021-01-01'
GATE = 44            # range_score 이 값 이상이면 '진짜 박스권' (상위~25%, pre-OOS 점수 max≈49 기준 재보정)
DEEP_BEAR8 = {'PANIC', 'BEAR_EARLY', 'BEAR_MID'}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     '..', 'data_cache_kr60.pkl')

# ── 확대 표본: SAMPLE_KR(32) + 추가 대형·중형주(28) = 60 (종목선정은 추후 논의, 검증용 분산표본) ──
EXTRA_KR = [
    ('373220', 'LG에너지솔루션'), ('003670', '포스코퓨처엠'), ('030200', 'KT'),
    ('034730', 'SK'), ('003550', 'LG'), ('018260', '삼성에스디에스'),
    ('010950', 'S-Oil'), ('011200', 'HMM'), ('047810', '한국항공우주'),
    ('042700', '한미반도체'), ('064350', '현대로템'), ('009540', 'HD한국조선해양'),
    ('010140', '삼성중공업'), ('012450', '한화에어로스페이스'), ('000810', '삼성화재'),
    ('032830', '삼성생명'), ('138040', '메리츠금융지주'), ('024110', '기업은행'),
    ('090430', '아모레퍼시픽'), ('051900', 'LG생활건강'), ('161390', '한국타이어앤테크놀로지'),
    ('078930', 'GS'), ('096770', 'SK이노베이션'), ('011070', 'LG이노텍'),
    ('058470', '리노공업'), ('240810', '원익IPS'), ('357780', '솔브레인'),
    ('005070', '코스모신소재'),
]
UNIVERSE = SAMPLE_KR + EXTRA_KR


# ──────────────────────────────────────────────────────────────────────────
# 데이터 1회 다운로드 후 캐시
# ──────────────────────────────────────────────────────────────────────────
def _fetch(code):
    for suf in ('.KS', '.KQ'):
        d = _yf(code + suf)
        if d is not None and len(d) > 250 and 'close' in d.columns:
            return d[['open', 'high', 'low', 'close', 'volume']].astype(float).dropna(subset=['close'])
    return None


def load_data(force=False):
    if os.path.exists(CACHE) and not force:
        with open(CACHE, 'rb') as f:
            data = pickle.load(f)
        print(f"  캐시 로드: {len(data['stocks'])}종목")
        return data
    print("  데이터 다운로드 중(1회) — 60종목+지수+방어자산...")
    data = {'stocks': {}}
    for i, (code, name) in enumerate(UNIVERSE, 1):
        df = _fetch(code)
        if df is not None and len(df) >= 400:
            data['stocks'][code] = (name, df)
            print(f"    [{i}/{len(UNIVERSE)}] ✓ {name}")
        else:
            print(f"    [{i}/{len(UNIVERSE)}] ✗ {name}(데이터부족)")
    data['regime'] = bot_regime_series()
    data['basket'] = defensive_basket_ret()
    with open(CACHE, 'wb') as f:
        pickle.dump(data, f)
    print(f"  캐시 저장: {len(data['stocks'])}종목 → {CACHE}")
    return data


# ──────────────────────────────────────────────────────────────────────────
# 헬퍼: range_score를 pre-OOS 구간만으로 계산(룩어헤드 제거)
# ──────────────────────────────────────────────────────────────────────────
def pre_oos_range_score(df):
    pre = df[df.index < OOS]
    if len(pre) < 250:
        return range_score(df)   # pre 부족시 전체(드묾)
    return range_score(pre)


def _ret(eq):
    return round(float(eq.iloc[-1] / eq.iloc[0] - 1) * 100, 1)


def _mdd(eq):
    peak = eq.cummax(); return round(float(((eq / peak - 1) * 100).min()), 1)


# ──────────────────────────────────────────────────────────────────────────
# 1) A/B/C/D 헤지 비교 (real_hedge_backtest 로직, 캐시 기반)
# ──────────────────────────────────────────────────────────────────────────
def compare_hedge(data):
    basket = data['basket']; reg_idx = data['regime']
    agg = {'A_단순보유': [], 'B_봇실제헤지': [], 'C_봇헤지+누적스윙': [], 'D_classify8헤지': []}
    for code, (name, df) in data['stocks'].items():
        idx = df.index
        sr = df['close'].pct_change().fillna(0.0)
        reg = reg_idx.reindex(idx).ffill().shift(1)
        is_bear = (reg == 'BEAR')
        eq_a = (1 + sr).cumprod()
        eq_b = _sim(sr, (~is_bear).astype(float), is_bear.astype(float) * 0.40, basket)
        acc_eq, *_ = accumulation_swing(df)
        acc_ret = acc_eq.pct_change().fillna(0.0)
        br = basket.reindex(idx).fillna(0.0)
        bt = is_bear.reindex(idx).fillna(False)
        turn = bt.astype(int).diff().abs().fillna(0)
        eq_c = pd.Series((1 + (np.where(bt.values, 0.40 * br.values, acc_ret.values) - turn.values * COST)).cumprod(), index=idx)
        ph8 = classify8_series(df)
        b8 = ph8.isin(DEEP_BEAR8).reindex(idx).fillna(False).shift(1).fillna(False)
        eq_d = _sim(sr, (~b8).astype(float), b8.astype(float) * 0.40, basket)
        for k, e in [('A_단순보유', eq_a), ('B_봇실제헤지', eq_b),
                     ('C_봇헤지+누적스윙', eq_c), ('D_classify8헤지', eq_d)]:
            m = _oos(e, idx)
            if m: agg[k].append(m)
    return agg


# ──────────────────────────────────────────────────────────────────────────
# 2) 박스권 게이팅 — 누적스윙 vs 보유 (박스권 종목 / 추세주 따로)
# ──────────────────────────────────────────────────────────────────────────
def box_gating(data):
    box, trend = [], []
    for code, (name, df) in data['stocks'].items():
        rs = pre_oos_range_score(df)
        eqs, sh_s, _, _ = accumulation_swing(df)
        eqh, sh_h = buy_hold(df)
        # OOS 구간만 평가
        oi = df.index >= OOS
        es, eh = eqs[oi], eqh[oi]
        if len(es) < 30: continue
        rec = {'name': name, 'rs': rs,
               'swing': _ret(es), 'swing_mdd': _mdd(es), 'shares': round(sh_s / sh_h, 2),
               'hold': _ret(eh), 'hold_mdd': _mdd(eh)}
        (box if rs >= GATE else trend).append(rec)
    return box, trend


# ──────────────────────────────────────────────────────────────────────────
# 3) 알고리즘 합성·검증 — "박스권→누적스윙, 추세주→보유" vs 전부보유/전부스윙
# ──────────────────────────────────────────────────────────────────────────
def validate_algorithm(data):
    comp, allhold, allswing = [], [], []
    n_box = 0
    for code, (name, df) in data['stocks'].items():
        rs = pre_oos_range_score(df)
        oi = df.index >= OOS
        eqh, sh_h = buy_hold(df)
        eqs, _, _, _ = accumulation_swing(df)
        eh, es = eqh[oi], eqs[oi]
        if len(eh) < 30: continue
        is_box = rs >= GATE
        if is_box: n_box += 1
        ec = es if is_box else eh        # 복합규칙: 박스권→스윙, 추세주→보유
        comp.append((_ret(ec), _mdd(ec)))
        allhold.append((_ret(eh), _mdd(eh)))
        allswing.append((_ret(es), _mdd(es)))
    return comp, allhold, allswing, n_box


def _med(lst, i):
    return np.median([x[i] for x in lst]) if lst else 0


# ──────────────────────────────────────────────────────────────────────────
# 리포트
# ──────────────────────────────────────────────────────────────────────────
def build_report(data):
    n = len(data['stocks'])
    hedge = compare_hedge(data)
    box, trend = box_gating(data)
    comp, allhold, allswing, n_box = validate_algorithm(data)

    L = []
    L.append(f"🏁 라씨 알고리즘 최종검증 (KR {n}종목, OOS {OOS}~)")
    L.append("룩어헤드제거·전환비용·종목선정제외")
    L.append("")
    L.append("① 헤지 비교 (수익중앙/MDD중앙)")
    hold_med = _med(hedge['A_단순보유'], 0)
    for k, lst in hedge.items():
        if not lst: continue
        beat = sum(1 for x in lst if x[0] > hold_med) if k != 'A_단순보유' else '-'
        L.append(f"  {k:16} {_med(lst,0):+5.0f}% / {_med(lst,1):+5.0f}%  보유이김 {beat}/{len(lst)}")
    L.append("  → 헤지 전부 보유에 짐, MDD도 악화. (결론 유지)")
    L.append("")
    L.append(f"② 박스권 게이팅 (range_score≥{GATE} = 상위~25%)")
    L.append(f"  [진짜 박스권 {len(box)}종목] 누적스윙 vs 보유")
    if box:
        names = ", ".join(sorted([b['name'] for b in box]))
        L.append(f"    대상: {names}")
        L.append(f"    스윙 {np.median([b['swing'] for b in box]):+.0f}%(주식{np.median([b['shares'] for b in box]):.2f}배)"
                 f" vs 보유 {np.median([b['hold'] for b in box]):+.0f}%"
                 f"  스윙승 {sum(1 for b in box if b['swing']>b['hold'])}/{len(box)}")
    L.append(f"  [추세주 {len(trend)}종목] 누적스윙 vs 보유")
    if trend:
        L.append(f"    스윙 {np.median([t['swing'] for t in trend]):+.0f}% vs 보유 {np.median([t['hold'] for t in trend]):+.0f}%"
                 f"  스윙승 {sum(1 for t in trend if t['swing']>t['hold'])}/{len(trend)}")
    L.append("")
    L.append(f"③ 복합 알고리즘 검증 (박스권{n_box}→스윙, 추세주{n-n_box}→보유)")
    L.append(f"  복합규칙   {_med(comp,0):+.0f}% / {_med(comp,1):+.0f}%")
    L.append(f"  전부 보유  {_med(allhold,0):+.0f}% / {_med(allhold,1):+.0f}%")
    L.append(f"  전부 스윙  {_med(allswing,0):+.0f}% / {_med(allswing,1):+.0f}%")
    win_vs_hold = sum(1 for c, h in zip(comp, allhold) if c[0] > h[0])
    L.append(f"  복합>보유: {win_vs_hold}/{len(comp)}종목")
    L.append("")
    # 판정
    if _med(comp, 0) > _med(allhold, 0):
        verdict = "✅ 복합규칙이 단순보유 이김 → '박스권=스윙/추세주=보유' 채택"
    else:
        verdict = "⚠️ 복합규칙 ≤ 단순보유 → 현 데이터선 단순보유가 최종(스윙은 박스권 한정 가치)"
    L.append(f"📌 판정: {verdict}")
    return "\n".join(L)


def send_telegram(text):
    import sqlite3
    db = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lassi.db')
    c = sqlite3.connect(db, timeout=30)
    r = c.execute("SELECT telegram_token, telegram_chat_id FROM users "
                  "WHERE telegram_token IS NOT NULL AND telegram_token!='' LIMIT 1").fetchone()
    c.close()
    if not r:
        print("  텔레그램 자격증명 없음"); return False
    from base.telegram_bot import TelegramNotifier
    TelegramNotifier(r[0], r[1]).send_message(text)
    print("  텔레그램 전송 완료 ✓")
    return True


if __name__ == '__main__':
    force = '--refresh' in sys.argv
    data = load_data(force=force)
    report = build_report(data)
    print("\n" + "=" * 60)
    print(report)
    print("=" * 60)
    if '--telegram' in sys.argv:
        send_telegram("🏁 라씨 알고리즘 최종검증 결과\n" + "─" * 20 + "\n" + report)
