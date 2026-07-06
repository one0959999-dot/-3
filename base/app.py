# -*- coding: utf-8 -*-
"""Lassi 대시보드 v5 — 포트폴리오 앱 스타일(도넛차트+비중바). 조회 전용(구봇 0).

계좌=toss_api 직접조회. 원금=원가기준 자동계산(매입가합+현금)=미실현 수익률.
색: 한국식(빨강=상승/이익, 파랑=하락/손실). 차트=인라인 SVG(외부 의존 0).
실행: python base/app.py / gunicorn base.app:app
"""
import os, sys, datetime, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask, request, redirect, url_for, render_template_string
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from base.database import get_db_connection, verify_user, init_db
from base.toss_api import TossInvestApi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

app = Flask(__name__)
app.secret_key = os.environ.get('LASSI_SECRET', 'lassi-dash-v5')
login_manager = LoginManager(app)
login_manager.login_view = 'login'
C_CIRC = 2 * math.pi * 54  # 도넛 둘레


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
    out = {'holdings': [], 'cash': 0, 'total': 0, 'hold_val': 0, 'cost_basis': 0,
           'ret': None, 'pl': 0, 'error': None, 'alloc': []}
    try:
        t = _toss(row)
        bal = t.get_account_balance()
        if not bal:
            out['error'] = '계좌조회 실패(토큰/IP/API 확인)'; return out
        hv = 0.0; cost = 0.0; etf_v = 0.0; stk_v = 0.0
        for s in bal.get('stocks', []):
            q = int(s.get('shares', 0) or 0)
            if q <= 0:
                continue
            px = float(s.get('current_price', 0) or 0) or float(s.get('purchase_price', 0) or 0)
            bp = float(s.get('purchase_price', 0) or 0) or px
            val = q * px; hv += val; cost += q * bp
            is_etf = s['ticker'] == '069500'
            etf_v += val if is_etf else 0; stk_v += 0 if is_etf else val
            out['holdings'].append({'name': s.get('name', s['ticker']), 'ticker': s['ticker'],
                                    'qty': q, 'price': px, 'buy': bp, 'value': val,
                                    'plpct': (px / bp - 1) * 100 if bp else 0, 'is_etf': is_etf})
        cash = t.get_buyable_cash(default=None)
        cash = float(cash) if cash is not None else 0.0
        out['cash'] = cash; out['hold_val'] = hv; out['total'] = cash + hv
        out['cost_basis'] = cost + cash
        if out['cost_basis'] > 0:
            out['ret'] = (out['total'] / out['cost_basis'] - 1) * 100
            out['pl'] = out['total'] - out['cost_basis']
        out['holdings'].sort(key=lambda x: (-x['is_etf'], -x['value']))
        for h in out['holdings']:
            h['w'] = (h['value'] / out['total'] * 100) if out['total'] else 0
        # 도넛: 지수ETF / 저변동 / 현금
        tot = out['total'] or 1
        cum = 0.0
        for label, v, col in [('지수 ETF', etf_v, '#3182f6'), ('저변동 25종목', stk_v, '#12b886'), ('현금', cash, '#ced4da')]:
            f = v / tot
            out['alloc'].append({'label': label, 'val': v, 'pct': f * 100, 'color': col,
                                 'dash': round(f * C_CIRC, 2), 'gap': round(C_CIRC - f * C_CIRC, 2),
                                 'off': round(-cum * C_CIRC, 2)})
            cum += f
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
        rows = c.execute("SELECT stock_name, action, price, shares, created_at, mode "
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
            st[label] = open(P(f)).read().strip()[:40] or '—'
        except Exception:
            st[label] = '—'
    try:
        from KR.reference import artifact_tickers
        st['참고서 아티팩트 제외'] = f"{len(artifact_tickers())}종목"
    except Exception:
        st['참고서 아티팩트 제외'] = '—'
    try:
        from KR.live_v1 import is_rebalance_week
        st['리밸런스 주간'] = '예 (교체)' if is_rebalance_week() else '아니오 (유지)'
    except Exception:
        st['리밸런스 주간'] = '—'
    return st


PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Lassi</title><style>
:root{--bg:#eef1f6;--card:#fff;--txt:#191f28;--sub:#8b95a1;--up:#f04452;--down:#3182f6;--pri:#3182f6;--soft:#f6f8fb}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,'Malgun Gothic','Apple SD Gothic Neo',system-ui,sans-serif;background:var(--bg);color:var(--txt);letter-spacing:-.3px}
.wrap{max-width:500px;margin:0 auto;padding:12px 14px 72px}
.top{display:flex;justify-content:space-between;align-items:center;padding:10px 4px}
.logo{font-size:21px;font-weight:800} .logo em{font-style:normal;color:var(--pri)}
.top a{color:var(--sub);text-decoration:none;font-size:13px;font-weight:600}
.note{font-size:11.5px;color:var(--sub);margin:0 4px 8px} .note a{color:var(--pri);text-decoration:none}
.seg{display:flex;background:#e3e8ef;border-radius:13px;padding:4px;gap:4px;margin:8px 0}
.seg div{flex:1;text-align:center;padding:10px;border-radius:10px;font-weight:700;font-size:14.5px;color:var(--sub);cursor:pointer;transition:.15s}
.seg div.on{background:#fff;color:var(--txt);box-shadow:0 2px 8px rgba(0,20,60,.1)}
.pane{display:none} .pane.on{display:block;animation:f .25s} @keyframes f{from{opacity:0;transform:translateY(8px)}to{opacity:1}}
.card{background:var(--card);border-radius:22px;padding:22px;margin:12px 0;box-shadow:0 1px 2px rgba(0,20,60,.05),0 10px 30px rgba(0,25,80,.05)}
.hero{background:linear-gradient(135deg,#fff,#f4f8ff)}
.lab{font-size:13px;color:var(--sub);font-weight:600}
.amt{font-size:36px;font-weight:800;margin:3px 0 12px;letter-spacing:-1.5px} .amt small{font-size:20px;color:var(--sub)}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:15px;font-weight:800;padding:8px 13px;border-radius:13px}
.up{color:var(--up)} .down{color:var(--down)} .pill.up{background:#fdeaec} .pill.down{background:#e9f1fe}
.donut{display:flex;align-items:center;gap:20px} .donut svg{flex-shrink:0}
.dc{position:relative;width:150px;height:150px} .dc .ctr{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.dc .ctr .t1{font-size:11px;color:var(--sub);font-weight:600} .dc .ctr .t2{font-size:17px;font-weight:800}
.leg{flex:1} .legrow{display:flex;align-items:center;gap:9px;padding:7px 0}
.dot{width:11px;height:11px;border-radius:4px;flex-shrink:0} .legrow .ln{flex:1;font-size:13.5px;font-weight:600}
.legrow .lp{font-weight:800;font-size:14px} .legrow .lv{font-size:11.5px;color:var(--sub)}
.h{font-size:15px;font-weight:800;margin:24px 6px 10px}
.hold{display:flex;align-items:center;gap:12px;padding:14px 4px;border-bottom:1px solid #f0f3f7} .hold:last-child{border:0}
.hicon{width:40px;height:40px;border-radius:13px;background:var(--soft);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;color:var(--sub);flex-shrink:0}
.hicon.etf{background:#e7f0ff;color:var(--pri)}
.hmid{flex:1;min-width:0} .hnm{font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hsub{font-size:12px;color:var(--sub);margin-top:1px}
.bar{height:5px;background:#eef1f5;border-radius:3px;margin-top:6px;overflow:hidden} .bar>i{display:block;height:100%;border-radius:3px;background:linear-gradient(90deg,#4b93f7,#3182f6)}
.bar.g>i{background:linear-gradient(90deg,#20c997,#12b886)}
.hend{text-align:right;flex-shrink:0} .hval{font-weight:800;font-size:15px} .hpl{font-size:12.5px;font-weight:700;margin-top:2px}
.st{display:flex;justify-content:space-between;padding:12px 2px;border-bottom:1px solid #f0f3f7;font-size:14px} .st:last-child{border:0}
.st .kk{color:var(--sub);font-weight:600} .st .vv{font-weight:700}
.warn{background:#fff4e5;color:#c2681a;font-weight:600} .mut{color:var(--sub)}
.cap{font-size:11.5px;color:var(--sub);margin:10px 4px 0;line-height:1.5}
.tag{display:inline-block;font-size:11px;font-weight:800;padding:2px 7px;border-radius:8px;margin-right:6px}
.tag.b{background:#fdeaec;color:var(--up)} .tag.s{background:#e9f1fe;color:var(--down)}
.foot{text-align:center;font-size:12px;color:#b6bdc7;margin:28px 0 0}
</style></head><body><div class=wrap>
<div class=top><div class=logo>Lassi<em>.</em></div><a href="{{url_for('logout')}}">로그아웃</a></div>
<div class=note>{{now}} · 크론 자동매매 · 조회 전용(주문 안 냄) · <a href="{{url_for('dashboard')}}">↻ 새로고침</a></div>
<div class=seg><div class="on" onclick="sw('kr')">🇰🇷 국내</div><div onclick="sw('us')">🇺🇸 미국</div></div>

<!-- KR -->
<div id=kr class="pane on">
{% if kr.error %}<div class="card warn">⚠️ {{kr.error}}</div>{% else %}
<div class="card hero">
  <div class=lab>총 자산</div><div class=amt>{{ '{:,.0f}'.format(kr.total) }}<small> 원</small></div>
  <span class="pill {{'up' if (kr.ret or 0)>=0 else 'down'}}">{{ '▲' if (kr.ret or 0)>=0 else '▼' }} {{ '%.2f'|format(kr.ret|abs) if kr.ret is not none else '—' }}% <span style=opacity:.6>·</span> {{ '{:+,.0f}'.format(kr.pl) }}원</span>
</div>
<div class=card>
  <div class=lab style=margin-bottom:14px>포트폴리오 구성</div>
  <div class=donut>
    <div class=dc><svg viewBox="0 0 120 120" width=150 height=150>
      <circle cx=60 cy=60 r=54 fill=none stroke=#f0f3f7 stroke-width=14/>
      {% for s in kr.alloc %}{% if s.pct>0.3 %}<circle cx=60 cy=60 r=54 fill=none stroke="{{s.color}}" stroke-width=14
        stroke-dasharray="{{s.dash}} {{s.gap}}" stroke-dashoffset="{{s.off}}" transform="rotate(-90 60 60)"/>{% endif %}{% endfor %}
    </svg><div class=ctr><div class=t1>보유종목</div><div class=t2>{{kr.holdings|length}}개</div></div></div>
    <div class=leg>{% for s in kr.alloc %}<div class=legrow><span class=dot style=background:{{s.color}}></span>
      <span class=ln>{{s.label}}<div class=lv>{{ '{:,.0f}'.format(s.val) }}원</div></span>
      <span class=lp>{{ '%.0f'|format(s.pct) }}%</span></div>{% endfor %}</div>
  </div>
  <div class=cap>목표 = 지수ETF 50% + 저변동 50%. {% if kr.alloc[0].pct < 40 %}⚠️ 지수 비중 부족(현금 재배분 대기).{% endif %}</div>
</div>
<div class=h>보유 종목 {{kr.holdings|length}}</div>
<div class=card>{% for h in kr.holdings %}<div class=hold>
  <div class="hicon {{'etf' if h.is_etf}}">{{ '지수' if h.is_etf else h.name[:2] }}</div>
  <div class=hmid><div class=hnm>{{h.name}}</div><div class=hsub>{{h.qty}}주 · 매입 {{ '{:,.0f}'.format(h.buy) }} → {{ '{:,.0f}'.format(h.price) }}</div>
    <div class="bar {{'' if h.is_etf else 'g'}}"><i style=width:{{ '%.0f'|format(h.w if h.w<100 else 100) }}%></i></div></div>
  <div class=hend><div class=hval>{{ '{:,.0f}'.format(h.value) }}</div>
    <div class="hpl {{'up' if h.plpct>=0 else 'down'}}">{{ '%+.1f'|format(h.plpct) }}%</div></div>
</div>{% endfor %}
{% if not kr.holdings %}<div class="mut" style=text-align:center;padding:20px>보유 종목 없음</div>{% endif %}</div>
<div class=cap>* 수익률 = 매입원가 대비 미실현 손익 (매입가 자동계산, 입력 불필요)</div>
{% endif %}
</div>

<!-- US -->
<div id=us class=pane>
{% if us.error %}<div class="card warn">⚠️ {{us.error}}</div>{% else %}
<div class="card hero"><div class=lab>USD 예수금</div><div class=amt>${{ '%.2f'|format(us.cash_usd) }}</div>
<div class=cap style=margin-top:2px>전략 = SPY(S&P500) 보유. 환전하면 크론이 통화검증 후 자동매수.</div></div>
{% if us.holdings %}<div class=card>{% for h in us.holdings %}<div class=hold>
<div class=hicon>{{h.ticker[:3]}}</div><div class=hmid><div class=hnm>{{h.ticker}}</div></div>
<div class=hend><div class=hval>{{ '%.4f'|format(h.qty) }}주</div></div></div>{% endfor %}</div>
{% else %}<div class="card mut" style=text-align:center;padding:26px>SPY 미보유<br><span style=font-size:12px>USD 환전 시 자동매수 대기</span></div>{% endif %}{% endif %}
</div>

<!-- 공통 -->
<div class=h>⚙️ 자동화 상태</div>
<div class=card>{% for k,v in status.items() %}<div class=st><span class=kk>{{k}}</span><span class=vv>{{v}}</span></div>{% endfor %}
<div class=cap>크론: 리밸 평일 10:00 · 신규배분 평일 10:30 · US 미국장 · deadman 매일</div></div>

<div class=h>📜 최근 거래</div>
<div class=card>{% for t in trades %}<div class=st><span><span class="tag {{'s' if t.action=='SELL' else 'b'}}">{{ '매도' if t.action=='SELL' else '매수' }}</span>{{t.stock_name}} <span class=mut style=font-size:12px>{{t.mode}}</span></span>
<span class=vv>{{ '{:,.0f}'.format(t.price) }} <span class=mut style=font-weight:500;font-size:11px>{{t.created_at[5:16]}}</span></span></div>{% endfor %}
{% if not trades %}<div class="mut" style=text-align:center;padding:16px>거래 없음</div>{% endif %}</div>

<div class=foot>Lassi · 조회 전용 · 매매는 검증된 크론이 담당</div>
</div>
<script>function sw(x){document.querySelectorAll('.seg div').forEach(t=>t.classList.remove('on'));
document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
document.getElementById(x).classList.add('on');event.currentTarget.classList.add('on');}</script>
</body></html>"""

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>Lassi 로그인</title><style>
body{font-family:-apple-system,'Malgun Gothic',system-ui,sans-serif;background:linear-gradient(160deg,#eef1f6,#e3ecf9);color:#191f28;display:flex;height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#fff;padding:36px 30px;border-radius:24px;width:320px;box-shadow:0 20px 50px rgba(0,25,80,.12)}
h2{margin:0 0 4px;font-size:26px} h2 span{color:#3182f6} .s{color:#8b95a1;font-size:13px;margin-bottom:20px}
input{width:100%;padding:15px;margin:7px 0;background:#f6f8fb;border:1.5px solid #eef1f5;color:#191f28;border-radius:13px;font-size:15px}
input:focus{outline:none;border-color:#3182f6;background:#fff}
button{width:100%;padding:15px;background:#3182f6;color:#fff;border:0;border-radius:13px;margin-top:14px;cursor:pointer;font-weight:800;font-size:16px}
.e{color:#f04452;font-size:13px;margin-bottom:8px;font-weight:600}</style></head><body>
<form method=post><h2>Lassi<span>.</span></h2><div class=s>교과서 v3 + 참고서 · 자동매매</div>
{% if error %}<div class=e>{{error}}</div>{% endif %}
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


if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False)
