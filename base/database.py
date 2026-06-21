import sqlite3
import os
import json
import threading
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, 'lassi.db')

db_lock = threading.Lock()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=20.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')
    conn.execute('PRAGMA synchronous=NORMAL;')
    conn.execute('PRAGMA busy_timeout=5000;')
    return conn

def init_db():
    with db_lock:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            real_app_key TEXT, real_app_secret TEXT, real_account_no TEXT,
            us_app_key TEXT, us_app_secret TEXT, us_account_no TEXT,
            telegram_token TEXT, telegram_chat_id TEXT, gemini_api_key TEXT,
            initial_cash REAL DEFAULT 10000000, is_running INTEGER DEFAULT 0,
            is_mock INTEGER DEFAULT 1, core_stocks TEXT
        )
        ''')

            new_columns = [
                ('real_app_key', 'TEXT'), ('real_app_secret', 'TEXT'), ('real_account_no', 'TEXT'),
                ('us_app_key', 'TEXT'), ('us_app_secret', 'TEXT'), ('us_account_no', 'TEXT'),
                ('gemini_api_key', 'TEXT'), ('claude_api_key', 'TEXT'),
                ('openai_api_key', 'TEXT'), ('grok_api_key', 'TEXT'),
                ('trade_ai_provider', 'TEXT DEFAULT "claude"'),
                ('trade_ai_key', 'TEXT'),
                ('backtest_ai_provider', 'TEXT DEFAULT "gemini"'),
                ('backtest_ai_key', 'TEXT'),
                ('is_running', 'INTEGER DEFAULT 0'),
                ('core_stocks', 'TEXT'), ('is_mock', 'INTEGER DEFAULT 1'),
                ('real_initial_cash', 'REAL DEFAULT 10000000'), ('us_initial_cash', 'REAL DEFAULT 10000000'),
                ('initial_cash_captured_at', 'TEXT'),
                ('dart_api_key', 'TEXT'), ('naver_client_id', 'TEXT'), ('naver_client_secret', 'TEXT'),
                ('sector_guide', 'TEXT'),
                ('toss_client_id', 'TEXT'),
                ('toss_client_secret', 'TEXT'),
                ('toss_account_seq', 'TEXT'),
                ('perplexity_api_key', 'TEXT'),
                ('fred_api_key', 'TEXT'),
            ]
            for col_name, col_type in new_columns:
                try:
                    cursor.execute(f'ALTER TABLE users ADD COLUMN {col_name} {col_type}')
                except sqlite3.OperationalError:
                    pass

            legacy_copies = [
                ('mock_app_key',     'us_app_key'),
                ('mock_app_secret',  'us_app_secret'),
                ('mock_account_no',  'us_account_no'),
                ('mock_initial_cash','us_initial_cash'),
                ('real_app_key',  'toss_client_id'),
                ('real_app_secret','toss_client_secret'),
                ('real_account_no','toss_account_seq'),
            ]
            existing_cols = {r[1] for r in cursor.execute('PRAGMA table_info(users)').fetchall()}
            for src, dst in legacy_copies:
                if src in existing_cols and dst in existing_cols:
                    cursor.execute(f'''
                        UPDATE users SET {dst} = {src}
                        WHERE ({dst} IS NULL OR {dst} = "") AND {src} IS NOT NULL AND {src} != ""
                    ''')

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
            shares REAL DEFAULT 0, mode TEXT DEFAULT 'KR',
            strategy TEXT, ai_reason TEXT, profit REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
            _tj_cols = {r[1] for r in cursor.execute('PRAGMA table_info(trade_journal)').fetchall()}
            for _col, _typ in [('shares', 'REAL DEFAULT 0'), ('mode', "TEXT DEFAULT 'KR'")]:
                if _col not in _tj_cols:
                    try:
                        cursor.execute(f'ALTER TABLE trade_journal ADD COLUMN {_col} {_typ}')
                    except sqlite3.OperationalError:
                        pass

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_rules (
            user_id INTEGER PRIMARY KEY, rule_text TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_rules_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            rule_text TEXT,
            trigger_type TEXT DEFAULT 'manual',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_history (
            user_id INTEGER,
            is_mock  INTEGER,
            messages TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, is_mock)
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_decision_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            mode TEXT DEFAULT 'KR',
            session_type TEXT DEFAULT 'live',
            ticker TEXT,
            stock_name TEXT,
            signal TEXT,
            ai_decision TEXT,
            confidence INTEGER DEFAULT 75,
            ai_reason TEXT,
            input_context TEXT,
            portfolio_snapshot TEXT,
            market_regime TEXT,
            strategy TEXT,
            price REAL,
            outcome_price REAL,
            outcome_pnl REAL,
            outcome_pnl_pct REAL,
            outcome_days INTEGER,
            outcome_updated_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS backtest_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT DEFAULT 'KR',
            ticker TEXT,
            stock_name TEXT,
            last_date TEXT,
            total_scenarios INTEGER DEFAULT 0,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(mode, ticker)
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS macro_daily_snapshot (
            date TEXT PRIMARY KEY,
            kospi_close REAL, kospi_vs_ma200 REAL, kospi_52w_pct REAL,
            sp500_chg REAL, nasdaq_chg REAL, nikkei_chg REAL, shanghai_chg REAL,
            vix REAL, dxy REAL, usd_krw REAL,
            wti REAL, gold REAL, copper REAL, sox_chg REAL,
            us_rate REAL, kr_rate REAL, us_10y REAL, us_2y REAL, yield_spread REAL,
            foreign_net_buy REAL, institution_net_buy REAL,
            is_fomc_week INTEGER DEFAULT 0, is_cpi_week INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS backtest_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            mode TEXT DEFAULT 'KR',
            ticker TEXT, stock_name TEXT,
            trade_date TEXT,
            signal TEXT,
            signal_type TEXT,
            price REAL,
            ai_decision TEXT, confidence INTEGER, ai_reason TEXT,
            macro_date TEXT,
            rsi REAL, macd REAL, macd_signal REAL,
            bb_upper REAL, bb_mid REAL, bb_lower REAL,
            sma5 REAL, sma20 REAL, sma60 REAL, sma120 REAL,
            vol_ratio REAL,
            support REAL, resistance REAL,
            fib_382 REAL, fib_500 REAL, fib_618 REAL,
            min_price_5d REAL, max_price_5d REAL, days_to_min_5d INTEGER, days_to_max_5d INTEGER,
            min_price_20d REAL, max_price_20d REAL, days_to_min_20d INTEGER, days_to_max_20d INTEGER,
            pnl_5d REAL, pnl_20d REAL,
            optimal_buy_zone TEXT, optimal_sell_zone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS backtest_full_progress (
            mode TEXT, ticker TEXT,
            last_processed_date TEXT,
            total_signals INTEGER DEFAULT 0,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (mode, ticker)
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS backtest_optimal_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            mode TEXT DEFAULT 'KR',
            ticker TEXT, stock_name TEXT,
            date TEXT,
            point_type TEXT,
            price REAL,
            magnitude_pct REAL,
            rsi REAL, macd REAL, macd_signal REAL,
            bb_upper REAL, bb_mid REAL, bb_lower REAL,
            sma5 REAL, sma20 REAL, sma60 REAL, sma120 REAL,
            vol_ratio REAL,
            support REAL, resistance REAL,
            fib_382 REAL, fib_500 REAL, fib_618 REAL,
            signals_active TEXT,
            signal_count INTEGER DEFAULT 0,
            macro_date TEXT,
            market_phase TEXT,
            market_phase_kr TEXT,
            phase_confidence REAL,
            ai_analysis TEXT,
            pnl_5d REAL, pnl_20d REAL, pnl_60d REAL,
            max_gain_60d REAL, max_loss_60d REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS phase_strategy_stats (
            mode TEXT, market_phase TEXT, signal_type TEXT,
            total INTEGER DEFAULT 0,
            win_20d INTEGER DEFAULT 0,
            avg_pnl_20d REAL DEFAULT 0,
            avg_max_gain_60d REAL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (mode, market_phase, signal_type)
        )
        ''')

            cursor.execute('''
        CREATE TABLE IF NOT EXISTS backtest_trade_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            mode TEXT,
            ticker TEXT,
            stock_name TEXT,
            trade_date TEXT,
            signal_types TEXT,
            signal_direction TEXT,
            price REAL,
            rsi REAL, macd REAL, macd_signal REAL,
            bb_upper REAL, bb_mid REAL, bb_lower REAL,
            sma5 REAL, sma20 REAL, sma60 REAL, sma120 REAL,
            vol_ratio REAL,
            support REAL, resistance REAL,
            market_phase TEXT,
            market_phase_kr TEXT,
            phase_confidence REAL,
            macro_str TEXT,
            vix REAL, usd_krw REAL, us_10y REAL, kr_rate REAL,
            days_to_peak INTEGER,
            max_gain_pct REAL,
            days_to_max_drawdown INTEGER,
            max_drawdown_pct REAL,
            days_to_recovery INTEGER,
            price_path_json TEXT,
            sector TEXT,
            ai_analysis TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

            cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_bts_mode_phase
            ON backtest_trade_signals(mode, market_phase, signal_direction)
        ''')

            cursor.execute('UPDATE users SET is_running = 0')
            conn.commit()
        finally:
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
    try:
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    finally:
        conn.close()
    if user and check_password_hash(user['password_hash'], password):
        return dict(user)
    return None

def update_user_keys(user_id, keys_dict):
    with db_lock:
        conn = get_db_connection()
        try:
            is_mock = keys_dict.get('is_mock', 1)

            existing = conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
            if not existing:
                return

            def _pick(key):
                v = keys_dict.get(key)
                if v is None or (isinstance(v, str) and v.strip() == ''):
                    return existing[key]
                return v

            conn.execute('''
                UPDATE users SET
                    toss_client_id = ?, toss_client_secret = ?, toss_account_seq = ?,
                    real_app_key = ?, real_app_secret = ?, real_account_no = ?,
                    us_app_key = ?, us_app_secret = ?, us_account_no = ?,
                    telegram_token = ?, telegram_chat_id = ?,
                    claude_api_key = ?, gemini_api_key = ?,
                    openai_api_key = ?, grok_api_key = ?,
                    trade_ai_provider = ?, trade_ai_key = ?,
                    backtest_ai_provider = ?, backtest_ai_key = ?,
                    core_stocks = ?, us_core_stocks = ?, is_mock = ? WHERE id = ?
            ''', (
                _pick('toss_client_id'), _pick('toss_client_secret'), _pick('toss_account_seq'),
                _pick('real_app_key'), _pick('real_app_secret'), _pick('real_account_no'),
                _pick('us_app_key'), _pick('us_app_secret'), _pick('us_account_no'),
                _pick('telegram_token'), _pick('telegram_chat_id'),
                _pick('claude_api_key'), _pick('gemini_api_key'),
                _pick('openai_api_key'), _pick('grok_api_key'),
                _pick('trade_ai_provider'), _pick('trade_ai_key'),
                _pick('backtest_ai_provider'), _pick('backtest_ai_key'),
                _pick('core_stocks'), _pick('us_core_stocks'), is_mock,
                user_id
            ))
            conn.commit()
        finally:
            conn.close()

def set_user_core_stocks(user_id: int, stocks: list):
    import json as _json
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                "UPDATE users SET core_stocks = ? WHERE id = ?",
                (_json.dumps(stocks, ensure_ascii=False), user_id)
            )
            conn.commit()
        finally:
            conn.close()

def _ensure_extra_stock_columns(conn):
    for col in ['satellite_stocks', 'us_core_stocks', 'us_satellite_stocks']:
        try:
            conn.execute(f'ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT NULL')
            conn.commit()
        except Exception:
            pass

def set_user_satellite_stocks(user_id: int, stocks: list, is_us: bool = False):
    import json as _json
    col = 'us_satellite_stocks' if is_us else 'satellite_stocks'
    with db_lock:
        conn = get_db_connection()
        try:
            _ensure_extra_stock_columns(conn)
            conn.execute(f"UPDATE users SET {col} = ? WHERE id = ?",
                         (_json.dumps(stocks, ensure_ascii=False), user_id))
            conn.commit()
        finally:
            conn.close()

def set_us_core_stocks(user_id: int, stocks: list):
    import json as _json
    with db_lock:
        conn = get_db_connection()
        try:
            _ensure_extra_stock_columns(conn)
            conn.execute("UPDATE users SET us_core_stocks = ? WHERE id = ?",
                         (_json.dumps(stocks, ensure_ascii=False), user_id))
            conn.commit()
        finally:
            conn.close()

def update_bot_status(user_id, is_running, is_mock=None):
    with db_lock:
        conn = get_db_connection()
        for col in [('us_running', 'INTEGER DEFAULT 0'), ('real_running', 'INTEGER DEFAULT 0')]:
            try:
                conn.execute(f'ALTER TABLE users ADD COLUMN {col[0]} {col[1]}')
                conn.commit()
            except Exception:
                pass
        val = 1 if is_running else 0
        try:
            conn.execute('UPDATE users SET is_running = ? WHERE id = ?', (val, user_id))
            if is_mock is True:
                conn.execute('UPDATE users SET us_running = ? WHERE id = ?', (val, user_id))
            elif is_mock is False:
                conn.execute('UPDATE users SET real_running = ? WHERE id = ?', (val, user_id))
            conn.commit()
        finally:
            conn.close()

def save_portfolio_state(user_id, state, is_mock):
    with db_lock:
        mode = 1 if is_mock else 0
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT OR REPLACE INTO bot_states (user_id, is_mock, state_json, last_updated)
                VALUES (?, ?, ?, ?)
            ''', (user_id, mode, json.dumps(state, ensure_ascii=False), datetime.now()))
            conn.commit()
        finally:
            conn.close()

def load_portfolio_state(user_id, is_mock):
    mode = 1 if is_mock else 0
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT state_json FROM bot_states WHERE user_id = ? AND is_mock = ?', (user_id, mode)).fetchone()
    finally:
        conn.close()
    if row and row['state_json']:
        return json.loads(row['state_json'])
    return None

def log_trade_journal(user_id, ticker, stock_name, action, price, strategy, ai_reason,
                      profit=0, shares=0, mode='KR'):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO trade_journal
                    (user_id, ticker, stock_name, action, price, shares, mode, strategy, ai_reason, profit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, ticker, stock_name, action, price, shares, mode, strategy, ai_reason, profit))
            conn.commit()
        finally:
            conn.close()

def get_recent_trades(user_id, ticker, limit=5):
    conn = get_db_connection()
    try:
        rows = conn.execute('''
            SELECT action, price, ai_reason, profit, date(created_at) as date
            FROM trade_journal WHERE user_id = ? AND ticker = ?
            ORDER BY created_at DESC LIMIT ?
        ''', (user_id, ticker, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]

def save_ai_rules(user_id, rule_text, trigger_type: str = 'manual'):
    with db_lock:
        conn = get_db_connection()
        try:
            current = conn.execute('SELECT rule_text FROM ai_rules WHERE user_id = ?', (user_id,)).fetchone()
            if current and current['rule_text']:
                conn.execute('''
                    INSERT INTO ai_rules_history (user_id, rule_text, trigger_type)
                    VALUES (?, ?, ?)
                ''', (user_id, current['rule_text'], trigger_type))
                conn.execute('''
                    DELETE FROM ai_rules_history
                    WHERE user_id = ? AND id NOT IN (
                        SELECT id FROM ai_rules_history
                        WHERE user_id = ? ORDER BY created_at DESC LIMIT 10
                    )
                ''', (user_id, user_id))
            conn.execute('''
                INSERT OR REPLACE INTO ai_rules (user_id, rule_text, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, rule_text))
            conn.commit()
        finally:
            conn.close()

def load_ai_rules(user_id):
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT rule_text FROM ai_rules WHERE user_id = ?', (user_id,)).fetchone()
    finally:
        conn.close()
    return (row['rule_text'] or "") if row else ""

def get_ai_rules_history(user_id, limit: int = 5):
    conn = get_db_connection()
    try:
        rows = conn.execute('''
            SELECT rule_text, trigger_type, created_at
            FROM ai_rules_history WHERE user_id = ?
            ORDER BY created_at DESC LIMIT ?
        ''', (user_id, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]

def get_user_initial_cash(user_id, is_mock):
    conn = get_db_connection()
    cash_col = "us_initial_cash" if is_mock else "real_initial_cash"
    try:
        row = conn.execute(f'SELECT {cash_col} FROM users WHERE id = ?', (user_id,)).fetchone()
    finally:
        conn.close()
    return float(row[cash_col]) if row and row[cash_col] is not None else 10000000.0

def set_user_initial_cash(user_id, pure_principal, is_mock):
    from datetime import date as _date
    today_str = _date.today().strftime('%Y-%m-%d')
    with db_lock:
        conn = get_db_connection()
        cash_col = "us_initial_cash" if is_mock else "real_initial_cash"
        try:
            conn.execute(
                f'UPDATE users SET {cash_col} = ?, initial_cash_captured_at = ? WHERE id = ?',
                (pure_principal, today_str, user_id)
            )
            conn.commit()
        finally:
            conn.close()

def add_user_initial_cash(user_id, deposit_delta, is_mock):
    with db_lock:
        conn = get_db_connection()
        cash_col = "us_initial_cash" if is_mock else "real_initial_cash"
        try:
            conn.execute(f'UPDATE users SET {cash_col} = {cash_col} + ? WHERE id = ?', (deposit_delta, user_id))
            conn.commit()
        finally:
            conn.close()

def get_news_api_keys(user_id: int) -> dict:
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT dart_api_key, naver_client_id, naver_client_secret FROM users WHERE id = ?',
            (user_id,)
        ).fetchone()
    finally:
        conn.close()
    if row:
        return {
            'dart_api_key':        row['dart_api_key'] or '',
            'naver_client_id':     row['naver_client_id'] or '',
            'naver_client_secret': row['naver_client_secret'] or '',
        }
    return {'dart_api_key': '', 'naver_client_id': '', 'naver_client_secret': ''}

def set_news_api_keys(user_id: int, dart_api_key: str, naver_client_id: str, naver_client_secret: str):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                'UPDATE users SET dart_api_key=?, naver_client_id=?, naver_client_secret=? WHERE id=?',
                (dart_api_key, naver_client_id, naver_client_secret, user_id)
            )
            conn.commit()
        finally:
            conn.close()


def get_sector_guide(user_id: int) -> str:
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT sector_guide FROM users WHERE id=?', (user_id,)).fetchone()
        return (row['sector_guide'] or '') if row else ''
    finally:
        conn.close()

def set_sector_guide(user_id: int, guide_text: str):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('UPDATE users SET sector_guide=? WHERE id=?', (guide_text, user_id))
            conn.commit()
        finally:
            conn.close()


def init_default_ai_rules(user_id: int):
    existing = load_ai_rules(user_id)
    if existing and len(existing.strip()) > 50:
        return

    DEFAULT_RULES = """【최우선 금지 원칙】
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


_CHAT_HISTORY_LIMIT = 30

def load_chat_history(user_id: int, is_mock: int) -> list:
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT messages FROM chat_history WHERE user_id=? AND is_mock=?',
            (user_id, is_mock)
        ).fetchone()
    finally:
        conn.close()
    if row and row['messages']:
        try:
            return json.loads(row['messages'])
        except Exception:
            return []
    return []


def save_chat_history(user_id: int, is_mock: int, messages: list):
    trimmed = messages[-_CHAT_HISTORY_LIMIT:] if len(messages) > _CHAT_HISTORY_LIMIT else messages
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT OR REPLACE INTO chat_history (user_id, is_mock, messages, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (user_id, is_mock, json.dumps(trimmed, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()


def clear_chat_history(user_id: int, is_mock: int):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute(
                'DELETE FROM chat_history WHERE user_id=? AND is_mock=?',
                (user_id, is_mock)
            )
            conn.commit()
        finally:
            conn.close()


def log_ai_decision(user_id: int, mode: str, ticker: str, stock_name: str,
                     signal: str, ai_decision: str, confidence: int, ai_reason: str,
                     input_context: str = "", portfolio_snapshot: str = "",
                     market_regime: str = "", strategy: str = "", price: float = 0,
                     session_type: str = "live") -> int:
    with db_lock:
        conn = get_db_connection()
        try:
            cur = conn.execute('''
                INSERT INTO ai_decision_log
                (user_id, mode, session_type, ticker, stock_name, signal,
                 ai_decision, confidence, ai_reason, input_context,
                 portfolio_snapshot, market_regime, strategy, price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (user_id, mode, session_type, ticker, stock_name, signal,
                  ai_decision, confidence, ai_reason, input_context,
                  portfolio_snapshot, market_regime, strategy, price))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def update_ai_decision_outcome(log_id: int, outcome_price: float,
                                outcome_pnl: float, outcome_pnl_pct: float,
                                outcome_days: int):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''
                UPDATE ai_decision_log
                SET outcome_price=?, outcome_pnl=?, outcome_pnl_pct=?,
                    outcome_days=?, outcome_updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            ''', (outcome_price, outcome_pnl, outcome_pnl_pct, outcome_days, log_id))
            conn.commit()
        finally:
            conn.close()


def get_ai_decision_stats(user_id: int, mode: str = 'KR',
                           session_type: str = 'live') -> dict:
    conn = get_db_connection()
    try:
        rows = conn.execute('''
            SELECT ai_decision, signal, outcome_pnl, confidence
            FROM ai_decision_log
            WHERE user_id=? AND mode=? AND session_type=?
              AND outcome_pnl IS NOT NULL
        ''', (user_id, mode, session_type)).fetchall()
    finally:
        conn.close()
    if not rows:
        return {"total": 0, "correct": 0, "accuracy": 0}
    total = len(rows)
    correct = sum(1 for r in rows if
                  (r['ai_decision'] == 'CONFIRM' and r['outcome_pnl'] > 0) or
                  (r['ai_decision'] == 'REJECT' and r['outcome_pnl'] <= 0))
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total * 100, 1),
        "avg_confidence": round(sum(r['confidence'] for r in rows) / total, 1)
    }


def update_backtest_progress(mode: str, ticker: str, stock_name: str,
                              last_date: str, total_scenarios: int):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO backtest_progress (mode, ticker, stock_name, last_date, total_scenarios)
                VALUES (?,?,?,?,?)
                ON CONFLICT(mode, ticker) DO UPDATE SET
                    last_date=excluded.last_date,
                    total_scenarios=total_scenarios + excluded.total_scenarios,
                    completed_at=CURRENT_TIMESTAMP
            ''', (mode, ticker, stock_name, last_date, total_scenarios))
            conn.commit()
        finally:
            conn.close()


def get_backtest_pending_tickers(mode: str, limit: int = 100) -> list:
    conn = get_db_connection()
    try:
        done = {r[0] for r in conn.execute(
            'SELECT ticker FROM backtest_progress WHERE mode=?', (mode,)
        ).fetchall()}
    finally:
        conn.close()
    return done


def save_macro_snapshot(date: str, data: dict):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT OR REPLACE INTO macro_daily_snapshot
                (date, kospi_close, kospi_vs_ma200, kospi_52w_pct,
                 sp500_chg, nasdaq_chg, nikkei_chg, shanghai_chg,
                 vix, dxy, usd_krw, wti, gold, copper, sox_chg,
                 us_rate, kr_rate, us_10y, us_2y, yield_spread,
                 foreign_net_buy, institution_net_buy,
                 is_fomc_week, is_cpi_week)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                date,
                data.get('kospi_close'), data.get('kospi_vs_ma200'), data.get('kospi_52w_pct'),
                data.get('sp500_chg'), data.get('nasdaq_chg'), data.get('nikkei_chg'), data.get('shanghai_chg'),
                data.get('vix'), data.get('dxy'), data.get('usd_krw'),
                data.get('wti'), data.get('gold'), data.get('copper'), data.get('sox_chg'),
                data.get('us_rate'), data.get('kr_rate'), data.get('us_10y'), data.get('us_2y'), data.get('yield_spread'),
                data.get('foreign_net_buy'), data.get('institution_net_buy'),
                data.get('is_fomc_week', 0), data.get('is_cpi_week', 0),
            ))
            conn.commit()
        finally:
            conn.close()


def get_macro_snapshot(date: str) -> dict:
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT * FROM macro_daily_snapshot WHERE date=?', (date,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def log_backtest_signal(data: dict) -> int:
    with db_lock:
        conn = get_db_connection()
        try:
            cur = conn.execute('''
                INSERT INTO backtest_signals
                (user_id, mode, ticker, stock_name, trade_date, signal, signal_type, price,
                 ai_decision, confidence, ai_reason, macro_date,
                 rsi, macd, macd_signal, bb_upper, bb_mid, bb_lower,
                 sma5, sma20, sma60, sma120, vol_ratio,
                 support, resistance, fib_382, fib_500, fib_618,
                 min_price_5d, max_price_5d, days_to_min_5d, days_to_max_5d,
                 min_price_20d, max_price_20d, days_to_min_20d, days_to_max_20d,
                 pnl_5d, pnl_20d, optimal_buy_zone, optimal_sell_zone)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                data.get('user_id'), data.get('mode', 'KR'),
                data.get('ticker'), data.get('stock_name'), data.get('trade_date'),
                data.get('signal'), data.get('signal_type'), data.get('price'),
                data.get('ai_decision'), data.get('confidence'), data.get('ai_reason'),
                data.get('macro_date'),
                data.get('rsi'), data.get('macd'), data.get('macd_signal'),
                data.get('bb_upper'), data.get('bb_mid'), data.get('bb_lower'),
                data.get('sma5'), data.get('sma20'), data.get('sma60'), data.get('sma120'),
                data.get('vol_ratio'),
                data.get('support'), data.get('resistance'),
                data.get('fib_382'), data.get('fib_500'), data.get('fib_618'),
                data.get('min_price_5d'), data.get('max_price_5d'),
                data.get('days_to_min_5d'), data.get('days_to_max_5d'),
                data.get('min_price_20d'), data.get('max_price_20d'),
                data.get('days_to_min_20d'), data.get('days_to_max_20d'),
                data.get('pnl_5d'), data.get('pnl_20d'),
                data.get('optimal_buy_zone'), data.get('optimal_sell_zone'),
            ))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def log_trade_signal_backtest(data: dict) -> int:
    with db_lock:
        conn = get_db_connection()
        try:
            cur = conn.execute('''
                INSERT INTO backtest_trade_signals
                (user_id, mode, ticker, stock_name, trade_date,
                 signal_types, signal_direction, price,
                 rsi, macd, macd_signal, bb_upper, bb_mid, bb_lower,
                 sma5, sma20, sma60, sma120, vol_ratio,
                 support, resistance,
                 market_phase, market_phase_kr, phase_confidence,
                 macro_str, vix, usd_krw, us_10y, kr_rate,
                 days_to_peak, max_gain_pct,
                 days_to_max_drawdown, max_drawdown_pct,
                 days_to_recovery, price_path_json,
                 sector, ai_analysis)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                data.get('user_id'), data.get('mode'), data.get('ticker'), data.get('stock_name'),
                data.get('trade_date'), data.get('signal_types'), data.get('signal_direction'), data.get('price'),
                data.get('rsi'), data.get('macd'), data.get('macd_signal'),
                data.get('bb_upper'), data.get('bb_mid'), data.get('bb_lower'),
                data.get('sma5'), data.get('sma20'), data.get('sma60'), data.get('sma120'),
                data.get('vol_ratio'), data.get('support'), data.get('resistance'),
                data.get('market_phase'), data.get('market_phase_kr'), data.get('phase_confidence'),
                data.get('macro_str'), data.get('vix'), data.get('usd_krw'),
                data.get('us_10y'), data.get('kr_rate'),
                data.get('days_to_peak'), data.get('max_gain_pct'),
                data.get('days_to_max_drawdown'), data.get('max_drawdown_pct'),
                data.get('days_to_recovery'), data.get('price_path_json'),
                data.get('sector'), data.get('ai_analysis'),
            ))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def get_backtest_context(mode: str, market_phase: str, signal_direction: str,
                         ticker: str = None, limit: int = 20) -> list:
    """실전 매매 시 백테스트 DB에서 유사 국면+신호 과거 사례 조회."""
    conn = get_db_connection()
    try:
        params = [mode, market_phase, signal_direction]
        ticker_clause = ''
        if ticker:
            ticker_clause = 'AND (ticker=? OR sector=(SELECT sector FROM backtest_trade_signals WHERE ticker=? LIMIT 1))'
            params += [ticker, ticker]
        rows = conn.execute(f'''
            SELECT trade_date, ticker, stock_name, sector, signal_types,
                   days_to_peak, max_gain_pct, max_drawdown_pct,
                   days_to_recovery, ai_analysis, macro_str
            FROM backtest_trade_signals
            WHERE mode=? AND market_phase=? AND signal_direction=?
              AND max_gain_pct IS NOT NULL
              {ticker_clause}
            ORDER BY created_at DESC
            LIMIT ?
        ''', params + [limit]).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_backtest_full_done(mode: str) -> set:
    conn = get_db_connection()
    try:
        return {r[0] for r in conn.execute(
            'SELECT ticker FROM backtest_full_progress WHERE mode=?', (mode,)
        ).fetchall()}
    finally:
        conn.close()


def update_backtest_full_progress(mode: str, ticker: str, last_date: str, total_signals: int):
    with db_lock:
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO backtest_full_progress (mode, ticker, last_processed_date, total_signals)
                VALUES (?,?,?,?)
                ON CONFLICT(mode, ticker) DO UPDATE SET
                    last_processed_date=excluded.last_processed_date,
                    total_signals=total_signals + excluded.total_signals,
                    completed_at=CURRENT_TIMESTAMP
            ''', (mode, ticker, last_date, total_signals))
            conn.commit()
        finally:
            conn.close()


def log_optimal_point(data: dict) -> int:
    with db_lock:
        conn = get_db_connection()
        try:
            cur = conn.execute('''
                INSERT INTO backtest_optimal_points
                (user_id, mode, ticker, stock_name, date, point_type, price, magnitude_pct,
                 rsi, macd, macd_signal, bb_upper, bb_mid, bb_lower,
                 sma5, sma20, sma60, sma120, vol_ratio,
                 support, resistance, fib_382, fib_500, fib_618,
                 signals_active, signal_count, macro_date,
                 market_phase, market_phase_kr, phase_confidence,
                 ai_analysis,
                 pnl_5d, pnl_20d, pnl_60d, max_gain_60d, max_loss_60d)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                data.get('user_id'), data.get('mode', 'KR'),
                data.get('ticker'), data.get('stock_name'),
                data.get('date'), data.get('point_type'), data.get('price'),
                data.get('magnitude_pct'),
                data.get('rsi'), data.get('macd'), data.get('macd_signal'),
                data.get('bb_upper'), data.get('bb_mid'), data.get('bb_lower'),
                data.get('sma5'), data.get('sma20'), data.get('sma60'), data.get('sma120'),
                data.get('vol_ratio'),
                data.get('support'), data.get('resistance'),
                data.get('fib_382'), data.get('fib_500'), data.get('fib_618'),
                data.get('signals_active'), data.get('signal_count', 0),
                data.get('macro_date'),
                data.get('market_phase'), data.get('market_phase_kr'), data.get('phase_confidence'),
                data.get('ai_analysis'),
                data.get('pnl_5d'), data.get('pnl_20d'), data.get('pnl_60d'),
                data.get('max_gain_60d'), data.get('max_loss_60d'),
            ))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def rebuild_phase_strategy_stats(mode: str = 'ALL'):
    """backtest_optimal_points 에서 국면×신호 승률 매트릭스를 재계산한다."""
    import json as _json
    conn = get_db_connection()
    try:
        modes = ['KR', 'US'] if mode == 'ALL' else [mode]
        for m in modes:
            rows = conn.execute('''
                SELECT market_phase, signals_active, pnl_20d, max_gain_60d
                FROM backtest_optimal_points
                WHERE mode=? AND market_phase IS NOT NULL AND pnl_20d IS NOT NULL
            ''', (m,)).fetchall()

            stats: dict = {}
            for r in rows:
                phase = r['market_phase']
                try:
                    sigs = _json.loads(r['signals_active'] or '[]')
                except Exception:
                    sigs = []
                for sig in sigs:
                    key = (m, phase, sig)
                    if key not in stats:
                        stats[key] = {'total': 0, 'win': 0, 'pnl_sum': 0, 'gain_sum': 0}
                    stats[key]['total'] += 1
                    if r['pnl_20d'] > 0:
                        stats[key]['win'] += 1
                    stats[key]['pnl_sum']  += r['pnl_20d']
                    stats[key]['gain_sum'] += (r['max_gain_60d'] or 0)

            with db_lock:
                for (m2, phase, sig), v in stats.items():
                    conn.execute('''
                        INSERT INTO phase_strategy_stats
                        (mode, market_phase, signal_type, total, win_20d, avg_pnl_20d, avg_max_gain_60d)
                        VALUES (?,?,?,?,?,?,?)
                        ON CONFLICT(mode, market_phase, signal_type) DO UPDATE SET
                            total=excluded.total, win_20d=excluded.win_20d,
                            avg_pnl_20d=excluded.avg_pnl_20d,
                            avg_max_gain_60d=excluded.avg_max_gain_60d,
                            updated_at=CURRENT_TIMESTAMP
                    ''', (m2, phase, sig, v['total'], v['win'],
                          round(v['pnl_sum'] / v['total'], 2),
                          round(v['gain_sum'] / v['total'], 2)))
                conn.commit()
    finally:
        conn.close()


def get_phase_strategy_stats(mode: str, phase: str) -> list[dict]:
    conn = get_db_connection()
    try:
        rows = conn.execute('''
            SELECT signal_type, total,
                   ROUND(100.0*win_20d/total, 1) AS win_rate,
                   avg_pnl_20d, avg_max_gain_60d
            FROM phase_strategy_stats
            WHERE mode=? AND market_phase=? AND total >= 5
            ORDER BY win_rate DESC
        ''', (mode, phase)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_trade_signals_summary(user_id: int):
    conn = get_db_connection()
    try:
        return conn.execute('''
            SELECT s.mode, s.ticker, s.stock_name, s.sector,
                   prog.last_processed_date, prog.completed_at,
                   COUNT(s.id) AS total_signals,
                   SUM(CASE WHEN s.signal_direction='BUY'  THEN 1 ELSE 0 END) AS buy_signals,
                   SUM(CASE WHEN s.signal_direction='SELL' THEN 1 ELSE 0 END) AS sell_signals,
                   ROUND(AVG(CASE WHEN s.signal_direction='BUY' THEN s.max_gain_pct END), 2) AS avg_max_gain,
                   ROUND(AVG(CASE WHEN s.signal_direction='BUY' THEN s.days_to_peak END), 1) AS avg_days_to_peak
            FROM backtest_trade_signals s
            LEFT JOIN backtest_full_progress prog ON prog.ticker=s.ticker AND prog.mode=s.mode
            WHERE s.user_id=?
            GROUP BY s.mode, s.ticker
            ORDER BY prog.completed_at DESC
        ''', (user_id,)).fetchall()
    finally:
        conn.close()


def get_trade_signals_detail(user_id: int, mode: str, ticker: str):
    conn = get_db_connection()
    try:
        return conn.execute('''
            SELECT trade_date, signal_direction, signal_types, price,
                   market_phase_kr, sector,
                   rsi, macd, bb_lower, bb_upper, vol_ratio,
                   days_to_peak, max_gain_pct,
                   days_to_max_drawdown, max_drawdown_pct, days_to_recovery,
                   ai_analysis
            FROM backtest_trade_signals
            WHERE user_id=? AND mode=? AND ticker=?
            ORDER BY trade_date ASC
        ''', (user_id, mode, ticker)).fetchall()
    finally:
        conn.close()


def get_optimal_points_summary(user_id: int):
    conn = get_db_connection()
    try:
        return conn.execute('''
            SELECT p.mode, p.ticker, p.stock_name,
                   prog.last_processed_date, prog.completed_at,
                   COUNT(p.id) AS total_points,
                   SUM(CASE WHEN p.point_type='BOTTOM' THEN 1 ELSE 0 END) AS bottoms,
                   SUM(CASE WHEN p.point_type='TOP'    THEN 1 ELSE 0 END) AS tops,
                   ROUND(AVG(p.pnl_20d), 2) AS avg_pnl_20d,
                   ROUND(AVG(p.max_gain_60d), 2) AS avg_max_gain_60d
            FROM backtest_optimal_points p
            LEFT JOIN backtest_full_progress prog ON prog.ticker=p.ticker AND prog.mode=p.mode
            WHERE p.user_id=?
            GROUP BY p.mode, p.ticker
            ORDER BY prog.completed_at DESC
        ''', (user_id,)).fetchall()
    finally:
        conn.close()


def get_optimal_points_detail(user_id: int, mode: str, ticker: str):
    conn = get_db_connection()
    try:
        return conn.execute('''
            SELECT date, point_type, price, magnitude_pct,
                   rsi, macd, bb_lower, bb_upper, vol_ratio,
                   signals_active, signal_count,
                   ai_analysis,
                   pnl_5d, pnl_20d, pnl_60d, max_gain_60d, max_loss_60d,
                   support, resistance, sma5, sma20, sma60, sma120
            FROM backtest_optimal_points
            WHERE user_id=? AND mode=? AND ticker=?
            ORDER BY date ASC
        ''', (user_id, mode, ticker)).fetchall()
    finally:
        conn.close()


def export_to_jsonl(mode: str = 'KR', output_path: str = 'finetune_data.jsonl'):
    import json as _json
    conn = get_db_connection()
    try:
        rows = conn.execute('''
            SELECT s.*, m.vix, m.sp500_chg, m.nasdaq_chg, m.usd_krw,
                   m.us_10y, m.yield_spread, m.foreign_net_buy, m.kospi_vs_ma200
            FROM backtest_signals s
            LEFT JOIN macro_daily_snapshot m ON s.macro_date = m.date
            WHERE s.mode=? AND s.ai_decision IS NOT NULL
        ''', (mode,)).fetchall()
    finally:
        conn.close()

    with open(output_path, 'w', encoding='utf-8') as f:
        for r in rows:
            r = dict(r)
            prompt = (
                f"종목: {r['stock_name']}({r['ticker']}) | 날짜: {r['trade_date']} | 신호: {r['signal']}({r['signal_type']})\n"
                f"현재가: {r['price']:,.0f} | RSI: {r['rsi']} | MACD: {r['macd']} | 볼린저: {r['bb_lower']}~{r['bb_upper']}\n"
                f"거래량: 평소대비 {r['vol_ratio']:.0f}% | 지지: {r['support']} | 저항: {r['resistance']}\n"
                f"[매크로] VIX:{r['vix']} | S&P500:{r['sp500_chg']:+.1f}% | 나스닥:{r['nasdaq_chg']:+.1f}% | 달러:{r['usd_krw']} | 미10년:{r['us_10y']}% | 장단기스프레드:{r['yield_spread']} | 외국인:{r['foreign_net_buy']} | KOSPI MA200대비:{r['kospi_vs_ma200']:+.1f}%\n"
                f"5일후 수익률: {r['pnl_5d']:+.1f}% | 20일후 수익률: {r['pnl_20d']:+.1f}%\n"
                f"매수구간: {r['optimal_buy_zone']} | 매도구간: {r['optimal_sell_zone']}"
            )
            completion = f"판단: {r['ai_decision']} (신뢰도 {r['confidence']}%) | 근거: {r['ai_reason']}"
            entry = {
                "messages": [
                    {"role": "system", "content": "당신은 한국 주식 트레이딩 AI입니다. 주어진 기술적/매크로 데이터를 분석해 매수/매도 판단을 내립니다."},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": completion}
                ]
            }
            f.write(_json.dumps(entry, ensure_ascii=False) + '\n')

    return len(rows)
