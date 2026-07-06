"""자동주문 v1.2 — 알고리즘 v1.0 목표를 실계좌에 diff 체결 (신규자금 배분 + 분기 완전자동 리밸런스).

★설계: 구봇(KR/bot.py) killswitch·AI게이트·손절·고빈도 배제. 검증된 실행층(toss)만 재활용.
  목표 종목 = live_v1.compute_target(단일소스). 수량 = 실 Toss 시세. 계좌상태(매도후 실현금)를 진실로.

두 모드:
  [기본] 신규자금 배분(buy-only): 가용현금을 50/50 배분해 매수만. 기존보유 불간섭. auto_deploy가 매일 호출.
  [--rebalance] 분기 완전자동 리밸런스(매도포함, T+2 안전):
     리밸주간에 '절대 목표수량' 플랜 생성·저장(rebalance_plan.json, 엑싯종목은 목표 0)
     → 매도(초과분) → 실현금(매수가능금액) 재조회 → 매수(부족분).
     현금이 덜 들어왔으면(T+2 결제 대기) 상태 유지 — 다음날 크론이 '목표 - 현보유'를 다시 계산해 이어감.
     절대목표라 부분체결·미체결취소·중간크래시에도 자기교정(남은일 = 목표와 현보유의 차이).
     상태 rebalance_state.txt: {분기}:ACTIVE(진행중) → {분기}:DONE(완료).

안전장치(감사 반영):
  ① 기본 드라이런(주문0). 실주문 --execute 명시.
  ② 매수 하드캡(--max, 미지정시 가용현금이 캡) + spent 누적, 실현금·캡 동시 상한.
  ③ diff만 체결. 시세검증 실패 종목은 매도·매수 모두 제외(전량매도 사고 방지).
  ④ lp 유효성 필수(price=0 매도 절대금지). 가격괴리·관리종목(매수만)·VI 스킵.
  ⑤ 매도먼저 → 재조회 실현금 기준 매수. 계좌상태로 체결확인(주문 bool 불신).
  ⑥ 정규장만. 미체결주문 있으면 중단(중복주문 방지의 핵심 — 재조정은 체결반영된 보유 기준). O_EXCL 락.
  ⑦ 부트스트랩 게이트: 지수ETF(069500) 미보유면 매도 거부 — 알고리즘 미투입 계좌를 청산하는 사고 방지.
     최초 1회는 사용자가 기존종목 매도 → auto_deploy가 현금 감지 자동매수 → 그 다음 분기부터 완전자동.
  ⑧ manual_hold.txt(한 줄에 종목코드 하나)의 종목은 리밸런스가 절대 안 건드림(놀이돈·수동보유 보호). 예산에서도 제외.
  ⑨ 최초 실주문 전 관찰체결 1회: ETF 1주 매수 → 계좌 재조회로 체결확인 후에만 본 매수 진행(첫 체결 검증).
  ⑩ 플랜 만료: 7일째 현금 안 들어오면/14일 넘게 미완이면 알림 후 종료(잔여현금은 auto_deploy가 흡수).

실행:
  python KR/auto_order.py --probe                      # 계좌조회만
  python KR/auto_order.py                              # 신규자금 배분 드라이런(주문0)
  python KR/auto_order.py --execute                    # 신규자금 배분 실주문(캡=가용현금 전액)
  python KR/auto_order.py --rebalance                  # 분기 리밸런스 드라이런(매도+매수 표시)
  python KR/auto_order.py --rebalance --execute        # 분기 리밸런스 실주문(리밸주간 시작·이후 자동 이어가기)
크론(EC2): 0 1 * * 1-5 리밸런스(KST 10:00, 비리밸주간·완료시 조용히 종료) / 30 1 * * 1-5 auto_deploy
"""
import sys, os, json, sqlite3, time, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import warnings
warnings.filterwarnings('ignore')
from KR.live_v1 import fetch_universe, compute_target, is_rebalance_week, tg, ETFS, ETF, BUY_COST, KR_ETF_WEIGHT

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
MARKER = P('auto_order_done.txt'); LOCK = P('auto_order.lock')
REB_MARKER = P('rebalance_state.txt'); PLAN_FILE = P('rebalance_plan.json')
MANUAL_HOLD = P('manual_hold.txt'); OBS_MARKER = P('first_fill_verified.txt')
PRICE_SANITY = 0.15; SELL_BUF = 0.02; BUY_BUF = 0.02; SETTLE = 8
MIN_DIFF_VAL = 50_000        # 기존보유 종목의 증량/감량 diff가 이 미만이면 스킵(수수료 낭비 방지). 엑싯·신규는 예외.
MIN_CASH_ORDER = 100_000     # 이 미만 현금/잔여플랜이면 사실상 완료 취급
LOWCASH_GIVEUP_DAYS = 7      # 플랜 후 이 일수 지나도 현금 안 들어오면(T+2면 늦어도 4일) 포기
PLAN_EXPIRE_DAYS = 14        # 플랜 하드만료

# ── 지수 DCA(목돈 분할진입): 대형유입시 '지수 슬리브만' N개월 분할, 저변동은 즉시 ──
# 근거: 코스피 직전2배↑ 후 진입시 이후1년 평균 -27%(상관-0.415). 고점 몰빵 회피용 진입-타이밍 도구.
# ★알고리즘(50/50 선정·리밸런스)은 불변. 이건 '신규자금 배포 방식'만 바꾼다(운용규칙 아님).
DCA_FILE = P('dca_index_plan.json')
DCA_MIN_LUMP = 1_000_000     # 신규현금의 '지수몫'이 이 이상이면 분할(월납입 30만=지수15만은 미해당→즉시)
DCA_INDEX_MONTHS = 6         # 지수 목돈을 이 개월수로 균등 분할


def load_toss():
    c = sqlite3.connect(P('lassi.db')); c.row_factory = sqlite3.Row
    u = c.execute("SELECT id, toss_client_id, toss_client_secret, toss_account_seq FROM users "
                  "WHERE toss_client_id IS NOT NULL AND toss_client_id!='' LIMIT 1").fetchone()
    c.close()
    if not u:
        return None, None
    from base.toss_api import TossInvestApi
    return TossInvestApi(u['toss_client_id'], u['toss_client_secret'], u['toss_account_seq'] or ''), u['id']


def read_account(toss):
    """(holdings{sym:(qty,name)}, cash, total) 또는 None(=중단).
    감사반영: ①매수가능금액 API 실패를 현금0으로 위장 금지(default=None로 실패 감지)
    ②보유종목 시세0(거래정지 등)이면 총자산 오산정 → 중단(예산축소→엉뚱한 매도 방지)."""
    bal = toss.get_account_balance()
    if not bal:
        return None
    holdings = {}; hold_val = 0.0
    for s in bal.get('stocks', []):
        q = int(s.get('shares', 0) or 0)
        if q > 0:
            px = float(s.get('current_price', 0) or 0)
            if px <= 0:
                # 거래정지 등 시세0 — 매입가로 대체평가(종목 1개가 시스템 전체를 동결하지 않게).
                # 매도는 어차피 build_plan_items의 lp 검증에서 걸러져 불가.
                px = float(s.get('purchase_price', 0) or 0)
            if px <= 0:
                print(f"⛔ 계좌평가 불능: {s.get('name', s['ticker'])} 시세·매입가 모두 0 — 중단")
                return None
            holdings[s['ticker']] = (q, s.get('name', s['ticker']))
            hold_val += q * px
    cash = toss.get_buyable_cash(default=None)
    if cash is None:
        print("⛔ 매수가능금액 조회 실패 — 현금0으로 오인하지 않고 중단")
        return None
    return holdings, float(cash), float(cash) + hold_val


def quarter_tag(d=None):
    d = d or datetime.date.today(); return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _tick_up(price):
    """KRX 호가단위 올림 — 현금계산에 쓰는 unit이 실제 제출가(틱반올림)보다 작아지는 일 방지."""
    p = int(price)
    for lim, tick in ((1_000, 1), (5_000, 5), (10_000, 10), (50_000, 50), (100_000, 100), (500_000, 500)):
        if p < lim:
            return -(-p // tick) * tick
    return -(-p // 1_000) * 1_000


def _cancel_unfilled(toss):
    """미체결 전량취소 — 실패(-1)를 삼키지 않고 경고(DAY주문은 장마감시 자동소멸이라 피해 한정)."""
    try:
        n = toss.cancel_all_unfilled()
        if n is not None and n < 0:
            tg("🚨 미체결취소 확인실패(주문조회 API) — 미체결 잔존 가능. DAY주문 장마감 자동소멸 + 내일 미체결게이트가 재실행 차단. 계좌 육안확인 권장")
    except Exception:
        tg("🚨 미체결취소 중 오류 — 미체결 잔존 가능. DAY주문 장마감 자동소멸 + 내일 미체결게이트가 재실행 차단. 계좌 육안확인 권장")


# ─────────── 마커/상태 (전부 원자적 쓰기) ───────────

def already_done():
    """신규자금 배분: 같은 날 중복실행 차단 (실질 dedup은 배분 후 현금≈0)."""
    try:
        return open(MARKER).read().strip() == datetime.date.today().isoformat() + ":DONE"
    except Exception:
        return False


def marker_started_uncommitted():
    """직전 신규자금 배분이 STARTED만 남기고 죽었나 (부분체결 위험 → 수동점검 필요)."""
    try:
        return open(MARKER).read().strip().endswith(":STARTED")
    except Exception:
        return False


def write_marker(state):
    tmp = MARKER + '.tmp'
    open(tmp, 'w').write(f"{datetime.date.today().isoformat()}:{state}")
    os.replace(tmp, MARKER)


def read_reb():
    """리밸런스 상태 (quarter, state) — 없으면 (None, None)."""
    try:
        v = open(REB_MARKER).read().strip()
        q, s = v.rsplit(':', 1)
        return q, s
    except Exception:
        return None, None


def write_reb(state):
    tmp = REB_MARKER + '.tmp'
    open(tmp, 'w').write(f"{quarter_tag()}:{state}")
    os.replace(tmp, REB_MARKER)


def save_plan(plan):
    tmp = PLAN_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(plan, f, ensure_ascii=False)
    os.replace(tmp, PLAN_FILE)


def load_plan():
    try:
        with open(PLAN_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


# ── 지수 DCA 원장 (미래 지수 트랜치용 예약금) ──

def load_dca():
    try:
        with open(DCA_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_dca(d):
    tmp = DCA_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d or {}, f, ensure_ascii=False)
    os.replace(tmp, DCA_FILE)


def _month_key(d):
    return d.year * 12 + d.month


def dca_reserved():
    """현재 원장의 예약금(향후 지수 트랜치용). 없으면 0."""
    try:
        return max(0, int(load_dca().get('reserved', 0)))
    except Exception:
        return 0


def dca_tranche_due(today=None):
    """예약분이 있고 이번 달 트랜치가 아직 집행 안 됐으면 True(auto_deploy 트리거용)."""
    today = today or datetime.date.today()
    d = load_dca()
    return dca_reserved() > 0 and _month_key(today) > int(d.get('last_month', 0))


def dca_budgets(cash, dca, today):
    """가용현금 → (저변동예산, 지수예산, 갱신원장). 순수함수(부작용X)라 테스트 용이.
    - reserved: 미래 지수 트랜치용 예약금. new_cash = cash - reserved(진짜 신규자금).
    - 신규 지수몫 ≥ DCA_MIN_LUMP(대형) → 지수만 DCA_INDEX_MONTHS 분할, 첫 트랜치만 지금·나머지 예약.
    - 예약분은 매 캘린더월 1회 한 트랜치씩 집행. 저변동은 항상 즉시(분할 안 함).
    ★안전(감사반영): 예약금은 계좌현금으로 '축소저장' 금지 — 일시부족(T+2 미결제·인출)에 축소하면
      예약분이 원장에서 증발→다음 달 저변동으로 잘못 재주입됨(설계 불변식 위반). 집행액만 현금 상한."""
    cash = max(0, int(cash))
    dca = dca or {}
    reserved = max(0, int(dca.get('reserved', 0)))       # 원장 예약금 보존(클램프 저장 안 함)
    tranche = max(0, int(dca.get('tranche', 0)))
    last_month = int(dca.get('last_month', 0))
    mkey = _month_key(today)
    new_cash = max(0, cash - reserved)                   # 예약분 초과분만 진짜 신규(reserved≥cash면 0 → 저변동 재주입 없음)
    lowvol = int(new_cash * (1 - KR_ETF_WEIGHT))
    index_new = new_cash - lowvol                        # 신규자금의 지수몫(잔여배분 오차 없이)
    index_budget = 0
    if index_new >= DCA_MIN_LUMP:                        # 대형유입 → 지수 분할, 첫 트랜치만 지금
        first = min(index_new // DCA_INDEX_MONTHS, cash)  # 계좌현금 초과 집행 금지(방어)
        index_budget += first
        reserved += index_new - first
        tranche = first if tranche == 0 else max(tranche, first)
        last_month = mkey                                # 이번 달 트랜치 소진(당일 중복 트랜치 방지)
    else:                                                # 소액 신규 지수는 즉시
        index_budget += index_new
        if reserved > 0 and mkey > last_month:           # 예약분 월 트랜치 due
            step = tranche if tranche > 0 else max(reserved // DCA_INDEX_MONTHS, 1)  # 원장손상(tranche=0)시 안전분할 폴백
            t = min(step, reserved, max(0, cash - index_budget))  # 예약·계좌현금 내에서만 집행
            index_budget += t; reserved -= t
            if t > 0:
                last_month = mkey                        # 실제 집행됐을 때만 이번 달 소진 표시
    if 0 < reserved <= MIN_CASH_ORDER:                   # 잔여 소액이면 마저 집행(계좌현금 내)·원장정리
        drain = min(reserved, max(0, cash - index_budget))
        index_budget += drain; reserved -= drain
    new_dca = {} if reserved <= 0 else {
        'reserved': int(reserved), 'tranche': int(tranche), 'last_month': int(last_month)}
    return lowvol, int(index_budget), new_dca


def _write_obs():
    tmp = OBS_MARKER + '.tmp'
    open(tmp, 'w').write(datetime.datetime.now().isoformat())
    os.replace(tmp, OBS_MARKER)


def load_manual_hold():
    """사용자 수동보유 종목(리밸런스 불간섭). 한 줄에 코드 하나, # 주석."""
    try:
        return {ln.split('#')[0].strip() for ln in open(MANUAL_HOLD, encoding='utf-8')
                if ln.split('#')[0].strip()}
    except Exception:
        return set()


def _journal(kind, payload):
    try:
        from KR.journal import record
        record(kind, 'KR', payload)
    except Exception:
        pass


# ─────────── 배분/플랜 계산 ───────────

def _alloc(cash, sleeve_kind, n_stock, n_etf):
    """국내 50/50 배분: ETF 슬리브 50%(ETF수로 균등) / 저변동 50%(종목수로 균등)."""
    if sleeve_kind == '지수ETF':
        return cash * KR_ETF_WEIGHT / max(n_etf, 1)
    return cash * (1 - KR_ETF_WEIGHT) / max(n_stock, 1)


SMALL_DEPLOY = 3_000_000  # 이 미만 소액은 25종목 분산 불가 → ETF로 몰아줌
SMALL_LOWVOL = int(SMALL_DEPLOY * (1 - KR_ETF_WEIGHT))  # 저변동 예산이 이 미만이면 분산불가 → 저변동몫도 ETF로(150만)


def plan_buyonly(target, toss, lowvol_cash, index_cash):
    """★신규자금 배분(매도 없음): 저변동예산·지수예산을 각 슬리브에 매수만. 기존보유 불간섭.
    지수예산은 DCA로 이미 트랜치 제한됐을 수 있음(dca_budgets가 산출).
    ※저변동예산이 25종목 분산에 못 미치면(소액) 저변동 몫도 지수ETF로 몰아줌. 반환 (buys, skipped)."""
    n_stock = sum(1 for t in target if t['sleeve'] == '저변동')
    n_etf = sum(1 for t in target if t['sleeve'] == '지수ETF')
    small = lowvol_cash < SMALL_LOWVOL          # 저변동 분산 불가 → 저변동몫도 ETF로
    if small:
        index_cash = index_cash + lowvol_cash; lowvol_cash = 0
    buys, skipped = [], []
    for t in target:
        if t['sleeve'] == '저변동':
            if lowvol_cash <= 0 or n_stock == 0:
                continue
            alloc = lowvol_cash / n_stock
        else:  # 지수ETF
            if index_cash <= 0 or n_etf == 0:
                continue
            alloc = index_cash / n_etf
        sym = t['symbol']; lp = toss.get_price(sym)
        if not lp or lp <= 0:
            skipped.append((t['name'], '시세실패')); continue
        if abs(lp / t['price'] - 1) > PRICE_SANITY:
            skipped.append((t['name'], f'괴리{(lp/t["price"]-1)*100:+.0f}%')); continue
        q = int(alloc // (lp * (1 + BUY_BUF)))
        if q > 0:
            buys.append((sym, q, t['name'], lp))
    if small and n_stock > 0:
        skipped.append(('개별종목', f'저변동예산<{SMALL_LOWVOL//10000}만 → ETF로 몰아줌'))
    return buys, skipped


def build_plan_items(target, holdings, toss, budget):
    """분기 리밸런스 절대목표 플랜. 반환 (items, skipped).
    items = [{sym, qty(절대목표; 0=엑싯), name, price(계획시점 검증시세)}]
    시세검증 실패한 목표종목은 items에서 제외 = 매도도 매수도 안 함(보유유지, 전량매도 사고 방지).
    보유중인데 목표에 없는 종목은 qty=0(엑싯) — 단 시세실패면 제외(price=0 매도 금지)."""
    n_stock = sum(1 for t in target if t['sleeve'] == '저변동')
    n_etf = sum(1 for t in target if t['sleeve'] == '지수ETF')
    items, skipped = [], []
    tgt_syms = set()
    for t in target:
        sym = t['symbol']; tgt_syms.add(sym)
        lp = toss.get_price(sym)
        if not lp or lp <= 0 or abs(lp / t['price'] - 1) > PRICE_SANITY:
            skipped.append((t['name'], '시세검증실패→불간섭')); continue
        q = int(_alloc(budget, t['sleeve'], n_stock, n_etf) // (lp * (1 + BUY_BUF)))
        items.append({'sym': sym, 'qty': q, 'name': t['name'], 'price': lp})
    for sym, (held, nm) in holdings.items():
        if sym in tgt_syms:
            continue  # 목표종목(플랜에 있거나, 시세실패로 불간섭)
        lp = toss.get_price(sym)
        if not lp or lp <= 0:
            skipped.append((nm, '매도시세실패→보유유지')); continue
        items.append({'sym': sym, 'qty': 0, 'name': nm, 'price': lp})
    return items, skipped


def plan_diffs(items, holdings):
    """절대목표 vs 현보유 → (sells, buys). 각 [(sym, qty, name, plan_price)].
    기존보유 종목의 소액 diff(<MIN_DIFF_VAL)는 스킵 — 엑싯(목표0)과 신규(보유0)는 항상 수행."""
    sells, buys = [], []
    for it in items:
        held = holdings.get(it['sym'], (0,))[0]
        diff = held - it['qty']
        if diff > 0:
            if it['qty'] > 0 and diff * it['price'] < MIN_DIFF_VAL:
                continue
            sells.append((it['sym'], diff, it['name'], it['price']))
        elif diff < 0:
            need = -diff
            if held > 0 and need * it['price'] < MIN_DIFF_VAL:
                continue
            buys.append((it['sym'], need, it['name'], it['price']))
    return sells, buys


# ─────────── 신규자금 배분 (buy-only) ───────────

def main(execute=False, max_buy=None, force=False, probe=False, budget_override=None, anytime=False):
    toss, uid = load_toss()
    if toss is None:
        print("⛔ Toss 자격증명 없음"); return 1
    acct = read_account(toss)
    if acct is None:
        msg = "⛔ auto_order 중단: 계좌조회 실패(토큰/IP/API) — 아무것도 안 함"; print(msg); tg(msg); return 1
    holdings, cash, total = acct
    print(f"[계좌] 보유 {len(holdings)}종목, 현금 {cash/1e4:,.0f}만, 총 {total/1e4:,.0f}만")
    if probe:
        tg(f"🔍 계좌: 보유 {len(holdings)}종목, 현금 {cash/1e4:,.0f}만, 총 {total/1e4:,.0f}만\n" +
           "\n".join(f"  {nm}({s}) {q}주" for s, (q, nm) in holdings.items()))
        return 0

    # 종목선정용 스캔 (budget은 qty계산용 — 신규자금 배분이라 가용현금 기준)
    deploy = budget_override if budget_override else cash
    # ★지수 DCA: 가용현금을 (저변동 즉시 / 지수 트랜치제한) 예산으로 분해. 저변동 불변, 지수만 대형시 분할.
    today = datetime.date.today()
    lowvol_bud, index_bud, new_dca = dca_budgets(deploy, load_dca(), today)
    eff = lowvol_bud + index_bud                  # 이번 회차 실제 배분액(예약분 제외)
    cap = max_buy if max_buy else eff             # 캡 미지정 = 이번 회차 배분액(예약분은 캡 밖)
    print("[스캔] 전 종목 시세 수집...")
    cl, names = fetch_universe()
    target, meta = compute_target(cl, names, max(deploy, 1_000_000))  # 선정은 예산무관
    if target is None:
        msg = f"⛔ 중단: 목표산출 실패 ({meta})"; print(msg); tg(msg); return 1
    buys, skipped = plan_buyonly(target, toss, lowvol_bud, index_bud)
    buy_val = sum(q * _tick_up(lp * (1 + BUY_BUF)) for _, q, _, lp in buys)
    cap_txt = f"{max_buy/1e4:,.0f}만" if max_buy else "이번회차전액"

    head = '🔴실행' if execute else '📋드라이런(주문0)'
    L = [f"🤖 자동주문 {head} — {meta['last_day']}"]
    L.append(f"[신규자금 배분] 가용 {deploy/1e4:,.0f}만 → 저변동 {lowvol_bud/1e4:,.0f}만 + 지수 {index_bud/1e4:,.0f}만 "
             f"= 매수 {len(buys)}건 ≈{buy_val/1e4:,.0f}만 (기존보유 불간섭)")
    if new_dca.get('reserved'):
        L.append(f"  📅 지수 DCA 예약 {new_dca['reserved']/1e4:,.0f}만 → 향후 {DCA_INDEX_MONTHS}개월 월분할 "
                 f"(고점 몰빵 회피, 저변동은 즉시 전액)")
    for s, q, nm, lp in buys[:30]:
        L.append(f"  ➕ {nm[:10]}({s}) {q}주 × {lp:,.0f}")
    if skipped:
        L.append(f"[스킵 {len(skipped)}] " + ", ".join(f"{nm}({r})" for nm, r in skipped[:6]))
    L.append("[참고: 분기 전체리밸런스(매도포함)는 --rebalance가 자동수행]")
    rep = "\n".join(L); print(rep); tg(rep)

    if not execute:
        print("\n📋 드라이런 종료 — 주문 없음."); return 0

    # ─────────── 실행 안전 게이트 (매수 전용) ───────────
    if not getattr(toss, 'account_seq', 'x'):
        msg = "⛔ 실행거부: 계좌seq 미확보(자동조회 실패) — 주문 헤더 누락 위험. --probe로 계좌확인 후 재시도"; print(msg); tg(msg); return 1
    if not anytime and not is_rebalance_week():
        msg = "⛔ 실행거부: 리밸런스 주간 아님(1·4·7·10월 첫주). 신규자금 상시투입은 auto_deploy 경로."; print(msg); tg(msg); return 0
    rq, rstate = read_reb()
    if rq == quarter_tag() and rstate == 'ACTIVE':
        msg = "⏸️ 신규자금 배분 보류: 분기 리밸런스 진행중 — 현금은 리밸런스 플랜 몫"; print(msg); tg(msg); return 0
    if eff < MIN_CASH_ORDER:
        # 이번 회차 배분액이 미미 = 신규자금 없음 or 지수예약분 대기중(월트랜치 미도래). 후자면 조용히(일일 노이즈 방지).
        msg = f"⛔ 배분할 신규자금 없음: 가용 {deploy/1e4:.0f}만 중 이번회차 {eff/1e4:.0f}만 < 10만 (기존보유는 그대로)"
        print(msg)
        if not dca_reserved():
            tg(msg)
        return 0
    if not toss.is_kr_market_open():
        msg = "⛔ 실행거부: 정규장 아님(장중에만 실주문)"; print(msg); tg(msg); return 0
    try:
        oo = toss.get_open_orders()
    except Exception:
        oo = None
    if oo is None:
        msg = "⛔ 실행거부: 미체결 주문 조회 실패 — 안전상 중단"; print(msg); tg(msg); return 0
    if oo:
        msg = "⛔ 실행거부: 미체결 주문 존재(구봇/이전실행?) — 충돌방지"; print(msg); tg(msg); return 0
    if already_done():
        msg = "⛔ 실행거부: 오늘 이미 신규자금 배분 완료(중복차단)"; print(msg); tg(msg); return 0
    if marker_started_uncommitted():
        # 같은 날 STARTED = 방금 죽었거나 진행중 → 수동점검. 전일 이전 것은 미체결 없음(위 게이트)
        # 확인됐고 잔여현금 재배분은 안전하므로 자동해제 (감사 6: 영구 동결 방지)
        if open(MARKER).read().strip().startswith(datetime.date.today().isoformat()):
            msg = "⛔ 실행거부: 오늘 STARTED 마커(진행중/직전크래시) — 수동 점검 필요."; print(msg); tg(msg); return 0
        tg("⚠️ 이전일 STARTED 마커 잔존 — 미체결 없음 확인됨, 자동해제 후 잔여현금 재배분 진행")
    if buy_val > cap:
        msg = f"⛔ 실행거부: 계획매수 {buy_val/1e4:,.0f}만 > 캡 {cap/1e4:,.0f}만."; print(msg); tg(msg); return 0
    if not _acquire_lock():
        msg = "⛔ 실행거부: 락파일(다른 실행 진행중). 오래된 락이면 auto_order.lock 수동삭제."; print(msg); tg(msg); return 0

    try:
        write_marker("STARTED")
        # ⑨ 최초 실주문 관찰체결: ETF 1주 매수 → 계좌 재조회로 체결확인 후 본 매수
        obs_cost = 0
        if not os.path.exists(OBS_MARKER):
            ok, buys, obs_cost = _observe_first_fill(toss, uid, buys)
            if not ok:
                write_marker("DONE")  # 오늘은 종료(부분체결 없음 — 관찰주문은 취소됨). 내일 재시도.
                return 0
        tg(f"🔴 실주문 시작(신규자금 배분) — 매수 {len(buys)}건, 캡 {cap_txt}. 매도 없음(기존보유 유지).")
        avail = eff - obs_cost  # 이번 회차 배분액 상한(예약분은 계좌에 남겨둠 — 다음 달 트랜치)
        cap = max(cap - obs_cost, 0)
        spent = 0; done_b = 0
        for s, q, nm, tlp in buys:
            w = toss.has_investment_warning(s)
            if w is None:
                tg(f"  ⚠️ 매수스킵(경고조회실패=안전측): {nm}"); continue
            if w:
                tg(f"  ⚠️ 매수스킵(관리/경고): {nm}"); continue
            lp = toss.get_price(s)
            if not lp or lp <= 0 or abs(lp / tlp - 1) > PRICE_SANITY:
                tg(f"  ⚠️ 매수스킵(시세이상): {nm}"); continue
            unit = _tick_up(lp * (1 + BUY_BUF))
            qty = min(q, (cap - spent) // unit, (avail - spent) // unit)
            if qty <= 0:
                continue  # 캡/현금 소진 → 자연 종료
            ok = toss.buy_market_order(s, int(qty), price=unit)
            if ok:
                spent += qty * unit; done_b += 1
                _log(uid, s, nm, 'BUY', unit, int(qty))
            tg(f"  {'✅' if ok else '❌'} 매수접수 {nm} {int(qty)}주 × {unit:,.0f} (누적 {spent/1e4:,.0f}만)")
            time.sleep(0.6)
        time.sleep(SETTLE)
        _cancel_unfilled(toss)
        write_marker("DONE")
        save_dca(new_dca)  # 지수 DCA 원장 갱신(예약분 감소·소진시 {}). 배포 성공 후에만 커밋.
        final = read_account(toss)
        if final is not None:
            actual = cash - final[1]  # 초기 매수가능현금 - 최종현금 = 실제 집행액(접수 아닌 실체결)
            ftxt = f"현금 {final[1]/1e4:,.0f}만, 보유 {len(final[0])}종목"
        else:
            actual = None; ftxt = "조회실패"
        est = spent + obs_cost  # 접수기준 추정(관찰분 포함)
        mismatch = actual is not None and abs(actual - est) > 100_000
        _journal('deploy', {'buys': done_b, 'spent_est': int(est),
                            'spent_actual': (int(actual) if actual is not None else None)})
        act_txt = f"{actual/1e4:,.0f}만" if actual is not None else f"~{est/1e4:,.0f}만(추정)"
        tg(f"🔴 완료(신규자금 배분): 매수접수 {done_b}건 · 실체결 ≈{act_txt}. 계좌: {ftxt}."
           + (f"\n🚨 접수({est/1e4:,.0f}만) vs 실체결 차이 큼 — 미체결 잔존 의심, 계좌·미체결 확인 요망" if mismatch else ""))
    finally:
        try:
            os.remove(LOCK)
        except Exception:
            pass
    return 0


def _observe_first_fill(toss, uid, buys):
    """최초 실주문 검증: ETF 1주 매수 → 체결을 계좌수량 변화로 확인. 성공시 OBS_MARKER 기록.
    반환 (통과여부, 관찰분 1주를 차감한 buys, 관찰매수 소요액)."""
    tg("🔬 최초 실주문 관찰체결: KODEX200 1주 매수로 주문경로 검증...")
    pre = read_account(toss)
    if pre is None:
        tg("⛔ 관찰체결 중단: 계좌조회 실패"); return False, buys, 0
    pre_q = pre[0].get(ETF, (0,))[0]
    lp = toss.get_price(ETF)
    if not lp or lp <= 0:
        tg("⛔ 관찰체결 중단: ETF 시세 실패"); return False, buys, 0
    unit = _tick_up(lp * (1 + BUY_BUF))
    ok = toss.buy_market_order(ETF, 1, price=unit)
    if not ok:
        tg("⛔ 관찰체결 실패: 주문접수 거부 — 오늘 중단, 수동확인 필요"); return False, buys, 0
    _log(uid, ETF, 'KODEX200', 'BUY', unit, 1)
    time.sleep(SETTLE)
    post = read_account(toss)
    post_q = post[0].get(ETF, (0,))[0] if post else pre_q
    if post_q <= pre_q:
        # 지연체결 가능성 — 한 번 더 기다렸다 재확인 (감사 7)
        time.sleep(SETTLE + 4)
        post2 = read_account(toss)
        post_q = post2[0].get(ETF, (0,))[0] if post2 else pre_q
    if post_q <= pre_q:
        _cancel_unfilled(toss)
        tg("⛔ 관찰체결 미확인: 체결 안 됨 — 관찰주문 취소, 오늘 중단(내일 재시도)")
        return False, buys, 0
    _write_obs()
    tg(f"✅ 관찰체결 확인(ETF {pre_q}→{post_q}주) — 본 매수 진행")
    out = []
    for s, q, nm, tlp in buys:  # 관찰분 1주 차감
        if s == ETF:
            q -= 1
        if q > 0:
            out.append((s, q, nm, tlp))
    return True, out, unit


# ─────────── 분기 완전자동 리밸런스 (매도포함, T+2 안전) ───────────

def rebalance_main(execute=False, max_buy=None):
    toss, uid = load_toss()
    if toss is None:
        print("⛔ Toss 자격증명 없음"); return 1
    q = quarter_tag()
    rq, rstate = read_reb()
    if rq == q and rstate == 'DONE':
        print(f"{q} 리밸런스 이미 완료 — 종료"); return 0
    pending = (rq == q and rstate == 'ACTIVE')
    if not pending and not is_rebalance_week():
        print("리밸런스 주간 아님 · 진행중 플랜 없음 — 종료"); return 0

    acct = read_account(toss)
    if acct is None:
        msg = "⛔ 리밸런스 중단: 계좌조회 실패 — 아무것도 안 함"; print(msg); tg(msg); return 1
    holdings, cash, total = acct
    if execute and not getattr(toss, 'account_seq', 'x'):
        msg = "⛔ 리밸런스 거부: 계좌seq 미확보(자동조회 실패) — 주문 헤더 누락 위험. --probe로 확인 필요"; print(msg); tg(msg); return 1

    # ⑧ 수동보유(놀이돈) 제외 — 리밸런스 불간섭 + 예산에서 차감
    mh = load_manual_hold() & set(holdings)
    mh_val = 0.0
    for s in sorted(mh):
        lp = toss.get_price(s)
        if not lp or lp <= 0:
            # 거래정지 등 — 매입가로 대체평가(수동보유 1종목이 리밸런스를 동결하지 않게)
            try:
                bal = toss.get_account_balance()
                row = next((x for x in bal.get('stocks', []) if x.get('ticker') == s), None)
                lp = float(row.get('purchase_price', 0) or 0) if row else 0
            except Exception:
                lp = 0
        if lp <= 0:
            msg = f"⛔ 리밸런스 중단: 수동보유 {holdings[s][1]}({s}) 평가불능(시세·매입가 0)"; print(msg); tg(msg); return 1
        mh_val += holdings[s][0] * lp
    holdings = {s: h for s, h in holdings.items() if s not in mh}
    budget = total - mh_val
    print(f"[계좌] 관리대상 {len(holdings)}종목 + 현금 {cash/1e4:,.0f}만 = 예산 {budget/1e4:,.0f}만"
          + (f" (수동보유 {len(mh)}종목 {mh_val/1e4:,.0f}만 제외)" if mh else ""))

    # ── 진행중 플랜 이어가기 (T+2 결제 대기 후 잔여 매도/매수) ──
    if pending:
        plan = load_plan()
        if not plan or plan.get('quarter') != q:
            # 감사 5: ACTIVE+플랜소실이 전체 동결로 이어지지 않게 자가복구 — 이번 분기 포기(DONE)
            msg = ("⚠️ 리밸런스 자가복구: 상태 ACTIVE인데 플랜파일 없음/분기불일치 — 이번 분기 리밸런스 포기(DONE 처리). "
                   "잔여현금은 auto_deploy가 흡수, 다음 분기에 실보유 기준 재계획.")
            print(msg)
            if execute:
                tg(msg); write_reb('DONE')
            return 1
        if not execute:
            sells, buys = plan_diffs(plan['items'], holdings)
            print(f"📋 [드라이런] 플랜 잔여: 매도 {len(sells)}건, 매수 {len(buys)}건, 현금 {cash/1e4:,.0f}만")
            return 0
        if not toss.is_kr_market_open():
            print("정규장 아님 — 다음 장중 이어감"); return 0
        if not _gate_open_orders(toss):
            return 0
        if not _acquire_lock():
            msg = "⛔ 리밸런스 거부: 락파일(다른 실행 진행중)"; print(msg); tg(msg); return 0
        try:
            done = _rebalance_pass(toss, uid, q, plan, holdings, max_buy)
            if done:
                write_reb('DONE'); _journal('rebalance_done', {'quarter': q})
                tg(f"✅ {q} 분기 리밸런스 완료 — 다음 분기까지 손볼 일 없음")
        finally:
            _release_lock()
        return 0

    # ── 리밸주간 첫 실행: 플랜 생성 ──
    print("[스캔] 전 종목 시세 수집...")
    cl, names = fetch_universe()
    target, meta = compute_target(cl, names, max(budget, 1_000_000))
    if target is None:
        msg = f"⛔ 리밸런스 중단: 목표산출 실패 ({meta})"; print(msg); tg(msg); return 1

    # ⑦ 부트스트랩 게이트: 지수ETF 미보유 = 알고리즘 미투입 계좌 → 매도 자동화 금지.
    #    실행모드면 이번 분기 DONE 기록 → auto_deploy가 리밸주간에도 현금투입 가능(감사 3)
    if ETF not in holdings:
        acct2 = read_account(toss)  # 일시적 응답누락으로 분기를 통째로 스킵하지 않게 이중확인
        if acct2 is not None and ETF in acct2[0]:
            msg = "⚠️ 리밸런스 중단: 계좌응답 불일치(ETF 보유여부) — 다음 실행에서 재시도"; print(msg); tg(msg); return 1
        msg = (f"⏸️ {q} 리밸런스 스킵: 지수ETF({ETF}) 미보유 — 아직 알고리즘 투입 전 계좌. 이번 분기 리밸런스 없음.\n"
               f"최초 1회만: 기존 종목을 직접 매도하세요 → 봇이 현금을 감지해 자동매수(auto_deploy) → 다음 분기부터 매도까지 완전자동.")
        print(msg)
        if execute:
            write_reb('DONE'); tg(msg)
        return 0

    items, skipped = build_plan_items(target, holdings, toss, budget)
    sells, buys = plan_diffs(items, holdings)
    sell_val = sum(qq * lp for _, qq, _, lp in sells)
    buy_val = sum(qq * lp for _, qq, _, lp in buys)
    hold_val = total - cash - mh_val

    L = [f"🔄 분기 리밸런스 {'🔴실행' if execute else '📋드라이런(주문0)'} — {q} ({meta['last_day']})"]
    L.append(f"예산 {budget/1e4:,.0f}만 · 매도 {len(sells)}건 ≈{sell_val/1e4:,.0f}만 → 매수 {len(buys)}건 ≈{buy_val/1e4:,.0f}만")
    for s, qq, nm, lp in sells[:30]:
        L.append(f"  ➖ {nm[:10]}({s}) {qq}주 × {lp:,.0f}")
    for s, qq, nm, lp in buys[:35]:
        L.append(f"  ➕ {nm[:10]}({s}) {qq}주 × {lp:,.0f}")
    if skipped:
        L.append(f"[스킵 {len(skipped)}] " + ", ".join(f"{nm}({r})" for nm, r in skipped[:6]))
    if hold_val > 0 and sell_val > hold_val * 0.6:
        L.append(f"⚠️ 매도비중 큼: 보유의 {sell_val/hold_val*100:.0f}% — 종목교체 많은 분기(확인 권장)")
    rep = "\n".join(L); print(rep); tg(rep)
    if not execute:
        print("\n📋 드라이런 종료 — 주문 없음."); return 0

    # 실행 게이트
    if not toss.is_kr_market_open():
        msg = "⛔ 리밸런스 거부: 정규장 아님(장중에만) — 다음 장중 자동 재시도"; print(msg); tg(msg); return 0
    if not _gate_open_orders(toss):
        return 0
    if not _acquire_lock():
        msg = "⛔ 리밸런스 거부: 락파일(다른 실행 진행중). 오래된 락이면 auto_order.lock 수동삭제."; print(msg); tg(msg); return 0
    try:
        plan = {'quarter': q, 'created': datetime.date.today().isoformat(), 'items': items}
        save_plan(plan)
        write_reb('ACTIVE')
        _journal('rebalance_plan', {'quarter': q, 'n_items': len(items),
                                    'sell_val': int(sell_val), 'buy_val': int(buy_val)})
        tg(f"🔴 {q} 리밸런스 시작 — 절대목표 {len(items)}종목 플랜 저장. 매도→실현금 재조회→매수 (미완이면 다음날 자동 이어감).")
        done = _rebalance_pass(toss, uid, q, plan, holdings, max_buy)
        if done:
            write_reb('DONE'); _journal('rebalance_done', {'quarter': q})
            tg(f"✅ {q} 분기 리밸런스 완료 — 다음 분기까지 손볼 일 없음")
    finally:
        _release_lock()
    return 0


def _gate_open_orders(toss):
    try:
        oo = toss.get_open_orders()
    except Exception:
        oo = None
    if oo is None:
        msg = "⛔ 리밸런스 거부: 미체결 주문 조회 실패 — 안전상 중단"; print(msg); tg(msg); return False
    if oo:
        msg = "⛔ 리밸런스 거부: 미체결 주문 존재 — 충돌방지(체결반영 후 재시도)"; print(msg); tg(msg); return False
    return True


def _release_lock():
    try:
        os.remove(LOCK)
    except Exception:
        pass


def _rebalance_pass(toss, uid, q, plan, holdings, max_buy=None):
    """플랜 1회 패스: 잔여매도 → 실현금 재조회 → 잔여매수 → 완료판정.
    반환 True=완료(DONE 가능), False=미완(내일 크론이 이어감)."""
    age = (datetime.date.today() - datetime.date.fromisoformat(plan['created'])).days
    sells, buys = plan_diffs(plan['items'], holdings)
    rem0 = sum(n * p for _, n, _, p in sells) + sum(n * p for _, n, _, p in buys)
    if rem0 < MIN_CASH_ORDER:
        return True  # 잔여가 10만 미만 = 사실상 완료
    if age > PLAN_EXPIRE_DAYS:
        tg(f"⚠️ 리밸런스 플랜 {age}일 경과(만료) — 잔여 매도{len(sells)}·매수{len(buys)} ≈{rem0/1e4:,.0f}만 포기하고 종료. "
           f"잔여현금은 auto_deploy가 흡수, 잔여보유는 다음 분기 리밸런스가 처리.")
        return True

    # 잔여 매도 (첫 패스엔 전체 매도가 여기서 나감)
    if sells:
        tg(f"🔄 리밸런스 매도(D+{age}): {len(sells)}건")
        done_s = 0
        need_obs = not os.path.exists(OBS_MARKER)  # ⑨ 최초 실주문이면 첫 매도를 관찰체결로 검증
        for s, qq, nm, pp in sells:
            lp = toss.get_price(s)
            if not lp or lp <= 0:
                tg(f"  ⚠️ 매도보류(시세실패): {nm} — 다음날 재시도"); continue
            if abs(lp / pp - 1) > PRICE_SANITY:
                # 감사 4: 계획가 대비 큰 괴리 — 오염시세인지 실제 급변인지 재조회 일치(±1%)로 판별.
                # 실제 가격변동이면 현재가 기준으로 매도 진행(급락 엑싯을 분기 내내 못 파는 사고 방지)
                time.sleep(1.2)
                lp2 = toss.get_price(s)
                if not lp2 or lp2 <= 0 or abs(lp2 / lp - 1) > 0.01:
                    tg(f"  ⚠️ 매도보류(시세불안정): {nm} 계획 {pp:,.0f}→현재 {lp:,.0f} — 다음날 재시도"); continue
                lp = lp2
            limit = int(lp * (1 - SELL_BUF))
            if limit <= 0:
                tg(f"  ⚠️ 매도보류(가격이상): {nm}"); continue
            ok = toss.sell_market_order(s, int(qq), price=limit)
            if ok:
                done_s += 1
                _log(uid, s, nm, 'SELL', limit, int(qq))
            tg(f"  {'✅' if ok else '❌'} 매도접수 {nm} {int(qq)}주 × {limit:,.0f}")
            if ok and need_obs:
                time.sleep(SETTLE)
                chk = read_account(toss)
                held0 = holdings.get(s, (0,))[0]
                if chk is None or chk[0].get(s, (0,))[0] >= held0:  # 계좌에서 사라짐=전량체결(0주)
                    _cancel_unfilled(toss)
                    tg("⛔ 관찰체결(첫 매도) 미확인 — 오늘 중단, 다음 장중 재시도"); return False
                _write_obs()
                tg("✅ 관찰체결 확인(첫 매도) — 리밸런스 계속"); need_obs = False
            time.sleep(0.6)
        time.sleep(SETTLE)
        _cancel_unfilled(toss)

    # 매도 반영된 실계좌 재조회 (⑤ 실현금 기준 매수)
    acct = read_account(toss)
    if acct is None:
        tg("⚠️ 리밸런스: 매도 후 계좌조회 실패 — 오늘 중단, 내일 이어감"); return False
    holdings, cash, _ = acct
    mh = load_manual_hold()
    holdings = {s: h for s, h in holdings.items() if s not in mh}
    sells_rem, buys = plan_diffs(plan['items'], holdings)
    rem_val = sum(n * p for _, n, _, p in buys) + sum(n * p for _, n, _, p in sells_rem)
    if rem_val < MIN_CASH_ORDER:
        return True  # 잔여가 10만 미만 = 사실상 완료

    if buys:
        if cash < MIN_CASH_ORDER:
            if age >= LOWCASH_GIVEUP_DAYS:
                tg(f"⚠️ 리밸런스: D+{age}에도 매수가능금액 {cash/1e4:,.0f}만 — 현금 유입 없음, 종료. 잔여 매수 {len(buys)}건 미수행.")
                return True
            tg(f"⏳ 리밸런스 매수대기(D+{age}): 매수가능금액 {cash/1e4:,.0f}만 <10만 — T+2 결제 대기, 다음 장중 자동 이어감.")
            return False
        if not os.path.exists(OBS_MARKER):  # ⑨ 매도 없이 매수부터 시작하는 희귀 케이스도 관찰체결
            okobs, _, obs_cost = _observe_first_fill(toss, uid, [])
            if not okobs:
                return False
            cash -= obs_cost
        cap = max_buy if max_buy else cash
        buy_val = sum(n * p for _, n, _, p in buys)
        tg(f"🔄 리밸런스 매수(D+{age}): {len(buys)}건 ≈{buy_val/1e4:,.0f}만, 가용 {cash/1e4:,.0f}만")
        spent = 0; done_b = 0
        for s, need, nm, pp in buys:
            w = toss.has_investment_warning(s)
            if w is None:
                tg(f"  ⚠️ 매수스킵(경고조회실패=안전측): {nm}"); continue
            if w:
                tg(f"  ⚠️ 매수스킵(관리/경고): {nm}"); continue
            lp = toss.get_price(s)
            if not lp or lp <= 0 or abs(lp / pp - 1) > PRICE_SANITY:
                tg(f"  ⚠️ 매수보류(시세이상): {nm} — 다음날 재시도"); continue
            unit = _tick_up(lp * (1 + BUY_BUF))
            qty = min(need, (cap - spent) // unit, (cash - spent) // unit)
            if qty <= 0:
                continue
            ok = toss.buy_market_order(s, int(qty), price=unit)
            if ok:
                spent += qty * unit; done_b += 1
                _log(uid, s, nm, 'BUY', unit, int(qty))
            tg(f"  {'✅' if ok else '❌'} 매수접수 {nm} {int(qty)}주 × {unit:,.0f} (누적 {spent/1e4:,.0f}만)")
            time.sleep(0.6)
        time.sleep(SETTLE)
        _cancel_unfilled(toss)

    # 완료판정: 최종 계좌 기준 잔여 diff
    final = read_account(toss)
    if final is None:
        tg("⚠️ 리밸런스: 최종 계좌조회 실패 — 내일 재검증"); return False
    fh, fc, _ = final
    fh = {s: h for s, h in fh.items() if s not in mh}
    s2, b2 = plan_diffs(plan['items'], fh)
    rem2 = sum(n * p for _, n, _, p in b2) + sum(n * p for _, n, _, p in s2)
    done = rem2 < MIN_CASH_ORDER
    _journal('rebalance_pass', {'quarter': q, 'day': age, 'rem_val': int(rem2), 'done': done})
    if not done:
        tg(f"⏳ 리밸런스 부분진행(D+{age}): 잔여 ≈{rem2/1e4:,.0f}만 (매도{len(s2)}·매수{len(b2)}), 현금 {fc/1e4:,.0f}만 — 다음 장중 자동 이어감.")
    return done


def _acquire_lock():
    """O_EXCL 배타락. stale(죽은PID/2h경과)면 회수 후 재시도."""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
        return True
    except FileExistsError:
        try:
            pid = int(open(LOCK).read().strip() or 0)
            alive = True
            try:
                os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                alive = False
            stale = (time.time() - os.path.getmtime(LOCK)) > 7200
            if not alive or stale:
                os.remove(LOCK)
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
                return True
        except Exception:
            pass
        return False


def _log(uid, ticker, name, action, price, qty):
    try:
        from base.database import log_trade_journal
        log_trade_journal(uid, ticker, name, action, price, strategy='algo_v1', ai_reason='', shares=qty, mode='KR')
    except Exception:
        pass


if __name__ == '__main__':
    a = sys.argv[1:]
    mx = int(a[a.index('--max') + 1]) if '--max' in a else None
    bg = int(a[a.index('--budget') + 1]) if '--budget' in a else None
    if '--rebalance' in a or '--full' in a:
        sys.exit(rebalance_main('--execute' in a, mx))
    sys.exit(main('--execute' in a, mx, '--force' in a, '--probe' in a, bg, '--anytime' in a))
