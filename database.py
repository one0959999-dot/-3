import sqlite3
import os
import json
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

# 💡 [버그 해결] 서비스(systemd)로 실행할 때 경로가 꼬이지 않도록, 현재 파일이 있는 폴더의 '절대 경로'를 강제 지정합니다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'lassi.db')

def get_db_connection():
    # 💡 단순히 'lassi.db'가 아닌 절대 경로(DB_PATH)를 바라보도록 수정합니다.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 1. 사용자 테이블 (users) 생성
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        
        real_app_key TEXT,
        real_app_secret TEXT,
        real_account_no TEXT,
        
        mock_app_key TEXT,
        mock_app_secret TEXT,
        mock_account_no TEXT,
        
        kis_app_key TEXT,
        kis_app_secret TEXT,
        kis_account_no TEXT,
        
        telegram_token TEXT,
        telegram_chat_id TEXT,
        gemini_api_key TEXT,
        initial_cash REAL DEFAULT 10000000,
        is_running INTEGER DEFAULT 0,
        is_mock INTEGER DEFAULT 1,
        core_stocks TEXT
    )
    ''')

    # 2. 기존 사용자를 위한 컬럼 추가 (ALTER TABLE)
    new_columns = [
        ('real_app_key', 'TEXT'), ('real_app_secret', 'TEXT'), ('real_account_no', 'TEXT'),
        ('mock_app_key', 'TEXT'), ('mock_app_secret', 'TEXT'), ('mock_account_no', 'TEXT'),
        ('gemini_api_key', 'TEXT'), ('is_running', 'INTEGER DEFAULT 0'), 
        ('core_stocks', 'TEXT'), ('is_mock', 'INTEGER DEFAULT 1')
    ]
    for col_name, col_type in new_columns:
        try:
            cursor.execute(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}')
        except sqlite3.OperationalError:
            pass 

    # 3. 봇 상태 테이블 (bot_states) - 실전/모의 장부 분리
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS bot_states (
        user_id INTEGER,
        is_mock INTEGER, 
        state_json TEXT,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, is_mock),
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    ''')

    # 4. 자가 학습용 매매 일지 및 AI 룰 테이블
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trade_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        ticker TEXT,
        stock_name TEXT,
        action TEXT,
        price REAL,
        strategy TEXT,
        ai_reason TEXT,
        profit REAL DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS ai_rules (
        user_id INTEGER PRIMARY KEY,
        rule_text TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # 💡 [버그 해결] 중복으로 들어가 있던 연결 종료(close) 구문을 삭제하고 한 번만 실행되도록 정리했습니다.
    cursor.execute('UPDATE users SET is_running = 0')
    
    conn.commit()
    conn.close()

def add_user(username, password):
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
    cursor = conn.cursor()
    user = cursor.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()
    
    if user and check_password_hash(user['password_hash'], password):
        return dict(user)
    return None

def update_user_keys(user_id, keys_dict):
    """모든 API 키(실전/모의 포함)를 업데이트합니다."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE users 
        SET real_app_key = ?, real_app_secret = ?, real_account_no = ?,
            mock_app_key = ?, mock_app_secret = ?, mock_account_no = ?,
            telegram_token = ?, telegram_chat_id = ?, gemini_api_key = ?, 
            core_stocks = ?, is_mock = ?
        WHERE id = ?
    ''', (
        keys_dict.get('real_app_key'),
        keys_dict.get('real_app_secret'),
        keys_dict.get('real_account_no'),
        keys_dict.get('mock_app_key'),
        keys_dict.get('mock_app_secret'),
        keys_dict.get('mock_account_no'),
        keys_dict.get('telegram_token'),
        keys_dict.get('telegram_chat_id'),
        keys_dict.get('gemini_api_key'),
        keys_dict.get('core_stocks'),
        keys_dict.get('is_mock', 1),
        user_id
    ))
    conn.commit()
    conn.close()

def update_bot_status(user_id, is_running):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('UPDATE users SET is_running = ? WHERE id = ?', (1 if is_running else 0, user_id))
    conn.commit()
    conn.close()

def save_portfolio_state(user_id, state, is_mock):
    """실전/모의투자 모드에 맞춰 상태를 분리 저장합니다."""
    mode = 1 if is_mock else 0
    conn = get_db_connection()
    conn.execute('''
        INSERT OR REPLACE INTO bot_states (user_id, is_mock, state_json, last_updated) 
        VALUES (?, ?, ?, ?)
    ''', (user_id, mode, json.dumps(state, ensure_ascii=False), datetime.now()))
    conn.commit()
    conn.close()

def load_portfolio_state(user_id, is_mock):
    """실전/모의투자 모드에 맞춰 상태를 불러옵니다."""
    mode = 1 if is_mock else 0
    conn = get_db_connection()
    # 💡 '일' 이라는 오타를 삭제하여 SQL 에러를 방지했습니다.
    row = conn.execute('SELECT state_json FROM bot_states WHERE user_id = ? AND is_mock = ?', 
                       (user_id, mode)).fetchone()
    conn.close()
    if row and row['state_json']:
        return json.loads(row['state_json'])
    return None

# 🟢 [여기에 새로 추가] AI 자가 학습용 DB 헬퍼 함수 🟢
def log_trade_journal(user_id, ticker, stock_name, action, price, strategy, ai_reason, profit=0):
    conn = get_db_connection()
    conn.execute('''
        INSERT INTO trade_journal (user_id, ticker, stock_name, action, price, strategy, ai_reason, profit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, ticker, stock_name, action, price, strategy, ai_reason, profit))
    conn.commit()
    conn.close()

def get_recent_trades(user_id, ticker, limit=5):
    """해당 종목의 최근 AI 매매 기록(오답 노트)을 불러옵니다."""
    conn = get_db_connection()
    rows = conn.execute('''
        SELECT action, price, ai_reason, profit, date(created_at) as date
        FROM trade_journal 
        WHERE user_id = ? AND ticker = ? 
        ORDER BY created_at DESC LIMIT ?
    ''', (user_id, ticker, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_ai_rules(user_id, rule_text):
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

if __name__ == '__main__':
    init_db()
    print("Database initialized.")