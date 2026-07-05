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
OBS_MARKER = P('first_fill_verified_us.txt')  # 최초 US 실주문 통화검증 완료 마커
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


def _write_obs():
    tmp = OBS_MARKER + '.tmp'
    open(tmp, 'w').write(datetime.datetime.now().isoformat())
    os.replace(tmp, OBS_MARKER)


def _observe_first_us_fill(toss, uid, price):
    """최초 US 실주문 통화검증(관찰체결): 소액 SPY 매수 → cash_usd 감소분이 주문액(USD)과
    일치하는지 확인. 토스가 orderAmount를 KRW로 오결제하면 cash_usd가 거의 안 줄어(₩10≈$0.008)
    감지된다. 반환 (통과여부, 관찰소요 USD)."""
    tg("🔬 US 최초 실주문 통화검증: SPY 소액매수로 'USD로 결제되나' 확인...")
    pre = read_us(toss)
    if pre is None:
        tg("⛔ US 통화검증 중단: 계좌조회 실패"); return False, 0.0
    pre_cash, pre_spy = pre[1], pre[0].get(SPY, 0)
    test_amt = round(min(10.0, pre_cash - CASH_BUFFER), 2)
    if test_amt < 5:
        tg(f"⛔ US 통화검증 불가: 테스트 가용 ${test_amt:.2f} < $5 — 환전 확인"); return False, 0.0
    ok = toss.buy_fractional_order(SPY, test_amt)
    if not ok:
        tg("⛔ US 통화검증 실패: 주문접수 거부 — 중단"); return False, 0.0
    _log(uid, SPY, True, test_amt, price)
    time.sleep(8)
    post = read_us(toss)
    if post is None:
        tg("⛔ US 통화검증 미확인: 매수후 계좌조회 실패 — 오늘 중단(내일 재검증)"); return False, test_amt
    usd_spent = pre_cash - post[1]
    spy_gained = post[0].get(SPY, 0) - pre_spy
    if spy_gained <= 0:
        tg("⛔ US 통화검증 미확인: SPY 미증가(체결 안 됨) — 중단"); return False, test_amt
    if usd_spent < test_amt * 0.5 or usd_spent > test_amt * 1.5:
        tg(f"🚨 US 통화검증 실패: SPY +{spy_gained:.4f}주인데 USD 감소 ${usd_spent:.2f} ≠ 주문 ${test_amt:.2f} "
           "— 결제통화 이상(KRW 오결제 의심). 중단·수동확인 필요"); return False, test_amt
    _write_obs()
    tg(f"✅ US 통화검증 통과: SPY +{spy_gained:.4f}주, USD -${usd_spent:.2f}(≈주문 ${test_amt:.2f}) — 정상 USD 결제 확인")
    return True, usd_spent


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
    try:
        from KR.journal import record
        record('plan', 'US', {'cash_usd': cash, 'spy_held': spy_held, 'spy_price': price, 'deploy_usd': deploy})
    except Exception:
        pass

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
    amount = round(min(deploy, max_usd), 2)
    if not acquire_lock():
        msg = "⛔ 실행거부: 락파일(다른 실행중)"; print(msg); tg(msg); return 0
    try:
        open(MARKER + '.tmp', 'w').write(today_tag()); os.replace(MARKER + '.tmp', MARKER)
        # ⑨ 최초 US 실주문 통화검증(관찰체결): USD로 결제되는지 소액 확인 후 본 매수
        if not os.path.exists(OBS_MARKER):
            okobs, obs_spent = _observe_first_us_fill(toss, uid, price)
            if not okobs:
                return 0  # 통화 미검증 → 오늘 중단(마커=중복차단, 내일 재시도)
            amount = round(min(deploy - obs_spent, max_usd - obs_spent), 2)
            if amount < 10:
                final = read_us(toss)
                ftxt = f"현금 ${final[1]:,.2f}, SPY {final[0].get(SPY,0)}주" if final else "조회실패"
                tg(f"🇺🇸 US 통화검증 완료(소액 매수). 잔여 ${amount:,.2f}<$10 — 다음 실행에 본 매수. 계좌: {ftxt}")
                return 0
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
