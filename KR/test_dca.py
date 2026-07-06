# -*- coding: utf-8 -*-
"""지수 DCA 순수함수 dca_budgets 단위테스트 (감사반영: 예약금 비파괴 + tranche손상 폴백 포함).
실행: python KR/test_dca.py"""
import sys, os, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from KR.auto_order import dca_budgets, DCA_INDEX_MONTHS, MIN_CASH_ORDER
from KR.live_v1 import KR_ETF_WEIGHT as W

D = datetime.date
fails = []


def ck(name, cond, detail=''):
    print(f"  {'PASS' if cond else 'FAIL'} {name}" + (f"  [{detail}]" if detail else ''))
    if not cond:
        fails.append(name)


# 1) 월납입 30만 (지수몫 15만 < 100만 LUMP) → 분할 안 함, 전액 즉시
lv, idx, dca = dca_budgets(300_000, {}, D(2026, 8, 1))
ck("T1 월납입: 저변동15만 즉시", lv == int(300_000 * (1 - W)))
ck("T1 월납입: 지수15만 즉시", idx == 300_000 - int(300_000 * (1 - W)))
ck("T1 월납입: 예약 없음", dca == {})

# 2) 대형유입 486만 → 저변동 즉시 243만, 지수 첫트랜치, 나머지 예약
lv, idx, dca = dca_budgets(4_860_000, {}, D(2026, 7, 7))
first = (4_860_000 - int(4_860_000 * (1 - W))) // DCA_INDEX_MONTHS
ck("T2 대형: 저변동243만 즉시", lv == int(4_860_000 * (1 - W)))
ck("T2 대형: 지수 첫트랜치만", idx == first)
ck("T2 대형: 예약+집행=지수총액", dca['reserved'] + idx == 4_860_000 - lv)
ck("T2 대형: 집행 ≤ 계좌현금", idx <= 4_860_000)

# 3) 486만 6개월 수명주기 → 지수 총 243만 정확 소진, 저변동 추가 0
dca_state = dict(dca); total_index = idx; total_lowvol = lv
cash_rem = dca_state['reserved']
for (y, m) in [(2026, 8), (2026, 9), (2026, 10), (2026, 11), (2026, 12), (2027, 1)]:
    lv2, idx2, dca_state = dca_budgets(cash_rem, dca_state, D(y, m, 3))
    total_index += idx2; total_lowvol += lv2; cash_rem -= idx2
    if not dca_state:
        break
ck("T3 수명: 지수 총 243만 소진", abs(total_index - int(4_860_000 * W)) < MIN_CASH_ORDER + 1, f"{total_index}")
ck("T3 수명: 저변동 추가배분 0", total_lowvol == lv)
ck("T3 수명: 원장 소진(빈값)", dca_state == {})

# 4) 같은 달 재실행 → 트랜치 중복 안 함
_, _, d7 = dca_budgets(4_860_000, {}, D(2026, 7, 7)); cash7 = d7['reserved']
_, idx_a, d_a = dca_budgets(cash7, d7, D(2026, 7, 20))
ck("T4 같은달: 트랜치 0", idx_a == 0)
ck("T4 같은달: 예약 보존", d_a.get('reserved') == d7['reserved'])

# 5) 예약중 새 달 + 월납입 30만 → 신규 즉시 + 트랜치 동시
lv_b, idx_b, _ = dca_budgets(cash7 + 300_000, d7, D(2026, 8, 3))
ck("T5 동시: 저변동=새30만 절반", lv_b == int(300_000 * (1 - W)))
ck("T5 동시: 지수=새몫+트랜치", idx_b >= 150_000 + d7['tranche'] - 1)

# 6) ★감사 HIGH 회귀: 계좌현금 < 예약금(T2/인출) → 예약금 증발 금지·저변동 재주입 0
d_stale = {'reserved': 5_000_000, 'tranche': 800_000, 'last_month': 2026 * 12 + 7}
lv_c, idx_c, d_c = dca_budgets(3_000_000, d_stale, D(2026, 8, 1))  # 8월(새 달), 현금 3M < 예약 5M
ck("T6 HIGH: 저변동 재주입 0(누수 없음)", lv_c == 0, f"lv={lv_c}")
ck("T6 HIGH: 집행 ≤ 계좌현금", idx_c <= 3_000_000, f"idx={idx_c}")
ck("T6 HIGH: 예약금 증발 안 함(집행분만 감소)", d_c.get('reserved', 0) == 5_000_000 - idx_c, f"{d_c}")

# 7) ★감사 MED 회귀: tranche==0 손상 원장 → 전액 일괄 아닌 안전분할
d_corrupt = {'reserved': 3_000_000, 'tranche': 0, 'last_month': 2026 * 12 + 7}
_, idx_d, d_d = dca_budgets(3_000_000, d_corrupt, D(2026, 8, 1))
ck("T7 MED: tranche0이면 전액집행 안 함(분할)", idx_d < 3_000_000, f"idx={idx_d}")
ck("T7 MED: 남은 예약 보존", d_d.get('reserved', 0) > 0, f"{d_d}")

# 8) 신규 없음·현금 0 → 전부 0, 안전
lv_e, idx_e, d_e = dca_budgets(0, {}, D(2026, 8, 1))
ck("T8 현금0: 전부 0", lv_e == 0 and idx_e == 0 and d_e == {})

# 9) 과다집행 방지: 어떤 경우도 (저변동+지수) ≤ 계좌현금
import random
random.seed(1)
over = []
for _ in range(2000):
    c = random.randint(0, 9_000_000)
    st = {} if random.random() < 0.3 else {
        'reserved': random.randint(0, 6_000_000), 'tranche': random.choice([0, 100_000, 405_000, 800_000]),
        'last_month': 2026 * 12 + random.randint(1, 8)}
    lo, ix, _ = dca_budgets(c, st, D(2026, random.randint(1, 12), random.randint(1, 28)))
    if lo + ix > c + 1 or lo < 0 or ix < 0:
        over.append((c, st, lo, ix))
ck("T9 퍼즈2000: 배분 ≤ 현금 & 음수 없음", not over, f"위반 {len(over)}건" + (str(over[0]) if over else ''))

# 10) ★지수 시세실패 방어: plan_buyonly index_ok (오늘 KODEX 시세실패 → 저변동 편중 방지)
import KR.auto_order as AO
AO.time.sleep = lambda *a, **k: None  # 재시도 지연 제거(테스트 가속)


class _FakeToss:
    def __init__(self, fail=()):
        self.fail = set(fail)

    def get_price(self, s):
        return None if s in self.fail else 10000


TGT = ([{'symbol': '069500', 'sleeve': '지수ETF', 'price': 10000, 'name': 'KODEX200'}]
       + [{'symbol': f'{i:06d}', 'sleeve': '저변동', 'price': 10000, 'name': f'S{i}'} for i in range(1, 26)])
b, sk, ok = AO.plan_buyonly(TGT, _FakeToss(), 2_000_000, 400_000)
ck("T10 정상: index_ok True + ETF 매수계획", ok is True and any(x[0] == '069500' for x in b))
b2, sk2, ok2 = AO.plan_buyonly(TGT, _FakeToss(fail=['069500']), 2_000_000, 400_000)
ck("T10 ETF시세실패: index_ok False", ok2 is False and not any(x[0] == '069500' for x in b2))
ck("T10 ETF시세실패: 저변동은 계획됨(상위서 보류할것)", len(b2) > 0)
b3, sk3, ok3 = AO.plan_buyonly(TGT, _FakeToss(fail=['069500']), 2_000_000, 0)  # 지수예산0(트랜치 대기중)
ck("T10 지수예산0: index_ok True(살 필요 없음)", ok3 is True)
ck("T10 _price_retry 실패시 None", AO._price_retry(_FakeToss(fail=['X']), 'X', tries=2) is None)

print("\n결과:", "ALL PASS" if not fails else f"FAIL {len(fails)}건: {fails}")
sys.exit(1 if fails else 0)
