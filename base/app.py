# -*- coding: utf-8 -*-
"""Lassi 대시보드 v8 — 반응형(데스크톱 2열/모바일 1열) + 종목상세 + 봇상태배너 + AI챗.

조회 전용(구봇 0). 계좌=toss_api. 원금=원가기준 자동. 자동로그인. 봇상태=crontab.
종목 클릭→매수이유+참고서. AI챗=Gemini. 색: 한국식(빨강=상승/파랑=하락).
"""
import os, sys, csv, datetime, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from flask import Flask, request, redirect, url_for, render_template_string, jsonify
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from base.database import get_db_connection, verify_user, init_db
from base.toss_api import TossInvestApi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def P(f):
    return os.path.join(ROOT, f)

app = Flask(__name__)
app.secret_key = os.environ.get('LASSI_SECRET', 'lassi-dash-v8')
login_manager = LoginManager(app)
login_manager.login_view = 'login'
AUTO_LOGIN = True
AUTO_LOGIN_UID = '1'
_MASTER = None


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


@app.before_request
def _auto_login():
    if not AUTO_LOGIN or request.endpoint in ('login', 'logout', 'static'):
        return
    if not current_user.is_authenticated:
        row = _user_row(AUTO_LOGIN_UID)
        if row:
            login_user(User(row), remember=True)


def _toss(row):
    return TossInvestApi(row['toss_client_id'], row['toss_client_secret'], row['toss_account_seq'] or '')


def _master():
    global _MASTER
    if _MASTER is None:
        _MASTER = {}
        try:
            with open(P('reference_data/stock_master.csv'), encoding='utf-8-sig') as f:
                for r in csv.DictReader(f):
                    _MASTER[str(r.get('ticker', '')).zfill(6)] = r
        except Exception:
            pass
    return _MASTER


def kr_snapshot(row):
    out = {'holdings': [], 'cash': 0, 'total': 0, 'hold_val': 0, 'cost_basis': 0,
           'ret': None, 'pl': 0, 'error': None, 'alloc': [], 'conic': ''}
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
            tk = str(s['ticker'])
            out['holdings'].append({'name': s.get('name', tk), 'ticker': tk, 'qty': q, 'price': px,
                                    'buy': bp, 'value': val, 'plpct': (px / bp - 1) * 100 if bp else 0,
                                    'is_etf': is_etf, 'hue': (int(tk) % 360) if tk.isdigit() else 210})
        cash = t.get_buyable_cash(default=None)
        cash = float(cash) if cash is not None else 0.0
        out['cash'] = cash; out['hold_val'] = hv; out['total'] = cash + hv
        out['cost_basis'] = cost + cash
        if out['cost_basis'] > 0:
            out['ret'] = (out['total'] / out['cost_basis'] - 1) * 100
            out['pl'] = out['total'] - out['cost_basis']
        out['holdings'].sort(key=lambda x: (-x['is_etf'], -x['value']))
        tot = out['total'] or 1
        cum = 0.0; stops = []
        for label, v, col in [('지수 ETF', etf_v, '#3182f6'), ('저변동 25종목', stk_v, '#20c997'), ('현금', cash, '#c4cdd8')]:
            f = v / tot * 100
            out['alloc'].append({'label': label, 'val': v, 'pct': f, 'color': col})
            stops.append(f"{col} {cum:.2f}% {cum + f:.2f}%")
            cum += f
        out['conic'] = ', '.join(stops)
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


def bot_status():
    st = {'kr': None, 'us': None, 'deadman': None, 'heartbeat': '—', 'rebal': '—', 'artifact': '—'}
    try:
        cron = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5).stdout
        st['kr'] = ('auto_deploy.py --execute' in cron) or ('auto_order.py --rebalance --execute' in cron)
        st['us'] = 'auto_order_us.py --execute' in cron
        st['deadman'] = 'deadman.py' in cron
    except Exception:
        pass
    for k, f in [('heartbeat', 'heartbeat.txt'), ('rebal', 'rebalance_state.txt')]:
        try:
            st[k] = open(P(f)).read().strip()[:24] or '—'
        except Exception:
            pass
    try:
        from KR.reference import artifact_tickers
        st['artifact'] = f"{len(artifact_tickers())}종목 제외"
    except Exception:
        pass
    return st


def recent_trades(uid, n=30):
    c = get_db_connection()
    try:
        rows = c.execute("SELECT stock_name, action, price, shares, created_at, mode "
                         "FROM trade_journal WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, n)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        c.close()


def _gemini(key, prompt):
    import requests
    last = 'unknown'
    for model in ('gemini-2.5-flash', 'gemini-2.0-flash-001'):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=20)
            j = r.json()
            if 'candidates' in j:
                return j['candidates'][0]['content']['parts'][0]['text']
            last = j.get('error', {}).get('message', str(j))
        except Exception as e:
            last = str(e)
    return f"(AI 응답 실패: {last[:120]})"


PAGE = """<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Lassi</title><style>
:root{--bg:#eef1f6;--card:#fff;--txt:#191f28;--sub:#8b95a1;--up:#f04452;--down:#3182f6;--pri:#3182f6;--soft:#f6f8fb;--grn:#12b886}
*{box-sizing:border-box;margin:0;-webkit-tap-highlight-color:transparent}
body{font-family:-apple-system,'Malgun Gothic','Apple SD Gothic Neo',system-ui,sans-serif;background:var(--bg);color:var(--txt);letter-spacing:-.3px;font-size:14px}
.wrap{max-width:1060px;margin:0 auto;padding:14px 20px 60px}
.top{display:flex;justify-content:space-between;align-items:center;padding:4px 2px}
.logo{font-size:21px;font-weight:800} .logo em{font-style:normal;color:var(--pri)}
.top a{color:var(--sub);text-decoration:none;font-size:13px;font-weight:600}
.note{font-size:11.5px;color:var(--sub);margin:2px 2px 10px} .note a{color:var(--pri);text-decoration:none}
/* 봇 상태 배너 */
.banner{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.bstat{flex:1;min-width:150px;display:flex;align-items:center;gap:11px;background:var(--card);border-radius:14px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,20,60,.05),0 6px 18px rgba(0,25,80,.04)}
.led{width:11px;height:11px;border-radius:50%;flex-shrink:0} .led.on{background:var(--grn);box-shadow:0 0 0 4px rgba(18,184,134,.18)} .led.off{background:#cbd3dd}
.bstat .bl{font-size:12px;color:var(--sub);font-weight:600} .bstat .bv{font-size:15px;font-weight:800;margin-top:1px}
.bstat .bv.on{color:var(--grn)} .bstat .bv.off{color:var(--sub)}
.seg{display:flex;background:#e3e8ef;border-radius:12px;padding:3px;gap:3px;margin:0 0 10px;max-width:360px}
.seg div{flex:1;text-align:center;padding:9px;border-radius:9px;font-weight:700;font-size:14.5px;color:var(--sub);cursor:pointer;transition:.15s}
.seg div.on{background:#fff;color:var(--txt);box-shadow:0 1px 5px rgba(0,20,60,.1)}
/* 반응형 2열 */
.grid{display:grid;grid-template-columns:1fr;gap:16px} @media(min-width:840px){.grid{grid-template-columns:1.25fr 1fr;align-items:start}}
.pane{display:none} .pane.on{display:block;animation:f .2s} @keyframes f{from{opacity:0;transform:translateY(6px)}to{opacity:1}}
.card{background:var(--card);border-radius:18px;padding:18px;margin-bottom:14px;box-shadow:0 1px 2px rgba(0,20,60,.05),0 6px 18px rgba(0,25,80,.05)}
.h{font-size:14px;font-weight:800;margin:0 4px 8px}
/* hero 중앙 */
.hero{background:linear-gradient(135deg,#fff,#f3f7ff);text-align:center;padding:24px 18px}
.hero .lab{font-size:13px;color:var(--sub);font-weight:600}
.hero .amt{font-size:34px;font-weight:800;margin:4px 0 12px;letter-spacing:-1.4px} .hero .amt small{font-size:18px;color:var(--sub)}
.pill{display:inline-flex;align-items:center;gap:5px;font-size:15px;font-weight:800;padding:7px 14px;border-radius:12px}
.up{color:var(--up)} .down{color:var(--down)} .pill.up{background:#fdeaec} .pill.down{background:#e9f1fe}
.donut{display:flex;align-items:center;gap:16px}
.dc{position:relative;width:118px;height:118px;flex-shrink:0} .pie{width:100%;height:100%;border-radius:50%}
.hole{position:absolute;inset:20px;background:#fff;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center}
.hole .t1{font-size:10px;color:var(--sub);font-weight:600} .hole .t2{font-size:17px;font-weight:800}
.leg{flex:1} .legrow{display:flex;align-items:center;gap:8px;padding:6px 0}
.dot{width:11px;height:11px;border-radius:3px;flex-shrink:0} .legrow .ln{flex:1;font-size:13.5px;font-weight:700}
.legrow .lv{font-size:11.5px;color:var(--sub);font-weight:500} .legrow .lp{font-weight:800;font-size:14.5px}
/* 보유종목 — 매입/현재 구분 강화 + 클릭 */
.hold{display:flex;align-items:center;gap:12px;padding:12px 6px;border-bottom:1px solid #f2f4f7;cursor:pointer;border-radius:10px;transition:.12s} .hold:last-child{border:0}
.hold:hover{background:var(--soft)}
.hicon{width:38px;height:38px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;color:#fff;flex-shrink:0}
.hicon.etf{background:#e7f0ff !important;color:var(--pri)}
.hmid{flex:1;min-width:0} .hnm{font-weight:700;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prices{display:flex;gap:12px;margin-top:3px;font-size:12px}
.prices .pb{color:var(--sub)} .prices .pc{font-weight:700}
.prices b{font-weight:800} .chev{color:#c4cdd8;font-size:16px;margin-left:2px}
.hend{text-align:right;flex-shrink:0} .hval{font-weight:800;font-size:15px} .hpl{font-size:12.5px;font-weight:800;margin-top:2px}
.st{display:flex;justify-content:space-between;padding:9px 2px;border-bottom:1px solid #f2f4f7;font-size:13.5px} .st:last-child{border:0}
.st .kk{color:var(--sub);font-weight:600} .st .vv{font-weight:700}
.warn{background:#fff4e5;color:#c2681a;font-weight:600} .mut{color:var(--sub)}
.cap{font-size:11.5px;color:var(--sub);margin:8px 3px 0;line-height:1.5}
.tag{display:inline-block;font-size:11px;font-weight:800;padding:2px 6px;border-radius:7px;margin-right:5px}
.tag.b{background:#fdeaec;color:var(--up)} .tag.s{background:#e9f1fe;color:var(--down)}
details{margin-top:6px} summary{cursor:pointer;font-size:13px;color:var(--sub);font-weight:700;padding:5px 2px;list-style:none} summary::-webkit-details-marker{display:none} summary:before{content:'▸ '} details[open] summary:before{content:'▾ '}
.chat .msgs{max-height:260px;overflow-y:auto;display:flex;flex-direction:column;gap:8px;padding:2px}
.m{max-width:85%;padding:9px 12px;border-radius:14px;font-size:13.5px;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.m.u{align-self:flex-end;background:var(--pri);color:#fff;border-bottom-right-radius:4px} .m.a{align-self:flex-start;background:var(--soft);border-bottom-left-radius:4px}
.cin{display:flex;gap:8px;margin-top:10px} .cin input{flex:1;padding:11px;border:1.5px solid #eef1f5;border-radius:12px;font-size:14px;background:var(--soft)}
.cin input:focus{outline:none;border-color:var(--pri);background:#fff} .cin button{padding:11px 15px;background:var(--pri);color:#fff;border:0;border-radius:12px;font-weight:800;cursor:pointer}
/* 모달 */
.modal{display:none;position:fixed;inset:0;background:rgba(15,25,50,.45);z-index:20;align-items:center;justify-content:center;padding:16px}
.modal.on{display:flex} .sheet{background:#fff;border-radius:22px;width:100%;max-width:440px;padding:24px;max-height:86vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,20,60,.3)}
.sheet h3{font-size:19px;margin-bottom:3px} .sheet .sub{color:var(--sub);font-size:12.5px;margin-bottom:16px}
.mrow{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #f2f4f7;font-size:14px} .mrow .k{color:var(--sub);font-weight:600}
.rsn{background:var(--soft);border-radius:14px;padding:14px;font-size:13.5px;line-height:1.6;margin:14px 0}
.rsn b{color:var(--pri)} .mclose{width:100%;padding:13px;background:#f1f3f7;border:0;border-radius:13px;font-weight:800;cursor:pointer;margin-top:8px;font-size:15px}
.foot{text-align:center;font-size:11.5px;color:#b6bdc7;margin:16px 0 0}
</style></head><body><div class=wrap>
<div class=top><div class=logo>Lassi<em>.</em></div><a href="{{url_for('logout')}}">로그아웃</a></div>
<div class=note>{{now}} · 조회 전용(주문 안 냄) · <a href="{{url_for('dashboard')}}">↻ 새로고침</a></div>

<div class=banner>
  <div class=bstat><span class="led {{'on' if bot.kr else 'off'}}"></span><div><div class=bl>국내 자동매매</div><div class="bv {{'on' if bot.kr else 'off'}}">{{ '가동중' if bot.kr else '정지' }}</div></div></div>
  <div class=bstat><span class="led {{'on' if bot.us else 'off'}}"></span><div><div class=bl>미국 자동매매</div><div class="bv {{'on' if bot.us else 'off'}}">{{ '가동중' if bot.us else '정지' }}</div></div></div>
  <div class=bstat><span class="led {{'on' if bot.deadman else 'off'}}"></span><div><div class=bl>감시 (deadman)</div><div class="bv {{'on' if bot.deadman else 'off'}}">{{ 'ON' if bot.deadman else 'OFF' }}</div></div></div>
</div>

<div class=seg><div class="on" onclick="sw('kr')">🇰🇷 국내</div><div onclick="sw('us')">🇺🇸 미국</div></div>

<div class=grid>
<div><!-- 왼쪽: 계좌/포트폴리오/보유 -->
<div id=kr class="pane on">
{% if kr.error %}<div class="card warn">⚠️ {{kr.error}}</div>{% else %}
<div class="card hero"><div class=lab>총 자산</div><div class=amt>{{ '{:,.0f}'.format(kr.total) }}<small> 원</small></div>
  <span class="pill {{'up' if (kr.ret or 0)>=0 else 'down'}}">{{ '▲' if (kr.ret or 0)>=0 else '▼' }} {{ '%.2f'|format(kr.ret|abs) if kr.ret is not none else '—' }}% <span style=opacity:.5>·</span> {{ '{:+,.0f}'.format(kr.pl) }}원</span></div>
<div class=card><div class=lab style=margin-bottom:12px>포트폴리오 구성</div>
  <div class=donut><div class=dc><div class=pie style="background:conic-gradient({{kr.conic}})"></div>
    <div class=hole><div class=t1>보유</div><div class=t2>{{kr.holdings|length}}개</div></div></div>
    <div class=leg>{% for s in kr.alloc %}<div class=legrow><span class=dot style=background:{{s.color}}></span>
      <span class=ln>{{s.label}}<div class=lv>{{ '{:,.0f}'.format(s.val) }}원</div></span>
      <span class=lp>{{ '%.0f'|format(s.pct) }}%</span></div>{% endfor %}</div></div>
  {% if kr.alloc[0].pct < 40 %}<div class=cap>⚠️ 지수 비중 부족 · 현금 {{ '%.0f'|format(kr.alloc[2].pct) }}% 재배분 대기</div>{% endif %}</div>
<div class=card><div class=h style=margin-bottom:2px>보유 종목 {{kr.holdings|length}} <span class=mut style=font-weight:500;font-size:11px>· 종목 누르면 매수이유</span></div>
{% for h in kr.holdings %}<div class=hold onclick="openStock('{{h.ticker}}','{{h.name}}',{{h.qty}},{{h.buy}},{{h.price}},{{h.plpct}},{{'1' if h.is_etf else '0'}})">
  <div class="hicon {{'etf' if h.is_etf}}" {% if not h.is_etf %}style="background:linear-gradient(135deg,hsl({{h.hue}},62%,58%),hsl({{h.hue}},66%,47%))"{% endif %}>{{ '📊' if h.is_etf else h.name[:2] }}</div>
  <div class=hmid><div class=hnm>{{h.name}}</div>
    <div class=prices><span class=pb>매입 {{ '{:,.0f}'.format(h.buy) }}</span><span class="pc {{'up' if h.plpct>=0 else 'down'}}">현재 <b>{{ '{:,.0f}'.format(h.price) }}</b></span></div></div>
  <div class=hend><div class=hval>{{ '{:,.0f}'.format(h.value) }}</div><div class="hpl {{'up' if h.plpct>=0 else 'down'}}">{{ '%+.1f'|format(h.plpct) }}%</div></div>
  <span class=chev>›</span></div>{% endfor %}
{% if not kr.holdings %}<div class="mut" style=text-align:center;padding:16px>보유 종목 없음</div>{% endif %}</div>
{% endif %}</div>

<div id=us class=pane>
{% if us.error %}<div class="card warn">⚠️ {{us.error}}</div>{% else %}
<div class="card hero"><div class=lab>USD 예수금</div><div class=amt>${{ '%.2f'|format(us.cash_usd) }}</div>
<div class=cap style=margin-top:1px>전략 = SPY 보유. 환전하면 크론이 통화검증 후 자동매수.</div></div>
{% if us.holdings %}<div class=card>{% for h in us.holdings %}<div class=hold style=cursor:default>
<div class=hicon style=background:linear-gradient(135deg,#f04452,#d63a48)>{{h.ticker[:3]}}</div><div class=hmid><div class=hnm>{{h.ticker}}</div></div>
<div class=hend><div class=hval>{{ '%.4f'|format(h.qty) }}주</div></div></div>{% endfor %}</div>
{% else %}<div class="card mut" style=text-align:center;padding:24px>SPY 미보유<br><span style=font-size:12px>USD 환전 시 자동매수 대기</span></div>{% endif %}{% endif %}</div>
</div>

<div><!-- 오른쪽: AI / 자동화상세 / 거래 -->
<div class=card chat><div class=h style=margin-bottom:10px>💬 AI 어시스턴트 <span class=mut style=font-weight:500;font-size:11px>· 조회·조언만</span></div>
  <div class=msgs id=msgs><div class="m a">안녕하세요! 포트폴리오·전략에 대해 물어보세요. 예: "지금 수익률 어때?", "참고서가 뭐야?", "왜 현금이 많아?"</div></div>
  <div class=cin><input id=ci placeholder="메시지 입력..." onkeydown="if(event.key=='Enter')send()"><button onclick=send()>전송</button></div></div>

<div class=card><div class=h style=margin-bottom:6px>⚙️ 자동화 상세</div>
  <div class=st><span class=kk>리밸런스 상태</span><span class=vv>{{bot.rebal}}</span></div>
  <div class=st><span class=kk>참고서 필터</span><span class=vv>{{bot.artifact}}</span></div>
  <div class=st><span class=kk>감시 heartbeat</span><span class=vv>{{bot.heartbeat}}</span></div>
  <div class=cap>크론: 리밸 평일10:00 · 신규배분 평일10:30 · US 미국장 · deadman 매일</div></div>

<div class=card><div class=h style=margin-bottom:4px>📜 최근 거래</div>
  <details {{ 'open' if trades|length<=4 else '' }}><summary>{{trades|length}}건 보기</summary>
  {% for t in trades %}<div class=st><span><span class="tag {{'s' if t.action=='SELL' else 'b'}}">{{ '매도' if t.action=='SELL' else '매수' }}</span>{{t.stock_name}}</span>
  <span class=vv>{{ '{:,.0f}'.format(t.price) }} <span class=mut style=font-weight:500;font-size:11px>{{t.created_at[5:16]}}</span></span></div>{% endfor %}
  {% if not trades %}<div class="mut" style=text-align:center;padding:10px>거래 없음</div>{% endif %}</details></div>
</div>
</div>

<div class=foot>Lassi · 조회 전용 · 매매는 검증된 크론이 담당</div>
</div>

<div id=modal class=modal onclick="if(event.target==this)closeM()"><div class=sheet id=sheet></div></div>

<script>
function sw(x){document.querySelectorAll('.seg div').forEach(t=>t.classList.remove('on'));
document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
document.getElementById(x).classList.add('on');event.currentTarget.classList.add('on');}
function closeM(){document.getElementById('modal').classList.remove('on');}
async function openStock(tk,nm,qty,buy,price,pl,etf){
var col=pl>=0?'#f04452':'#3182f6';
var s=document.getElementById('sheet');
s.innerHTML='<h3>'+nm+'</h3><div class=sub>'+tk+(etf==1?' · 지수 ETF':' · 저변동 선정')+'</div>'
+'<div class=mrow><span class=k>보유</span><span>'+qty+'주</span></div>'
+'<div class=mrow><span class=k>매입가</span><span>'+buy.toLocaleString()+'원</span></div>'
+'<div class=mrow><span class=k>현재가</span><span style="color:'+col+';font-weight:800">'+price.toLocaleString()+'원 ('+(pl>=0?'+':'')+pl.toFixed(1)+'%)</span></div>'
+'<div class=mrow><span class=k>평가액</span><span style=font-weight:800>'+(qty*price).toLocaleString()+'원</span></div>'
+'<div class=rsn id=rsn>불러오는 중…</div><button class=mclose onclick=closeM()>닫기</button>';
document.getElementById('modal').classList.add('on');
try{var r=await fetch('/api/stock/'+tk);var j=await r.json();
document.getElementById('rsn').innerHTML=j.html;}catch(e){document.getElementById('rsn').textContent='정보 로드 실패';}}
async function send(){var i=document.getElementById('ci'),m=document.getElementById('msgs'),v=i.value.trim();if(!v)return;
i.value='';m.innerHTML+='<div class="m u">'+v.replace(/</g,'&lt;')+'</div>';
var a=document.createElement('div');a.className='m a';a.textContent='…';m.appendChild(a);m.scrollTop=m.scrollHeight;
try{var r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:v})});
var j=await r.json();a.textContent=j.reply||'(응답 없음)';}catch(e){a.textContent='(오류)';}m.scrollTop=m.scrollHeight;}
</script></body></html>"""

LOGIN = """<!doctype html><html lang=ko><head><meta charset=utf-8><title>Lassi 로그인</title><style>
body{font-family:-apple-system,'Malgun Gothic',system-ui,sans-serif;background:linear-gradient(160deg,#eef1f6,#e3ecf9);color:#191f28;display:flex;height:100vh;align-items:center;justify-content:center;margin:0}
form{background:#fff;padding:34px 28px;border-radius:22px;width:310px;box-shadow:0 20px 50px rgba(0,25,80,.12)}
h2{margin:0 0 4px;font-size:25px} h2 span{color:#3182f6} .s{color:#8b95a1;font-size:13px;margin-bottom:18px}
input{width:100%;padding:14px;margin:6px 0;background:#f6f8fb;border:1.5px solid #eef1f5;color:#191f28;border-radius:12px;font-size:15px}
input:focus{outline:none;border-color:#3182f6;background:#fff}
button{width:100%;padding:14px;background:#3182f6;color:#fff;border:0;border-radius:12px;margin-top:12px;cursor:pointer;font-weight:800;font-size:16px}
.e{color:#f04452;font-size:13px;margin-bottom:6px;font-weight:600}</style></head><body>
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
        trades=recent_trades(int(row['id'])), bot=bot_status())


@app.route('/api/stock/<ticker>')
@login_required
def api_stock(ticker):
    tk = str(ticker).zfill(6)
    m = _master().get(tk, {})
    if tk == '069500':
        html = ("<b>지수 슬리브 (코스피200)</b><br>KODEX200 = 코스피200 시총가중 ETF. "
                "폭등장 참여를 담당하는 <b>포트폴리오의 50%</b> 목표. 지수를 그대로 추종해 "
                "저변동 슬리브의 상승장 열위를 보완합니다.")
    else:
        tier = {'clean': '데이터 정상', 'watch': '검토(단일신호)', 'confirmed': '⚠️아티팩트'}.get(m.get('artifact_tier'), '—')
        html = ("<b>v3 저변동 선정</b><br>이 종목은 매 분기 '<b>126일 변동성이 낮은 상위</b> + "
                "가격이 <b>상승중인 200일선 위</b> + 부실(자본잠식·2년적자)·정체가격·거래일부족 <b>제외</b>' "
                "규칙을 통과해 저변동 25종목(동일비중)에 편입됐습니다. 타이밍·손절 없이 분기 보유.<br><br>"
                f"· 참고서 데이터: <b>{tier}</b><br>· 수익패턴: {m.get('pattern', '—')}")
    return jsonify(html=html)


@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    msg = (request.json or {}).get('message', '') if request.is_json else request.form.get('message', '')
    msg = (msg or '').strip()[:500]
    if not msg:
        return jsonify(reply='메시지를 입력해주세요.')
    row = current_user.row
    key = row['gemini_api_key']
    if not key:
        return jsonify(reply='Gemini API 키가 설정되어 있지 않습니다.')
    kr = kr_snapshot(row)
    ctx = (f"총자산 {kr['total']:,.0f}원, 미실현수익률 {kr['ret']:.2f}%, 보유 {len(kr['holdings'])}종목, "
           f"현금 {kr['cash']:,.0f}원(미투입). 전략=KODEX200 지수ETF 50% + v3저변동 25종목 50%, 분기 리밸런스, "
           f"참고서(데이터아티팩트·부실상폐 회피). US=SPY. 매매는 EC2 크론 자동.") if not kr['error'] else '계좌조회 실패'
    prompt = ("너는 Lassi 자동투자 대시보드 어시스턴트다. 아래 맥락으로 사용자 질문에 한국어로 간결·친근하게 답해라. "
              "매매 실행은 못 하고 설명·조언만 한다.\n\n[포트폴리오]\n" + ctx + "\n\n[질문]\n" + msg)
    return jsonify(reply=_gemini(key, prompt))


if __name__ == '__main__':
    try:
        init_db()
    except Exception:
        pass
    app.run(host='0.0.0.0', port=5000, debug=False)
