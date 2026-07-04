"""자동주문 v1.1 — 알고리즘 v1.0(⅓×3) 목표를 실계좌에 diff 체결 (안전감사 반영).

★설계: 구봇(KR/bot.py) killswitch·AI게이트·손절·고빈도 배제. 검증된 실행층(toss)만 재활용.
  목표 종목 = live_v1.compute_target(단일소스). 수량 = 실 Toss 시세. 계좌상태(매도후 실현금)를 진실로.

안전장치(감사 반영):
  ① 기본 드라이런(주문0). 실주문 --execute 명시.  ② --force는 드라이런 전용(실주문 무력).
  ③ 총매수 하드캡 --max + 매수루프 spent 누적, 실현금·캡 동시 상한.  ④ diff만 체결.
  ⑤ lp 유효성 필수(실패=스킵, price=0 매도 절대금지). 가격괴리·관리종목·거래정지 스킵.
  ⑥ 매도먼저 → 재조회 실현금 기준 매수. 계좌상태로 체결확인(bool 불신).
  ⑦ 정규장만. 타프로세스 미체결주문 있으면 중단(구봇 충돌 방지). O_EXCL 락 + 원자적 DONE마커.

실행:
  python KR/auto_order.py --probe                     # 계좌조회만
  python KR/auto_order.py                              # 드라이런(주문0)
  python KR/auto_order.py --execute --max 500000      # 실주문(리밸런스주간·정규장·캡50만)
"""
import sys, os, sqlite3, time, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import warnings
warnings.filterwarnings('ignore')
from KR.live_v1 import fetch_universe, compute_target, is_rebalance_week, tg, ETFS, BUY_COST

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
MARKER = P('auto_order_done.txt'); LOCK = P('auto_order.lock')
PRICE_SANITY = 0.15; SELL_BUF = 0.02; BUY_BUF = 0.02; SETTLE = 8


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
    """(holdings{sym:(qty,name)}, cash, total) 또는 None(=API실패, 중단)."""
    bal = toss.get_account_balance()
    if not bal:
        return None
    holdings = {}; hold_val = 0.0
    for s in bal.get('stocks', []):
        q = int(s.get('shares', 0) or 0)
        if q > 0:
            holdings[s['ticker']] = (q, s.get('name', s['ticker']))
            hold_val += q * float(s.get('current_price', 0) or 0)
    cash = float(bal.get('cash', 0) or 0)
    return holdings, cash, cash + hold_val


def quarter_tag(d=None):
    d = d or datetime.date.today(); return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def already_done():
    try:
        return open(MARKER).read().strip().endswith(quarter_tag() + ":DONE")
    except Exception:
        return False  # 읽기실패=안전측(미완료로 간주)하되, STARTED 마커로 재진입은 별도 차단


def marker_started_uncommitted():
    """직전 실행이 STARTED만 남기고 죽었나 (부분체결 위험 → 수동점검 필요)."""
    try:
        v = open(MARKER).read().strip()
        return v.endswith(quarter_tag() + ":STARTED")
    except Exception:
        return False


def write_marker(state):  # 원자적
    tmp = MARKER + '.tmp'
    open(tmp, 'w').write(f"{quarter_tag()}:{state}")
    os.replace(tmp, MARKER)


def plan(target, holdings, toss, budget):
    """목표수량=실 Toss시세. 반환 (sells[(sym,qty,name)], buys[(sym,qty,name,lp)], skipped)."""
    sleeve = budget / 3
    n_stock = sum(1 for t in target if t['sleeve'] == '저변동')
    tgt = {}; skipped = []
    for t in target:
        sym = t['symbol']; lp = toss.get_price(sym)
        if not lp or lp <= 0:
            skipped.append((t['name'], '시세실패')); continue
        if abs(lp / t['price'] - 1) > PRICE_SANITY:
            skipped.append((t['name'], f'괴리{(lp/t["price"]-1)*100:+.0f}%')); continue
        alloc = sleeve if t['sleeve'] == '지수ETF' else sleeve / max(n_stock, 1)
        tgt[sym] = (int(alloc // (lp * (1 + BUY_COST))), t['name'], lp)
    sells, buys = [], []
    for sym, (q, nm) in holdings.items():
        want = tgt.get(sym, (0,))[0]
        if q > want:
            sells.append((sym, q - want, nm))
    for sym, (want, nm, lp) in tgt.items():
        cur = holdings.get(sym, (0,))[0]
        if want > cur:
            buys.append((sym, want - cur, nm, lp))
    return sells, buys, skipped


def _confirm_sold(toss, before):
    """계좌 재조회로 실제 매도수량 확인(bool 불신). 반환 (실현금, 남은holdings)."""
    time.sleep(SETTLE)
    try:
        toss.cancel_all_unfilled()  # 미체결 지정가 정리
    except Exception:
        pass
    time.sleep(2)
    acct = read_account(toss)
    return acct  # None이면 실패


def main(execute=False, max_buy=500_000, force=False, probe=False, budget_override=None):
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

    budget = budget_override or total
    print("[스캔] 전 종목 시세 수집...")
    cl, names = fetch_universe()
    target, meta = compute_target(cl, names, budget)
    if target is None:
        msg = f"⛔ 중단: 목표산출 실패 ({meta})"; print(msg); tg(msg); return 1
    sells, buys, skipped = plan(target, holdings, toss, budget)
    buy_val = sum(q * int(lp * (1 + BUY_BUF)) for _, q, _, lp in buys)

    head = '🔴실행' if execute else '📋드라이런(주문0)'
    L = [f"🤖 자동주문 {head} — {meta['last_day']} (운용 {budget/1e4:,.0f}만, 캡 {max_buy/1e4:.0f}만)"]
    L.append(f"[매도 {len(sells)}건]  " + ", ".join(f"{nm[:8]} {q}주" for _, q, nm in sells[:20]))
    L.append(f"[매수 {len(buys)}건 계획 ≈{buy_val/1e4:,.0f}만]")
    for s, q, nm, lp in buys[:30]:
        L.append(f"  ➕ {nm[:10]}({s}) {q}주 × {lp:,.0f}")
    if skipped:
        L.append(f"[스킵 {len(skipped)}] " + ", ".join(f"{nm}({r})" for nm, r in skipped[:8]))
    rep = "\n".join(L); print(rep); tg(rep)

    if not execute:
        print("\n📋 드라이런 종료 — 주문 없음."); return 0

    # ─────────── 실행 안전 게이트 ───────────
    if force:
        print("⚠️ --force는 드라이런 전용, 실주문에선 무시됨")
    if not is_rebalance_week():
        msg = "⛔ 실행거부: 리밸런스 주간 아님(1·4·7·10월 첫주)"; print(msg); tg(msg); return 0
    if already_done():
        msg = f"⛔ 실행거부: {quarter_tag()} 이미 완료(중복차단)"; print(msg); tg(msg); return 0
    if marker_started_uncommitted():
        msg = f"⛔ 실행거부: 직전 {quarter_tag()} 실행이 미완료 종료(부분체결 위험). 수동 점검 필요."; print(msg); tg(msg); return 0
    if not toss.is_kr_market_open():
        msg = "⛔ 실행거부: 정규장 아님(장중에만 실주문)"; print(msg); tg(msg); return 0
    try:
        if toss.get_open_orders():
            msg = "⛔ 실행거부: 미체결 주문 존재(구봇/이전실행 활동?) — 충돌방지 중단"; print(msg); tg(msg); return 0
    except Exception:
        pass
    if buy_val > max_buy:
        msg = f"⛔ 실행거부: 계획매수 {buy_val/1e4:,.0f}만 > 하드캡 {max_buy/1e4:.0f}만. --max 상향 필요."; print(msg); tg(msg); return 0
    # 배타 락
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
    except FileExistsError:
        msg = "⛔ 실행거부: 락파일 존재(다른 실행 진행중)"; print(msg); tg(msg); return 0

    try:
        write_marker("STARTED")
        tg(f"🔴 실주문 시작 — 매도 {len(sells)} → 매수 {len(buys)} (캡 {max_buy/1e4:.0f}만)")
        # 1) 매도 (lp 필수, price=0 금지)
        for s, q, nm in sells:
            lp = toss.get_price(s)
            if not lp or lp <= 0:
                tg(f"  ⚠️ 매도스킵(시세실패): {nm}({s})"); continue
            sellable = toss.get_sellable_qty(s)
            if sellable is None or sellable <= 0:
                tg(f"  ⚠️ 매도스킵(매도가능0/실패): {nm}({s})"); continue
            qty = min(q, sellable)
            ok = toss.sell_market_order(s, qty, price=int(lp * (1 - SELL_BUF)))
            _log(uid, s, nm, 'SELL', int(lp * (1 - SELL_BUF)), qty)
            tg(f"  {'✅' if ok else '❌'} 매도접수 {nm} {qty}주"); time.sleep(0.6)
        # 2) 매도 실체결 확인 → 실현금
        acct2 = _confirm_sold(toss, holdings)
        if acct2 is None:
            write_marker("STARTED"); msg = "⛔ 매도후 계좌조회 실패 — 매수 중단(부분상태). 수동점검."; print(msg); tg(msg); return 1
        holdings2, cash2, _ = acct2
        try:
            bp = toss.get_buyable_cash() or 0
        except Exception:
            bp = 0
        avail = max(cash2, bp)  # 매수여력(미결제 매도대금 포함) 우선, 안전측
        tg(f"  💰 매도후 매수여력 {avail/1e4:,.0f}만 — 이 범위+캡 내에서만 매수")
        # 3) 매수 (실현금·캡 동시 상한, spent 누적)
        spent = 0; done_b = 0
        for s, q, nm, tlp in buys:
            if toss.has_investment_warning(s):
                tg(f"  ⚠️ 매수스킵(관리/경고): {nm}"); continue
            lp = toss.get_price(s)
            if not lp or lp <= 0 or abs(lp / tlp - 1) > PRICE_SANITY:
                tg(f"  ⚠️ 매수스킵(시세이상): {nm}"); continue
            unit = int(lp * (1 + BUY_BUF))
            cap_room = max_buy - spent
            cash_room = avail - spent
            qty = min(q, cap_room // unit, cash_room // unit)
            if qty <= 0:
                continue  # 캡/현금 소진 → 이후 자연 중단
            ok = toss.buy_market_order(s, int(qty), price=unit)
            if ok:
                spent += qty * unit; done_b += 1
            _log(uid, s, nm, 'BUY', unit, int(qty))
            tg(f"  {'✅' if ok else '❌'} 매수접수 {nm} {int(qty)}주 × {unit:,.0f} (누적 {spent/1e4:,.0f}만)")
            time.sleep(0.6)
        time.sleep(SETTLE)
        try:
            toss.cancel_all_unfilled()
        except Exception:
            pass
        write_marker("DONE")
        final = read_account(toss)
        ftxt = f"현금 {final[1]/1e4:,.0f}만, 보유 {len(final[0])}종목" if final else "조회실패"
        tg(f"🔴 실주문 완료: 매수접수 {done_b}건 ≈{spent/1e4:,.0f}만 (캡 {max_buy/1e4:.0f}만). 계좌: {ftxt}. {quarter_tag()} DONE.")
    finally:
        try:
            os.remove(LOCK)
        except Exception:
            pass
    return 0


def _log(uid, ticker, name, action, price, qty):
    try:
        from base.database import log_trade_journal
        log_trade_journal(uid, ticker, name, action, price, strategy='algo_v1', ai_reason='', shares=qty, mode='KR')
    except Exception:
        pass


if __name__ == '__main__':
    a = sys.argv[1:]
    mx = int(a[a.index('--max') + 1]) if '--max' in a else 500_000
    bg = int(a[a.index('--budget') + 1]) if '--budget' in a else None
    sys.exit(main('--execute' in a, mx, '--force' in a, '--probe' in a, bg))
