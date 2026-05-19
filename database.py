import sqlite3
import os
import json
import threading
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'lassi.db')

# 🔒 DB 쓰기 충돌 방지를 위한 전역 락
db_lock = threading.Lock()

def get_db_connection():
    """데이터베이스 연결 객체를 생성하고 고성능 병렬 처리(WAL) 모드를 활성화합니다."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.row_factory = sqlite3.Row
    
    # ⚡ [핵심 안정화] WAL 모드 활성화: 읽기와 쓰기가 동시에 가능해져 database is locked 에러 원천 차단
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    return conn

def init_db():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            real_app_key TEXT, real_app_secret TEXT, real_account_no TEXT,
            mock_app_key TEXT, mock_app_secret TEXT, mock_account_no TEXT,
            kis_app_key TEXT, kis_app_secret TEXT, kis_account_no TEXT,
            telegram_token TEXT, telegram_chat_id TEXT, gemini_api_key TEXT,
            initial_cash REAL DEFAULT 10000000, is_running INTEGER DEFAULT 0,
            is_mock INTEGER DEFAULT 1, core_stocks TEXT
        )
        ''')

        new_columns = [
            ('real_app_key', 'TEXT'), ('real_app_secret', 'TEXT'), ('real_account_no', 'TEXT'),
            ('mock_app_key', 'TEXT'), ('mock_app_secret', 'TEXT'), ('mock_account_no', 'TEXT'),
            ('gemini_api_key', 'TEXT'), ('claude_api_key', 'TEXT'),
            ('is_running', 'INTEGER DEFAULT 0'),
            ('core_stocks', 'TEXT'), ('is_mock', 'INTEGER DEFAULT 1'),
            ('real_initial_cash', 'REAL DEFAULT 10000000'), ('mock_initial_cash', 'REAL DEFAULT 10000000')
        ]
        for col_name, col_type in new_columns:
            try:
                cursor.execute(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}')
            except sqlite3.OperationalError:
                pass 

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS bot_states (
            user_id INTEGER, is_mock INTEGER, state_json TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, is_mock),
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
        ''')

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            ticker TEXT, stock_name TEXT, action TEXT, price REAL,
            strategy TEXT, ai_reason TEXT, profit REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_rules (
            user_id INTEGER PRIMARY KEY, rule_text TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        
        cursor.execute('UPDATE users SET is_running = 0')
        conn.commit()
        conn.close()

def add_user(username, password):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        password_hash = generate_password_hash(password)
        try:
            cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (username, password_hash))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()

def verify_user(username, password):
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    if user and check_password_hash(user['password_hash'], password):
        return dict(user)
    return None

def update_user_keys(user_id, keys_dict):
    with db_lock:
        conn = get_db_connection()
        
        # 🚨 [장부 완벽 분리] 사용자가 화면에서 수정한 원금을 실전/모의 모드에 맞춰 각각 독립된 장부에 정확히 꽂아넣습니다.
        is_mock = keys_dict.get('is_mock', 1)
        initial_cash = keys_dict.get('initial_cash', 10000000)
        cash_col = "mock_initial_cash" if is_mock else "real_initial_cash"
        
        conn.execute(f'''
            UPDATE users SET real_app_key = ?, real_app_secret = ?, real_account_no = ?,
                mock_app_key = ?, mock_app_secret = ?, mock_account_no = ?,
                telegram_token = ?, telegram_chat_id = ?,
                claude_api_key = ?,
                core_stocks = ?, is_mock = ?, initial_cash = ?, {cash_col} = ? WHERE id = ?
        ''', (
            keys_dict.get('real_app_key'), keys_dict.get('real_app_secret'), keys_dict.get('real_account_no'),
            keys_dict.get('mock_app_key'), keys_dict.get('mock_app_secret'), keys_dict.get('mock_account_no'),
            keys_dict.get('telegram_token'), keys_dict.get('telegram_chat_id'),
            keys_dict.get('claude_api_key'),
            keys_dict.get('core_stocks'), is_mock, initial_cash, initial_cash, user_id
        ))
        conn.commit()
        conn.close()

def update_bot_status(user_id, is_running):
    with db_lock:
        conn = get_db_connection()
        conn.execute('UPDATE users SET is_running = ? WHERE id = ?', (1 if is_running else 0, user_id))
        conn.commit()
        conn.close()

def save_portfolio_state(user_id, state, is_mock):
    with db_lock:
        mode = 1 if is_mock else 0
        conn = get_db_connection()
        conn.execute('''
            INSERT OR REPLACE INTO bot_states (user_id, is_mock, state_json, last_updated) 
            VALUES (?, ?, ?, ?)
        ''', (user_id, mode, json.dumps(state, ensure_ascii=False), datetime.now()))
        conn.commit()
        conn.close()

def load_portfolio_state(user_id, is_mock):
    mode = 1 if is_mock else 0
    conn = get_db_connection()
    row = conn.execute('SELECT state_json FROM bot_states WHERE user_id = ? AND is_mock = ?', (user_id, mode)).fetchone()
    conn.close()
    if row and row['state_json']:
        return json.loads(row['state_json'])
    return None

def log_trade_journal(user_id, ticker, stock_name, action, price, strategy, ai_reason, profit=0):
    with db_lock:
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO trade_journal (user_id, ticker, stock_name, action, price, strategy, ai_reason, profit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, ticker, stock_name, action, price, strategy, ai_reason, profit))
        conn.commit()
        conn.close()

def get_recent_trades(user_id, ticker, limit=5):
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT action, price, ai_reason, profit, date(created_at) as date
        FROM trade_journal WHERE user_id = ? AND ticker = ? 
        ORDER BY created_at DESC LIMIT ?
    ''', (user_id, ticker, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_ai_rules(user_id, rule_text):
    with db_lock:
        conn = get_db_connection()
        conn.execute('''
            INSERT OR REPLACE INTO ai_rules (user_id, rule_text, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        ''', (user_id, rule_text))
        conn.commit()
        conn.close()

def load_ai_rules(user_id):
    conn = get_db_connection()
    row = conn.execute('SELECT rule_text FROM ai_rules WHERE user_id = ?', (user_id,)).fetchone()
    conn.close()
    return row['rule_text'] if row else ""

# 🟢 [리팩토링] BaseBot에서 SQL을 직접 다루지 않도록 Repository 함수들을 신규 추가합니다.
def get_user_initial_cash(user_id, is_mock):
    """현재 모드(실전/모의)에 맞는 원금 장부를 조회합니다."""
    conn = get_db_connection()
    cash_col = "mock_initial_cash" if is_mock else "real_initial_cash"
    row = conn.execute(f'SELECT {cash_col} FROM users WHERE id = ?', (user_id,)).fetchone()
    conn.close()
    return float(row[cash_col]) if row and row[cash_col] is not None else 10000000.0

def set_user_initial_cash(user_id, pure_principal, is_mock):
    """최초 투자 원금을 세팅하여 장부를 잠급니다."""
    with db_lock:
        conn = get_db_connection()
        cash_col = "mock_initial_cash" if is_mock else "real_initial_cash"
        conn.execute(f'UPDATE users SET {cash_col} = ? WHERE id = ?', (pure_principal, user_id))
        conn.commit()
        conn.close()

def add_user_initial_cash(user_id, deposit_delta, is_mock):
    """외부 입출금 발생 시 해당 모드의 장부 원금을 깔끔하게 증감시킵니다."""
    with db_lock:
        conn = get_db_connection()
        cash_col = "mock_initial_cash" if is_mock else "real_initial_cash"
        conn.execute(f'UPDATE users SET {cash_col} = {cash_col} + ? WHERE id = ?', (deposit_delta, user_id))
        conn.commit()
        conn.close()

def init_default_ai_rules(user_id: int):
    """
    사용자의 AI 규칙이 비어 있을 때 실전 검증 매매 원칙을 기본값으로 저장합니다.
    (출처: 실전 트레이더 원칙모음.zip)
    """
    existing = load_ai_rules(user_id)
    if existing and len(existing.strip()) > 50:
        return  # 이미 규칙이 있으면 덮어쓰지 않음

    DEFAULT_RULES = """[📋 실전 검증 매매 원칙 — 딥러닝 학습 완료]

【최우선 금지 원칙】
1. 이슈 기대 베팅 금지: "파업 해결 기대", "실적 좋을 것 같다" 등 이슈 기대만으로 매수/보유 금지.
   → 매수 근거 = 실제 결과 + 가격 회복 + 거래량 증가 + 상대강도 중 2개 이상 동시 충족 필수.

2. 데이터 게이트: 아래 6개 중 2개 미만이면 강한 판정 금지(REJECT 권고)
   ① 전일종가/시초가 회복  ② 거래량/거래대금 증가  ③ 지수 대비 상대강도 우위
   ④ 볼린저밴드 중심선 이상  ⑤ 5일 이동평균선 위  ⑥ 외국인/기관 수급 동반

3. 폐기 이론 재사용 금지:
   - "09:15 회복 기대 단독" → 폐기 (D0 성공률 8.2%)
   - "전일 외국인 매수 단독 지속 가정" → 폐기 (이벤트 시즌 반복 실패)

【매수 품질 기준】
- 진입 전 무효화 조건 반드시 설정. 무효화 조건 없는 매수 = 희망 매매 → 거절.
- "좋은 종목이니까 결국 오른다" 단독 = thesis가 아니라 희망 → 거절.
- 익절 후 새 thesis/진입조건/무효화조건 없이 즉시 재매수 → 거절.
- 순환매 진입: 대장주+후행주 동반 상승 + 거래대금 증가 + 상대강도 중 2개 이상 확인 후 진입.

【매도/손절 원칙】
- 손절선 하향 조정 절대 금지: 손절선 이탈 시 즉시 계획대로 실행.
- 무효화 기회는 최대 2번: 1차 무효화→재확인 1회, 2차 무효화→종료.
- 분할 매도 기본값: 30% → 30% → 40% 순서.
- 5분봉 MA5 이탈 + 2봉 회복 실패 + 고점 미달 → 30% 축소 실행.
- 상승분 50~70% 반납 + MA5 이탈 → 강한 익절/손절.

【재진입 조건 (2개 이상 충족 시만)】
매도가 회복 / MA5 재탈환+다음봉 저점 유지 / 거래량 회복 / 매도 사유 해소 / 대장주 동조

【포지션 사이징】
- 기본 비중 500단위(75% 1차 + 25% 눌림목 대기)
- 살 게 없으면(후보 3개 미만): 현금 유지. 억지 매수 금지.
- 보합장/하락장/불확실장: 현금 자체가 포지션."""

    save_ai_rules(user_id, DEFAULT_RULES)


if __name__ == '__main__':
    init_db()
    print("Database initialized with WAL mode and Thread Locks.")