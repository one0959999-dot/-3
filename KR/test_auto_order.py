# -*- coding: utf-8 -*-
"""auto_order v1.2 유닛테스트 — 네트워크/텔레그램/실DB 없이 가짜 Toss로 로직 검증.
감사(2026-07-04) 수정사항 회귀 테스트 포함."""
import sys, os, json, tempfile, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

import KR.auto_order as ao

TMP = tempfile.mkdtemp(prefix='ao_test_')
# 부작용 차단: 텔레그램/거래DB로그/슬립/파일경로 전부 격리
MSGS = []
ao.tg = lambda m: MSGS.append(str(m))
ao._log = lambda *a, **k: None
ao.SETTLE = 0
time.sleep = lambda s: None
ao.MARKER = os.path.join(TMP, 'marker.txt')
ao.LOCK = os.path.join(TMP, 'lock')
ao.REB_MARKER = os.path.join(TMP, 'reb.txt')
ao.PLAN_FILE = os.path.join(TMP, 'plan.json')
ao.MANUAL_HOLD = os.path.join(TMP, 'manual_hold.txt')
ao.OBS_MARKER = os.path.join(TMP, 'obs.txt')

FAIL = []
def check(name, cond, detail=''):
    print(('PASS' if cond else 'FAIL'), name, detail if not cond else '')
    if not cond:
        FAIL.append((name, detail))


class FakeToss:
    """즉시체결 가정. t2=True면 매도대금이 settle() 호출 전까지 매수가능금액에 반영 안 됨."""
    def __init__(self, prices, holdings, cash, t2=False, bp_fail=False):
        self.prices = dict(prices); self.h = dict(holdings)
        self.cash = float(cash); self.t2 = t2; self.pending = 0.0
        self.bp_fail = bp_fail
        self.orders = []
    def get_price(self, s): return self.prices.get(s, 0)
    def get_buyable_cash(self, *a, default=0.0, **k):
        return None if self.bp_fail else self.cash
    def get_account_balance(self):
        stocks = [{'ticker': s, 'name': 'N' + s, 'shares': q, 'current_price': self.prices.get(s, 0),
                   'purchase_price': getattr(self, 'buy_px', {}).get(s, 0)}
                  for s, q in self.h.items() if q > 0]
        return {'cash': self.cash, 'stocks': stocks}
    def sell_market_order(self, s, q, price=0):
        assert q > 0 and price > 0, f'매도 price/qty 이상: {s} {q} {price}'
        assert self.h.get(s, 0) >= q, f'오버셀: {s} 보유{self.h.get(s,0)} 매도{q}'
        self.orders.append(('SELL', s, q, price))
        self.h[s] -= q
        proceeds = q * self.prices[s]
        if self.t2: self.pending += proceeds
        else: self.cash += proceeds
        return True
    def buy_market_order(self, s, q, price=0):
        assert q > 0 and price > 0
        cost = q * self.prices[s]
        assert cost <= self.cash + 1e-6, f'현금초과 매수: {s} cost {cost} > cash {self.cash}'
        self.orders.append(('BUY', s, q, price))
        self.h[s] = self.h.get(s, 0) + q
        self.cash -= cost
        return True
    def settle(self): self.cash += self.pending; self.pending = 0.0
    def has_investment_warning(self, s): return False
    def cancel_all_unfilled(self): return 0
    def is_kr_market_open(self): return True
    def get_open_orders(self): return []


# ── T1: plan_diffs — diff/엑싯/신규/소액스킵 ─────────────────────────────
items = [
    {'sym': 'A', 'qty': 10, 'name': 'A', 'price': 10000},   # held 20 → 매도 10 (10만 ≥ 5만)
    {'sym': 'B', 'qty': 0,  'name': 'B', 'price': 5000},    # held 3 엑싯 → 전량매도(1.5만이라도)
    {'sym': 'C', 'qty': 5,  'name': 'C', 'price': 20000},   # held 0 신규 → 매수 5
    {'sym': 'D', 'qty': 100,'name': 'D', 'price': 1000},    # held 99 → diff 1천원 → 스킵
    {'sym': 'E', 'qty': 12, 'name': 'E', 'price': 3000},    # held 14 → 매도 2 (6천원 <5만) 스킵
]
hold = {'A': (20, 'A'), 'B': (3, 'B'), 'D': (99, 'D'), 'E': (14, 'E')}
sells, buys = ao.plan_diffs(items, hold)
check('T1 sells', sorted([(s, q) for s, q, _, _ in sells]) == [('A', 10), ('B', 3)], str(sells))
check('T1 buys', [(s, q) for s, q, _, _ in buys] == [('C', 5)], str(buys))

# ── T2: build_plan_items — 시세실패 목표종목은 매도도 매수도 금지 ─────────
target = [
    {'symbol': '069500', 'name': 'KODEX', 'qty': 0, 'price': 10000, 'sleeve': '지수ETF'},
    {'symbol': 'AA', 'name': '정상', 'qty': 0, 'price': 10000, 'sleeve': '저변동'},
    {'symbol': 'XX', 'name': '시세실패', 'qty': 0, 'price': 10000, 'sleeve': '저변동'},
    {'symbol': 'YY', 'name': '괴리', 'qty': 0, 'price': 10000, 'sleeve': '저변동'},
]
hold2 = {'XX': (7, '시세실패'), 'ZZ': (2, '엑싯정상'), 'WW': (4, '엑싯시세실패')}
ft = FakeToss({'069500': 10000, 'AA': 10000, 'XX': 0, 'YY': 13000, 'ZZ': 8000, 'WW': 0}, hold2, 0)
items2, skipped2 = ao.build_plan_items(target, hold2, ft, 1_000_000)
by = {it['sym']: it for it in items2}
check('T2 시세실패 목표종목 제외(보유유지)', 'XX' not in by)
check('T2 괴리>15% 제외', 'YY' not in by)
check('T2 정상목표 포함', '069500' in by and 'AA' in by)
check('T2 엑싯(목표밖 보유) qty=0', by.get('ZZ', {}).get('qty', -1) == 0)
check('T2 엑싯 시세실패 제외(보유유지)', 'WW' not in by)
check('T2 ETF 50% 배분', by['069500']['qty'] == int(1_000_000 * 0.5 // (10000 * 1.02)), str(by['069500']))
s2, b2 = ao.plan_diffs(items2, hold2)
check('T2 시세실패 보유 매도금지', all(s != 'XX' and s != 'WW' for s, _, _, _ in s2), str(s2))
check('T2 엑싯 매도 포함', any(s == 'ZZ' for s, _, _, _ in s2), str(s2))

# ── T3: _rebalance_pass — T+2 (매도대금 다음날 반영) + 첫 매도 관찰체결 ───
prices = {'069500': 10000, 'A': 20000, 'B': 5000}
plan = {'quarter': ao.quarter_tag(), 'created': __import__('datetime').date.today().isoformat(),
        'items': [
            {'sym': '069500', 'qty': 100, 'name': 'KODEX', 'price': 10000},
            {'sym': 'A', 'qty': 0, 'name': 'A엑싯', 'price': 20000},
            {'sym': 'B', 'qty': 30, 'name': 'B신규', 'price': 5000},
        ]}
ft3 = FakeToss(prices, {'069500': 100, 'A': 50}, 0, t2=True)
h3 = {'069500': (100, 'K'), 'A': (50, 'A')}
MSGS.clear()
done = ao._rebalance_pass(ft3, 1, plan['quarter'], plan, h3)
check('T3 1일차 미완료(False)', done is False)
check('T3 1일차 매도만 나감', [o[0] for o in ft3.orders] == ['SELL'], str(ft3.orders))
check('T3 A 전량매도', ('SELL', 'A', 50) == ft3.orders[0][:3], str(ft3.orders[0]))
check('T3 첫 매도 관찰체결 마커', os.path.exists(ao.OBS_MARKER))
check('T3 매수대기 알림', any('T+2' in m or '매수대기' in m for m in MSGS))
ft3.settle()
h3b = {s: (q, s) for s, q in ft3.h.items() if q > 0}
MSGS.clear()
done2 = ao._rebalance_pass(ft3, 1, plan['quarter'], plan, h3b)
check('T3 2일차 완료(True)', done2 is True)
buys3 = [o for o in ft3.orders if o[0] == 'BUY']
check('T3 B 30주 매수', len(buys3) == 1 and buys3[0][1] == 'B' and buys3[0][2] == 30, str(buys3))
check('T3 ETF 불간섭', ft3.h['069500'] == 100)

# ── T4: 매도대금 당일반영 계좌면 하루에 완료 ─────────────────────────────
ft4 = FakeToss(prices, {'069500': 100, 'A': 50}, 0, t2=False)
h4 = {'069500': (100, 'K'), 'A': (50, 'A')}
MSGS.clear()
done4 = ao._rebalance_pass(ft4, 1, plan['quarter'], plan, h4)
check('T4 당일 완료(True)', done4 is True)
check('T4 매도→매수 순서', [o[0] for o in ft4.orders] == ['SELL', 'BUY'], str(ft4.orders))

# ── T5: 마커/플랜/수동보유 파일 ──────────────────────────────────────────
ao.write_reb('ACTIVE')
check('T5 reb 상태 왕복', ao.read_reb() == (ao.quarter_tag(), 'ACTIVE'))
ao.save_plan(plan)
check('T5 플랜 왕복', ao.load_plan()['items'][2]['sym'] == 'B')
open(ao.MANUAL_HOLD, 'w', encoding='utf-8').write('# 놀이돈\n123456  # 에스앤에스텍\n\n')
check('T5 manual_hold 파싱', ao.load_manual_hold() == {'123456'})
ao.write_marker('STARTED')
check('T5 STARTED 감지', ao.marker_started_uncommitted() is True and ao.already_done() is False)
ao.write_marker('DONE')
check('T5 오늘 DONE 감지', ao.already_done() is True and ao.marker_started_uncommitted() is False)

# ── T6: 관찰체결 — 체결확인 후 ETF 1주 차감 + 소요액 반환 ────────────────
ft6 = FakeToss({'069500': 10000}, {}, 100000)
buys6 = [('069500', 5, 'KODEX', 10000)]
ok6, buys6b, cost6 = ao._observe_first_fill(ft6, 1, buys6)
check('T6 관찰체결 통과', ok6 is True and os.path.exists(ao.OBS_MARKER))
check('T6 ETF 1주 차감', buys6b == [('069500', 4, 'KODEX', 10000)], str(buys6b))
check('T6 관찰비용 반환(틱올림)', cost6 == ao._tick_up(10000 * 1.02), str(cost6))

class NoFillToss(FakeToss):
    def buy_market_order(self, s, q, price=0):
        self.orders.append(('BUY', s, q, price)); return True  # 미체결
os.remove(ao.OBS_MARKER)
ft7 = NoFillToss({'069500': 10000}, {}, 100000)
ok7, _, cost7 = ao._observe_first_fill(ft7, 1, buys6)
check('T6 미체결시 중단', ok7 is False and cost7 == 0 and not os.path.exists(ao.OBS_MARKER))

# ── T7: 플랜만료(주문 없이 종료)/현금 안들어옴 포기 ──────────────────────
import datetime as dt
ao._write_obs()  # 이후 테스트는 관찰체결 완료 상태 가정
old = dict(plan); old['created'] = (dt.date.today() - dt.timedelta(days=15)).isoformat()
ft8 = FakeToss(prices, {'069500': 100, 'A': 50}, 0, t2=True)
h8 = {'069500': (100, 'K'), 'A': (50, 'A')}
MSGS.clear()
done8 = ao._rebalance_pass(ft8, 1, old['quarter'], old, h8)
check('T7 만료플랜 종료(True)+알림', done8 is True and any('만료' in m or '경과' in m for m in MSGS), str(MSGS[-2:]))
check('T7 만료플랜 주문0 (감사4 순서수정)', ft8.orders == [], str(ft8.orders))
old7 = dict(plan); old7['created'] = (dt.date.today() - dt.timedelta(days=7)).isoformat()
ft9 = FakeToss(prices, {'069500': 100}, 0, t2=True)
h9 = {'069500': (100, 'K')}
MSGS.clear()
done9 = ao._rebalance_pass(ft9, 1, old7['quarter'], old7, h9)
check('T7 D+7 현금없음 포기(True)', done9 is True and any('유입 없음' in m or '종료' in m for m in MSGS), str(MSGS[-2:]))

# ── T8: read_account 안전화 (감사1) ─────────────────────────────────────
ra1 = ao.read_account(FakeToss({'A': 10000}, {'A': 5}, 500000, bp_fail=True))
check('T8 매수가능금액 조회실패 → None(중단)', ra1 is None)
ra2 = ao.read_account(FakeToss({'A': 0}, {'A': 5}, 500000))
check('T8 시세0+매입가0 → None(중단)', ra2 is None)
ftH = FakeToss({'A': 0}, {'A': 5}, 500000); ftH.buy_px = {'A': 9000}
raH = ao.read_account(ftH)
check('T8 거래정지 → 매입가 대체평가(동결 방지)', raH == ({'A': (5, 'NA')}, 500000.0, 545000.0), str(raH))
ra3 = ao.read_account(FakeToss({'A': 10000}, {'A': 5}, 500000))
check('T8 정상 계좌', ra3 == ({'A': (5, 'NA')}, 500000.0, 550000.0), str(ra3))

# ── T9: 매도 시세 급변 — 재조회 일치시 진행, 불안정시 보류 (감사4) ────────
plan9 = {'quarter': ao.quarter_tag(), 'created': dt.date.today().isoformat(),
         'items': [{'sym': 'A', 'qty': 0, 'name': 'A급락엑싯', 'price': 10000}]}
ftA = FakeToss({'A': 7500}, {'A': 10}, 0, t2=False)  # -25% 급락, 시세는 안정
hA = {'A': (10, 'A')}
MSGS.clear()
doneA = ao._rebalance_pass(ftA, 1, plan9['quarter'], plan9, hA)
check('T9 급락 엑싯도 매도됨(재조회 일치)', doneA is True and ftA.orders and ftA.orders[0][:3] == ('SELL', 'A', 10), str(ftA.orders))
check('T9 현재가 기준 지정가', ftA.orders[0][3] == int(7500 * 0.98), str(ftA.orders[0]))

class JitterToss(FakeToss):
    def get_price(self, s):
        self._i = getattr(self, '_i', 0) + 1
        return 7500 if self._i % 2 else 8000  # 호출마다 6.7% 널뜀
ftB = JitterToss({'A': 7500}, {'A': 10}, 0)
hB = {'A': (10, 'A')}
MSGS.clear()
doneB = ao._rebalance_pass(ftB, 1, plan9['quarter'], plan9, hB)
check('T9 시세불안정 매도보류', doneB is False and not [o for o in ftB.orders if o[0] == 'SELL'],
      str(ftB.orders) + ' | ' + str(MSGS[-2:]))

# ── T10: _tick_up 호가단위 올림 ──────────────────────────────────────────
check('T10 tick', (ao._tick_up(10001), ao._tick_up(9999), ao._tick_up(523400), ao._tick_up(1000), ao._tick_up(487)) ==
      (10050, 10000, 524000, 1000, 487), str((ao._tick_up(10001), ao._tick_up(9999), ao._tick_up(523400), ao._tick_up(1000), ao._tick_up(487))))

print()
print('결과:', 'ALL PASS' if not FAIL else f'{len(FAIL)} FAIL: {FAIL}')
sys.exit(1 if FAIL else 0)
