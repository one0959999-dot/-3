import logging
import os
import json
import re
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

from bots.bot_manager import manager
from database import get_db_connection, verify_user, add_user, init_db, update_user_keys, init_default_ai_rules, set_user_initial_cash, get_news_api_keys, set_news_api_keys, get_sector_guide, set_sector_guide, load_chat_history, save_chat_history, clear_chat_history, set_user_core_stocks, set_user_satellite_stocks, set_us_core_stocks

# ── 통합 로깅 설정 (파일 + 콘솔) ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('lassi_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('lassi_bot')

app = Flask(__name__)

@app.errorhandler(500)
def internal_error(error):
    import traceback
    tb = traceback.format_exc()
    logger.error(f"500 Internal Server Error:\n{tb}")
    return f"""<pre style='font-family:monospace;padding:20px;background:#1e1e1e;color:#f88;'>
⚠️ 500 Internal Server Error

{tb}

— lassi_bot.log 파일에도 기록되었습니다 —
</pre>""", 500

# 보안 키 설정
_key_file = os.path.join(os.path.dirname(__file__), '.secret_key')
if os.path.exists(_key_file):
    with open(_key_file, 'rb') as f:
        app.secret_key = f.read()
else:
    app.secret_key = os.urandom(32)
    with open(_key_file, 'wb') as f:
        f.write(app.secret_key)

app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=7)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id, username, data):
        self.id = id
        self.username = username
        self.data = data

@login_manager.user_loader
def load_user(user_id):
    try:
        conn = get_db_connection()
        try:
            user_data = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        finally:
            conn.close()
        if user_data:
            return User(user_data['id'], user_data['username'], dict(user_data))
    except Exception as e:
        logger.error(f"load_user 오류 (user_id={user_id}): {e}", exc_info=True)
    return None

def get_current_bot():
    return manager.get_bot(current_user.id, current_user.data)

@app.route('/')
@login_required
def home():
    """홈 화면 — 총자산 추이 + KR/US 진입 버튼."""
    return render_template('home.html', user=current_user)

@app.route('/dashboard')
@login_required
def index():
    user_data = current_user.data
    ai_enabled = bool(user_data.get('claude_api_key'))
    manager.get_bot(current_user.id, user_data)
    return render_template('index.html', user=current_user, claude_enabled=ai_enabled)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user_data = verify_user(username, password)
        if user_data:
            user = User(user_data['id'], user_data['username'], user_data)
            login_user(user, remember=True)
            session.permanent = True
            # 최초 로그인 시 실전 검증 매매 원칙 자동 세팅
            try:
                init_default_ai_rules(user_data['id'])
            except Exception:
                pass
            return redirect(url_for('home'))
        flash('아이디 또는 비밀번호가 올바르지 않습니다.')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        # [BUG-N4] 아이디·비밀번호 최소 길이 검증 — 빈 값 또는 너무 짧은 비밀번호 차단
        if len(username) < 3:
            flash('아이디는 3자 이상이어야 합니다.')
            return render_template('register.html')
        if len(password) < 6:
            flash('비밀번호는 6자 이상이어야 합니다.')
            return render_template('register.html')
        if add_user(username, password):
            flash('회원가입이 완료되었습니다.')
            return redirect(url_for('login'))
        flash('이미 존재하는 아이디입니다.')
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# --- API Endpoints ---

@app.route('/api/status')
@login_required
def status():
    bot = get_current_bot()
    result = bot.get_status()

    # 반대 모드 봇의 실행 상태를 같이 전달 — UI 상태 배지용
    is_mock = bool(current_user.data.get('is_mock', 1))
    other_bot = manager.bots.get((current_user.id, not is_mock))
    result['other_mode_running'] = bool(other_bot and other_bot.is_running)
    result['other_mode_label'] = 'KR' if is_mock else 'US'

    return jsonify(result)

@app.route('/api/kis_balance')
@login_required
def kis_balance():
    """실시간 한국투자증권 계좌 잔고 조회 API"""
    try:
        bot = get_current_bot()
        if not bot or not bot.kis:
            return jsonify({"status": "error", "message": "API 설정이 필요합니다."})
        
        rt_prices = bot.live_prices if hasattr(bot, 'live_prices') else {}
            
        def patch_balance(balance_data):
            patched = dict(balance_data)
            
            patched_stocks = []
            recalc_total_value = 0.0
            recalc_total_purchase = 0.0
            
            for stock in patched.get('stocks', []):
                new_stock = dict(stock)
                ticker = new_stock.get('ticker')
                shares = float(new_stock.get('shares', 0))
                purchase_p = float(new_stock.get('purchase_price', 0))
                
                # 웹소켓 실시간 가격이 있으면 최우선 덮어쓰고, 없으면 증권사가 보낸 진짜 현재가 사용
                current_p = rt_prices.get(ticker, float(new_stock.get('current_price', 0)))
                
                new_stock['current_price'] = current_p
                new_stock['value'] = shares * current_p  
                
                if purchase_p > 0:
                    new_stock['profit_rt'] = ((current_p / purchase_p) - 1) * 100
                else:
                    new_stock['profit_rt'] = 0.0
                
                recalc_total_value += new_stock['value']
                recalc_total_purchase += (shares * purchase_p)
                patched_stocks.append(new_stock)
                
            patched['stocks'] = patched_stocks
            patched['total_value'] = recalc_total_value
            patched['total_purchase'] = recalc_total_purchase
            return patched

        if bot.cached_balance:
            return jsonify({"status": "success", "data": patch_balance(bot.cached_balance)})
        else:
            return jsonify({
                "status": "success", 
                "data": {
                    "total_cash": 0, 
                    "total_value": 0, 
                    "total_purchase": 0, 
                    "stocks": []
                }
            })
            
    except Exception as e:
        import traceback
        logger.error(f"kis_balance 동기화 에러: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"잔고 조회 중 오류: {str(e)}"})

@app.route('/api/test_order', methods=['POST'])
@login_required
def test_order():
    """KIS 해외주식 주문 API 연결 테스트 — US 봇으로 실행"""
    data = request.get_json() or {}
    ticker = data.get('ticker', '').strip()
    side   = data.get('side', 'BUY').upper()
    if not ticker or side not in ('BUY', 'SELL'):
        return jsonify({"status": "error", "message": "ticker와 side(BUY/SELL) 필요"}), 400
    try:
        use_real = data.get('use_real', False)
        is_mock = not use_real
        mode_label = "KR실전" if use_real else "US실전"

        target_bot = manager.bots.get((current_user.id, is_mock))
        if not target_bot:
            user_data = dict(current_user.data)
            user_data['is_mock'] = 1 if is_mock else 0
            target_bot = manager.get_bot(current_user.id, user_data)
        if not target_bot:
            return jsonify({"status": "error", "message": "봇 인스턴스를 찾을 수 없습니다."})

        # KR 봇: target_bot.kis  /  US 봇: target_bot.kis_overseas
        api = getattr(target_bot, 'kis_overseas', None) or getattr(target_bot, 'kis', None)
        if not api or not hasattr(api, 'buy_market_order'):
            return jsonify({"status": "error", "message": f"KIS API 미설정 — API 키를 확인하세요"})
        if side == 'BUY':
            ok = api.buy_market_order(ticker, 1)
        else:
            ok = api.sell_market_order(ticker, 1)
        if ok:
            return jsonify({"status": "success", "message": f"[{mode_label}] {ticker} 1주 {side} 주문 접수 완료"})
        return jsonify({"status": "error", "message": "주문 접수 실패 — 서버 로그 확인"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/api/toggle', methods=['POST'])
@login_required
def toggle_bot():
    bot = get_current_bot()
    if bot.is_running:
        bot.stop()
        return jsonify({"status": "stopped"})
    else:
        is_mock = current_user.data.get('is_mock', 1)
        cash_key = 'us_initial_cash' if is_mock else 'real_initial_cash'
        user_cash = current_user.data.get(cash_key, current_user.data.get('initial_cash', 10000000))
        
        success = bot.start(total_cash=user_cash)
        if success:
            return jsonify({"status": "started"})
        return jsonify({"status": "error", "message": "봇 시작 실패"}), 400

@app.route('/api/pnl')
@login_required
def get_pnl():
    bot = get_current_bot()
    return jsonify(bot.get_pnl_data())

# ── USD/KRW 환율 (60초 캐시) ───────────────────────────────────────────────
import time as _time_module
_fx_cache: dict      = {'data': None, 'ts': 0.0}
_futures_cache: dict = {'data': None, 'ts': 0.0}   # 선물 스냅샷 캐시 (300초)

@app.route('/api/exchange_rate')
@login_required
def get_exchange_rate():
    """USD/KRW 현재 환율 + 전일 대비 등락 반환 (yfinance, 60초 캐시)."""
    global _fx_cache
    now_ts = _time_module.time()
    if _fx_cache['data'] and now_ts - _fx_cache['ts'] < 60:
        return jsonify(_fx_cache['data'])
    try:
        import yfinance as yf
        ticker = yf.Ticker('USDKRW=X')
        hist = ticker.history(period='5d')
        if hist is None or hist.empty:
            return jsonify({'error': '데이터 없음'})
        hist = hist.dropna(subset=['Close'])
        curr = float(hist['Close'].iloc[-1])
        if len(hist) >= 2:
            prev  = float(hist['Close'].iloc[-2])
            chg   = round(curr - prev, 2)
            chg_p = round((curr - prev) / prev * 100, 2)
        else:
            chg, chg_p = 0.0, 0.0
        data = {'rate': round(curr, 2), 'change': chg, 'change_pct': chg_p}
        _fx_cache = {'data': data, 'ts': now_ts}
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/home/toggle', methods=['POST'])
@login_required
def home_toggle_bot():
    """홈 화면에서 KR/US 봇 개별 운영·정지."""
    data   = request.json or {}
    market = data.get('market', '').upper()   # 'KR' or 'US'
    if market not in ('KR', 'US'):
        return jsonify({"status": "error", "message": "market은 KR 또는 US이어야 합니다."}), 400

    is_mock = (market == 'US')   # KR=False, US=True
    bot = manager.bots.get((current_user.id, is_mock))
    if bot is None:
        user_data = dict(current_user.data)
        user_data['is_mock'] = 1 if is_mock else 0
        bot = manager.get_bot(current_user.id, user_data)
    if bot is None:
        return jsonify({"status": "error", "message": "봇 인스턴스를 생성할 수 없습니다."}), 500

    if bot.is_running:
        bot.stop()
        return jsonify({"status": "stopped", "market": market})
    else:
        cash_key = 'us_initial_cash' if is_mock else 'real_initial_cash'
        total_cash = current_user.data.get(cash_key, current_user.data.get('initial_cash', 10_000_000))
        success = bot.start(total_cash=float(total_cash))
        if success:
            return jsonify({"status": "started", "market": market})
        return jsonify({"status": "error", "message": f"{market} 봇 시작 실패 — API 키를 확인하세요."}), 400


@app.route('/api/home_summary')
@login_required
def home_summary():
    """홈 화면용 KR+US 합산 요약 데이터."""
    kr_bot = manager.bots.get((current_user.id, False))   # KR = is_mock=False
    us_bot = manager.bots.get((current_user.id, True))    # US = is_mock=True

    # ── 환율 (캐시 활용) ─────────────────────────────────────────────
    usd_krw = 1350.0
    try:
        if _fx_cache['data']:
            usd_krw = float(_fx_cache['data']['rate'])
    except Exception:
        pass

    # ── KST 오늘 날짜 ────────────────────────────────────────────────
    from datetime import timezone as _tz, timedelta as _tdd
    _kst_now = datetime.now(_tz(_tdd(hours=9)))
    today_str = _kst_now.strftime('%Y-%m-%d')

    def _kr_card() -> dict:
        if kr_bot is None:
            return {"market": "KR", "running": False, "total_krw": 0,
                    "pnl_today": 0, "pnl_pct": 0.0, "positions": 0}
        try:
            st = kr_bot.get_status()
            total_krw = float(st.get('mock_total_asset', 0))
            init_cash = float(st.get('initial_cash', 1))
            pnl_pct   = float(st.get('mock_pnl_rt', 0))
            pnl_today = float(kr_bot.daily_pnl.get(today_str, 0.0))
            positions = (
                sum(1 for c in st.get('cores', []) if float(c.get('shares', 0)) > 0)
                + sum(1 for p in st.get('satellites', []) if int(p.get('shares', 0)) > 0)
                + sum(1 for m in st.get('momentum_list', []) if m and int(m.get('shares', 0)) > 0)
            )
            return {"market": "KR", "running": bool(kr_bot.is_running),
                    "total_krw": round(total_krw), "pnl_today": round(pnl_today),
                    "pnl_pct": round(pnl_pct, 2), "positions": positions}
        except Exception as e:
            logger.warning(f"home_summary KR 오류: {e}")
            return {"market": "KR", "running": False, "total_krw": 0,
                    "pnl_today": 0, "pnl_pct": 0.0, "positions": 0}

    def _us_card() -> dict:
        if us_bot is None:
            return {"market": "US", "running": False, "total_krw": 0,
                    "pnl_today": 0, "pnl_pct": 0.0, "positions": 0}
        try:
            st = us_bot.get_status()
            total_krw = float(st.get('us_total_asset', 0))
            init_cash = float(st.get('initial_cash', 1))
            pnl_pct   = float(st.get('us_pnl_rt', 0))
            # US daily_pnl은 USD 단위 → KRW 환산
            pnl_today = float(us_bot.daily_pnl.get(today_str, 0.0)) * usd_krw
            positions = sum(1 for p in st.get('satellites', []) if int(p.get('shares', 0)) > 0)
            return {"market": "US", "running": bool(us_bot.is_running),
                    "total_krw": round(total_krw), "pnl_today": round(pnl_today),
                    "pnl_pct": round(pnl_pct, 2), "positions": positions}
        except Exception as e:
            logger.warning(f"home_summary US 오류: {e}")
            return {"market": "US", "running": False, "total_krw": 0,
                    "pnl_today": 0, "pnl_pct": 0.0, "positions": 0}

    kr_card = _kr_card()
    us_card = _us_card()

    # ── 합산 일별 PnL 집계 (2026-05-20 ~) ─────────────────────────────
    from collections import defaultdict
    START_DATE = '2026-05-20'

    combined: dict = defaultdict(float)   # {YYYY-MM-DD: KRW}
    if kr_bot:
        for d, v in kr_bot.daily_pnl.items():
            if d >= START_DATE:
                combined[d] += float(v)
    if us_bot:
        for d, v in us_bot.daily_pnl.items():
            if d >= START_DATE:
                combined[d] += float(v) * usd_krw

    all_days = sorted(combined.keys())

    # ── 누적 합산 헬퍼 ────────────────────────────────────────────────
    def _cumulative(days):
        c, result = 0.0, []
        for d in days:
            c += combined[d]
            result.append(round(c))
        return result

    # 일별 (최근 30일)
    daily_days   = all_days[-30:]
    # 일별 누적은 전체 기준으로 시작점 오프셋 맞춤
    offset = sum(combined[d] for d in all_days if d < (daily_days[0] if daily_days else '9999'))
    daily_cum_offset = round(offset)
    dc, daily_vals = daily_cum_offset, []
    for d in daily_days:
        dc += combined[d]
        daily_vals.append(round(dc))

    # 월별 집계 (YYYY-MM)
    monthly: dict = defaultdict(float)
    for d, v in combined.items():
        monthly[d[:7]] += v
    monthly_keys = sorted(monthly.keys())
    monthly_cum, monthly_vals = 0.0, []
    for mk in monthly_keys:
        monthly_cum += monthly[mk]
        monthly_vals.append(round(monthly_cum))

    # 연별 집계 (YYYY)
    yearly: dict = defaultdict(float)
    for d, v in combined.items():
        yearly[d[:4]] += v
    yearly_keys = sorted(yearly.keys())
    yearly_cum, yearly_vals = 0.0, []
    for yk in yearly_keys:
        yearly_cum += yearly[yk]
        yearly_vals.append(round(yearly_cum))

    # ── 기준일(2026-05-20) 대비 등락 ──────────────────────────────────
    total_pnl_from_start = sum(combined.values())
    combined_initial = 0.0
    try:
        from database import get_user_initial_cash
        kr_init = get_user_initial_cash(current_user.id, False) if kr_bot else 0
        us_init = get_user_initial_cash(current_user.id, True)  if us_bot else 0
        combined_initial = float(kr_init) + float(us_init) * usd_krw
    except Exception:
        pass
    pnl_from_start_pct = round(total_pnl_from_start / combined_initial * 100, 2) if combined_initial > 0 else 0.0

    return jsonify({
        "kr": kr_card,
        "us": us_card,
        "combined_total_krw": kr_card["total_krw"] + us_card["total_krw"],
        "pnl_from_start": round(total_pnl_from_start),
        "pnl_from_start_pct": pnl_from_start_pct,
        "chart": {
            "daily":   {"labels": daily_days,   "values": daily_vals},
            "monthly": {"labels": monthly_keys, "values": monthly_vals},
            "yearly":  {"labels": yearly_keys,  "values": yearly_vals},
        },
        "usd_krw": usd_krw,
    })


@app.route('/api/futures_snapshot')
@login_required
def get_futures_snapshot_api():
    """야간선물 스냅샷 — NQ=F / ES=F / EWY (5분 캐시)"""
    global _futures_cache
    now_ts = _time_module.time()
    if _futures_cache['data'] and now_ts - _futures_cache['ts'] < 300:
        return jsonify(_futures_cache['data'])
    try:
        from us_screener import get_futures_snapshot
        data = get_futures_snapshot()
        _futures_cache = {'data': data, 'ts': now_ts}
        return jsonify(data)
    except Exception as e:
        logger.warning(f"futures_snapshot API 오류: {e}")
        return jsonify({'error': str(e)})


@app.route('/api/reset_initial_cash', methods=['POST'])
@login_required
def reset_initial_cash():
    """투자 원금 기준값 수동 리셋 — 재시작 후 수익률 왜곡 시 사용."""
    data = request.json or {}
    # is_mock: 실전봇은 False(0), KR모의/US는 True(1)
    is_mock = bool(current_user.data.get('is_mock', 0))

    amount = float(data.get('amount', 0))
    if amount > 0:
        # 명시적 금액 → 직접 설정
        set_user_initial_cash(current_user.id, amount, is_mock)
        msg = f"투자 원금 기준값이 {amount:,.0f}원으로 재설정되었습니다."
    else:
        # amount 없음 → initial_capital_captured 리셋
        # 다음 _sync_internal_balances에서 KIS 잔고 기준으로 자동 재측정
        bot = manager.bots.get((current_user.id, is_mock))
        if bot:
            bot.initial_capital_captured = False
            msg = "원금 기준값 재측정 예약 완료 — 1분 내 KIS 잔고 기준으로 자동 갱신됩니다."
        else:
            # 봇 없으면 DB 기본값(10M) 복원
            set_user_initial_cash(current_user.id, 10_000_000, is_mock)
            msg = "봇 미가동 상태 — 원금을 기본값(10,000,000원)으로 리셋했습니다."

    return jsonify({"status": "ok", "message": msg})


@app.route('/api/set_core_dca', methods=['POST'])
@login_required
def set_core_dca():
    """코어 종목 DCA 적립식 모드 토글."""
    data   = request.json or {}
    ticker = data.get('ticker', '').strip()
    enable = bool(data.get('dca', False))
    if not ticker:
        return jsonify({"status": "error", "message": "ticker 누락"}), 400

    is_mock = bool(current_user.data.get('is_mock', 0))
    bot = manager.bots.get((current_user.id, is_mock))

    # 1) 메모리 봇의 코어 포지션에 즉시 반영
    changed_name = ticker
    if bot:
        with bot.lock:
            for core in bot.core_positions:
                if core.ticker == ticker:
                    core.dca_mode = enable
                    if enable:
                        core.dca_amount         = float(data.get('dca_amount', 0))
                        core.dca_interval_hours = int(data.get('dca_hours', 72))
                        core.dca_dip_pct        = float(data.get('dca_dip_pct', 3.0))
                        if not enable:
                            core.last_dca_time = 0.0  # 비활성화 시 타이머 초기화
                    changed_name = core.name
                    break

    # 2) DB의 user_core_stocks에도 dca 플래그 저장
    stocks = []
    if bot and hasattr(bot, 'user_core_stocks'):
        stocks = bot.user_core_stocks or []
    else:
        import json
        _row = get_db_connection().execute(
            "SELECT core_stocks FROM users WHERE id=?", (current_user.id,)
        ).fetchone()
        if _row and _row['core_stocks']:
            try: stocks = json.loads(_row['core_stocks'])
            except Exception: stocks = []
    for s in stocks:
        if s.get('ticker') == ticker:
            s['dca'] = enable
            if enable:
                if data.get('dca_amount'): s['dca_amount'] = float(data['dca_amount'])
                if data.get('dca_hours'):  s['dca_hours']  = int(data['dca_hours'])
                if data.get('dca_dip_pct'):s['dca_dip_pct']= float(data['dca_dip_pct'])
    set_user_core_stocks(current_user.id, stocks)

    action = "활성화" if enable else "비활성화"
    msg = f"{changed_name}({ticker}) 적립식 DCA {action} 완료"
    return jsonify({"status": "ok", "message": msg, "dca": enable})


@app.route('/api/daily_report')
@login_required
def get_daily_report():
    bot = get_current_bot()
    if not bot or not bot.claude:
        return jsonify({"status": "error", "message": "AI 설정이 필요합니다."})

    is_us = bool(current_user.data.get('is_mock', 1))

    if is_us:
        # ── US 봇: ET 기준 날짜 / 슬롯 (16:10 ET, 장 마감 10분 후) ────
        from datetime import timezone, timedelta as _td
        _et = timezone(_td(hours=-4))
        today_str = datetime.now(_et).strftime('%Y-%m-%d')
        weekday   = datetime.now(_et).weekday()

        if bot.daily_report and bot.daily_report.get('date') == today_str:
            return jsonify({"status": "success", "data": bot.daily_report})

        if weekday >= 5:
            if bot.daily_report:
                return jsonify({"status": "success", "data": bot.daily_report})
            return jsonify({"status": "success", "data": {
                "date": today_str,
                "report_markdown": "### 📢 알림\n\n금일은 미국장 휴무일(주말)입니다. 직전 거래일의 리포트가 없습니다."
            }})

        return jsonify({"status": "success", "data": {
            "date": today_str,
            "16:10": None,
            "report_markdown": "아직 오늘의 리포트가 생성되지 않았습니다. 16:10 ET (장 마감 후) 자동으로 발간됩니다."
        }})

    else:
        # ── KR 봇: KST 기준 날짜 / 슬롯 (15:40 KST, 장 마감 10분 후) ──
        # [BUG-FIX] datetime.today()는 시스템 로컬 시간 기준 → EC2(UTC) 서버에서 KST 날짜와 불일치.
        # bot.daily_report['date']는 _now_kst() 기준(KST)으로 저장되므로 비교도 KST 기준으로 통일.
        from datetime import timezone, timedelta as _td
        _kst = timezone(_td(hours=9))
        today_str = datetime.now(_kst).strftime('%Y-%m-%d')
        weekday   = datetime.now(_kst).weekday()

        if bot.daily_report and bot.daily_report.get('date') == today_str:
            return jsonify({"status": "success", "data": bot.daily_report})

        if weekday >= 5:
            if bot.daily_report:
                return jsonify({"status": "success", "data": bot.daily_report})
            return jsonify({"status": "success", "data": {
                "date": today_str,
                "report_markdown": "### 📢 알림\n\n금일은 장 휴무일(주말)입니다. 직전 거래일에 기록된 분석 리포트 장부가 비어있습니다."
            }})

        return jsonify({"status": "success", "data": {
            "date": today_str,
            "15:40": None,
            "report_markdown": "아직 오늘의 리포트가 생성되지 않았습니다. 15:40 KST (장 마감 후) 자동으로 발간됩니다."
        }})

@app.route('/api/ai_chat', methods=['POST'])
@login_required
def ai_chat():
    bot = get_current_bot()
    data = request.json or {}
    user_message = data.get('message', '').strip()
    if not bot or not bot.claude:
        return jsonify({"status": "error", "reply": "AI API 키를 등록해주세요."})

    stock_analysis_context = ""
    is_us_mode = bool(current_user.data.get('is_mock', 1))

    try:
        # ── KR 봇 전용 컨텍스트 (pykrx / fetch_ohlcv 는 KR 전용) ──────
        if not is_us_mode:
            from pykrx import stock as krx_stock
            from stock_screener import fetch_ohlcv, calc_rsi

            macro_lines = []
            for m_ticker, m_name in [("069500", "KOSPI 대용(KODEX 200)"), ("229200", "KOSDAQ 대용(KODEX 코스닥150)")]:
                m_df = fetch_ohlcv(m_ticker, days=40, kis=bot.kis)
                if not m_df.empty:
                    m_close = m_df['close']
                    m_price = m_close.iloc[-1]
                    m_sma20 = m_close.rolling(window=20, min_periods=1).mean().iloc[-1]
                    m_status = "20일선 위에 위치 (대세 상승/안정장)" if m_price >= m_sma20 else "20일선 아래 붕괴 (대세 하락장)"
                    macro_lines.append(f"- {m_name}: 현재가 {int(m_price):,}원 | 20일 이평선 {int(m_sma20):,}원 ({m_status})")

            if macro_lines:
                stock_analysis_context += "[🌍 실시간 시장 지수 및 20일선 트렌드 파악]\n" + "\n".join(macro_lines) + "\n\n"

        target_tickers = []

        for core in bot.core_positions:
            if core.name in user_message: target_tickers.append((core.ticker, core.name))
        for ticker, pos in bot.satellite_positions.items():
            if pos.name in user_message: target_tickers.append((ticker, pos.name))
            
        if not target_tickers:
            for core in bot.core_positions:
                target_tickers.append((core.ticker, core.name))
            for ticker, pos in bot.satellite_positions.items():
                target_tickers.append((ticker, pos.name))

        target_tickers = list(dict.fromkeys(target_tickers))[:5]

        if target_tickers and not is_us_mode:
            # KR 봇 전용: pykrx OHLCV 기반 종목 분석 컨텍스트
            context_lines = ["[📈 회원님이 궁금해하시는 종목의 실시간 데이터 분석 장부]"]
            for ticker, name in target_tickers:
                try:
                    ohlcv_df = fetch_ohlcv(ticker, days=130, kis=bot.kis)
                    today_str = datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')
                    cache_key = f"{ticker}_{today_str}"
                    financial_data = getattr(bot, 'fundamental_cache', {}).get(cache_key, "PER: 10.0배, PBR: 1.0배 (실시간 추정치)")

                    if not ohlcv_df.empty:
                        close_series = ohlcv_df['close']
                        vol_series = ohlcv_df['volume']

                        rsi_14 = calc_rsi(close_series, 14).iloc[-1] if not close_series.empty else 50
                        sma_120 = close_series.rolling(window=120, min_periods=1).mean().iloc[-1]

                        current_price = bot.live_prices.get(ticker) or close_series.iloc[-1]
                        status_120 = "120일선 위에 안착함 (상승 추세 진행중)" if current_price >= sma_120 else "120일선 아래에 위치함 (역배열 하락 추세)"

                        vol_today = vol_series.iloc[-1] if not vol_series.empty else 0
                        vol_20_avg = vol_series.rolling(window=20, min_periods=1).mean().iloc[-2] if len(vol_series) > 1 else 1
                        vol_ratio = (vol_today / vol_20_avg * 100) if vol_20_avg > 0 else 100

                        context_lines.append(
                            f"- {name}({ticker}): 현재 주가 {int(current_price):,}원 | 120일 이동평균선 위치: {int(sma_120):,}원 ({status_120}) | "
                            f"실시간 RSI(14) 지표: {rsi_14:.1f} | 마감 거래량: 평소 대비 {vol_ratio:.0f}% 수준 | 가치 지표: {financial_data}"
                        )
                    else:
                        current_price = bot.live_prices.get(ticker, 0)
                        context_lines.append(
                            f"- {name}({ticker}): 현재 주가 {int(current_price):,}원 | 세부 차트 조회 지연 중이나 강력한 주도주 모멘텀이 확인됨 | 가치 지표: {financial_data}"
                        )
                except Exception as ex:
                    print(f"⚠️ {name} 데이터 바인딩 중 소규모 에러: {ex}")

            if len(context_lines) > 1:
                stock_analysis_context += "\n".join(context_lines)
        
        # ── 봇 명령 적용 가능 리마인더 (시스템 프롬프트 보강) ───────────
        stock_analysis_context += (
            "\n[리마인더] 사용자가 봇 설정 변경을 요청하면 "
            "[BOT_COMMAND]{\"action\":\"update_sector_guide\",\"content\":\"...\"}[/BOT_COMMAND] "
            "블록을 답변 마지막에 포함하세요. 당신은 이 명령을 통해 봇 설정을 직접 바꿀 수 있습니다.\n"
        )

        if stock_analysis_context:
            stock_analysis_context += "\n\n[🚨 다정한 AI 비서를 위한 특별 지침]\n"
            stock_analysis_context += "당신은 회원님의 소중한 자산을 지켜주는 다정다감하고 영리한 최고의 투자 파트너입니다. "
            stock_analysis_context += "수급이나 ROE 데이터가 완벽하게 주어지지 않았다고 해서 딱딱하게 평가를 거부하면 회원님이 속상해하십니다. "
            stock_analysis_context += "현재 제공된 '20일선 트렌드', '120일선 추세', 'RSI', '거래량 비율', 'PER/PBR' 데이터만으로도 당신의 천재적인 재능을 발휘하여 "
            stock_analysis_context += "현 상황이 절대 매뉴얼에 잘 부합하는지 친절하고 상냥하며 부드러운 말투로 조언해 주십시오. "
            stock_analysis_context += "답변 첫 줄에 대문자로 [CONFIRM/REJECT/HOLD/SELL]을 적을 때도 뒤에 다정한 코멘트를 곁들여 주시고, "
            stock_analysis_context += "이유를 설명할 때도 부드러운 경어체(~요, ~습니다)를 사용해 따뜻하게 다독여 주시기 바랍니다."

    except Exception as e:
        print(f"⚠️ [종목 데이터 가공 오류] : {e}")

    try:
        current_status = bot.get_status()
        bot_logs = current_status.get('logs', [])
        if bot_logs:
            stock_analysis_context += "\n\n[📝 백엔드 자동 매매 시스템 최근 실행 로그 (필독)]\n"
            for log in bot_logs[-15:]:
                stock_analysis_context += f"- [{log['time']}] {log['message']}\n"
            stock_analysis_context += "위 로그를 바탕으로 현재 매매 봇이 백엔드에서 무엇을 하고 있는지 파악하여 답변에 자연스럽게 녹여주세요.\n"
    except Exception as log_e:
        print(f"⚠️ [로그 데이터 가공 오류] : {log_e}")

    # C-02: bot.claude를 지역 변수로 캡처하여 스레드 교체 타이밍 race condition 방지
    claude_client = bot.claude
    if not claude_client:
        return jsonify({"status": "error", "reply": "⚠️ Claude API 키가 설정되지 않았습니다."})

    # ── 채팅 히스토리 DB 복원 (세션이 끊겨도 대화 기억) ──────────────────
    is_mock_flag = int(current_user.data.get('is_mock', 1))
    saved_history = load_chat_history(current_user.id, is_mock_flag)
    if saved_history:
        claude_client._conversation_history = saved_history

    reply = claude_client.chat(
        user_message,
        portfolio_context=bot.get_status(),
        stock_analysis_context=stock_analysis_context
    )

    # 응답 후 최신 히스토리를 DB에 저장
    save_chat_history(current_user.id, is_mock_flag, claude_client._conversation_history)

    # ── 봇 명령 파싱 및 실행 ────────────────────────────────────────────
    # AI가 [BOT_COMMAND]{...}[/BOT_COMMAND] 블록을 포함하면 즉시 실행
    applied_commands = []
    _cmd_pattern = r'\[BOT_COMMAND\](.*?)\[/BOT_COMMAND\]'
    for _match in re.findall(_cmd_pattern, reply, re.DOTALL):
        try:
            cmd = json.loads(_match.strip())
            if cmd.get('action') == 'update_sector_guide':
                new_guide = (cmd.get('content') or '').strip()
                if new_guide:
                    set_sector_guide(current_user.id, new_guide)
                    for _is_mock_v in (True, False):
                        _b = manager.bots.get((current_user.id, _is_mock_v))
                        if _b:
                            _b.sector_guide = new_guide
                    applied_commands.append("✅ 봇 전략 가이드가 업데이트되었습니다.")
                    logging.getLogger('lassi_bot').info(
                        f"[AI봇명령] user={current_user.id} sector_guide 업데이트 ({len(new_guide)}자)"
                    )

            elif cmd.get('action') == 'update_core_stocks':
                # AI가 코어 종목 교체 명령 — KR: [{"ticker":"005490","name":"POSCO홀딩스"}]
                #                              US: [{"ticker":"NVDA","name":"Nvidia"}]
                new_stocks = cmd.get('stocks', [])
                target = cmd.get('market', 'KR').upper()  # 'KR' or 'US'
                if isinstance(new_stocks, list) and new_stocks:
                    valid = [s for s in new_stocks if s.get('ticker') and s.get('name')]
                    if valid:
                        if target == 'US':
                            set_us_core_stocks(current_user.id, valid)
                            _b = manager.bots.get((current_user.id, True))  # US = is_mock=True
                            if _b and hasattr(_b, 'user_core_stocks'):
                                _b.user_core_stocks = valid
                                _b._inject_user_cores()
                        else:
                            set_user_core_stocks(current_user.id, valid)
                            _b = manager.bots.get((current_user.id, False))  # KR = is_mock=False
                            if _b and hasattr(_b, '_init_dummy_cores'):
                                _b.user_core_stocks = valid
                                _u = valid[0] if valid else {}
                                _b.core_ticker = _u.get('ticker', '')
                                _b.core_name   = _u.get('name', '')
                                _b._init_dummy_cores()
                        names = ", ".join(s['name'] for s in valid)
                        applied_commands.append(f"✅ [{target}] 코어 종목이 [{names}]로 업데이트되었습니다.")
                        logging.getLogger('lassi_bot').info(
                            f"[AI봇명령] user={current_user.id} {target} core_stocks 교체: {valid}"
                        )

            elif cmd.get('action') == 'update_satellite_stocks':
                # AI가 위성 종목 교체 명령 — KR: [{"ticker":"005930","name":"삼성전자"}]
                #                              US: [{"ticker":"TSLA","name":"Tesla"}]
                new_stocks = cmd.get('stocks', [])
                target = cmd.get('market', 'KR').upper()
                if isinstance(new_stocks, list) and new_stocks:
                    valid = [s for s in new_stocks if s.get('ticker') and s.get('name')]
                    if valid:
                        is_us = (target == 'US')
                        set_user_satellite_stocks(current_user.id, valid, is_us=is_us)
                        _is_mock_v = True if is_us else False
                        _b = manager.bots.get((current_user.id, _is_mock_v))
                        if _b and hasattr(_b, 'user_satellite_stocks'):
                            _b.user_satellite_stocks = valid
                            _b._inject_user_satellites()
                        names = ", ".join(s['name'] for s in valid)
                        applied_commands.append(f"✅ [{target}] 위성 종목이 [{names}]로 업데이트되었습니다.")
                        logging.getLogger('lassi_bot').info(
                            f"[AI봇명령] user={current_user.id} {target} satellite_stocks 교체: {valid}"
                        )

        except Exception as _cmd_err:
            logging.getLogger('lassi_bot').warning(f"[AI봇명령] 파싱 오류: {_cmd_err}")

    # 명령 블록을 최종 답변에서 제거
    clean_reply = re.sub(_cmd_pattern, '', reply, flags=re.DOTALL).strip()

    return jsonify({"status": "success", "reply": clean_reply, "applied_commands": applied_commands})


@app.route('/api/ai_chat/reset', methods=['POST'])
@login_required
def ai_chat_reset():
    """AI 채팅 히스토리 초기화 — 메모리 + DB 모두 삭제."""
    bot = get_current_bot()
    is_mock_flag = int(current_user.data.get('is_mock', 1))
    # 메모리 히스토리 초기화
    if bot and bot.claude:
        bot.claude.reset_chat()
    # DB 히스토리 삭제
    clear_chat_history(current_user.id, is_mock_flag)
    return jsonify({"status": "success", "message": "대화 기록이 초기화되었습니다."})


@app.route('/api/settings/mode', methods=['POST'])
@login_required
def set_mode():
    """실전/모의 투자 모드 전환 API — 화면만 전환, 각 봇의 실행 상태는 독립 유지"""
    data = request.json or {}
    try:
        is_mock = int(data.get('is_mock', 1))
        if is_mock not in (0, 1):  # [BUG-M6] 0(실전)/1(모의) 외 비정상값 차단
            return jsonify({"status": "error", "message": "is_mock은 0 또는 1이어야 합니다."}), 400
    except (TypeError, ValueError):
        is_mock = 1

    # DB 업데이트 (화면 전환만, 봇 실행 상태 건드리지 않음)
    conn = get_db_connection()
    try:
        conn.execute('UPDATE users SET is_mock = ? WHERE id = ?', (is_mock, current_user.id))
        conn.commit()
        user_data = conn.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
    finally:
        conn.close()

    for k, v in dict(user_data).items():
        current_user.data[k] = v

    # 새 모드 봇 인스턴스만 미리 생성(실행 X) — 이후 toggle로 개별 제어
    manager.get_bot(current_user.id, current_user.data)

    us_bot  = manager.bots.get((current_user.id, True))
    real_bot = manager.bots.get((current_user.id, False))
    logger.info(
        f"[mode switch] user={current_user.id} 화면=({'US' if is_mock else 'KR'}) "
        f"| US봇={'실행중' if us_bot and us_bot.is_running else '정지'} "
        f"| KR봇={'실행중' if real_bot and real_bot.is_running else '정지'}"
    )

    return jsonify({"status": "success", "is_mock": is_mock})

@app.route('/api/settings/satellites', methods=['POST'])
@login_required
def set_satellites_count():
    """위성 종목 개수 변경 설정을 저장합니다."""
    data = request.json or {}
    try:
        count = int(data.get('count', 5))
        count = max(1, min(10, count))  # [BUG-M6] 1~10 범위 강제 — 0 이하 입력 시 ZeroDivisionError 방지
    except (TypeError, ValueError):
        count = 5

    bot = get_current_bot()
    if bot:
        bot.num_satellites = count
        bot._save_state()
        return jsonify({"status": "success", "num_satellites": count})
    return jsonify({"status": "error", "message": "봇을 활성화할 수 없습니다."}), 400

@app.route('/api/settings/news_keys', methods=['POST'])
@login_required
def set_news_keys():
    """DART + Naver 뉴스 API 키 저장 및 봇 즉시 반영."""
    data = request.json or {}
    dart_key   = (data.get('dart_api_key') or '').strip()
    naver_id   = (data.get('naver_client_id') or '').strip()
    naver_sec  = (data.get('naver_client_secret') or '').strip()
    set_news_api_keys(current_user.id, dart_key, naver_id, naver_sec)
    # 실행 중인 봇에 즉시 적용
    for is_mock in (True, False):
        bot = manager.bots.get((current_user.id, is_mock))
        if bot:
            bot.reload_news_monitor(dart_key, naver_id, naver_sec)
    return jsonify({"status": "success"})

@app.route('/api/settings/news_keys', methods=['GET'])
@login_required
def get_news_keys():
    """저장된 뉴스 API 키 조회 (Secret은 마스킹)."""
    keys = get_news_api_keys(current_user.id)
    return jsonify({
        "dart_api_key":        keys['dart_api_key'][:8] + '****' if keys['dart_api_key'] else '',
        "naver_client_id":     keys['naver_client_id'][:4] + '****' if keys['naver_client_id'] else '',  # [BUG-N5] 마스킹 추가
        "naver_client_secret": keys['naver_client_secret'][:4] + '****' if keys['naver_client_secret'] else '',
    })

@app.route('/api/settings/sector_guide', methods=['GET'])
@login_required
def get_sector_guide_route():
    """섹터 가이드 조회."""
    return jsonify({"sector_guide": get_sector_guide(current_user.id)})

@app.route('/api/settings/sector_guide', methods=['POST'])
@login_required
def set_sector_guide_route():
    """섹터 가이드 저장 + 실행 중인 봇에 즉시 반영."""
    data = request.json or {}
    guide = (data.get('sector_guide') or '').strip()
    set_sector_guide(current_user.id, guide)
    # 실행 중인 봇에 즉시 반영
    for is_mock in (True, False):
        bot = manager.bots.get((current_user.id, is_mock))
        if bot:
            bot.sector_guide = guide
    return jsonify({"status": "success"})

@app.route('/api/settings/keys', methods=['POST'])
@login_required
def set_keys():
    data = request.json or {}
    update_data = {
        'real_app_key': data.get('real_app_key'),
        'real_app_secret': data.get('real_app_secret'),
        'real_account_no': data.get('real_account_no'),
        'us_app_key': data.get('us_app_key'),
        'us_app_secret': data.get('us_app_secret'),
        'us_account_no': data.get('us_account_no'),
        'telegram_token': data.get('telegram_token'),
        'telegram_chat_id': data.get('telegram_chat_id'),
        'claude_api_key': data.get('claude_api_key'),
        'core_stocks': data.get('core_stocks'),
        'is_mock': int(data.get('is_mock', 1)),
        'initial_cash': float(data.get('initial_cash', 10000000))
    }

    update_user_keys(current_user.id, update_data)

    for k, v in update_data.items():
        current_user.data[k] = v

    is_mock = update_data['is_mock']
    prefix = 'us_' if is_mock else 'real_'
    
    bot = get_current_bot()
    if bot:
        bot.reload_api_keys(
            kis_config={
                "app_key": data.get(f'{prefix}app_key'),
                "app_secret": data.get(f'{prefix}app_secret'),
                "account_no": data.get(f'{prefix}account_no')
            },
            telegram_config={
                "token": data.get('telegram_token'),
                "chat_id": data.get('telegram_chat_id')
            },
            claude_config={},
            core_stocks=data.get('core_stocks')
        )

    other_mock = not bool(is_mock)
    other_prefix = 'us_' if other_mock else 'real_'
    other_bot = manager.bots.get((current_user.id, other_mock))
    if other_bot:
        other_bot.reload_api_keys(
            kis_config={
                "app_key": data.get(f'{other_prefix}app_key'),
                "app_secret": data.get(f'{other_prefix}app_secret'),
                "account_no": data.get(f'{other_prefix}account_no')
            },
            telegram_config={
                "token": data.get('telegram_token'),
                "chat_id": data.get('telegram_chat_id')
            },
            claude_config={},
            core_stocks=data.get('core_stocks')
        )

    return jsonify({"status": "success"})

@app.route('/api/search/stock')
@login_required
def search_stock():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"results": []})

    # 1순위: 네이버 Finance 자동완성 (키 불필요, 빠름)
    try:
        import requests as _req
        res = _req.get(
            "https://ac.finance.naver.com/ac",
            params={"q": query, "r_format": "json", "r_enc": "utf-8", "r_unicode": "1", "t_kwd": "expr"},
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"},
            timeout=4
        )
        if res.status_code == 200:
            data = res.json()
            results = []
            for group in data.get("items", []):
                if not isinstance(group, list):
                    continue
                for item in group:
                    if len(item) >= 2 and isinstance(item[1], str) and item[1].isdigit() and len(item[1]) == 6:
                        results.append({"ticker": item[1], "name": item[0]})
            if results:
                return jsonify({"results": results[:15]})
    except Exception as e:
        logger.warning(f"네이버 종목검색 실패: {e}")

    # 2순위: KIS 실전 API 검색 (실전 키가 있을 때)
    try:
        bot = get_current_bot()
        if bot and bot.kis:
            results = bot.kis.search_stock_name(query)
            if results:
                return jsonify({"results": results})
    except Exception as e:
        logger.warning(f"KIS 종목검색 실패: {e}")

    # 3순위: pykrx로 섹터 종목 풀에서 이름 매칭
    try:
        from pykrx import stock as krx
        from stock_screener import SECTOR_STOCKS
        all_tickers = list(dict.fromkeys(t for tickers in SECTOR_STOCKS.values() for t in tickers))
        results = []
        for ticker in all_tickers:
            name = krx.get_market_ticker_name(ticker)
            if name and query in name:
                results.append({"ticker": ticker, "name": name})
        if results:
            return jsonify({"results": results[:15]})
    except Exception as e:
        logger.warning(f"pykrx 종목검색 실패: {e}")

    return jsonify({"results": []})

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)