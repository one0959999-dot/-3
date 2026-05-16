from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from bot_controller import manager
from database import get_db_connection, verify_user, add_user, init_db, update_user_keys
import os
import json
from datetime import datetime, timedelta
import threading

app = Flask(__name__)

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
    conn = get_db_connection()
    user_data = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    if user_data:
        return User(user_data['id'], user_data['username'], dict(user_data))
    return None

def get_current_bot():
    return manager.get_bot(current_user.id, current_user.data)

@app.route('/')
@login_required
def index():
    user_data = current_user.data
    gemini_enabled = bool(user_data.get('gemini_api_key'))
    return render_template('index.html', user=current_user, gemini_enabled=gemini_enabled)

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
            return redirect(url_for('index'))
        flash('아이디 또는 비밀번호가 올바르지 않습니다.')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
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
    return jsonify(bot.get_status())

@app.route('/api/kis_balance')
@login_required
def kis_balance():
    """실시간 한국투자증권 계좌 잔고 조회 API"""
    bot = get_current_bot()
    if not bot or not bot.kis:
        return jsonify({"status": "error", "message": "API 설정이 필요합니다."})
    
    # 💎 백그라운드 영속 스레드에 의해 이미 수집된 캐시 데이터가 있다면 지연 없이 즉시 반환
    if bot.cached_balance:
        return jsonify({"status": "success", "data": bot.cached_balance})
    
    # 서버 최초 구동 직후 캐시가 준비 안 된 최초 1회만 동기 조회 수행
    balance = bot.kis.get_account_balance()
    if balance:
        bot.cached_balance = balance
        bot._sync_internal_balances(balance)
        return jsonify({"status": "success", "data": balance})
    return jsonify({"status": "error", "message": "잔고 조회 실패"})

@app.route('/api/toggle', methods=['POST'])
@login_required
def toggle_bot():
    bot = get_current_bot()
    if bot.is_running:
        bot.stop()
        return jsonify({"status": "stopped"})
    else:
        # DB에 저장된 사용자의 실제 투자 원금(initial_cash)을 안전하게 읽어와 봇을 시작하도록 변경합니다.
        user_cash = current_user.data.get('initial_cash', 10000000)
        success = bot.start(total_cash=user_cash)
        if success:
            return jsonify({"status": "started"})
        return jsonify({"status": "error", "message": "봇 시작 실패"}), 400

@app.route('/api/pnl')
@login_required
def get_pnl():
    bot = get_current_bot()
    return jsonify(bot.get_pnl_data())

@app.route('/api/daily_report')
@login_required
def get_daily_report():
    bot = get_current_bot()
    if not bot or not bot.gemini:
        return jsonify({"status": "error", "message": "AI 설정이 필요합니다."})
        
    today_str = datetime.today().strftime('%Y-%m-%d')
    weekday = datetime.today().weekday()
    
    # 1. 오늘 날짜로 생성된 리포트가 존재하면 즉시 반환
    if bot.daily_report and bot.daily_report.get('date') == today_str:
        return jsonify({"status": "success", "data": bot.daily_report})
    
    # 2. 토요일(5) 또는 일요일(6) 등 주말/휴일장인 경우의 예외 처리
    if weekday >= 5:
        if bot.daily_report:
            # 전날(금요일 등) 작성된 리포트가 있다면 리턴
            return jsonify({"status": "success", "data": bot.daily_report})
        else:
            # 작성된 리포트가 아예 없다면 지정된 휴일 안내 멘트 리턴
            return jsonify({
                "status": "success",
                "data": {
                    "date": today_str,
                    "report_markdown": "### 📢 알림\n\n금일은 휴일장입니다."
                }
            })
            
    # 3. 평일인데 아직 오늘 자 리포트가 생성되지 않은 경우 비동기 생성 시작
    threading.Thread(target=bot.generate_daily_report, daemon=True).start()
    return jsonify({"status": "waiting", "message": "리포트 생성 중..."})

@app.route('/api/ai_chat', methods=['POST'])
@login_required
def ai_chat():
    bot = get_current_bot()
    data = request.json
    user_message = data.get('message', '').strip()
    if not bot or not bot.gemini:
        return jsonify({"status": "error", "reply": "AI API 키를 등록해주세요."})

    stock_analysis_context = None
    query_ticker = None
    stock_name = None

    if "삼성전자" in user_message: query_ticker, stock_name = "005930", "삼성전자"
    elif "하이닉스" in user_message: query_ticker, stock_name = "000660", "SK하이닉스"
    elif "보령" in user_message: query_ticker, stock_name = "003850", "보령"
    elif "현대차" in user_message: query_ticker, stock_name = "005380", "현대차"
    elif "NAVER" in user_message or "네이버" in user_message: query_ticker, stock_name = "035420", "NAVER"

    if query_ticker:
        try:
            from pykrx import stock as krx_stock
            from stock_screener import fetch_ohlcv, calc_rsi

            # 1. 차트 데이터를 먼저 60일치 긁어옵니다. (주말이어도 알아서 과거 영업일 데이터를 안전하게 들고 옴)
            ohlcv_df = fetch_ohlcv(query_ticker, days=60)

            if not ohlcv_df.empty:
                # 🟢 핵심 수정: 오늘 날짜 대신 차트 데이터의 가장 마지막 인덱스(실제 최신 영업일 날짜)를 끄집어냅니다.
                latest_biz_date = ohlcv_df.index[-1].strftime("%Y%m%d")
                
                # 추출한 최신 영업일 날짜로 재무 펀더멘탈을 조회하여 주말 공통 에러를 원천 차단합니다.
                fund_df = krx_stock.get_market_fundamental_by_ticker(latest_biz_date, latest_biz_date, query_ticker)

                if not fund_df.empty:
                    per = fund_df.loc[query_ticker, 'PER']
                    pbr = fund_df.loc[query_ticker, 'PBR']
                    eps = fund_df.loc[query_ticker, 'EPS']
                    div_yield = fund_df.loc[query_ticker, '배당수익률']

                    close_series = ohlcv_df['close']
                    rsi_series = calc_rsi(close_series, 14)
                    
                    current_price = int(close_series.iloc[-1])
                    current_rsi = float(rsi_series.iloc[-1])

                    stock_analysis_context = (
                        f"종목명: {stock_name} ({query_ticker})\n"
                        f"기준 영업일: {latest_biz_date[:4]}년 {latest_biz_date[4:6]}월 {latest_biz_date[6:]}일\n"
                        f"1. [재무제표 지표] 현재가: {current_price:,}원 | PER: {per:.2f}배 | PBR: {pbr:.2f}배 | EPS: {eps:,}원 | 배당수익률: {div_yield:.2f}%\n"
                        f"2. [기술적 차트 지표] 실시간 RSI(14): {current_rsi:.1f} (30 이하 과매도, 70 이상 과매수)\n"
                        f"3. [최근 5거래일 종가 추이]: {[int(x) for x in close_series.tail(5).values]}"
                    )
        except Exception as e:
            print(f"⚠️ [AI 비서 데이터 바인딩 에러] : {e}")

    reply = bot.gemini.chat(
        user_message, 
        portfolio_context=bot.get_status(), 
        stock_analysis_context=stock_analysis_context
    )
    return jsonify({"status": "success", "reply": reply})

@app.route('/api/ai_reset', methods=['POST'])
@login_required
def ai_reset():
    """AI 채팅 기록 초기화 (누락된 API 추가)"""
    bot = get_current_bot()
    if bot and bot.gemini:
        bot.gemini.reset_chat()
        return jsonify({"status": "success"})
    return jsonify({"status": "error"})

@app.route('/api/search/stock')
@login_required
def search_stock():
    """프론트엔드 종목 검색창 요청 처리 API (KIS API 연동)"""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"results": []})
        
    bot = get_current_bot()
    if not bot or not bot.kis:
        return jsonify({"results": [], "message": "API 설정이 비어있습니다."})
        
    # kis_api.py에 정의된 search_stock_name 메서드를 사용하여 한국거래소 종목을 검색합니다.
    results = bot.kis.search_stock_name(query)
    return jsonify({"results": results})

@app.route('/api/settings/mode', methods=['POST'])
@login_required
def set_mode():
    """실전/모의 투자 모드 전환 API"""
    data = request.json
    is_mock = int(data.get('is_mock', 1))
    
    # 1. DB 업데이트
    from database import get_db_connection
    conn = get_db_connection()
    conn.execute('UPDATE users SET is_mock = ? WHERE id = ?', (is_mock, current_user.id))
    conn.commit()
    conn.close()
    
    # [핵심 수정] 로그인 세션 메모리(current_user.data)의 모드 상태를 즉시 변경해 줍니다.
    current_user.data['is_mock'] = is_mock
    
    # 새롭게 전환된 모드에 맞는 쌍둥이 봇 인스턴스를 백엔드 메모리에 깨끗하게 생성 및 복구해 둡니다.
    get_current_bot()
        
    return jsonify({"status": "success", "is_mock": is_mock})

# 🟢 [여기에 새로 추가된 부분] 🟢
@app.route('/api/settings/satellites', methods=['POST'])
@login_required
def set_satellites_count():
    """웹 대시보드에서 요청한 위성 종목 개수 변경 설정을 저장합니다."""
    data = request.json
    count = int(data.get('count', 5))
    
    bot = get_current_bot()
    if bot:
        bot.num_satellites = count
        bot._save_state()  # 💡 변경된 종목 개수 설정을 DB 장부에 즉시 반영합니다.
        return jsonify({"status": "success", "num_satellites": count})
    return jsonify({"status": "error", "message": "봇을 활성화할 수 없습니다."}), 400

@app.route('/api/settings/keys', methods=['POST'])
@login_required
def set_keys():
    data = request.json
    update_data = {
        'real_app_key': data.get('real_app_key'),
        'real_app_secret': data.get('real_app_secret'),
        'real_account_no': data.get('real_account_no'),
        'mock_app_key': data.get('mock_app_key'),
        'mock_app_secret': data.get('mock_app_secret'),
        'mock_account_no': data.get('mock_account_no'),
        'telegram_token': data.get('telegram_token'),
        'telegram_chat_id': data.get('telegram_chat_id'),
        'gemini_api_key': data.get('gemini_api_key'),
        'core_stocks': data.get('core_stocks'),
        'is_mock': int(data.get('is_mock', 1)) # 명확한 정수형 보장
    }

    # 1. 데이터 저장
    update_user_keys(current_user.id, update_data)

    # [핵심 수정] 사용자가 수정한 새로운 API 키셋 정보들을 로그인 세션 캐시 메모리에도 통틀어 동기화합니다.
    for k, v in update_data.items():
        current_user.data[k] = v

    is_mock = update_data['is_mock']
    prefix = 'mock_' if is_mock else 'real_'
    
    # [쌍둥이 구조 최적화] 현재 가동 중인 메인 봇 뿐만 아니라, 반대편 방에 대기 중인 쌍둥이 봇도 구형 키를 들고 있지 않도록 정밀 갱신합니다.
    # 1) 현재 활성화되어 화면에 노출 중인 봇 객체 동기화
    bot = get_current_bot()
    if bot:
        bot.reload_api_keys(
            kis_config={
                "app_key": data.get(f'{prefix}app_key'),
                "app_secret": data.get(f'{prefix}app_secret'),
                "account_no": data.get(f'{prefix}account_no'),
                "is_mock": bool(is_mock)
            },
            telegram_config={
                "token": data.get('telegram_token'),
                "chat_id": data.get('telegram_chat_id')
            },
            gemini_config={
                "api_key": data.get('gemini_api_key')
            },
            core_stocks=data.get('core_stocks')
        )

    # 2) 반대편 대기실에 존재하는 쌍둥이 봇 객체도 메모리에 있다면 구형 접속 정보가 남지 않도록 동시 갱신
    other_mock = not bool(is_mock)
    other_prefix = 'mock_' if other_mock else 'real_'
    other_bot = manager.bots.get((current_user.id, other_mock))
    if other_bot:
        other_bot.reload_api_keys(
            kis_config={
                "app_key": data.get(f'{other_prefix}app_key'),
                "app_secret": data.get(f'{other_prefix}app_secret'),
                "account_no": data.get(f'{other_prefix}account_no'),
                "is_mock": other_mock
            },
            telegram_config={
                "token": data.get('telegram_token'),
                "chat_id": data.get('telegram_chat_id')
            },
            gemini_config={
                "api_key": data.get('gemini_api_key')
            },
            core_stocks=data.get('core_stocks')
        )

    # 2. 브라우저에게 "성공했다"고 대답해줌
    return jsonify({"status": "success"})

if __name__ == '__main__':
    init_db()
    # debug=False 및 use_reloader=False로 설정하여 프로세스 이중 실행과 의도치 않은 자동 시작을 원천 차단합니다.
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)