# -*- coding: utf-8 -*-
"""클린 대시보드 v3 — 새 체제(교과서 v3 + 참고서, 크론 자동매매) 조회 전용.

★구봇/매매 코드 전무. 이 앱은 절대 주문을 내지 않는다(매매는 EC2 크론 담당).
  계좌=toss_api 직접조회. 원금=사용자 수동설정(자동감지 없음=원금버그 근절).
  KR/US 탭 분리. 실행: python base/app.py (로컬) / gunicorn base.app:app (서버)
"""
import os, sys, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask, request, redirect, url_for, render_template_string
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from base.database import (get_db_connection, verify_user, get_user_initial_cash,
                           set_user_initial_cash, init_db)
from base.toss_api import TossInvestApi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

app = Flask(__name__)
app.secret_key = os.environ.get('LASSI_SECRET', 'lassi-dash-v3')
login_manager = LoginManager(app)
login_manager.login_view = 'login'


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row['id']); self.username = row['username']; self.row = row


def _user_row(uid):
    c = get_db_connection()
    try:
        return c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    finally:
        c.close()


@login_manager.user_loader
def load_user(uid):
    row = _user_row(uid)
    return User(row) if row else None


def _toss(row):
    return TossInvestApi(row['toss_client_id'], row['toss_client_secret'], row['toss_account_seq'] or '')


def kr_snapshot(row):
    out = {'holdings': [], 'cash': 0, 'total': 0, 'principal': 0, 'ret': None, 'pl': 0, 'error': None, 'hold_val': 0}
    try:
        t = _toss(row)
        bal = t.get_account_balance()
        if not bal:
            out['error'] = '계좌조회 실패(토큰/IP/API)'; return out
        hv = 0.0
        for s in bal.get('stocks', []):
            q = int(s.get('shares', 0) or 0)
            if q <= 0:
                continue
            px = float(s.get('current_price', 0) or 0) or float(s.get('purchase_price', 0) or 0)
            val = q * px
            hv += val
            out['holdings'].append({'name': s.get('name', s['ticker']), 'ticker': s['ticker'],
                                    'qty': q, 'price': px, 'value': val, 'is_etf': s['ticker'] == '069500'})
        cash = t.get_buyable_cash(default=None)
        cash = float(cash) if cash is not None else 0.0
        out['cash'] = cash; out['hold_val'] = hv; out['total'] = cash + hv
        pr = get_user_initial_cash(int(row['id']), is_mock=False)
        out['principal'] = pr
        if pr and pr > 0:
            out['ret'] = (out['total'] / pr - 1) * 100
            out['pl'] = out['total'] - pr
        out['holdings'].sort(key=lambda x: (-x['is_etf'], -x['value']))
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out


def us_snapshot(row):
    out = {'holdings': [], 'cash_usd': 0, 'error': None}
    try:
        t = _toss(row)
        b = t.get_balance()
        if not b:
            out['error'] = 'US 계좌조회 실패'; return out
        out['cash_usd'] = float(b.get('cash_usd', 0) or 0)
        for s in b.get('stocks', []):
            q = float(s.get('shares', 0) or 0)
            if q > 0:
                out['holdings'].append({'ticker': s.get('ticker'), 'qty': q})
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    return out


def recent_trades(uid, n=20):
    c = get_db_connection()
    try:
        rows = c.execute("SELECT ticker, stock_name, action, price, shares, strategy, created_at, mode "
                         "FROM trade_journal WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, n)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        c.close()


def automation_status():
    st = {}
    for label, f in [('deadman 감시', 'heartbeat.txt'), ('신규자금 배분', 'auto_deploy_done.txt'),
                     ('US 통화검증', 'first_fill_verified_us.txt'), ('리밸런스 상태', 'rebalance_state.txt')]:
        try:
            st[label] = open(P(f)).read().strip()[:40]
        except Exception:
            st[label] = '—'
    try:
        from KR.reference import artifact_tickers
        st['참고서 아티팩트 제외'] = f"{len(artifact_tickers())}종목"
    except Exception:
        st['참고서 아티팩트 제외'] = '—'
    try:
        from KR.live_v1 import is_rebalance_week
        st['리밸런스 주간'] = '예 (교체 진행)' if is_rebalance_week() else '아니오 (유지)'
    except Exception:
        st['리밸런스 주간'] = '—'
    return st


PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Lassi</title><style>
:root{--bg:#0b0f1a;--card:#141b2d;--card2:#1a2337;--line:#24304a;--txt:#eaf0fb;--mut:#8394b3;
--blue:#3b82f6;--grn:#34d399;--red:#f87171;--gold:#fbbf24}
*{box-sizing:border-box;margin:0} body{font-family:system-ui,'Malgun Gothic',sans-serif;
background:linear-gradient(160deg,#0b0f1a,#0e1526);color:var(--txt);min-height:100vh}
.wrap{max-width:900px;margin:0 auto;padding:18px 16px 60px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.logo{font-size:19px;font-weight:800;letter-spacing:-.3px} .logo span{font-weight:400;color:var(--mut);font-size:12px}
.sub{color:var(--mut);font-size:12px;margin-bottom:16px} .sub a{color:var(--blue);text-decoration:none}
.tabs{display:flex;gap:8px;margin:14px 0} .tab{flex:1;padding:12px;text-align:center;border-radius:12px;
background:var(--card);border:1px solid var(--line);cursor:pointer;font-weight:700;font-size:15px;color:var(--mut);transition:.15s}
.tab.on{background:linear-gradient(135deg,#1e3a8a,#2563eb);color:#fff;border-color:#2563eb;box-shadow:0 4px 14px rgba(37,99,235,.35)}
.pane{display:none} .pane.on{display:block;animation:fade .2s} @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1}}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;margin:12px 0;box-shadow:0 2px 10px rgba(0,0,0,.25)}
.hero{background:linear-gradient(135deg,#141b2d,#1c2743)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:14px}
.k{color:var(--mut);font-size:12px;margin-bottom:4px} .v{font-size:22px;font-weight:700}
.big{font-size:32px;font-weight:800;letter-spacing:-1px} .pos{color:var(--grn)} .neg{color:var(--red)} .blue{color:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:13px} td,th{padding:9px 8px;border-bottom:1px solid #1c2740}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
td{text-align:right} td.l,th.l{text-align:left} th{text-align:right}
.etf{color:#60a5fa;font-weight:700} .badge{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700}
.b-buy{background:rgba(52,211,153,.15);color:var(--grn)} .b-sell{background:rgba(248,113,113,.15);color:var(--red)}
.b-etf{background:rgba(59,130,246,.15);color:#60a5fa}
input{background:#0b0f1a;border:1px solid #2c3a58;color:var(--txt);padding:11px;border-radius:10px;font-size:15px;width:170px}
.btn{background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;border:0;padding:11px 16px;border-radius:10px;cursor:pointer;font-weight:700;font-size:14px}
.btn2{background:var(--card2);border:1px solid var(--line);color:var(--txt);padding:11px 14px;border-radius:10px;cursor:pointer;font-size:13px}
.warn{background:linear-gradient(135deg,#3a2318,#2d1c12);border-color:#7c3a1a;color:var(--gold)}
h3{font-size:13px;color:var(--mut);margin:22px 0 4px;font-weight:700;letter-spacing:.3px}
.st{display:flex;justify-content:space-between;padding:9px 2px;border-bottom:1px solid #1c2740;font-size:13px}
.st:last-child{border:0} .st .sv{font-weight:600} .bar{height:6px;border-radius:4px;background:#1c2740;overflow:hidden;margin-top:6px}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#60a5fa)}
.mut{color:var(--mut);font-size:12px} .center{text-align:center;margin:24px 0}
</style></head><body><div class=wrap>
<div class=top><div class=logo>🤖 Lassi <span>교과서 v3 + 참고서</span></div>
<div class=mut><a href="{{url_for('logout')}}">로그아웃</a></div></div>
<div class=sub>{{now}} · 크론 자동매매 · <b>이 화면은 주문을 내지 않습니다(조회 전용)</b> · <a href="{{url_for('dashboard')}}">↻ 새로고침</a></div>

<div class=tabs>
  <div class="tab on" onclick="sw('kr')">🇰🇷 국내 (KR)</div>
  <div class=tab onclick="sw('us')">🇺🇸 미국 (US)</div>
</div>

<!-- ===== KR ===== -->
<div id=kr class="pane on">
{% if kr.error %}<div class="card warn">⚠️ {{kr.error}}</div>{% else %}
<div class="card hero">
  <div class=k>총자산</div><div class=big>{{ '{:,.0f}'.format(kr.total) }}<span style=font-size:18px> 원</span></div>
  <div class=grid style=margin-top:16px>
    <div><div class=k>수익률</div><div class="v {{'pos' if (kr.ret or 0)>=0 else 'neg'}}">{{ '%+.2f'|format(kr.ret) if kr.ret is not none else '—' }}%</div></div>
    <div><div class=k>평가손익</div><div class="v {{'pos' if kr.pl>=0 else 'neg'}}">{{ '{:+,.0f}'.format(kr.pl) }}</div></div>
    <div><div class=k>원금</div><div class=v>{{ '{:,.0f}'.format(kr.principal) }}</div></div>
    <div><div class=k>현금(미투입)</div><div class=v>{{ '{:,.0f}'.format(kr.cash) }}</div></div>
  </div>
</div>
<div class=card><table><tr><th class=l>종목</th><th>수량</th><th>현재가</th><th>평가액</th><th>비중</th></tr>
{% for h in kr.holdings %}<tr><td class=l>{% if h.is_etf %}<span class="badge b-etf">지수</span> {% endif %}<span class="{{'etf' if h.is_etf}}">{{h.name}}</span> <span class=mut>{{h.ticker}}</span></td>
<td>{{h.qty}}</td><td>{{ '{:,.0f}'.format(h.price) }}</td><td>{{ '{:,.0f}'.format(h.value) }}</td>
<td>{{ '%.1f'|format(h.value/kr.total*100 if kr.total else 0) }}%</td></tr>{% endfor %}
{% if not kr.holdings %}<tr><td colspan=5 class="l mut">보유 종목 없음</td></tr>{% endif %}
</table></div>
<div class=card><h3 style=margin-top:0>💵 원금 설정 <span class=mut style=font-weight:400>· 수동(자동감지 없음=버그근절)</span></h3>
<form method=post action="{{url_for('set_principal')}}" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input name=amount type=number placeholder="실제 투자원금" value="{{ '%.0f'|format(kr.principal) }}">
<button class=btn type=submit>원금 확정</button>
<button class=btn2 type=submit name=amount value="{{ '%.0f'|format(kr.total) }}">현재 총액({{ '{:,.0f}'.format(kr.total) }})으로</button>
</form><div class=mut style=margin-top:8px>수익률 = 총자산 ÷ 원금 − 1. 실제 넣은 돈을 입력하거나, 지금부터 재시작하려면 "현재 총액으로".</div></div>
{% endif %}
</div>

<!-- ===== US ===== -->
<div id=us class=pane>
{% if us.error %}<div class="card warn">⚠️ {{us.error}}</div>{% else %}
<div class="card hero"><div class=k>USD 예수금</div><div class=big>${{ '%.2f'|format(us.cash_usd) }}</div>
<div class=mut style=margin-top:8px>전략 = SPY(S&P500) 보유. 환전하면 크론이 자동으로 통화검증 후 매수.</div></div>
<div class=card><table><tr><th class=l>종목</th><th>수량</th></tr>
{% for h in us.holdings %}<tr><td class=l>{{h.ticker}}</td><td>{{ '%.4f'|format(h.qty) }}주</td></tr>{% endfor %}
{% if not us.holdings %}<tr><td colspan=2 class="l mut">SPY 미보유 — USD 환전 시 자동매수 대기</td></tr>{% endif %}
</table></div>{% endif %}
</div>

<!-- ===== 공통 ===== -->
<h3>⚙️ 자동화 상태</h3>
<div class=card>{% for k,v in status.items() %}<div class=st><span class=mut>{{k}}</span><span class=sv>{{v}}</span></div>{% endfor %}
<div class=mut style=margin-top:10px>크론: 리밸 평일 10:00 · 신규배분 평일 10:30 · US 미국장(새벽) · deadman 매일</div></div>

<h3>📜 최근 거래</h3>
<div class=card><table><tr><th class=l>일시</th><th class=l>종목</th><th>구분</th><th>가격</th><th>수량</th><th class=l>비고</th></tr>
{% for t in trades %}<tr><td class=l mut>{{t.created_at[5:16]}}</td><td class=l>{{t.stock_name}} <span class=mut>{{t.mode}}</span></td>
<td><span class="badge {{'b-sell' if t.action=='SELL' else 'b-buy'}}">{{t.action}}</span></td>
<td>{{ '{:,.0f}'.format(t.price) }}</td><td>{{ '{:.0f}'.format(t.shares) }}</td><td class="l mut">{{t.strategy or '-'}}</td></tr>{% endfor %}
</table></div>
<div class="center mut">Lassi 대시보드 v3 · 조회 전용 · 매매는 검증된 크론이 담당</div>
</div>
<script>function sw(x){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('on'));
document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
document.getElementById(x).classList.add('on');event.currentTarget.classList.add('on');}</script>
</body></html>"""

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>Lassi 로그인</title><style>
body{font-family:system-ui,sans-serif;background:linear-gradient(160deg,#0b0f1a,#0e1526);color:#eaf0fb;
display:flex;height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#141b2d;padding:32px;border-radius:18px;border:1px solid #24304a;width:300px;box-shadow:0 10px 40px rgba(0,0,0,.4)}
h2{margin:0 0 18px} input{width:100%;padding:12px;margin:7px 0;background:#0b0f1a;border:1px solid #2c3a58;color:#eaf0fb;border-radius:10px;font-size:15px}
button{width:100%;padding:12px;background:linear-gradient(135deg,#2563eb,#3b82f6);color:#fff;border:0;border-radius:10px;margin-top:10px;cursor:pointer;font-weight:700;font-size:15px}
.e{color:#f87171;font-size:13px;margin-bottom:8px}</style></head><body>
<form method=post><h2>🤖 Lassi</h2>{% if error %}<div class=e>{{error}}</div>{% endif %}
<input name=username placeholder=아이디 autofocus><input name=password type=password placeholder=비밀번호>
<button>로그인</button></form></body></html>"""


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        u = verify_user(request.form.get('username', ''), request.form.get('password', ''))
        if u:
            login_user(User(u)); return redirect(url_for('dashboard'))
        error = '아이디 또는 비밀번호가 틀립니다.'
    return render_template_string(LOGIN, error=error)


@app.route('/logout')
@login_required
def logout():
    logout_user(); return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    row = current_user.row
    return render_template_string(
        PAGE, now=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
        kr=kr_snapshot(row), us=us_snapshot(row),
        trades=recent_trades(int(row['id'])), status=automation_status())


@app.route('/set_principal', methods=['POST'])
@login_required
def set_principal():
    try:
        amt = float(request.form.get('amount', 0))
        if amt > 0:
            set_user_initial_cash(int(current_user.row['id']), amt, is_mock=False)
    except Exception:
        pass
    return redirect(url_for('dashboard'))


if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False)
