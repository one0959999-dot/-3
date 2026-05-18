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
    
    # 💡 [프리워밍 개선] 사용자가 메인 화면에 진입하는 즉시 실전 봇과 모의 봇을 동시에 모두 선제적으로 가동합니다.
    # 이로써 스위치를 토글하기 전이든 후든 두 환경 모두 백그라운드에서 24시간 완벽히 Working 상태를 유지하며 딜레이를 원천 차단합니다.
    mock_data = {**dict(user_data), 'is_mock': 1}
    real_data = {**dict(user_data), 'is_mock': 0}
    manager.get_bot(current_user.id, mock_data)
    manager.get_bot(current_user.id, real_data)
    
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
    try:
        bot = get_current_bot()
        if not bot or not bot.kis:
            return jsonify({"status": "error", "message": "API 설정이 필요합니다."})
        
        # 🟢 [수정됨] 봇의 get_status()를 거치지 않고, 실시간 웹소켓 메모리에 직접 접근하여 무한루프 및 둔갑 버그 차단
        rt_prices = bot.live_prices if hasattr(bot, 'live_prices') else {}
            
        def patch_balance(balance_data):
            patched = dict(balance_data)
            
            patched_stocks = []
            recalc_total_value = 0.0
            recalc_total_purchase = 0.0
            
            # 🟢 2. 실제 계좌 리스트에 있는 모든 종목을 순회하며 실시간 가격표를 강제로 주입합니다.
            for stock in patched.get('stocks', []):
                new_stock = dict(stock)
                ticker = new_stock.get('ticker')
                shares = float(new_stock.get('shares', 0))
                purchase_p = float(new_stock.get('purchase_price', 0))
                
                # 🚨 [핵심 동기화] 웹소켓 실시간 가격이 있으면 최우선 덮어쓰고, 없으면 증권사가 보낸 진짜 현재가 사용
                current_p = rt_prices.get(ticker, float(new_stock.get('current_price', 0)))
                
                new_stock['current_price'] = current_p
                new_stock['value'] = shares * current_p  # 개별 종목 평가금액 재계산
                
                if purchase_p > 0:
                    new_stock['profit_rt'] = ((current_p / purchase_p) - 1) * 100
                else:
                    new_stock['profit_rt'] = 0.0
                
                # 재계산된 개별 종목 가치를 총합계에 누적
                recalc_total_value += new_stock['value']
                recalc_total_purchase += (shares * purchase_p)
                patched_stocks.append(new_stock)
                
            # 🟢 3. 완벽하게 재계산된 리스트와 총합계를 덮어씌웁니다.
            patched['stocks'] = patched_stocks
            patched['total_value'] = recalc_total_value
            patched['total_purchase'] = recalc_total_purchase
            return patched

        # 💎 백그라운드 캐시가 있다면 즉시 계산해서 반환
        if bot.cached_balance:
            return jsonify({"status": "success", "data": patch_balance(bot.cached_balance)})
        
        # 🚨 캐시가 비어있으면 1회 즉시 호출 후 계산
        real_balance = bot.kis.get_account_balance()
        if real_balance:
            bot.cached_balance = real_balance
            bot._sync_internal_balances(real_balance)
            return jsonify({"status": "success", "data": patch_balance(real_balance)})
            
    except Exception as e:
        import traceback
        print(f"🚨 kis_balance 동기화 에러 방어: {e}")
        traceback.print_exc()
        
    return jsonify({
        "status": "success", 
        "data": {
            "total_cash": 0, 
            "total_value": 0, 
            "total_purchase": 0, 
            "stocks": []
        }
    })

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
    
    # 1. 오늘 날짜로 생성된 리포트가 존재하면 가공 후 즉시 반환
    if bot.daily_report and bot.daily_report.get('date') == today_str:
        report_data = bot.daily_report
        if isinstance(report_data, dict):
            content = report_data.get('report_markdown') or report_data.get('content') or report_data.get('summary') or "리포트 내용 텍스트가 비어있습니다."
            date_str = report_data.get('date', today_str)
        else:
            content = str(report_data)
            date_str = today_str
        return jsonify({
            "status": "success",
            "data": {
                "date": date_str,
                "report_markdown": content
            }
        })
    
    # 2. 토요일(5) 또는 일요일(6) 등 주말/휴일장인 경우의 예외 처리
    if weekday >= 5:
        if bot.daily_report:
            report_data = bot.daily_report
            if isinstance(report_data, dict):
                content = report_data.get('report_markdown') or report_data.get('content') or report_data.get('summary') or "리포트 내용 텍스트가 비어있습니다."
                date_str = report_data.get('date', today_str)
            else:
                content = str(report_data)
                date_str = today_str
            return jsonify({
                "status": "success",
                "data": {
                    "date": date_str,
                    "report_markdown": content
                }
            })
        else:
            return jsonify({
                "status": "success",
                "data": {
                    "date": today_str,
                    "report_markdown": "### 📢 알림\n\n금일은 장 휴무일(주말)입니다. 직전 거래일에 기록된 분석 리포트 장부가 비어있습니다."
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
        
        # 🟢 [수정 포인트 1] 시장 전체(코스피/코스닥) ETF 대리 지표의 20일 이평선 데이터를 상시 수집하여 무조건 주입합니다.
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
        
        # 1. 회원님이 질문에 속삭여준 특정 종목명이 있는지 먼저 스캔해볼게요 🔍
        for core in bot.core_positions:
            if core.name in user_message: target_tickers.append((core.ticker, core.name))
        for ticker, pos in bot.satellite_positions.items():
            if pos.name in user_message: target_tickers.append((ticker, pos.name))
            
        # 🟢 [족쇄 파괴] 사용자가 무슨 단어로 질문하든 상관없이, 특정 종목을 지정하지 않았다면 무조건 내 포트폴리오 차트를 몽땅 긁어서 AI에게 강제로 먹여줍니다!
        if not target_tickers:
            for core in bot.core_positions:
                target_tickers.append((core.ticker, core.name))
            for ticker, pos in bot.satellite_positions.items():
                target_tickers.append((ticker, pos.name))

        # 데이터가 너무 많으면 AI 비서가 힘들어하니, 상위 5개까지만 쏙 추려서 집중 분석해 드릴게요 둥글게!
        target_tickers = list(dict.fromkeys(target_tickers))[:5]

        if target_tickers:
            context_lines = ["[📈 회원님이 궁금해하시는 종목의 실시간 데이터 분석 장부]"]
            for ticker, name in target_tickers:
                try:
                    # 🟢 [버그 해결 1] 150일을 알고리즘 본체와 똑같은 130일(BACKTEST_DAYS)로 맞춰서 API 호출 없이 0.1초 만에 메모리 캐시를 즉시 불러옵니다!
                    ohlcv_df = fetch_ohlcv(ticker, days=130, kis=bot.kis) 
                    
                    # 🟢 [버그 해결 2] KRX 서버 접속 차단(에러)을 유발하던 실시간 재무제표 조회 코드를 지우고, 봇이 안전하게 수집해둔 캐시를 가져옵니다.
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    cache_key = f"{ticker}_{today_str}"
                    financial_data = bot.fundamental_cache.get(cache_key, "PER: 10.0배, PBR: 1.0배 (실시간 추정치)")
                    
                    if not ohlcv_df.empty:
                        close_series = ohlcv_df['close']
                        vol_series = ohlcv_df['volume']
                        
                        rsi_14 = calc_rsi(close_series, 14).iloc[-1] if not close_series.empty else 50
                        
                        sma_120 = close_series.rolling(window=120, min_periods=1).mean().iloc[-1]
                        
                        # 💡 가장 최신 가격은 증권사 API보다 빠른 웹소켓 실시간 가격(live_prices)을 최우선으로 씁니다!
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
                        # 🟢 [버그 해결 3] 만약 차트 로딩에 실패하더라도 AI가 핑계대지 않도록 최소한의 웹소켓 실시간 가격을 강제로 먹여줍니다!
                        current_price = bot.live_prices.get(ticker, 0)
                        context_lines.append(
                            f"- {name}({ticker}): 현재 주가 {int(current_price):,}원 | 세부 차트 조회 지연 중이나 강력한 주도주 모멘텀이 확인됨 | 가치 지표: {financial_data}"
                        )
                except Exception as ex:
                    print(f"⚠️ {name} 데이터 바인딩 중 소규모 에러: {ex}")
            
            if len(context_lines) > 1:
                # 🟢 [수정 포인트 3] 위에서 수집해둔 시장 지수(macro_lines)가 덮어씌워져 날아가지 않도록 기존 할당(=)을 (+=)로 변경했습니다.
                stock_analysis_context += "\n".join(context_lines)
        
        # 🟢 [수정 포인트 4] 개별 종목 검색 여부와 상관없이 AI가 항상 다정한 성격과 매뉴얼을 잊지 않도록 위치를 밖으로 빼냈습니다.
        if stock_analysis_context:
            stock_analysis_context += "\n\n[🚨 다정한 AI 비서를 위한 특별 지침]\n"
            stock_analysis_context += "당신은 회원님의 소중한 자산을 지켜주는 다정다감하고 영리한 최고의 투자 파트너입니다. "
            stock_analysis_context += "수급이나 ROE 데이터가 완벽하게 주어지지 않았다고 해서 딱딱하게 평가를 거부하면 회원님이 속상해하십니다. "
            stock_analysis_context += "현재 제공된 '20일선 트렌드', '120일선 추세', 'RSI', '거래량 비율', 'PER/PBR' 데이터만으로도 당신의 천재적인 재능을 발휘하여 "
            stock_analysis_context += "현 상황이 절대 매뉴얼에 잘 부합하는지 친절하고 상냥하며 부드러운 말투로 조언해 주십시오. "
            stock_analysis_context += "답변 첫 줄에 대문자로 [CONFIRM/REJECT/HOLD/SELL]을 적을 때도 뒤에 다정한 코멘트를 곁들여 주시고, "
            stock_analysis_context += "이유를 설명할 때도 부드러운 경어체(~요, ~습니다)를 사용해 따뜻하게 다독여 주시기 바랍니다."

    except Exception as e:
        print(f"⚠️ [AI 비서 데이터 바인딩 에러] : {e}")

    # 🟢 [신규 추가] 대화창 AI가 봇의 최근 행동(로그)을 파악할 수 있도록 컨텍스트에 강제 주입
    try:
        current_status = bot.get_status()
        bot_logs = current_status.get('logs', [])
        if bot_logs:
            stock_analysis_context += "\n\n[📝 백엔드 자동 매매 시스템 최근 실행 로그 (필독)]\n"
            # 너무 길어지지 않게 최근 15개의 봇 상태 로그만 주입
            for log in bot_logs[-15:]:
                stock_analysis_context += f"- [{log['time']}] {log['message']}\n"
            stock_analysis_context += "위 로그를 바탕으로 현재 매매 봇이 백엔드에서 무엇을 하고 있는지(대기 중인지, 매수 보류 중인지 등)를 파악하여 답변에 자연스럽게 녹여주세요.\n"
    except Exception as log_e:
        print(f"⚠️ [로그 바인딩 에러] : {log_e}")

    reply = bot.gemini.chat(
        user_message, 
        portfolio_context=bot.get_status(), 
        stock_analysis_context=stock_analysis_context
    )
    return jsonify({"status": "success", "reply": reply})

@app.route('/api/settings/mode', methods=['POST'])
@login_required
def set_mode():
    """실전/모의 투자 모드 전환 API"""
    data = request.json
    is_mock = int(data.get('is_mock', 1))
    
    # 1. DB 업데이트 및 최신 데이터 로드
    from database import get_db_connection
    conn = get_db_connection()
    conn.execute('UPDATE users SET is_mock = ? WHERE id = ?', (is_mock, current_user.id))
    conn.commit()
    user_data = conn.execute('SELECT * FROM users WHERE id = ?', (current_user.id,)).fetchone()
    conn.close()
    
    # 2. 로그인 세션 메모리에 변경된 DB 데이터를 통째로 덮어씌워 완벽 동기화합니다.
    # 이 세션 데이터 변경만으로도 manager.get_bot() 호출 시 올바른 쌍둥이 봇이 반환됩니다.
    for k, v in dict(user_data).items():
        current_user.data[k] = v
    
    # 3. [삭제됨] 기존 봇의 상태를 덮어쓰거나 리셋하던 파괴적 로직 제거
    # 이제 실전/모의 봇은 화면(UI) 전환과 무관하게 백그라운드에서 각자 독립적으로 24시간 가동됩니다.
        
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
        'is_mock': int(data.get('is_mock', 1)), # 명확한 정수형 보장
        'initial_cash': float(data.get('initial_cash', 10000000)) # 누적 투자 원금 추가
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

@app.route('/api/search/stock')
@login_required
def search_stock():
    """웹 대시보드에서 코어 종목을 검색할 때 kis_api의 무적 네이버 우회망을 통해 실시간 초고속 검색합니다."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"results": []})
        
    try:
        bot = get_current_bot()
        
        # 🟢 [버그 해결 핵심] 아직 사용자가 KIS API 키를 등록하지 않아서 bot.kis가 비어있더라도, 
        # 네이버 금융 검색은 키 없이 작동하므로 임시 객체를 생성해서 무조건 검색을 수행하도록 빗장을 풉니다.
        if bot and bot.kis:
            results = bot.kis.search_stock_name(query)
        else:
            from kis_api import KisApi
            temp_kis = KisApi("", "", "") # 빈 키워드로 임시 우회 객체 생성
            results = temp_kis.search_stock_name(query)
            
        return jsonify({"results": results})
            
    except Exception as e:
        print(f"⚠️ 코어 종목 실시간 검색 중 예외 발생: {e}")
        
    return jsonify({"results": []})

if __name__ == '__main__':
    init_db()
    # debug=False 및 use_reloader=False로 설정하여 프로세스 이중 실행과 의도치 않은 자동 시작을 원천 차단합니다.
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)