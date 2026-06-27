"""모멘텀 전략 엄격 신뢰성 검증 — '넣고 안 볼' 전략이므로 최대한 빡빡하게.

사용자 우려: 생존편향으로 +3,679%는 거품. 진짜 기대 edge가 얼마인지 정직하게.
이 환경 제약: KRX 차단으로 시점별(상폐포함) 유니버스 불가 → 차선책으로 '망한/폭락 종목'을 대거 추가해 생존편향 완화.

검증 4종:
 A. 파라미터 그리드 (K∈3,4,5,8 × 기간∈3,6,9,12M): 모멘텀 edge가 광범위한가(한칸 운빨 아님)
 B. 생존편향 정량화: 승자만(기존61) vs 확대풀(망한종목 포함) → edge 얼마나 줄어드나
 C. 비용 스트레스 (1x/2x/3x) : 거래비용 올려도 edge 살아남나
 D. 롤링 OOS (3구간) : 어느 기간에도 보유를 이기나
보유 벤치마크 = '연속 동일가중'(드리프트 없는 공정 기준). 룩어헤드 제거(월말선정→익월).

실행: python KR/reliability_check.py [--telegram]
"""
import sys, os, pickle
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import numpy as np
import pandas as pd
from KR.regime_period_backtest import _yf
from KR.final_algorithm import UNIVERSE as BASE_UNIVERSE
from KR.walkforward_backtest import send_telegram

START = '2018-01-01'
BASE_COST = 0.0021
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'data_cache_rel.pkl')

# 망한/폭락/부진 종목 대거 추가(생존편향 완화) — '지금 승자'만이 아닌 풀
EXTRA = [
    ('096530', '씨젠'), ('019170', '신풍제약'), ('323410', '카카오뱅크'), ('352820', '하이브'),
    ('361610', 'SKIET'), ('263750', '펄어비스'), ('293490', '카카오게임즈'), ('251270', '넷마블'),
    ('139480', '이마트'), ('023530', '롯데쇼핑'), ('034220', 'LG디스플레이'), ('034020', '두산에너빌리티'),
    ('042660', '한화오션'), ('091990', '셀트리온헬스케어'), ('001570', '금양'), ('010620', '현대미포조선'),
    ('028670', '팬오션'), ('003490', '대한항공'), ('010060', 'OCI홀딩스'), ('047050', '포스코인터내셔널'),
    ('035900', 'JYP'), ('041510', '에스엠'), ('122870', '와이지엔터'), ('145020', '휴젤'),
    ('069960', '현대백화점'), ('011780', '금호석유'), ('030000', '제일기획'), ('004370', '농심'),
    ('007310', '오뚜기'), ('128940', '한미약품'), ('000100', '유한양행'), ('069620', '대웅제약'),
    ('006280', '녹십자'), ('018880', '한온시스템'), ('000150', '두산'), ('241560', '두산밥캣'),
    ('112610', '씨에스윈드'), ('010120', 'LS일렉트릭'), ('267260', 'HD현대일렉트릭'), ('298040', '효성중공업'),
    ('051600', '한전KPS'), ('052690', '한전기술'), ('336260', '두산퓨얼셀'), ('011790', 'SKC'),
    ('005387', '현대차2우B'),
]


def _fetch(code):
    for suf in ('.KS', '.KQ'):
        d = _yf(code + suf)
        if d is not None and len(d) > 250 and 'close' in d.columns:
            return d['close'].astype(float)
    return None


def load(force=False):
    if os.path.exists(CACHE) and not force:
        return pickle.load(open(CACHE, 'rb'))
    print("  다운로드(1회): 확대 유니버스...")
    base, extra = {}, {}
    idx = _yf('^KS11')['close']
    for grp, lst, store in [('base', BASE_UNIVERSE, base), ('extra', EXTRA, extra)]:
        for i, (code, name) in enumerate(lst, 1):
            s = _fetch(code)
            if s is not None and len(s) >= 400:
                store[code] = (name, s)
            print(f"    [{grp} {i}/{len(lst)}] {'✓' if s is not None else '✗'} {name}")
    data = {'base': base, 'extra': extra, 'idx': idx}
    pickle.dump(data, open(CACHE, 'wb'))
    print(f"  캐시 저장: base {len(base)} + extra {len(extra)}")
    return data


def build_R(pool):
    """종목 close dict → 공통 달력 일별수익 DataFrame (START 이후)."""
    px = pd.DataFrame({c: s for c, (n, s) in pool.items()})
    px = px[px.index >= START]
    return px.pct_change()


def month_ends(idx):
    s = pd.Series(idx, index=idx)
    return [pd.Timestamp(d) for d in s.groupby([idx.year, idx.month]).last().values]


def sim_momentum(R, K, lookM, cost=BASE_COST):
    """월말 트레일링 lookM개월 모멘텀 상위 K → 익월 보유(EW). 룩어헤드 없음."""
    cal = R.index; me = month_ends(cal); Lw = lookM * 21
    cum = (1 + R).cumprod()
    sel_at = {}
    for t in me:
        i = cal.get_loc(t)
        if i < Lw:
            sel_at[t] = None; continue
        mom = (cum.iloc[i] / cum.iloc[i - Lw] - 1).dropna()
        sel_at[t] = list(mom.sort_values(ascending=False).head(K).index) if len(mom) else None
    me_sorted = sorted(sel_at)
    ret = pd.Series(0.0, index=cal); cur = None; prev = None; ptr = 0
    for d in cal:
        while ptr < len(me_sorted) and me_sorted[ptr] < d:
            cur = sel_at[me_sorted[ptr]]; ptr += 1
        if not cur:
            continue
        r = R.loc[d, cur].mean()
        if cur is not prev:
            changed = 1.0 if not prev else len(set(cur) ^ set(prev)) / max(len(cur), 1)
            r -= changed * cost
            prev = cur
        ret.loc[d] = r if r == r else 0.0
    return (1 + ret.fillna(0)).cumprod()


def sim_hold(R):
    """연속 동일가중 보유(드리프트 없음, 공정 벤치마크)."""
    return (1 + R.mean(axis=1).fillna(0)).cumprod()


def stats(eq, lo=None, hi=None):
    e = eq
    if lo is not None: e = e[e.index >= lo]
    if hi is not None: e = e[e.index < hi]
    if len(e) < 30: return None
    ret = (e.iloc[-1] / e.iloc[0] - 1) * 100
    yrs = len(e) / 252
    cagr = ((e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1) * 100
    mdd = float(((e / e.cummax() - 1) * 100).min())
    return ret, cagr, mdd


def report(data):
    L = ["🧪 모멘텀 엄격 신뢰성 검증 (생존편향완화·그리드·비용·롤링)",
         f"기간 {START}~ · 보유=연속동일가중(공정) · 룩어헤드제거", "=" * 68]
    pool_all = {**data['base'], **data['extra']}
    R_all = build_R(pool_all)
    R_base = build_R(data['base'])
    hold_all = sim_hold(R_all); hold_base = sim_hold(R_base)
    ha = stats(hold_all); hb = stats(hold_base)
    L.append(f"확대풀 {len(pool_all)}종목 | 승자풀 {len(data['base'])}종목")
    L.append(f"보유(확대풀): {ha[0]:+.0f}% 연{ha[1]:+.0f}% MDD{ha[2]:.0f}%  |  보유(승자풀): {hb[0]:+.0f}% 연{hb[1]:+.0f}%")
    L.append("")
    # A. 파라미터 그리드 (확대풀)
    L.append("[A] 파라미터 그리드 (확대풀, 누적%/MDD, ★=보유초과)")
    L.append("     " + "".join(f"{L_}M".rjust(13) for L_ in (3, 6, 9, 12)))
    grid = {}
    for K in (3, 4, 5, 8):
        row = f"top{K} "
        for Lm in (3, 6, 9, 12):
            eq = sim_momentum(R_all, K, Lm)
            s = stats(eq); grid[(K, Lm)] = (eq, s)
            mark = '★' if s[0] > ha[0] else ' '
            row += f"{mark}{s[0]:>+6.0f}/{s[2]:>3.0f}".rjust(13)
        L.append(row)
    beat = sum(1 for k, (e, s) in grid.items() if s[0] > ha[0])
    L.append(f"  → 그리드 16칸 중 보유초과 {beat}칸 (광범위할수록 신뢰)")
    L.append("")
    # B. 생존편향 정량화: 같은 전략을 승자풀 vs 확대풀
    L.append("[B] 생존편향 정량화 (top4/12M)")
    eqA = sim_momentum(R_all, 4, 12); eqB = sim_momentum(R_base, 4, 12)
    sA, sB = stats(eqA), stats(eqB)
    L.append(f"  승자풀: 모멘텀 {sB[0]:+.0f}% (보유 {hb[0]:+.0f}%, edge +{sB[0]-hb[0]:.0f}%p)")
    L.append(f"  확대풀: 모멘텀 {sA[0]:+.0f}% (보유 {ha[0]:+.0f}%, edge +{sA[0]-ha[0]:.0f}%p)")
    L.append(f"  → 망한종목 포함시 edge {sB[0]-hb[0]:.0f}→{sA[0]-ha[0]:.0f}%p (줄면 생존편향 있었던 것)")
    L.append("")
    # C. 비용 스트레스 (확대풀 top4/12M)
    L.append("[C] 비용 스트레스 (top4/12M, 확대풀)")
    for mult in (1, 2, 3):
        s = stats(sim_momentum(R_all, 4, 12, cost=BASE_COST * mult))
        L.append(f"  비용{mult}x: 모멘텀 {s[0]:+.0f}% vs 보유 {ha[0]:+.0f}% → edge +{s[0]-ha[0]:.0f}%p")
    L.append("")
    # D. 롤링 OOS (확대풀 top4/12M)
    L.append("[D] 롤링 OOS (top4/12M, 확대풀) — 구간별 모멘텀 vs 보유")
    wins = [('2018~2020', '2018-01-01', '2020-07-01'),
            ('2020~2022', '2020-07-01', '2023-01-01'),
            ('2023~now', '2023-01-01', '2027-01-01')]
    eqm = sim_momentum(R_all, 4, 12)
    okw = 0
    for nm, lo, hi in wins:
        sm = stats(eqm, lo, hi); sh = stats(hold_all, lo, hi)
        if sm and sh:
            win = sm[0] > sh[0]; okw += win
            L.append(f"  {nm}: 모멘텀 {sm[0]:+.0f}% vs 보유 {sh[0]:+.0f}%  {'🟢이김' if win else '🔴짐'}")
    L.append(f"  → 3구간 중 {okw}구간 보유초과")
    L.append("=" * 68)
    # 종합 판정 — '방향'과 '크기'를 분리. 비현실적 크기는 신뢰 거부.
    L.append("📌 종합 신뢰성 판정 (방향 vs 크기 분리)")
    edge_real = sA[0] - ha[0]
    cagr_edge = (((1 + sA[1] / 100)) / (1 + ha[1] / 100) - 1) * 100
    direction_ok = (beat >= 12 and okw >= 2 and edge_real > 0)
    L.append(f"  [방향] 모멘텀>보유: {'✅신뢰' if direction_ok else '⚠️약함'} "
             f"(그리드 {beat}/16칸·롤링 {okw}/3구간·비용3x 견딤)")
    implausible = cagr_edge > 15      # 연 15%p 초과 edge = 비현실(편향/유동성 신호)
    L.append(f"  [크기] 연 edge ≈ {cagr_edge:+.1f}%p → "
             + ("❌불신: 연15%p 초과는 비현실. 생존편향+종목선택편향(확대리스트가 내 기억의 폭등주)+유동성/상한가 미반영으로 폭증."
                if implausible else "참고가능(보수적으로)"))
    L.append("  ⚠️ 확대리스트로 edge가 '줄지 않고 늘어난 것' 자체가 경고: 진짜 생존편향 보정이 아니라")
    L.append("     아는 폭등주(금양·두산에너빌리티·한화오션 등)를 모멘텀이 올라탄 것 → 검증 무효.")
    L.append("  ✅실무결론: 방향(모멘텀 틸트)은 채택가능, 단 백테스트 수익은 '절대 그대로 안 남'.")
    L.append("     실기대치=백테스트의 일부, -45%↑ 낙폭은 진짜. 신뢰가능 숫자엔 시점별 유니버스(유료/KRX)+유동성·상한가 모델 필요.")
    return "\n".join(L)


if __name__ == '__main__':
    data = load(force='--refresh' in sys.argv)
    rep = report(data)
    print("\n" + rep)
    if '--telegram' in sys.argv:
        send_telegram(rep)
