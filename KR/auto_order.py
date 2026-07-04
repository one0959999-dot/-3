"""자동주문 v1.0 — 알고리즘 v1.0(⅓×3) 목표를 실계좌에 diff 체결.

★설계: 구봇(KR/bot.py)의 killswitch·AI게이트·손절·고빈도 로직은 전부 배제.
  오직 검증된 실행층(toss.buy/sell_market_order)만 재활용 + 안전장치 6겹.
  목표 종목 = live_v1.compute_target(단일소스). 수량 = 실제 Toss 시세로 재계산(조정가≠체결가).

안전장치:
  ① 기본 = 드라이런(주문 0). 실주문은 --execute 명시 필요.
  ② 총매수 하드캡 --max (기본 50만). 초과 시 실행 거부.
  ③ --execute는 리밸런스 주간(1·4·7·10월 첫주)만. (--force로 우회 = 테스트 전용)
  ④ diff만 체결: 현 보유와 목표가 같으면 안 건드림.
  ⑤ 매수 전: 관리종목·거래정지 스킵, 가격 이상치(Toss시세 vs 목표 ±15%↑) 스킵, 지정가 ±2% 버퍼.
  ⑥ 매도 먼저 → 현금 확인 → 매수. 분기 중복실행 차단(마커파일). 전 주문 텔레그램 로그.

실행:
  python KR/auto_order.py --probe             # 계좌조회만 (읽기전용)
  python KR/auto_order.py                      # 드라이런: 목표·diff 계획만 (주문 0)
  python KR/auto_order.py --execute --max 500000   # 실주문 (리밸런스 주간, 총매수 50만 이하)
"""
import sys, os, sqlite3, time, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import warnings
warnings.filterwarnings('ignore')
from KR.live_v1 import fetch_universe, compute_target, is_rebalance_week, tg, ETFS, N_SLEEVE, BUY_COST

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
MARKER = P('auto_order_done.txt')
PRICE_SANITY = 0.15   # Toss 실시세 vs 목표가 괴리 상한
LIMIT_BUF = 0.02      # 지정가 버퍼 (매수 +2% / 매도 -2%)


def load_toss():
    c = sqlite3.connect(P('lassi.db')); c.row_factory = sqlite3.Row
    u = c.execute("SELECT toss_client_id, toss_client_secret, toss_account_seq FROM users "
                  "WHERE toss_client_id IS NOT NULL AND toss_client_id!='' LIMIT 1").fetchone()
    uid = c.execute("SELECT id FROM users WHERE toss_client_id IS NOT NULL AND toss_client_id!='' LIMIT 1").fetchone()
    c.close()
    if not u:
        return None, None
    from base.toss_api import TossInvestApi
    return TossInvestApi(u['toss_client_id'], u['toss_client_secret'], u['toss_account_seq'] or ''), uid['id']


def read_account(toss):
    """(holdings{sym:qty}, cash, total_value) 또는 None(실패=중단)."""
    bal = toss.get_account_balance()
    if not bal:
        return None
    holdings = {}
    hold_val = 0.0
    for s in bal.get('stocks', []):
        q = int(s.get('shares', 0) or 0)
        if q > 0:
            holdings[s['ticker']] = q
            hold_val += q * float(s.get('current_price', 0) or 0)
    cash = float(bal.get('cash', 0) or 0)
    if cash <= 0:
        cash = toss.get_buyable_cash()
    total = cash + hold_val
    return holdings, cash, total


def quarter_tag(d=None):
    d = d or datetime.date.today()
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def already_done():
    if not os.path.exists(MARKER):
        return False
    return open(MARKER).read().strip() == quarter_tag()


def mark_done():
    open(MARKER, 'w').write(quarter_tag())


def plan(target, holdings, toss, budget):
    """목표 수량을 실 Toss 시세로 재계산 → (sells, buys, skipped). budget 기준 ⅓×3 재배분."""
    sleeve = budget / 3
    n_stock = sum(1 for t in target if t['sleeve'] == '저변동')
    tgt_qty = {}; live = {}; skipped = []
    for t in target:
        sym = t['symbol']
        lp = toss.get_price(sym)
        live[sym] = lp
        if lp is None or lp <= 0:
            skipped.append((sym, t['name'], '시세조회 실패')); continue
        if abs(lp / t['price'] - 1) > PRICE_SANITY:
            skipped.append((sym, t['name'], f'가격괴리 {(lp/t["price"]-1)*100:+.0f}%')); continue
        alloc = sleeve if t['sleeve'] == '지수ETF' else sleeve / n_stock
        tgt_qty[sym] = (int(alloc // (lp * (1 + BUY_COST))), t['name'], lp)
    sells, buys = [], []
    tgt_syms = set(tgt_qty)
    for sym, cur in holdings.items():
        want = tgt_qty.get(sym, (0,))[0]
        if cur > want:
            nm = tgt_qty.get(sym, (0, sym))[1] if sym in tgt_qty else sym
            sells.append((sym, cur - want, nm))
    for sym, (want, nm, lp) in tgt_qty.items():
        cur = holdings.get(sym, 0)
        if want > cur:
            buys.append((sym, want - cur, nm, lp))
    return sells, buys, skipped


def main(execute=False, max_buy=500_000, force=False, probe=False, budget_override=None):
    toss, uid = load_toss()
    if toss is None:
        print("⛔ Toss 자격증명 없음"); return 1
    acct = read_account(toss)
    if acct is None:
        msg = "⛔ auto_order 중단: 계좌 조회 실패 (토큰/IP/API 오류) — 아무것도 안 함"
        print(msg); tg(msg); return 1
    holdings, cash, total = acct
    print(f"[계좌] 보유 {len(holdings)}종목, 현금 {cash/1e4:,.0f}만, 평가총액 {total/1e4:,.0f}만")
    for sym, q in holdings.items():
        print(f"   보유: {sym} {q}주")
    if probe:
        tg(f"🔍 계좌조회: 보유 {len(holdings)}종목, 현금 {cash/1e4:,.0f}만, 총 {total/1e4:,.0f}만\n" +
           "\n".join(f"  {s} {q}주" for s, q in holdings.items()))
        return 0

    budget = budget_override or total
    if budget < 100000:
        msg = f"⛔ auto_order 중단: 운용가능 {budget/1e4:.0f}만 < 10만 — 입금 필요"
        print(msg); tg(msg); return 1

    print(f"[스캔] 전 종목 시세 수집 중...")
    cl, names = fetch_universe()
    target, meta = compute_target(cl, names, budget)
    if target is None:
        msg = f"⛔ auto_order 중단: 목표산출 실패 ({meta})"
        print(msg); tg(msg); return 1
    sells, buys, skipped = plan(target, holdings, toss, budget)
    buy_val = sum(q * lp for _, q, _, lp in buys)
    sell_val = sum(q * toss.get_price(s) for s, q, _ in sells if toss.get_price(s))

    L = [f"🤖 자동주문 {'🔴실행' if execute else '📋드라이런(주문0)'} — {meta['last_day']} (운용 {budget/1e4:,.0f}만)"]
    L.append(f"목표: 코스피ETF+미국ETF+저변동 {meta['n_picks']}종목 (⅓×3)")
    L.append(f"\n[매도 {len(sells)}건 ≈{sell_val/1e4:,.0f}만]")
    for s, q, nm in sells[:30]:
        L.append(f"  ➖ {nm[:10]}({s}) {q}주")
    L.append(f"[매수 {len(buys)}건 ≈{buy_val/1e4:,.0f}만]")
    for s, q, nm, lp in buys[:30]:
        L.append(f"  ➕ {nm[:10]}({s}) {q}주 × {lp:,.0f}")
    if skipped:
        L.append(f"[스킵 {len(skipped)}건] " + ", ".join(f"{nm}({r})" for _, nm, r in skipped[:8]))
    rep = "\n".join(L)
    print(rep); tg(rep)

    if not execute:
        print("\n📋 드라이런 종료 — 주문 없음. 실주문은 --execute --max N")
        return 0

    # ── 실행 안전 게이트 ──
    if not (is_rebalance_week() or force):
        msg = "⛔ 실행 거부: 리밸런스 주간 아님 (1·4·7·10월 첫주만). 계획만 유지."
        print(msg); tg(msg); return 0
    if already_done():
        msg = f"⛔ 실행 거부: {quarter_tag()} 이미 리밸런스 완료 (중복 방지)"
        print(msg); tg(msg); return 0
    if buy_val > max_buy:
        msg = f"⛔ 실행 거부: 총매수 {buy_val/1e4:,.0f}만 > 하드캡 {max_buy/1e4:,.0f}만. --max 올려야 실행."
        print(msg); tg(msg); return 0

    tg(f"🔴 실주문 시작 — 매도 {len(sells)} → 매수 {len(buys)} (하드캡 {max_buy/1e4:.0f}만)")
    done_s = done_b = 0
    # 1) 매도 먼저
    for s, q, nm in sells:
        sellable = toss.get_sellable_qty(s)
        qty = min(q, sellable) if sellable else q
        if qty <= 0:
            continue
        lp = toss.get_price(s); price = int(lp * (1 - LIMIT_BUF)) if lp else 0
        ok = toss.sell_market_order(s, qty, price=price)
        done_s += ok
        _log(uid, s, nm, 'SELL', price or lp or 0, qty)
        tg(f"  {'✅' if ok else '❌'} 매도 {nm}({s}) {qty}주")
        time.sleep(0.5)
    time.sleep(2)
    # 2) 매수 (관리종목·현금 재확인)
    for s, q, nm, tlp in buys:
        if toss.has_investment_warning(s):
            tg(f"  ⚠️ 매수 스킵(관리/경고): {nm}({s})"); continue
        lp = toss.get_price(s)
        if not lp or lp <= 0 or abs(lp / tlp - 1) > PRICE_SANITY:
            tg(f"  ⚠️ 매수 스킵(시세이상): {nm}({s})"); continue
        buyable = toss.get_buyable_cash(s, int(lp))
        qty = q
        need = qty * lp * (1 + LIMIT_BUF)
        if buyable < need:
            qty = int(buyable // (lp * (1 + LIMIT_BUF)))
        if qty <= 0:
            tg(f"  ⚠️ 매수 스킵(현금부족): {nm}({s})"); continue
        price = int(lp * (1 + LIMIT_BUF))
        ok = toss.buy_market_order(s, qty, price=price)
        done_b += ok
        _log(uid, s, nm, 'BUY', price, qty)
        tg(f"  {'✅' if ok else '❌'} 매수 {nm}({s}) {qty}주 × {price:,.0f}")
        time.sleep(0.5)
    mark_done()
    tg(f"🔴 실주문 완료: 매도 {done_s}/{len(sells)} · 매수 {done_b}/{len(buys)}. {quarter_tag()} 마킹됨.")
    return 0


def _log(uid, ticker, name, action, price, qty):
    try:
        from base.database import log_trade_journal
        log_trade_journal(uid, ticker, name, action, price, strategy='algo_v1',
                          ai_reason='', shares=qty, mode='KR')
    except Exception:
        pass


if __name__ == '__main__':
    a = sys.argv[1:]
    mx = int(a[a.index('--max') + 1]) if '--max' in a else 500_000
    bg = int(a[a.index('--budget') + 1]) if '--budget' in a else None
    sys.exit(main(execute='--execute' in a, max_buy=mx, force='--force' in a,
                  probe='--probe' in a, budget_override=bg))
