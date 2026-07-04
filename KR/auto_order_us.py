"""US 자동주문 v1.0 — SPY 보유 (검증: US는 종목픽 4.5% << SPY 13.7%, 지수가 정답).

★US 전략 = SPY(S&P500) 단순 보유. 이유: 미국은 재무필터 데이터 없어 종목픽이 실패
  (전체 3790종목 검증 CAGR 4.5%/MDD-49% vs SPY 13.7%). S&P500 자체가 우량 분산 바스켓.
설계: KR과 동일 안전뼈대 — 기본 드라이런, 신규 USD만 매수(기존 불간섭), 하드캡, 락, 정규장.

실행:
  python KR/auto_order_us.py --probe               # US계좌 조회
  python KR/auto_order_us.py                        # 드라이런(주문0): 가용 USD로 SPY 계획
  python KR/auto_order_us.py --execute --max 500    # 실주문(미국장·캡 $500)
"""
import sys, os, sqlite3, time, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import warnings
warnings.filterwarnings('ignore')
from KR.live_v1 import tg

P = lambda f: os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', f)
LOCK = P('auto_order_us.lock'); MARKER = P('auto_order_us_done.txt')
SPY = 'SPY'; CASH_BUFFER = 5.0  # 최소 잔여 $


def load_toss():
    c = sqlite3.connect(P('lassi.db')); c.row_factory = sqlite3.Row
    u = c.execute("SELECT id, toss_client_id, toss_client_secret, toss_account_seq FROM users "
                  "WHERE toss_client_id IS NOT NULL AND toss_client_id!='' LIMIT 1").fetchone()
    c.close()
    if not u:
        return None, None
    from base.toss_api import TossInvestApi
    return TossInvestApi(u['toss_client_id'], u['toss_client_secret'], u['toss_account_seq'] or ''), u['id']


def read_us(toss):
    """(holdings{sym:qty}, cash_usd) 또는 None(실패)."""
    b = toss.get_balance()
    if not b:
        return None
    holdings = {s['ticker']: float(s.get('shares', 0) or 0) for s in b.get('stocks', []) if float(s.get('shares', 0) or 0) > 0}
    return holdings, float(b.get('cash_usd', 0) or 0)


def today_tag():
    return datetime.date.today().isoformat()


def already_done_today():
    try:
        return open(MARKER).read().strip() == today_tag()
    except Exception:
        return False


def acquire_lock():
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
        return True
    except FileExistsError:
        try:
            if (time.time() - os.path.getmtime(LOCK)) > 3600:
                os.remove(LOCK)
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY); os.write(fd, str(os.getpid()).encode()); os.close(fd)
                return True
        except Exception:
            pass
        return False


def main(execute=False, max_usd=500.0, probe=False):
    toss, uid = load_toss()
    if toss is None:
        print("⛔ Toss 자격증명 없음"); return 1
    acct = read_us(toss)
    if acct is None:
        msg = "⛔ US auto_order 중단: 계좌조회 실패 — 아무것도 안 함"; print(msg); tg(msg); return 1
    holdings, cash = acct
    spy_held = holdings.get(SPY, 0)
    print(f"[US계좌] 현금 ${cash:,.2f}, SPY {spy_held}주, 보유 {len(holdings)}종목")
    if probe:
        tg(f"🔍 US계좌: 현금 ${cash:,.2f}, SPY {spy_held}주\n" +
           "\n".join(f"  {s} {q}" for s, q in holdings.items()))
        return 0

    price = toss.get_price(SPY)
    deploy = max(0.0, cash - CASH_BUFFER)
    head = '🔴실행' if execute else '📋드라이런(주문0)'
    L = [f"🇺🇸 US 자동주문 {head} — {today_tag()} (전략=SPY 보유)"]
    L.append(f"가용 USD ${deploy:,.2f} → SPY ${'?' if not price else f'{price:,.2f}'} 매수 (기존보유 불간섭, 캡 ${max_usd:,.0f})")
    rep = "\n".join(L); print(rep); tg(rep)

    if not execute:
        print("📋 드라이런 종료 — 주문 없음."); return 0
    # ── 실행 게이트 ──
    if deploy < 10:
        msg = f"⛔ 실행거부: 가용 USD ${deploy:,.2f} < $10 — 배분할 신규자금 없음"; print(msg); tg(msg); return 0
    if already_done_today():
        msg = "⛔ 실행거부: 오늘 이미 실행됨(중복차단)"; print(msg); tg(msg); return 0
    if not toss.is_us_market_open():
        msg = "⛔ 실행거부: 미국 정규장 아님"; print(msg); tg(msg); return 0
    if not price or price <= 0:
        msg = "⛔ 실행거부: SPY 시세조회 실패"; print(msg); tg(msg); return 0
    try:
        oo = toss.get_open_orders()
    except Exception:
        oo = None
    if oo is None:
        msg = "⛔ 실행거부: 미체결 조회 실패 — 안전중단"; print(msg); tg(msg); return 0
    if oo:
        msg = "⛔ 실행거부: 미체결 주문 존재 — 충돌방지"; print(msg); tg(msg); return 0
    amount = min(deploy, max_usd)
    if not acquire_lock():
        msg = "⛔ 실행거부: 락파일(다른 실행중)"; print(msg); tg(msg); return 0
    try:
        open(MARKER + '.tmp', 'w').write(today_tag()); os.replace(MARKER + '.tmp', MARKER)
        tg(f"🔴 US 실주문: SPY ${amount:,.2f} 소수점 매수 (캡 ${max_usd:,.0f})")
        ok = toss.buy_fractional_order(SPY, amount)
        _log(uid, SPY, ok, amount, price)
        final = read_us(toss)
        ftxt = f"현금 ${final[1]:,.2f}, SPY {final[0].get(SPY,0)}주" if final else "조회실패"
        tg(f"🇺🇸 {'✅ 완료' if ok else '❌ 실패'}: SPY ${amount:,.2f} 매수. 계좌: {ftxt}")
    finally:
        try:
            os.remove(LOCK)
        except Exception:
            pass
    return 0


def _log(uid, ticker, ok, amount, price):
    if not ok:
        return
    try:
        from base.database import log_trade_journal
        log_trade_journal(uid, ticker, 'SPY', 'BUY', price or 0, strategy='algo_v1_us', ai_reason='',
                          shares=round(amount / price, 4) if price else 0, mode='US')
    except Exception:
        pass


if __name__ == '__main__':
    a = sys.argv[1:]
    mx = float(a[a.index('--max') + 1]) if '--max' in a else 500.0
    sys.exit(main('--execute' in a, mx, '--probe' in a))
