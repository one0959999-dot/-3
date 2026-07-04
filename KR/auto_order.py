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


def plan_buyonly(target, toss, cash):
    """★신규자금 배분(매도 없음): 가용현금을 ⅓×3로 배분해 매수만. 기존보유 불간섭.
    반환 (buys[(sym,qty,name,lp)], skipped)."""
    sleeve = cash / 3
    n_stock = sum(1 for t in target if t['sleeve'] == '저변동')
    buys, skipped = [], []
    for t in target:
        sym = t['symbol']; lp = toss.get_price(sym)
        if not lp or lp <= 0:
            skipped.append((t['name'], '시세실패')); continue
        if abs(lp / t['price'] - 1) > PRICE_SANITY:
            skipped.append((t['name'], f'괴리{(lp/t["price"]-1)*100:+.0f}%')); continue
        alloc = sleeve if t['sleeve'] == '지수ETF' else sleeve / max(n_stock, 1)
        q = int(alloc // (lp * (1 + BUY_BUF)))
        if q > 0:
            buys.append((sym, q, t['name'], lp))
    return buys, skipped


def plan_full(target, holdings, toss, budget):
    """전체 리밸런스(매도포함) — 드라이런 표시 전용. 실행은 T+2 안전확보 전까지 차단."""
    sleeve = budget / 3
    n_stock = sum(1 for t in target if t['sleeve'] == '저변동')
    tgt = {}
    for t in target:
        sym = t['symbol']; lp = toss.get_price(sym)
        if not lp or lp <= 0 or abs(lp / t['price'] - 1) > PRICE_SANITY:
            continue
        alloc = sleeve if t['sleeve'] == '지수ETF' else sleeve / max(n_stock, 1)
        tgt[sym] = (int(alloc // (lp * (1 + BUY_COST))), t['name'], lp)
    sells = [(s, q - tgt.get(s, (0,))[0], nm) for s, (q, nm) in holdings.items() if q > tgt.get(s, (0,))[0]]
    buys = [(s, w - holdings.get(s, (0,))[0], nm, lp) for s, (w, nm, lp) in tgt.items() if w > holdings.get(s, (0,))[0]]
    return sells, buys


def main(execute=False, max_buy=500_000, force=False, probe=False, budget_override=None, full=False):
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
    print("[스캔] 전 종목 시세 수집...")
    cl, names = fetch_universe()
    target, meta = compute_target(cl, names, max(deploy, 1_000_000))  # 선정은 예산무관
    if target is None:
        msg = f"⛔ 중단: 목표산출 실패 ({meta})"; print(msg); tg(msg); return 1
    buys, skipped = plan_buyonly(target, toss, deploy)
    buy_val = sum(q * int(lp * (1 + BUY_BUF)) for _, q, _, lp in buys)
    fsells, fbuys = plan_full(target, holdings, toss, total)

    head = '🔴실행' if (execute and not full) else '📋드라이런(주문0)'
    L = [f"🤖 자동주문 {head} — {meta['last_day']}"]
    L.append(f"[기본=신규자금 배분] 가용현금 {deploy/1e4:,.0f}만 → 매수 {len(buys)}건 ≈{buy_val/1e4:,.0f}만 (기존보유 불간섭, 캡 {max_buy/1e4:.0f}만)")
    for s, q, nm, lp in buys[:30]:
        L.append(f"  ➕ {nm[:10]}({s}) {q}주 × {lp:,.0f}")
    if skipped:
        L.append(f"[스킵 {len(skipped)}] " + ", ".join(f"{nm}({r})" for nm, r in skipped[:6]))
    L.append(f"[참고: 전체리밸런스(--full)라면 매도 {len(fsells)}건 포함 — 실행은 T+2 안전확보 전까지 차단]")
    rep = "\n".join(L); print(rep); tg(rep)

    if not execute:
        print("\n📋 드라이런 종료 — 주문 없음."); return 0
    if full:
        msg = "⛔ 전체 리밸런스(--full) 실주문은 T+2 결제 안전확보 전까지 차단. 신규자금 배분만 실행 가능."; print(msg); tg(msg); return 0

    # ─────────── 실행 안전 게이트 (매수 전용) ───────────
    if not is_rebalance_week():
        msg = "⛔ 실행거부: 리밸런스 주간 아님(1·4·7·10월 첫주)"; print(msg); tg(msg); return 0
    if deploy < 100_000:
        msg = f"⛔ 실행거부: 가용현금 {deploy/1e4:.0f}만 < 10만 — 배분할 신규자금 없음. (기존보유는 그대로)"; print(msg); tg(msg); return 0
    if already_done():
        msg = f"⛔ 실행거부: {quarter_tag()} 이미 완료(중복차단)"; print(msg); tg(msg); return 0
    if marker_started_uncommitted():
        msg = f"⛔ 실행거부: 직전 {quarter_tag()} 미완료 종료 — 수동 점검 필요."; print(msg); tg(msg); return 0
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
    if buy_val > max_buy:
        msg = f"⛔ 실행거부: 계획매수 {buy_val/1e4:,.0f}만 > 하드캡 {max_buy/1e4:.0f}만. --max 상향 필요."; print(msg); tg(msg); return 0
    if not _acquire_lock():
        msg = "⛔ 실행거부: 락파일(다른 실행 진행중). 오래된 락이면 auto_order.lock 수동삭제."; print(msg); tg(msg); return 0

    try:
        write_marker("STARTED")
        tg(f"🔴 실주문 시작(신규자금 배분) — 매수 {len(buys)}건, 캡 {max_buy/1e4:.0f}만. 매도 없음(기존보유 유지).")
        avail = deploy
        spent = 0; done_b = 0
        for s, q, nm, tlp in buys:
            if toss.has_investment_warning(s):
                tg(f"  ⚠️ 매수스킵(관리/경고): {nm}"); continue
            lp = toss.get_price(s)
            if not lp or lp <= 0 or abs(lp / tlp - 1) > PRICE_SANITY:
                tg(f"  ⚠️ 매수스킵(시세이상): {nm}"); continue
            unit = int(lp * (1 + BUY_BUF))
            qty = min(q, (max_buy - spent) // unit, (avail - spent) // unit)
            if qty <= 0:
                continue  # 캡/현금 소진 → 자연 종료
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
        tg(f"🔴 완료(신규자금 배분): 매수 {done_b}건 ≈{spent/1e4:,.0f}만. 계좌: {ftxt}. {quarter_tag()} DONE.")
    finally:
        try:
            os.remove(LOCK)
        except Exception:
            pass
    return 0


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
    mx = int(a[a.index('--max') + 1]) if '--max' in a else 500_000
    bg = int(a[a.index('--budget') + 1]) if '--budget' in a else None
    sys.exit(main('--execute' in a, mx, '--force' in a, '--probe' in a, bg, '--full' in a))
