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

    stock_analysis_context = ""

    try:
        from pykrx import stock as krx_stock
        from stock_screener import fetch_ohlcv, calc_rsi
        
        target_tickers = []
        
        # 1. 사용자가 질문에 언급한 종목 스캔
        for core in bot.core_positions:
            if core.name in user_message: target_tickers.append((core.ticker, core.name))
        for ticker, pos in bot.satellite_positions.items():
            if pos.name in user_message: target_tickers.append((ticker, pos.name))
            
        # 2. 특정 종목 언급이 없다면 코어/위성 리스트 전체를 분석 대상으로 가져옴 (보유 수량 0주여도 무조건 가져옴!)
        if not target_tickers and any(keyword in user_message for keyword in ["포트폴리오", "위성", "분석", "종목", "시장", "금요일", "어제"]):
            for core in bot.core_positions:
                target_tickers.append((core.ticker, core.name))
            for ticker, pos in bot.satellite_positions.items():
                target_tickers.append((ticker, pos.name))

        # API 속도 방어를 위해 중복을 제거하고 최대 5개까지만 집중 분석합니다.
        target_tickers = list(dict.fromkeys(target_tickers))[:5]

        if target_tickers:
            context_lines = ["[📈 요청받은 종목의 실시간 차트 및 재무 데이터 (AI 판단용)]"]
            for ticker, name in target_tickers:
                # 120일 이동평균선을 계산하기 위해 넉넉히 150일치 차트 데이터를 긁어옵니다.
                ohlcv_df = fetch_ohlcv(ticker, days=150)
                if not ohlcv_df.empty:
                    latest_biz_date = ohlcv_df.index[-1].strftime("%Y%m%d")
                    fund_df = krx_stock.get_market_fundamental_by_ticker(latest_biz_date, latest_biz_date, ticker)
                    
                    close_series = ohlcv_df['close']
                    vol_series = ohlcv_df['volume']
                    
                    rsi_14 = calc_rsi(close_series, 14).iloc[-1]
                    
                    # 💡 AI 매뉴얼 1번 원칙: 120일선 계산
                    sma_120 = close_series.rolling(120).mean().iloc[-1] if len(close_series) >= 120 else close_series.mean()
                    current_price = close_series.iloc[-1]
                    status_120 = "120일선 상단(상승장)" if current_price >= sma_120 else "120일선 하단(역배열 하락장)"
                    
                    # 💡 AI 매뉴얼 3번 원칙: 최근 영업일 거래량 폭증 여부 계산
                    vol_today = vol_series.iloc[-1]
                    vol_20_avg = vol_series.rolling(20).mean().iloc[-2] if len(vol_series) > 20 else 1
                    vol_ratio = (vol_today / vol_20_avg * 100) if vol_20_avg > 0 else 100
                    
                    # 💡 AI 매뉴얼 2번 원칙: 재무제표 (PER, PBR)
                    per = fund_df.loc[ticker, 'PER'] if not fund_df.empty else 0
                    pbr = fund_df.loc[ticker, 'PBR'] if not fund_df.empty else 0
                    
                    context_lines.append(
                        f"- {name}({ticker}): 현재가 {int(current_price):,}원 | 120일선 {int(sma_120):,}원 ({status_120}) | "
                        f"RSI: {rsi_14:.1f} | 최근 거래량 평소 대비 {vol_ratio:.0f}% 수준 | PER: {per:.2f}, PBR: {pbr:.2f}"
                    )
            
            stock_analysis_context = "\n".join(context_lines)
            
            # 💡 [핵심] 깐깐한 AI를 제압하는 특수 명령어 추가
            stock_analysis_context += "\n\n[🚨 AI 퀀트 시스템 핵심 지시사항]\n"
            stock_analysis_context += "위 제공된 지표(120일선, 거래량 폭증 비율, PER, PBR 등)가 현재 시스템이 수집할 수 있는 **최대치의 실시간 데이터**입니다. "
            stock_analysis_context += "수급(외국인/기관), ROE, 부채비율 등 제공되지 않은 데이터는 '적정 수준'으로 간주하고, "
            stock_analysis_context += "데이터가 부족하다는 변명이나 추가 데이터를 요구하는 답변은 절대 금지합니다. "
            stock_analysis_context += "오직 주어진 데이터만을 조합하여 분석 결과를 내놓으십시오."

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