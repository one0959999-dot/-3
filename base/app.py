import sys
import os
# 프로젝트 루트(lassi_bot/)를 sys.path에 추가 — base/ 하위에서 실행 시 KR/US/ai/ 모듈 탐색 가능
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import json
import re
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

from base.bot_manager import manager
from base.database import get_db_connection, verify_user, add_user, init_db, update_user_keys, init_default_ai_rules, set_user_initial_cash, get_news_api_keys, set_news_api_keys, get_sector_guide, set_sector_guide, load_chat_history, save_chat_history, clear_chat_history, set_user_core_stocks, set_user_satellite_stocks, set_us_core_stocks

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

import pathlib, time as _time
_BASE_DIR = pathlib.Path(__file__).parent.parent  # lassi_bot/
_STATIC_VER = str(int(_time.time()))  # 서버 시작 시 고정 — 재시작마다 갱신

app = Flask(
    __name__,
    template_folder=str(_BASE_DIR / 'base' / 'templates'),
    static_folder=str(_BASE_DIR / 'base' / 'static'),
)

# ── KR / US Blueprint ──────────────────────────────────────────────────────
from flask import Blueprint

kr_bp = Blueprint(
    'kr', __name__,
    template_folder=str(_BASE_DIR / 'KR' / 'templates'),
    static_folder=str(_BASE_DIR / 'KR' / 'static'),
    static_url_path='/static/KR',
)

us_bp = Blueprint(
    'us', __name__,
    template_folder=str(_BASE_DIR / 'US' / 'templates'),
    static_folder=str(_BASE_DIR / 'US' / 'static'),
    static_url_path='/static/US',
)

# register_blueprint는 라우트 정의 후 파일 하단에서 호출

# favicon 404 에러 방지
from flask import send_from_directory
@app.route('/favicon.ico')
def favicon():
    return '', 204

# ── pykrx 종목명 캐시 (당일 1회 전체 로드 → 이후 검색 O(n)) ─────────────
_pykrx_name_cache: dict[str, str] = {}   # {ticker: name}
_pykrx_cache_date: str = ""
_pykrx_cache_lock = threading.Lock()


def _load_krx_stock_list() -> dict[str, str]:
    """KRX KIND 포털에서 KOSPI+KOSDAQ 전체 종목 로드 → {ticker: name}.
    인증 불필요, EC2에서도 동작. 실패 시 pykrx SECTOR_STOCKS fallback."""
    import requests as _req
    from bs4 import BeautifulSoup
    cache: dict[str, str] = {}
    try:
        res = _req.get(
            "https://kind.krx.co.kr/corpgeneral/corpList.do",
            params={"method": "download", "searchType": "13"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if res.status_code == 200:
            soup = BeautifulSoup(res.content, "html.parser", from_encoding="euc-kr")
            for row in soup.find_all("tr"):
                cols = row.find_all("td")
                if len(cols) >= 3:
                    name   = cols[0].text.strip()
                    ticker = cols[2].text.strip()
                    if ticker.isdigit() and len(ticker) == 6 and name:
                        cache[ticker] = name
    except Exception as e:
        logger.warning(f"KRX KIND 종목 목록 로드 실패: {e}")

    # fallback: pykrx SECTOR_STOCKS 개별 이름 조회
    if not cache:
        try:
            from pykrx import stock as krx
            from KR.screener import SECTOR_STOCKS
            for ts in SECTOR_STOCKS.values():
                for t in ts:
                    if t not in cache:
                        try:
                            name = krx.get_market_ticker_name(t)
                            if name:
                                cache[t] = name
                        except Exception:
                            pass
        except Exception:
            pass
    return cache


def _search_pykrx_cached(query: str) -> list[dict]:
    """KRX 전체 종목 캐시(당일 1회)에서 query 포함 종목 반환. KOSPI+KOSDAQ 모두 검색."""
    global _pykrx_name_cache, _pykrx_cache_date
    today = datetime.now().strftime('%Y-%m-%d')

    with _pykrx_cache_lock:
        if _pykrx_name_cache and _pykrx_cache_date == today:
            cache = dict(_pykrx_name_cache)
        else:
            cache = _load_krx_stock_list()
            _pykrx_name_cache = cache
            _pykrx_cache_date = today

    results = []
    q_up = query.upper()
    for ticker, name in cache.items():
        if query in name or q_up in ticker:
            results.append({"ticker": ticker, "name": name})
    return results

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

def _dashboard_response(template, is_mock):
    from flask import make_response, redirect, url_for
    user_data = current_user.data
    ai_enabled = bool(user_data.get('claude_api_key'))
    # is_mock 동기화
    if bool(user_data.get('is_mock', 0)) != is_mock:
        conn = get_db_connection()
        try:
            conn.execute('UPDATE users SET is_mock=? WHERE id=?', (1 if is_mock else 0, current_user.id))
            conn.commit()
            user_data['is_mock'] = 1 if is_mock else 0
            current_user.data['is_mock'] = 1 if is_mock else 0
        finally:
            conn.close()
    manager.get_bot(current_user.id, current_user.data)
    resp = make_response(render_template(template, user=current_user, claude_enabled=ai_enabled, sv=_STATIC_VER))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp

@kr_bp.route('/kr/dashboard')
@login_required
def kr_dashboard():
    return _dashboard_response('KR/index.html', is_mock=False)

@us_bp.route('/us/dashboard')
@login_required
def us_dashboard():
    return _dashboard_response('US/index.html', is_mock=True)

app.register_blueprint(kr_bp)
app.register_blueprint(us_bp)

@app.route('/dashboard')
@login_required
def index():
    from flask import redirect, url_for
    is_us = bool(current_user.data.get('is_mock', 0))
    return redirect(url_for('us.us_dashboard' if is_us else 'kr.kr_dashboard'))

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
@app.route('/api/toss_balance')
@login_required
def kis_balance():
    """실시간 토스증권 계좌 잔고 조회 API (kis_balance URL 하위 호환 유지)"""
    try:
        bot = get_current_bot()
        _api = getattr(bot, 'toss', None) or getattr(bot, 'kis', None)
        if not bot or not _api:
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
                ws_price = rt_prices.get(ticker)
                kis_price = float(new_stock.get('current_price', 0))
                current_p = ws_price if ws_price else kis_price

                new_stock['current_price'] = current_p
                new_stock['value'] = shares * current_p

                if purchase_p > 0:
                    if ws_price:
                        # 웹소켓 실시간가 → 직접 재계산 (가장 최신 수익률)
                        new_stock['profit_rt'] = ((ws_price / purchase_p) - 1) * 100
                    else:
                        # 웹소켓 미연결 → KIS 원본 evlu_pfls_rt 그대로 사용 (KIS 앱과 일치)
                        new_stock['profit_rt'] = float(new_stock.get('profit_rt', 0.0))
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
_futures_cache: dict = {'data': None, 'ts': 0.0}   # 선물 스냅샷 캐시 (60초)

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
            if _fx_cache['data']:
                return jsonify(_fx_cache['data'])
            return jsonify({'rate': 1350.0, 'change': 0.0, 'change_pct': 0.0})
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
        if _fx_cache['data']:
            return jsonify(_fx_cache['data'])
        return jsonify({'rate': 1350.0, 'change': 0.0, 'change_pct': 0.0})

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
            _rate = float(_fx_cache['data']['rate'])
            if _rate > 0:
                usd_krw = _rate
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
            # US daily_pnl은 ET 날짜 기준 USD 단위 → KRW 환산
            from zoneinfo import ZoneInfo as _ZI
            _et_today = datetime.now(_ZI("America/New_York")).strftime('%Y-%m-%d')
            pnl_today = float(us_bot.daily_pnl.get(_et_today, 0.0)) * usd_krw
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
        with kr_bot.lock:
            _kr_pnl_snap = dict(kr_bot.daily_pnl)
        for d, v in _kr_pnl_snap.items():
            if d >= START_DATE:
                combined[d] += float(v)
    if us_bot:
        with us_bot.lock:
            _us_pnl_snap = dict(us_bot.daily_pnl)
        for d, v in _us_pnl_snap.items():
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

    # ── 총자산 vs 원금 수익률 (미실현+실현 통합) ──────────────────────
    combined_initial = 0.0
    try:
        from base.database import get_user_initial_cash
        kr_init = get_user_initial_cash(current_user.id, False) if kr_bot else 0
        _us_raw = float(get_user_initial_cash(current_user.id, True) if us_bot else 0)
        # us_initial_cash: 0 < val < 500,000 이면 USD → KRW 환산
        # 0 이거나 500,000 이상(기본값/구형)이면 원금 미감지 → US 합산 제외
        us_init_krw = round(_us_raw * usd_krw) if (0 < _us_raw < 500_000) else 0
        combined_initial = float(kr_init) + us_init_krw
    except Exception:
        pass
    combined_total_krw = kr_card["total_krw"] + us_card["total_krw"]
    total_pnl_from_start = combined_total_krw - combined_initial if combined_initial > 0 else sum(combined.values())
    pnl_from_start_pct = round(total_pnl_from_start / combined_initial * 100, 2) if combined_initial > 0 else 0.0

    # 원금 감지 날짜
    since_date = ""
    try:
        from base.database import get_db_connection as _gdb
        _conn = _gdb()
        _row = _conn.execute('SELECT initial_cash_captured_at FROM users WHERE id = ?', (current_user.id,)).fetchone()
        _conn.close()
        since_date = (_row[0] or "") if _row else ""
    except Exception:
        pass

    return jsonify({
        "kr": kr_card,
        "us": us_card,
        "combined_total_krw": combined_total_krw,
        "pnl_from_start": round(total_pnl_from_start),
        "pnl_from_start_pct": pnl_from_start_pct,
        "since_date": since_date,
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
    if _futures_cache['data'] and now_ts - _futures_cache['ts'] < 60:
        return jsonify(_futures_cache['data'])
    try:
        from US.screener import get_futures_snapshot
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
    currency = data.get('currency', 'krw')  # 'usd' or 'krw'
    if amount > 0:
        # 명시적 금액 → 직접 설정
        set_user_initial_cash(current_user.id, amount, is_mock)
        if currency == 'usd':
            msg = f"투자 원금 기준값이 ${amount:,.2f} (USD)로 재설정되었습니다."
        else:
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


@app.route('/api/clear_blacklist', methods=['POST'])
@login_required
def clear_blacklist():
    """당일 블랙리스트 즉시 초기화 — UI 버튼에서 호출."""
    is_mock = bool(current_user.data.get('is_mock', 0))
    bot = manager.bots.get((current_user.id, is_mock))
    if not bot:
        return jsonify({"status": "error", "message": "봇이 실행 중이지 않습니다."})
    cleared = 0
    if hasattr(bot, '_satellite_rejects'):
        with bot.lock:
            cleared += len(bot._satellite_rejects)
            bot._satellite_rejects = {}
    if hasattr(bot, '_save_state'):
        bot._save_state()
    logger.info(f"[블랙리스트초기화] user={current_user.id} {cleared}개 항목 제거")
    return jsonify({"status": "ok", "message": f"블랙리스트 초기화 완료 ({cleared}개 제거)"})


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
            from KR.screener import fetch_ohlcv, calc_rsi

            macro_lines = []
            for m_ticker, m_name in [("069500", "KOSPI 대용(KODEX 200)"), ("229200", "KOSDAQ 대용(KODEX 코스닥150)")]:
                m_df = fetch_ohlcv(m_ticker, days=40, kis=getattr(bot, "toss", None) or bot.kis)
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

        if target_tickers and is_us_mode:
            # US 봇 전용: 보유 포지션 현황 + 실시간 가격 컨텍스트
            us_lines = ["[📊 US봇 보유 포지션 실시간 현황]"]
            status_data = bot.get_status()
            fx = status_data.get('fx_rate', 1400)
            for c in status_data.get('cores', []):
                if c['ticker'] in [t for t, _ in target_tickers]:
                    pnl_pct = ((c['price'] / c['avg_price'] - 1) * 100) if c.get('avg_price', 0) > 0 and c.get('price', 0) > 0 else 0
                    us_lines.append(
                        f"  코어 {c['name']}({c['ticker']}): {c['shares']}주 | "
                        f"단가 ${c.get('avg_price',0)/fx:.2f} → 현재 ${c.get('price',0)/fx:.2f} | "
                        f"수익률 {pnl_pct:+.2f}% | 상태: {c.get('status','')}"
                    )
            for s in status_data.get('satellites', []):
                if s['ticker'] in [t for t, _ in target_tickers]:
                    pnl_pct = ((s['price'] / s['avg_price'] - 1) * 100) if s.get('avg_price', 0) > 0 and s.get('price', 0) > 0 else 0
                    us_lines.append(
                        f"  위성 {s['name']}({s['ticker']}): {s['shares']}주 | "
                        f"수익률 {pnl_pct:+.2f}% | 전략: {s.get('strategy','')} | 상태: {s.get('status','')}"
                    )
            us_lines.append(f"  시장국면: {status_data.get('market_regime','?')} | 현금: ${bot.cash_usd:,.0f}")
            if len(us_lines) > 1:
                stock_analysis_context += "\n".join(us_lines) + "\n\n"

        if target_tickers and not is_us_mode:
            # KR 봇 전용: pykrx OHLCV 기반 종목 분석 컨텍스트
            context_lines = ["[📈 회원님이 궁금해하시는 종목의 실시간 데이터 분석 장부]"]
            for ticker, name in target_tickers:
                try:
                    ohlcv_df = fetch_ohlcv(ticker, days=130, kis=getattr(bot, "toss", None) or bot.kis)
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
            "\n[리마인더] 사용자의 요청에 따라 아래 BOT_COMMAND 블록을 답변 마지막에 포함하세요. "
            "당신은 이 명령을 통해 봇 설정을 직접 바꿀 수 있습니다.\n"
            "① 전략 가이드 변경: [BOT_COMMAND]{\"action\":\"update_sector_guide\",\"content\":\"...\"}[/BOT_COMMAND]\n"
            "② 위성 종목 교체(자동 재스캔): [BOT_COMMAND]{\"action\":\"trigger_rescreen\",\"market\":\"KR\"}[/BOT_COMMAND] "
            "(US면 market을 'US'로)\n"
            "③ 위성 종목 직접 지정: [BOT_COMMAND]{\"action\":\"update_satellite_stocks\",\"market\":\"KR\","
            "\"stocks\":[{\"ticker\":\"005930\",\"name\":\"삼성전자\"}]}[/BOT_COMMAND]\n"
            "④ 봇 파라미터 변경: [BOT_COMMAND]{\"action\":\"update_bot_params\",\"market\":\"KR\","
            "\"params\":{\"num_satellites\":5,\"entry_threshold_bull\":4,\"entry_threshold_neutral\":5,\"entry_threshold_bear\":6}}[/BOT_COMMAND]\n"
            "   • num_satellites: 위성 슬롯 수 (1~10)\n"
            "   • entry_threshold: 전 국면 공통 진입점수 기준\n"
            "   • entry_threshold_bull/neutral/bear: 국면별 진입점수 기준 (기본: BULL=5, NEUTRAL=6, BEAR=7)\n"
            "   사용자가 '기준 낮춰줘', '4점이면 매수해줘', '위성 5개로 늘려줘' 등을 요청하면 "
            "즉시 update_bot_params 명령을 실행하세요.\n"
            "사용자가 '위성 종목 교체', '위성 재선정', '위성 바꿔줘' 등을 요청하면 조건을 묻지 말고 "
            "즉시 trigger_rescreen 명령을 실행하세요.\n"
        )

        if stock_analysis_context:
            stock_analysis_context += (
                "\n\n[💬 말투 지침 — 반드시 따를 것]\n"
                "친한 트레이더 친구처럼 편하고 자연스럽게 말하세요.\n"
                "❌ 금지: [CONFIRM], [REJECT] 같은 판정 태그 — 일반 대화에서 절대 사용 금지\n"
                "❌ 금지: '~입니다', '~합니다' 딱딱한 보고서체\n"
                "❌ 금지: 서론·중론·결론 구조의 문서 형식\n"
                "✅ 권장: '~이에요', '~거든요', '~것 같아요' 자연스러운 구어체\n"
                "✅ 권장: 결론 먼저, 이유는 짧게. 전체 2~4문장이면 충분해요.\n"
                "데이터가 불완전해도 있는 정보로 바로 판단 내려주세요. 거부하거나 '확인 필요'로 돌리지 마세요."
            )

    except Exception as e:
        print(f"⚠️ [종목 데이터 가공 오류] : {e}")

    try:
        current_status = bot.get_status()

        # ── 시장 국면 + 예산 배분 현황 ──────────────────────────────────
        _regime = current_status.get('market_regime', getattr(bot, 'market_regime', 'NEUTRAL'))
        _hot_sectors = current_status.get('hot_sectors', [])
        _total_asset = current_status.get('mock_total_asset', 0) or current_status.get('us_total_asset', 0)
        _avail_cash  = current_status.get('available_cash', 0)
        _regime_emoji = {"BULL": "🐂", "BEAR": "🐻", "NEUTRAL": "📊"}.get(_regime, "📊")
        _n_cores = len([c for c in current_status.get('cores', []) if c.get('ticker') != 'TBD'])
        _n_sats  = len(current_status.get('satellites', []))
        _n_total = max(1, _n_cores + _n_sats)

        if _regime == "BEAR":
            _budget_per = _total_asset * 0.60 / _n_total if _total_asset > 0 else 0
            _budget_note = f"BEAR: 방어자산40% + 저점매수현금60% | 종목당 예산 약 {_budget_per:,.0f}원"
        else:
            _budget_per = _total_asset / _n_total if _total_asset > 0 else 0
            _budget_note = f"100%를 {_n_total}종목 균등배분 | 종목당 약 {_budget_per:,.0f}원"

        _mock_pnl    = current_status.get('mock_pnl', 0)
        _mock_pnl_rt = current_status.get('mock_pnl_rt', 0)

        stock_analysis_context += f"\n\n[🏦 봇 운용 현황 요약 — 반드시 숙지]\n"
        stock_analysis_context += f"■ 시장 국면: {_regime_emoji} {_regime}\n"
        stock_analysis_context += f"■ 총 평가자산: {_total_asset:,.0f}원 | 가용 현금: {_avail_cash:,.0f}원\n"
        stock_analysis_context += f"■ 누적 손익: {_mock_pnl:+,.0f}원 ({_mock_pnl_rt:+.2f}%)\n"
        stock_analysis_context += f"■ 예산 배분: {_budget_note}\n"
        stock_analysis_context += f"■ 코어 {_n_cores}개 / 위성 {_n_sats}개\n"
        if _hot_sectors:
            stock_analysis_context += f"■ 강세 섹터: {', '.join(_hot_sectors[:6])}\n"

        # ── 코어 포지션 상세 (P&L + 봇 상태) ──────────────────────────
        _cores = current_status.get('cores', [])
        if _cores:
            stock_analysis_context += "\n[📌 코어 포지션 상세]\n"
            for c in _cores:
                _avg = c.get('avg_price', 0)
                _price = c.get('price', 0)
                _pnl_pct = ((_price / _avg - 1) * 100) if _avg > 0 and _price > 0 else 0
                _val = c.get('value', 0)
                _budget = c.get('budget', 0)
                stock_analysis_context += (
                    f"  {c['name']}({c['ticker']}): {c['shares']}주 보유 | "
                    f"평단 {int(_avg):,}원 → 현재 {int(_price):,}원 ({_pnl_pct:+.1f}%) | "
                    f"평가 {int(_val):,}원 | 잔여예산 {int(_budget):,}원\n"
                    f"  └ 봇상태: {c.get('status','?')} | {c.get('status_msg','')[:60]}\n"
                )

        # ── 위성 포지션 상세 (P&L + 전략 + 봇 상태) ───────────────────
        _sats = current_status.get('satellites', [])
        if _sats:
            stock_analysis_context += "\n[🛰️ 위성 포지션 상세]\n"
            for s in _sats:
                _avg = s.get('avg_price', 0)
                _price = s.get('price', 0)
                _pnl_pct = ((_price / _avg - 1) * 100) if _avg > 0 and _price > 0 else 0
                _val = s.get('value', 0)
                _held = "보유중" if s.get('shares', 0) > 0 else "감시중(미매수)"
                stock_analysis_context += (
                    f"  {s['name']}({s['ticker']}): {_held} {s.get('shares',0)}주 | "
                    f"평단 {int(_avg):,}원 → 현재 {int(_price):,}원 ({_pnl_pct:+.1f}%) | "
                    f"전략: {s.get('strategy','-')}\n"
                    f"  └ 봇상태: {s.get('status','?')} | {s.get('status_msg','')[:60]}\n"
                )

        # ── 방어자산 보유 현황 ──────────────────────────────────────────
        _def_list = current_status.get('defensive_list', [])
        if _def_list:
            _def_held = [d for d in _def_list if d.get('shares', 0) > 0]
            if _def_held:
                stock_analysis_context += "\n[🛡️ 방어자산 현재 보유]\n"
                for d in _def_held:
                    stock_analysis_context += (
                        f"  {d['emoji']} {d['name']}({d['ticker']}): {d['shares']}주 | "
                        f"평가 {int(d.get('value',0)):,}원 | 등락 {d.get('change_pct',0):+.1f}%\n"
                    )
            else:
                stock_analysis_context += f"[🛡️ 방어자산]: BEAR 아니거나 미매수 상태\n"

        # ── 단타(모멘텀) 슬롯 현황 ──────────────────────────────────────
        _momentum_list = current_status.get('momentum_list', [])
        _active_mom = [m for m in _momentum_list if m]
        if _active_mom:
            stock_analysis_context += "\n[🚀 단타 슬롯 현황]\n"
            for m in _active_mom:
                stock_analysis_context += (
                    f"  {m.get('name','?')}({m.get('ticker','?')}): "
                    f"{m.get('shares',0)}주 | 수익률 {m.get('pnl_pct',0):+.1f}% | "
                    f"{m.get('elapsed','')} | 진입사유: {m.get('reason','')[:60]}\n"
                )
        else:
            stock_analysis_context += "[🚀 단타 슬롯]: 현재 비어있음\n"

        # ── 오늘 AI 거절된 위성 종목 ────────────────────────────────────
        _sat_rejects = getattr(bot, '_satellite_rejects', {})
        if _sat_rejects:
            stock_analysis_context += f"\n[🚫 오늘 AI 거절된 위성 종목 — 당일 재편입 금지]\n"
            for _rt, _rr in list(_sat_rejects.items())[:10]:
                stock_analysis_context += f"  · {_rt}: {str(_rr)[:50]}\n"

        # ── 오늘 실제 체결된 매매 내역 ──────────────────────────────────
        try:
            from base.database import get_db_connection
            _today_str = datetime.now(timezone(timedelta(hours=9))).strftime('%Y-%m-%d')
            _db_conn = get_db_connection()
            _today_trades = _db_conn.execute(
                "SELECT action, ticker, stock_name, price, strategy, ai_reason, profit "
                "FROM trade_journal WHERE user_id = ? AND date(created_at) = ? "
                "ORDER BY created_at DESC LIMIT 20",
                (current_user.id, _today_str)
            ).fetchall()
            _db_conn.close()
            if _today_trades:
                stock_analysis_context += f"\n[📒 오늘 체결된 매매 내역 ({_today_str})]\n"
                for _tr in _today_trades:
                    _action_icon = "🟢매수" if _tr['action'] == 'BUY' else "🔴매도"
                    _profit_str = f" | 손익 {int(_tr['profit'] or 0):+,}원" if _tr['action'] == 'SELL' else ""
                    stock_analysis_context += (
                        f"  {_action_icon} {_tr['stock_name']}({_tr['ticker']}) "
                        f"@ {int(_tr['price'] or 0):,}원 | {_tr['strategy']}"
                        f"{_profit_str}\n"
                    )
        except Exception:
            pass

        # ── 장중 AI 시황 분석 ──────────────────────────────────────────
        _ai_view = getattr(bot, 'current_ai_market_view', '')
        if _ai_view:
            stock_analysis_context += f"\n[🧠 장중 AI 시황 분석]\n{_ai_view[:600]}\n"

        # ── 거래량 급증 종목 실제 리스트 ─────────────────────────────────
        _surge_details = getattr(bot, 'volume_surge_details', [])
        if _surge_details:
            stock_analysis_context += f"\n[📈 거래량 2배 급증 종목 실제 리스트 — {len(_surge_details)}개]\n"
            for _s in _surge_details[:30]:
                stock_analysis_context += f"  · {_s.get('name','?')}({_s.get('ticker','?')}): {_s.get('ratio',0):.1f}배\n"
            stock_analysis_context += (
                "위 종목들에 대해 질문받으면 종목명과 배율을 직접 언급하세요. "
                "'데이터 없음', '리스트 없음'이라고 하면 안 됩니다.\n"
            )

        # ── 일일 리포트 (가장 최근 슬롯) ────────────────────────────────
        _daily_report = getattr(bot, 'daily_report', None)
        if isinstance(_daily_report, dict):
            _today = _daily_report.get('date', '')
            _report_slots = {k: v for k, v in _daily_report.items() if k != 'date' and v}
            if _report_slots:
                _latest_slot = sorted(_report_slots.keys())[-1]
                _report_content = _report_slots[_latest_slot]
                if _report_content:
                    stock_analysis_context += (
                        f"\n[📋 오늘의 일일 리포트 ({_today} {_latest_slot}) — 이 내용이 채팅 질문의 배경일 수 있음]\n"
                        f"{str(_report_content)[:1500]}\n"
                        f"(리포트 관련 질문은 위 내용을 바탕으로 답하세요. '데이터 없음'이라고 하면 안 됩니다.)\n"
                    )

        # ── 봇 최근 로그 ───────────────────────────────────────────────
        bot_logs = current_status.get('logs', [])
        if bot_logs:
            stock_analysis_context += "\n[📝 백엔드 자동 매매 시스템 최근 실행 로그 (필독)]\n"
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

            elif cmd.get('action') == 'trigger_rescreen':
                # AI가 위성 종목 자동 재스캔 명령 (블랙리스트 초기화 후 재스캔)
                target = cmd.get('market', 'KR').upper()
                is_us = (target == 'US')
                _is_mock_v = True if is_us else False
                _b = manager.bots.get((current_user.id, _is_mock_v))
                if _b and hasattr(_b, '_rescreen_satellites'):
                    # 블랙리스트 강제 초기화 — 오늘 AI 거절 내역 리셋 후 재스캔
                    if hasattr(_b, '_satellite_rejects'):
                        with _b.lock:
                            _b._satellite_rejects = {}
                        logging.getLogger('lassi_bot').info(f"[AI봇명령] {target} satellite_rejects 초기화")
                    if hasattr(_b, '_last_rescreen_actual_ts'):
                        _b._last_rescreen_actual_ts = 0.0  # 쿨다운 리셋
                    import threading as _threading
                    _threading.Thread(target=_b._rescreen_satellites, daemon=True).start()
                    applied_commands.append(f"✅ [{target}] 블랙리스트 초기화 후 위성 재스캔을 시작했습니다.")
                    logging.getLogger('lassi_bot').info(
                        f"[AI봇명령] user={current_user.id} {target} _rescreen_satellites 트리거"
                    )
                else:
                    applied_commands.append(f"⚠️ [{target}] 봇이 실행 중이지 않아 재스캔할 수 없습니다.")

            elif cmd.get('action') == 'update_bot_params':
                # AI가 봇 파라미터를 동적으로 변경 (진입점수 기준, 위성 슬롯 수 등)
                # params 예: {"num_satellites": 5, "entry_threshold_bull": 4, "entry_threshold_neutral": 5}
                target = cmd.get('market', 'KR').upper()
                is_us = (target == 'US')
                _is_mock_v = True if is_us else False
                _b = manager.bots.get((current_user.id, _is_mock_v))
                if _b:
                    params = cmd.get('params', {})
                    changed = []

                    # 위성 슬롯 수
                    if 'num_satellites' in params:
                        _ns = int(params['num_satellites'])
                        _ns = max(1, min(10, _ns))
                        _b.num_satellites = _ns
                        _b._save_state()
                        changed.append(f"위성 슬롯 수: {_ns}개")

                    # 진입점수 기준 오버라이드 — 키 형식: entry_threshold_{regime소문자}
                    # 예: entry_threshold_bull=4 → entry_thresholds['BULL']=4
                    regime_map = {'bull': 'BULL', 'neutral': 'NEUTRAL', 'bear': 'BEAR'}
                    for k, v in params.items():
                        if k.startswith('entry_threshold_'):
                            suffix = k.replace('entry_threshold_', '')
                            regime_key = regime_map.get(suffix)
                            if regime_key:
                                _b.entry_thresholds[regime_key] = int(v)
                                changed.append(f"진입점수 기준 {regime_key}: {int(v)}pt")
                        elif k == 'entry_threshold':   # 전 국면 공통 설정 단축키
                            for rk in ('BULL', 'NEUTRAL', 'BEAR'):
                                _b.entry_thresholds[rk] = int(v)
                            changed.append(f"진입점수 기준 전 국면: {int(v)}pt")

                    if changed:
                        applied_commands.append(f"✅ [{target}] 파라미터 변경: {', '.join(changed)}")
                        logging.getLogger('lassi_bot').info(
                            f"[AI봇명령] user={current_user.id} {target} update_bot_params: {params}"
                        )
                    else:
                        applied_commands.append(f"⚠️ [{target}] 변경할 파라미터가 없습니다.")
                else:
                    applied_commands.append(f"⚠️ [{target}] 봇이 활성화되어 있지 않습니다.")

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
        count = int(data.get('count', 3))
        count = max(1, min(3, count))   # 1~3 범위 강제
    except (TypeError, ValueError):
        count = 3

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

def _save_keys_common(data, is_mock):
    """토스증권 API 설정 저장 — KR/US 동일 계좌 사용."""
    existing = current_user.data

    def _v(key):
        v = data.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    # 토스증권은 KR/US 공통 단일 계좌
    _client_id     = _v('client_id')     or existing.get('real_app_key')
    _client_secret = _v('client_secret') or existing.get('real_app_secret')
    _account_seq   = _v('account_seq')   or existing.get('real_account_no')

    toss_cfg = {
        "client_id":     _client_id     or "",
        "client_secret": _client_secret or "",
        "account_seq":   _account_seq   or "",
    }

    # DB에는 KR/US 구분 없이 real_* 필드에 통합 저장
    update_data = {
        'real_app_key':    _client_id,
        'real_app_secret': _client_secret,
        'real_account_no': _account_seq,
        # US 필드도 동일값으로 동기화 (레거시 코드 호환)
        'us_app_key':      _client_id,
        'us_app_secret':   _client_secret,
        'us_account_no':   _account_seq,
        'telegram_token':  _v('telegram_token')   or existing.get('telegram_token'),
        'telegram_chat_id':_v('telegram_chat_id') or existing.get('telegram_chat_id'),
        'claude_api_key':  _v('claude_api_key')   or existing.get('claude_api_key'),
        'is_mock': 1 if is_mock else 0,
    }

    if is_mock:
        update_data['us_core_stocks'] = data.get('us_core_stocks') or existing.get('us_core_stocks')
        core = update_data['us_core_stocks']
    else:
        update_data['core_stocks'] = data.get('core_stocks') or existing.get('core_stocks')
        core = update_data['core_stocks']

    # None 값은 기존값 유지
    update_data = {k: v for k, v in update_data.items() if v is not None}

    update_user_keys(current_user.id, update_data)
    for k, v in update_data.items():
        current_user.data[k] = v

    tele = {"token": update_data.get('telegram_token'), "chat_id": update_data.get('telegram_chat_id')}
    bot = manager.bots.get((current_user.id, is_mock))
    if bot:
        bot.reload_api_keys(kis_config=toss_cfg, telegram_config=tele, gemini_config={}, core_stocks=core)

    return jsonify({"status": "success"})


@app.route('/api/settings/keys', methods=['POST'])
@login_required
def set_keys():
    data = request.json or {}
    is_mock = bool(int(data.get('is_mock', 1)))
    return _save_keys_common(data, is_mock)


@app.route('/api/settings/kr_keys', methods=['POST'])
@login_required
def set_kr_keys():
    return _save_keys_common(request.json or {}, is_mock=False)


@app.route('/api/settings/us_keys', methods=['POST'])
@login_required
def set_us_keys():
    return _save_keys_common(request.json or {}, is_mock=True)

@app.route('/api/search/stock')
@login_required
def search_stock():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"results": []})

    import requests as _req

    # ── 헬퍼: items 배열에서 {ticker, name} 추출 ──────────────────────────
    def _parse_naver_items(items) -> list:
        """네이버 AC 응답 items를 파싱 — flat / nested 모두 처리."""
        out = []
        for item in items:
            if not isinstance(item, list):
                continue
            # flat: ["삼성전자", "005930", ...]
            if len(item) >= 2 and isinstance(item[0], str) and isinstance(item[1], str):
                code, name = item[1], item[0]
                if code.isdigit() and len(code) == 6:
                    out.append({"ticker": code, "name": name})
                    continue
            # nested: [["삼성전자", "005930", ...], ...]
            if item and isinstance(item[0], list):
                for sub in item:
                    if isinstance(sub, list) and len(sub) >= 2:
                        code, name = sub[1], sub[0]
                        if isinstance(code, str) and code.isdigit() and len(code) == 6:
                            out.append({"ticker": code, "name": name})
        return out

    # 1순위: 네이버 Finance 자동완성 AC
    try:
        res = _req.get(
            "https://ac.finance.naver.com/ac",
            params={"q": query, "r_format": "json", "r_enc": "utf-8",
                    "r_unicode": "1", "t_kwd": "1"},
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "Referer": "https://finance.naver.com/"},
            timeout=4
        )
        if res.status_code == 200:
            results = _parse_naver_items(res.json().get("items", []))
            if results:
                return jsonify({"results": results[:15]})
    except Exception as e:
        logger.warning(f"네이버 AC 종목검색 실패: {e}")

    # 2순위: 네이버 모바일 검색 API (EC2에서 AC가 막힐 때 대안)
    try:
        res = _req.get(
            "https://m.stock.naver.com/api/search/all",
            params={"keyword": query, "size": 15},
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)"},
            timeout=4
        )
        if res.status_code == 200:
            data = res.json()
            results = []
            for item in data.get("result", {}).get("stocks", []):
                code = str(item.get("itemCode", ""))
                name = item.get("itemName", "")
                if code.isdigit() and len(code) == 6 and name:
                    results.append({"ticker": code, "name": name})
            if results:
                return jsonify({"results": results[:15]})
    except Exception as e:
        logger.warning(f"네이버 모바일 종목검색 실패: {e}")

    # 3순위: 토스증권 API 검색 — KR 봇 우선, 현재 봇 차선
    try:
        kr_bot = manager.bots.get((current_user.id, False))
        _toss = (getattr(kr_bot, 'toss', None) or getattr(kr_bot, 'kis', None)
                 if kr_bot else None)
        if not _toss:
            _cb = get_current_bot()
            _toss = getattr(_cb, 'toss', None) or getattr(_cb, 'kis', None)
        if _toss:
            results = _toss.search_stock_name(query)
            if results:
                return jsonify({"results": results})
    except Exception as e:
        logger.warning(f"토스 종목검색 실패: {e}")

    # 4순위: pykrx — KOSPI + KOSDAQ 전체 검색 (캐시)
    try:
        results = _search_pykrx_cached(query)
        if results:
            return jsonify({"results": results[:15]})
    except Exception as e:
        logger.warning(f"pykrx 종목검색 실패: {e}")

    return jsonify({"results": []})

# ─────────────────────────────────────────────────────────────────────────────
# 성과 리포트 페이지
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/report')
@login_required
def report_page():
    return render_template('report.html', user=current_user)


@app.route('/api/report')
@login_required
def report_api():
    """일/주/월별 수익률 + 매매 승률 통계."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            '''SELECT ticker, stock_name, action, price, shares, mode,
                      strategy, ai_reason, profit,
                      strftime('%Y-%m-%d', created_at) as date,
                      strftime('%Y-%W',    created_at) as week,
                      strftime('%Y-%m',    created_at) as month,
                      created_at
               FROM trade_journal WHERE user_id = ?
               ORDER BY created_at DESC''',
            (current_user.id,)
        ).fetchall()
    finally:
        conn.close()

    trades = [dict(r) for r in rows]

    # ── 승률 / 평균 손익 계산 ────────────────────────────────────────
    sell_trades = [t for t in trades if t['action'] == 'SELL']
    wins   = [t for t in sell_trades if (t['profit'] or 0) > 0]
    losses = [t for t in sell_trades if (t['profit'] or 0) < 0]
    win_rate  = round(len(wins) / len(sell_trades) * 100, 1) if sell_trades else 0
    avg_win   = round(sum(t['profit'] for t in wins)   / len(wins),   0) if wins   else 0
    avg_loss  = round(sum(t['profit'] for t in losses) / len(losses), 0) if losses else 0
    total_pnl = round(sum((t['profit'] or 0) for t in sell_trades), 0)

    # ── 일별 손익 집계 ───────────────────────────────────────────────
    from collections import defaultdict as _dd
    daily: dict  = _dd(float)
    weekly: dict = _dd(float)
    monthly: dict = _dd(float)
    for t in sell_trades:
        p = t['profit'] or 0
        daily[t['date']]   += p
        weekly[t['week']]  += p
        monthly[t['month']] += p

    daily_sorted   = sorted(daily.items())[-30:]
    weekly_sorted  = sorted(weekly.items())[-12:]
    monthly_sorted = sorted(monthly.items())[-12:]

    # ── KR/US 별 승률 ────────────────────────────────────────────────
    def _mode_stats(mode):
        mt = [t for t in sell_trades if t.get('mode', 'KR') == mode]
        mw = [t for t in mt if (t['profit'] or 0) > 0]
        return {
            'trades': len(mt),
            'wins': len(mw),
            'win_rate': round(len(mw) / len(mt) * 100, 1) if mt else 0,
            'total_pnl': round(sum((t['profit'] or 0) for t in mt), 0),
        }

    return jsonify({
        'summary': {
            'total_trades': len(sell_trades),
            'win_rate': win_rate,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'total_pnl': total_pnl,
        },
        'by_mode': {'KR': _mode_stats('KR'), 'US': _mode_stats('US')},
        'chart': {
            'daily':   {'labels': [r[0] for r in daily_sorted],   'values': [round(r[1]) for r in daily_sorted]},
            'weekly':  {'labels': [r[0] for r in weekly_sorted],  'values': [round(r[1]) for r in weekly_sorted]},
            'monthly': {'labels': [r[0] for r in monthly_sorted], 'values': [round(r[1]) for r in monthly_sorted]},
        },
        'recent_trades': trades[:50],
    })


# ─────────────────────────────────────────────────────────────────────────────
# 백테스트 페이지 + API
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/backtest')
@login_required
def backtest_page():
    return render_template('backtest.html', user=current_user)


@app.route('/api/backtest', methods=['POST'])
@login_required
def backtest_api():
    """간이 백테스트 — yfinance 일봉으로 MA/RSI 전략 시뮬레이션."""
    data   = request.json or {}
    ticker = (data.get('ticker') or '').strip().upper()
    mode   = (data.get('mode') or 'KR').upper()          # KR / US
    period = int(data.get('period', 180))                 # 일수 (기본 6개월)
    ma_fast  = int(data.get('ma_fast', 20))
    ma_slow  = int(data.get('ma_slow', 60))
    rsi_buy  = float(data.get('rsi_buy', 40))
    rsi_sell = float(data.get('rsi_sell', 70))
    init_cash = float(data.get('init_cash', 10_000_000))

    if not ticker:
        return jsonify({'error': 'ticker 누락'}), 400

    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np

        yt = ticker + '.KS' if mode == 'KR' and not ticker.endswith(('.KS', '.KQ')) else ticker
        df = yf.download(yt, period=f'{period + 80}d', interval='1d',
                         progress=False, auto_adjust=True)
        if hasattr(df.columns, 'get_level_values'):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=['Close'])
        df.columns = [c.lower() for c in df.columns]

        if len(df) < ma_slow + 5:
            return jsonify({'error': f'데이터 부족 ({len(df)}봉)'}), 400

        # MA 계산
        df['ma_fast'] = df['close'].rolling(ma_fast).mean()
        df['ma_slow'] = df['close'].rolling(ma_slow).mean()

        # RSI(14) 계산
        delta  = df['close'].diff()
        gain   = delta.clip(lower=0).rolling(14).mean()
        loss   = (-delta.clip(upper=0)).rolling(14).mean()
        rs     = gain / loss.replace(0, np.nan)
        df['rsi'] = 100 - (100 / (1 + rs))

        df = df.dropna().tail(period)

        # ── 시뮬레이션 ───────────────────────────────────────────────
        cash  = init_cash
        shares = 0
        avg_p  = 0.0
        trades_log = []
        equity_curve = []

        for i, (idx, row) in enumerate(df.iterrows()):
            price = float(row['close'])
            rsi   = float(row['rsi'])
            maf   = float(row['ma_fast'])
            mas   = float(row['ma_slow'])
            date_str = str(idx)[:10]

            # 매수 신호: MA 골든크로스 + RSI < rsi_buy
            if shares == 0 and maf > mas and rsi < rsi_buy and cash > price:
                qty   = int(cash * 0.95 / price)
                cost  = qty * price
                cash -= cost
                shares = qty
                avg_p  = price
                trades_log.append({'date': date_str, 'action': 'BUY',
                                   'price': round(price, 2), 'qty': qty, 'profit': None})

            # 매도 신호: MA 데드크로스 or RSI > rsi_sell
            elif shares > 0 and (maf < mas or rsi > rsi_sell):
                proceeds = shares * price * 0.998
                profit   = round(proceeds - avg_p * shares, 0)
                cash    += proceeds
                trades_log.append({'date': date_str, 'action': 'SELL',
                                   'price': round(price, 2), 'qty': shares,
                                   'profit': profit})
                shares = 0
                avg_p  = 0.0

            equity_curve.append({'date': date_str, 'value': round(cash + shares * price)})

        # 미청산 포지션 시가 평가
        last_price = float(df['close'].iloc[-1])
        final_val  = cash + shares * last_price
        total_pnl  = round(final_val - init_cash, 0)
        total_ret  = round((final_val / init_cash - 1) * 100, 2)

        wins   = [t for t in trades_log if t['action'] == 'SELL' and (t['profit'] or 0) > 0]
        losses = [t for t in trades_log if t['action'] == 'SELL' and (t['profit'] or 0) <= 0]
        n_sell = len(wins) + len(losses)

        return jsonify({
            'ticker': ticker,
            'period': period,
            'params': {'ma_fast': ma_fast, 'ma_slow': ma_slow,
                       'rsi_buy': rsi_buy, 'rsi_sell': rsi_sell},
            'result': {
                'total_pnl':  total_pnl,
                'total_ret':  total_ret,
                'final_val':  round(final_val),
                'n_trades':   n_sell,
                'win_rate':   round(len(wins) / n_sell * 100, 1) if n_sell else 0,
                'avg_win':    round(sum(t['profit'] for t in wins)   / len(wins),   0) if wins   else 0,
                'avg_loss':   round(sum(t['profit'] for t in losses) / len(losses), 0) if losses else 0,
            },
            'equity_curve': equity_curve[-period:],
            'trades': trades_log[-30:],
        })

    except Exception as e:
        logger.warning(f"backtest 오류: {e}", exc_info=True)
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False, threaded=True)