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
            ('gemini_api_key', 'TEXT'), ('is_running', 'INTEGER DEFAULT 0'), 
            ('core_stocks', 'TEXT'), ('is_mock', 'INTEGER DEFAULT 1')
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
        conn.execute('''
            UPDATE users SET real_app_key = ?, real_app_secret = ?, real_account_no = ?,
                mock_app_key = ?, mock_app_secret = ?, mock_account_no = ?,
                telegram_token = ?, telegram_chat_id = ?, gemini_api_key = ?, 
                core_stocks = ?, is_mock = ? WHERE id = ?
        ''', (
            keys_dict.get('real_app_key'), keys_dict.get('real_app_secret'), keys_dict.get('real_account_no'),
            keys_dict.get('mock_app_key'), keys_dict.get('mock_app_secret'), keys_dict.get('mock_account_no'),
            keys_dict.get('telegram_token'), keys_dict.get('telegram_chat_id'), keys_dict.get('gemini_api_key'),
            keys_dict.get('core_stocks'), keys_dict.get('is_mock', 1), user_id
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

if __name__ == '__main__':
    init_db()
    print("Database initialized with WAL mode and Thread Locks.")