// ══════════════════════════════════════════════════════════════
// 💱 통화 전환 (₩ KRW ↔ $ USD)
// ══════════════════════════════════════════════════════════════
window.FX = { mode: 'KRW', rate: 1 };   // rate: USD/KRW 환율 (e.g. 1516)

/** KRW 값을 현재 통화 모드에 맞게 포맷 */
window.fmtMoney = function(krw, { sign = false, unit = true } = {}) {
    const v = (FX.mode === 'USD' && FX.rate > 1) ? krw / FX.rate : Math.round(krw);
    const isUsd = FX.mode === 'USD' && FX.rate > 1;
    const prefix = sign ? (krw >= 0 ? '+' : '') : '';
    if (isUsd) {
        const formatted = Math.abs(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
        return prefix + (krw < 0 ? '-' : '') + '$' + formatted;
    }
    return prefix + v.toLocaleString() + (unit ? '원' : '');
};

/** 단가(주당 가격) 포맷 — 소수점 없이 */
window.fmtPrice = function(krw) {
    if (FX.mode === 'USD' && FX.rate > 1) {
        return '$' + (krw / FX.rate).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }
    return Math.round(krw).toLocaleString() + '원';
};

/** 통화 모드 전환 + 버튼 스타일 + UI 전체 재렌더 */
window.setCurrency = function(mode) {
    FX.mode = mode;
    const btnKrw = document.getElementById('btn-currency-krw');
    const btnUsd = document.getElementById('btn-currency-usd');
    if (btnKrw && btnUsd) {
        const activeStyle  = 'background:rgba(255,255,255,0.18);color:#f1f5f9;';
        const inactiveStyle = 'background:rgba(255,255,255,0.04);color:#94a3b8;';
        btnKrw.style.cssText = btnKrw.style.cssText.replace(/background:[^;]+;color:[^;]+;/, mode === 'KRW' ? activeStyle : inactiveStyle);
        btnUsd.style.cssText = btnUsd.style.cssText.replace(/background:[^;]+;color:[^;]+;/, mode === 'USD' ? activeStyle : inactiveStyle);
    }
    // 마지막으로 받은 status 데이터로 UI 즉시 재렌더
    if (window._lastStatusData) updateUI(window._lastStatusData);
};

// ── Toast 알림 ──
function showToast(message, type = 'success', duration = 3000) {
    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        container.style.cssText = 'position:fixed;top:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;';
        document.body.appendChild(container);
    }
    const toast = document.createElement('div');
    const borderColor = type === 'success' ? 'rgba(63,185,80,0.5)' : type === 'error' ? 'rgba(248,81,73,0.5)' : 'rgba(88,166,255,0.5)';
    toast.style.cssText = `padding:12px 20px;border-radius:12px;font-size:0.875rem;font-weight:600;color:#e6edf3;background:rgba(22,27,34,0.97);border:1px solid ${borderColor};backdrop-filter:blur(12px);box-shadow:0 8px 32px rgba(0,0,0,0.4);min-width:200px;transition:all 0.35s cubic-bezier(0.34,1.56,0.64,1);transform:translateX(0);opacity:1;`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.transform = 'translateX(120%)';
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 380);
    }, duration);
}

document.addEventListener('DOMContentLoaded', () => {

    // ── DOM refs ──
    const btnToggle = document.getElementById('btn-toggle');
    const toggleLabel = document.getElementById('toggle-label');
    const miniLog = document.getElementById('mini-log');
    const satTbody = document.getElementById('sat-tbody');

    // ── Toggle Button ──
    btnToggle.addEventListener('click', () => {
        btnToggle.disabled = true;
        const prevLabel = toggleLabel.textContent;
        toggleLabel.textContent = '처리 중...';
        fetch('/api/toggle', { method: 'POST' })
            .then(async r => {
                if (!r.ok) {
                    const err = await r.json();
                    showToast(err.message || '봇 시작 실패', 'error');
                }
                return r.json();
            })
            .then(() => fetchStatus())
            .catch(e => { console.error('Toggle error', e); toggleLabel.textContent = prevLabel; })
            .finally(() => { btnToggle.disabled = false; });
    });

    // ── Status Fetch ──
    function fetchStatus() {
        fetch('/api/status')
            .then(r => r.json())
            .then(data => updateUI(data))
            .catch(e => console.error('status fetch error', e));
    }

    // ── 모멘텀 슬롯 카드 렌더링: 보유 중인 종목만 동적 생성 ──
    function renderDefensiveAssets(defensiveList, regime) {
        const container = document.getElementById('defensive-slots');
        const badge     = document.getElementById('defensive-regime-badge');
        if (!container) return;

        const isBear = (regime === 'BEAR');

        // 배지 업데이트
        if (badge) {
            if (isBear) {
                badge.textContent = '🐻 BEAR — 헤지 가동 중';
                badge.style.background    = 'rgba(248,81,73,0.15)';
                badge.style.borderColor   = 'rgba(248,81,73,0.45)';
                badge.style.color         = '#fca5a5';
            } else {
                badge.textContent = regime === 'BULL' ? '🚀 BULL — 대기' : '➡️ NEUTRAL — 대기';
                badge.style.background    = 'rgba(255,255,255,0.04)';
                badge.style.borderColor   = 'rgba(255,255,255,0.12)';
                badge.style.color         = '#8b949e';
            }
        }

        if (!defensiveList || defensiveList.length === 0) {
            container.innerHTML = '<div style="color:#6b7280;font-size:0.84rem;padding:14px 4px;text-align:center;">방어자산 데이터 없음</div>';
            return;
        }

        container.style.display = 'grid';
        container.style.gridTemplateColumns = 'repeat(3, 1fr)';

        // 테마 감지: theme-us(밝은 배경) vs 기본(어두운 배경)
        const isLightTheme = document.body.classList.contains('theme-us');
        const clr = isLightTheme
            ? { name: '#111827', ticker: '#374151', price: '#111827', label: '#6b7280', valueTxt: '#374151' }
            : { name: '#e6edf3', ticker: '#64748b', price: '#e6edf3', label: '#6b7280', valueTxt: '#94a3b8' };

        container.innerHTML = defensiveList.map(asset => {
            const holding  = asset.shares > 0;
            const priceStr = asset.price > 0 ? fmtPrice(asset.price) : null;
            const valueStr = holding ? fmtMoney(asset.value) : '-';
            const ratioStr = (asset.ratio * 100).toFixed(0) + '% 배정';

            // 등락률 — 상승 빨강, 하락 파랑
            const chg = asset.change_pct || 0;
            const chgColor = chg > 0 ? '#f85149' : (chg < 0 ? '#58a6ff' : '#6b7280');
            const chgSign  = chg > 0 ? '+' : '';
            const chgStr   = priceStr && chg !== 0
                ? `<span style="color:${chgColor};font-size:0.78rem;margin-left:5px;">${chgSign}${chg.toFixed(2)}%</span>`
                : '';

            let borderColor, bgColor, statusText, statusColor;
            if (isBear && holding) {
                borderColor = 'rgba(248,81,73,0.5)'; bgColor = 'rgba(248,81,73,0.06)';
                statusText = `${asset.shares.toLocaleString()}주 보유 중`; statusColor = '#ef4444';
            } else if (isBear) {
                borderColor = 'rgba(245,158,11,0.45)'; bgColor = 'rgba(245,158,11,0.05)';
                statusText = 'BEAR — 매수 대기'; statusColor = '#d97706';
            } else {
                borderColor = isLightTheme ? 'rgba(0,0,0,0.1)' : 'rgba(255,255,255,0.1)';
                bgColor = isLightTheme ? 'rgba(0,0,0,0.02)' : 'transparent';
                statusText = holding ? `${asset.shares.toLocaleString()}주 보유` : '비활성 (대기)';
                statusColor = holding ? '#2563eb' : '#6b7280';
            }

            return `<div style="border:1px solid ${borderColor};background:${bgColor};border-radius:12px;padding:14px 16px;transition:all 0.3s;">
                <div style="font-size:0.72rem;color:${clr.label};margin-bottom:6px;">${asset.emoji} ${ratioStr}</div>
                <div style="font-size:0.95rem;font-weight:700;color:${clr.name};margin-bottom:4px;">${asset.name}</div>
                <div style="font-size:0.78rem;color:${clr.ticker};margin-bottom:8px;">${asset.ticker}</div>
                <div style="font-size:0.88rem;color:${clr.valueTxt};">
                    ${priceStr
                        ? `<span style="color:${clr.price};font-weight:600;">${priceStr}</span>${chgStr}`
                        : '<span style="color:#9ca3af;">현재가 조회 중</span>'}
                </div>
                ${holding ? `<div style="font-size:0.82rem;color:${clr.valueTxt};margin-top:2px;">평가 ${valueStr}</div>` : ''}
                <div style="margin-top:8px;font-size:0.78rem;font-weight:600;color:${statusColor};">${statusText}</div>
            </div>`;
        }).join('');
    }

    // 🟢 팝업창(모달)을 띄우는 함수
    window.showStatusModal = function (name, message) {
        document.getElementById('modalTickerName').innerText = `[${name}] 진행 상황`;
        document.getElementById('modalStatusMsg').innerText = message;
        document.getElementById('statusModal').style.display = 'flex';
    }
    // 📦 onclick 속성용 — encodeURIComponent로 개행·특수문자 안전 처리
    window.showStatusModalEncoded = function (name, encodedMsg) {
        showStatusModal(name, decodeURIComponent(encodedMsg));
    }

    // 🚫 블랙리스트 즉시 초기화
    window.clearBlacklist = function () {
        const btn = document.getElementById('clearBlBtn');
        if (btn) { btn.disabled = true; btn.textContent = '초기화 중...'; }
        fetch('/api/clear_blacklist', { method: 'POST', headers: {'Content-Type': 'application/json'} })
            .then(r => r.json())
            .then(d => {
                alert(d.message || '완료');
                if (btn) { btn.disabled = false; btn.textContent = '🚫 블랙리스트 초기화'; }
            })
            .catch(() => {
                alert('초기화 실패');
                if (btn) { btn.disabled = false; btn.textContent = '🚫 블랙리스트 초기화'; }
            });
    }

    // ── Main UI Update ──
    function updateUI(data) {
        window._lastStatusData = data;   // 통화 전환 시 재렌더링용 스냅샷
        if (data.initial_cash !== undefined) {
            USER_INVESTED_CAPITAL = data.initial_cash;
        }

        // US 봇: us_total_asset / KR 봇(base_bot 레거시): mock_total_asset
        const _totalAsset = data.us_total_asset ?? data.mock_total_asset;
        const _pnl        = data.us_pnl        ?? data.mock_pnl;
        const _pnlRt      = data.us_pnl_rt     ?? data.mock_pnl_rt;
        if (_totalAsset !== undefined) {
            const totalValEl = document.getElementById('total-value');
            if (totalValEl) {
                totalValEl.textContent = fmtMoney(_totalAsset);
                // 수익 여부에 따라 색상: 이익 → 빨강, 손실 → 파랑, 중립 → 기본
                // data-pnl 속성도 함께 설정: theme-us CSS 덮어쓰기용
                if (_pnl !== undefined) {
                    const pnlState = _pnl > 0 ? 'profit' : (_pnl < 0 ? 'loss' : 'neutral');
                    totalValEl.style.color = _pnl > 0 ? '#f85149' : (_pnl < 0 ? '#58a6ff' : '');
                    totalValEl.dataset.pnl = pnlState;
                }
            }
        }
        if (_pnl !== undefined && _pnlRt !== undefined) {
            const pnlEl = document.getElementById('total-pnl');
            if (pnlEl) {
                const sign = _pnl >= 0 ? '+' : '';
                const pnlState = _pnl > 0 ? 'profit' : (_pnl < 0 ? 'loss' : 'neutral');
                const color = _pnl > 0 ? '#f85149' : (_pnl < 0 ? '#58a6ff' : '#8b949e');
                pnlEl.style.color = color;
                pnlEl.style.fontWeight = '700';
                pnlEl.dataset.pnl = pnlState;
                pnlEl.textContent = `수익: ${fmtMoney(_pnl, {sign: true})} (${sign}${_pnlRt.toFixed(2)}%)`;
            }
        }

        // 예수금 표시
        if (data.available_cash !== undefined) {
            const cashValEl = document.getElementById('available-cash-val');
            if (cashValEl) {
                cashValEl.textContent = fmtMoney(data.available_cash);
            }
        }

        const isLive = (data.is_mock === false || data.is_mock === 0);
        const isUS   = true;   // US 전용 페이지

        // ── 통화 토글 버튼: US 모드에서만 표시 ──────────────────────────────
        const currencyWrap = document.getElementById('currency-toggle-wrap');
        if (currencyWrap) {
            currencyWrap.style.display = isUS ? 'inline-block' : 'none';
        }
        // KR 모드로 전환되면 항상 원화로 리셋 (달러 표시 잔류 방지)
        if (!isUS && FX.mode !== 'KRW') {
            FX.mode = 'KRW';
            const btnKrw = document.getElementById('btn-currency-krw');
            const btnUsd = document.getElementById('btn-currency-usd');
            if (btnKrw) btnKrw.style.cssText = btnKrw.style.cssText.replace(/background:[^;]+;color:[^;]+;/, 'background:rgba(255,255,255,0.18);color:#f1f5f9;');
            if (btnUsd) btnUsd.style.cssText = btnUsd.style.cssText.replace(/background:[^;]+;color:[^;]+;/, 'background:rgba(255,255,255,0.04);color:#94a3b8;');
        }

        const cb = document.getElementById('modeSwitch');
        const lblReal = document.getElementById('label-real');
        const lblUs = document.getElementById('label-us');

        if (cb && data.is_mock !== undefined) {
            cb.checked = !!data.is_mock;
            if (lblReal && lblUs) {
                if (data.is_mock) {
                    lblUs.classList.add('mode-active');
                    lblReal.classList.remove('mode-active');
                } else {
                    lblReal.classList.add('mode-active');
                    lblUs.classList.remove('mode-active');
                }
            }
        }

        // 테마 클래스 먼저 적용 — renderDefensiveAssets가 isLightTheme 체크 전에 세팅돼야 함
        if (isLive) {
            document.body.classList.remove('theme-us');
        } else {
            document.body.classList.add('theme-us');
        }

        // 방어자산 섹션: KR/US 모두 표시 (US도 PSQ/GLD/UUP 방어 전략 있음)
        renderDefensiveAssets(data.defensive_list, data.market_regime);

        // 선물 위젯 가시성 (모드별 다름)
        if (window.applyFuturesVisibility) window.applyFuturesVisibility(isUS);

        const pnlTitle = document.getElementById('pnl-title');
        if (pnlTitle && data.is_mock !== undefined) {
            pnlTitle.textContent = data.is_mock ? 'US 수익률' : 'KR 수익률';
        }

        const running = data.is_running;
        if (running) {
            btnToggle.className = 'btn-toggle btn-running';
            toggleLabel.textContent = '⏹ Running';
        } else {
            btnToggle.className = 'btn-toggle btn-stopped';
            toggleLabel.textContent = 'Stopped';
        }

        // 반대 모드 봇 실행 상태 배지
        let otherBadge = document.getElementById('other-mode-badge');
        if (data.other_mode_running) {
            if (!otherBadge) {
                otherBadge = document.createElement('span');
                otherBadge.id = 'other-mode-badge';
                otherBadge.style.cssText = 'display:inline-block;margin-left:10px;padding:3px 10px;border-radius:20px;font-size:0.72rem;font-weight:700;background:rgba(63,185,80,0.18);color:#3fb950;border:1px solid rgba(63,185,80,0.4);vertical-align:middle;';
                btnToggle.parentNode.insertBefore(otherBadge, btnToggle.nextSibling);
            }
            otherBadge.textContent = `${data.other_mode_label} 봇 실행 중`;
            otherBadge.style.display = 'inline-block';
        } else {
            if (otherBadge) otherBadge.style.display = 'none';
        }

        if (!data.has_keys) {
            if (!document.getElementById('key-warning')) {
                const warn = document.createElement('div');
                warn.id = 'key-warning';
                warn.style.cssText = 'background: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid #ef4444; padding: 12px; border-radius: 12px; text-align: center; margin-bottom: 25px; font-weight: bold; font-size: 0.9rem;';
                warn.innerHTML = '⚠️ API 키가 설정되지 않았습니다. [계좌 설정] 버튼을 눌러 본인의 KIS 정보를 입력해 주세요.';
                document.querySelector('.dashboard-container').prepend(warn);
            }
        } else {
            const warn = document.getElementById('key-warning');
            if (warn) warn.remove();
        }

        const hotSectorsEl = document.getElementById('hot-sectors');
        if (data.hot_sectors && data.hot_sectors.length > 0) {
            hotSectorsEl.textContent = '🔥 현재 강세 섹터: ' + data.hot_sectors.join(', ');
        } else {
            hotSectorsEl.textContent = '🔥 분석 중이거나 강세 섹터가 없습니다.';
        }

        const cores = data.cores || [];
        const sats = data.satellites || [];

        if (data.num_satellites !== undefined) {
            document.getElementById('sat-num-display').textContent = data.num_satellites;
        }

        if (data.cores) {
            window.cachedCoreStocks = data.cores.map(c => ({ ticker: c.ticker, name: c.name }));
        }

        const topCardsContainer = document.getElementById('top-cards-container');

        // ── 포지션 뱃지 스타일 공통 헬퍼 ──────────────────────────────
        function _positionBadgeStyle(sText) {
            if (sText.includes('AI') || sText.includes('심사')) return "background:rgba(168,85,247,0.2); color:#c084fc; border:1px solid rgba(168,85,247,0.4); animation:pulse 2s infinite;";
            if (sText.includes('주문') || sText.includes('대기')) return "background:rgba(245,158,11,0.2); color:#fcd34d; border:1px solid rgba(245,158,11,0.4); animation:pulse 2s infinite;";
            if (sText.includes('거절') || sText.includes('손절') || sText.includes('청산') || sText.includes('보류')) return "background:rgba(239,68,68,0.2); color:#fca5a5; border:1px solid rgba(239,68,68,0.4);";
            return "background:rgba(255,255,255,0.1); color:#94a3b8; border:1px solid rgba(255,255,255,0.2);";
        }

        // ── 수익률 셀 공통 헬퍼 ──────────────────────────────────────
        function _pnlCell(shares, avgP, curP) {
            if (!(shares > 0)) return '';
            if (avgP > 0 && curP > 0) {
                const pct = ((curP / avgP) - 1) * 100;
                const state = pct > 0 ? 'profit' : (pct < 0 ? 'loss' : 'neutral');
                const clr   = pct > 0 ? '#f85149' : (pct < 0 ? '#58a6ff' : '#8b949e');
                const sign  = pct >= 0 ? '+' : '';
                return `<div class="pnl-rate" data-pnl="${state}" style="font-size:0.75rem;color:${clr};margin-top:3px;font-weight:700;">${sign}${pct.toFixed(2)}%</div>`;
            }
            return `<div class="pnl-rate" data-pnl="neutral" style="font-size:0.75rem;color:#64748b;margin-top:3px;">수익률 계산 중...</div>`;
        }

        // ══════════════════════════════════════════════════════════
        // US 모드: KR과 동일한 카드 레이아웃
        // ══════════════════════════════════════════════════════════
        topCardsContainer.style.display = '';

        const satSection = document.querySelector('.satellite-card:not(#defensive-section)');
        if (satSection) {
            const h2 = satSection.querySelector('h2');
            if (h2) h2.innerHTML = '🚀 Growth Positions';
            const thead = satSection.querySelector('thead tr');
            if (thead) thead.innerHTML = '<th>종목</th><th>현재가</th><th>보유주식</th><th>평가금액</th><th>상태</th>';
        }

        // 코어 카드 렌더
        const satCard = topCardsContainer.lastElementChild;
        document.querySelectorAll('.core-card').forEach(e => e.remove());
        const fragment = document.createDocumentFragment();

        cores.forEach((core) => {
            const sText = core.status || "감시 중 👀";
            const sMsg  = core.status_msg || "지표 점검 중...";
            const isDca = !!core.dca_mode;
            const dcaBtnStyle = isDca
                ? 'background:rgba(16,185,129,0.15);color:#065f46;border:1px solid rgba(16,185,129,0.4);'
                : 'background:rgba(0,0,0,0.05);color:#374151;border:1px solid rgba(0,0,0,0.15);';
            let corePnlHtml = '';
            const coreAvgP = core.avg_price || 0, coreCurP = core.price || 0;
            if (core.shares > 0 && coreAvgP > 0 && coreCurP > 0) {
                const pct = ((coreCurP / coreAvgP) - 1) * 100;
                const state = pct > 0 ? 'profit' : (pct < 0 ? 'loss' : 'neutral');
                const clr   = pct > 0 ? '#dc2626' : (pct < 0 ? '#2563eb' : '#475569');
                corePnlHtml = `<div class="pnl-rate" data-pnl="${state}" style="font-size:0.85rem;font-weight:700;margin-top:3px;color:${clr};">${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%</div>`;
            } else if (core.shares > 0) {
                corePnlHtml = `<div class="pnl-rate" data-pnl="neutral" style="font-size:0.8rem;color:#6b7280;margin-top:3px;">수익률 계산 중...</div>`;
            }
            const div = document.createElement('div');
            div.className = 'info-card glass-card core-card';
            div.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                    <h3 style="margin:0;display:flex;align-items:center;gap:8px;color:#111827;">
                        🏛️ ${core.name} (Core)
                        <span class="badge core-status-badge" data-name="${core.name.replace(/"/g,'&quot;')}" style="cursor:pointer; ${_positionBadgeStyle(sText)}">${sText}</span>
                    </h3>
                    <div style="display:flex;gap:6px;align-items:center;">
                        <button onclick="toggleCoreDCA('${core.ticker}', '${core.name}', ${isDca})"
                            style="font-size:0.7rem;padding:3px 8px;border-radius:6px;cursor:pointer;${dcaBtnStyle}" title="적립식 자동매수 ON/OFF">${isDca ? '💰 DCA ON' : '💰 DCA'}</button>
                    </div>
                </div>
                <div class="card-value highlight" style="color:#111827;">${(core.shares || 0).toLocaleString()} 주</div>
                <div class="card-subvalue" style="color:#374151;">
                    평가금액 ${fmtMoney(core.value || 0)}<br>
                    <span style="color:#6b7280;font-size:0.8rem;">(배정 예산: ${fmtMoney(core.budget || 0)})</span>
                </div>
                ${corePnlHtml}
                <div class="card-subvalue" style="color:#d97706;font-size:0.8rem;margin-top:4px">🔒 floor: ${core.floor || 0}주 보호</div>
            `;
            const badge = div.querySelector('.core-status-badge');
            if (badge) {
                badge.dataset.msg = sMsg;
                badge.addEventListener('click', function() { showStatusModal(this.dataset.name, this.dataset.msg); });
            }
            fragment.appendChild(div);
        });

        // 시장 & 다음 후보 카드 (3번째 슬롯)
        document.getElementById('market-insight-card')?.remove();
        const insightCard = document.createElement('div');
        insightCard.id = 'market-insight-card';
        insightCard.className = 'info-card glass-card';
        const regime = data.market_regime || 'NEUTRAL';
        const regimeColor = regime === 'BULL' ? '#dc2626' : regime === 'BEAR' ? '#2563eb' : '#475569';
        const regimeEmoji = regime === 'BULL' ? '🐂' : regime === 'BEAR' ? '🐻' : '〰️';
        const pnl = data.us_pnl ?? 0;
        const pnlRt = data.us_pnl_rt ?? 0;
        const pnlColor = pnl > 0 ? '#dc2626' : pnl < 0 ? '#2563eb' : '#475569';
        const pnlSign  = pnl >= 0 ? '+' : '';
        const avail = data.available_cash ?? 0;
        const satInfo = (data.satellite_info || []).slice(0, 3);
        const candidateRows = satInfo.length > 0
            ? satInfo.map(c => {
                const ret = (c.return_pct ?? 0);
                const retColor = ret >= 0 ? '#dc2626' : '#2563eb';
                return `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid rgba(0,0,0,0.07);">
                    <span style="font-size:0.82rem;font-weight:600;color:#111827;">${c.name}<span style="color:#6b7280;font-size:0.72rem;margin-left:4px">${c.ticker}</span></span>
                    <span style="font-size:0.8rem;font-weight:700;color:${retColor};">${ret >= 0 ? '+' : ''}${ret.toFixed(1)}%</span>
                </div>`;
            }).join('')
            : `<div style="color:#6b7280;font-size:0.82rem;padding:6px 0;">후보 선정 중...</div>`;
        insightCard.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <h3 style="margin:0;font-size:0.95rem;color:#111827;font-weight:700;">📊 시장 & 다음 후보</h3>
                <span style="font-size:0.82rem;font-weight:700;color:${regimeColor};background:${regimeColor}18;padding:3px 10px;border-radius:8px;border:1px solid ${regimeColor}40;">${regimeEmoji} ${regime}</span>
            </div>
            <div style="display:flex;gap:16px;margin-bottom:12px;padding-bottom:10px;border-bottom:1px solid rgba(0,0,0,0.08);">
                <div>
                    <div style="font-size:0.7rem;color:#6b7280;margin-bottom:2px;font-weight:600;">오늘 수익</div>
                    <div style="font-size:1rem;font-weight:700;color:${pnlColor};">${pnlSign}${fmtMoney(pnl)}</div>
                    <div style="font-size:0.75rem;color:${pnlColor};">${pnlSign}${pnlRt.toFixed(2)}%</div>
                </div>
                <div>
                    <div style="font-size:0.7rem;color:#6b7280;margin-bottom:2px;font-weight:600;">가용 현금</div>
                    <div style="font-size:1rem;font-weight:700;color:#111827;">${fmtMoney(avail)}</div>
                </div>
            </div>
            <div style="font-size:0.72rem;color:#374151;margin-bottom:6px;font-weight:700;">🚀 Growth 감시 상위</div>
            ${candidateRows}
        `;
        fragment.appendChild(insightCard);
        topCardsContainer.insertBefore(fragment, satCard);

        // Growth(위성) 테이블
        let satHtmlBuffer = '';
        if (sats.length > 0) {
            sats.forEach(s => {
                const isHolding = s.shares > 0;
                const sText = s.status || "감시 중 👀";
                const sMsg  = s.status_msg || "지표 점검 중...";
                const priceCell = s.price > 0 ? `<span style="font-weight:600;">${fmtMoney(s.price)}</span>` : '<span style="color:#64748b">-</span>';
                const sharesCell = isHolding ? `${s.shares.toLocaleString()}주` : `<span style="color:#64748b">-</span>`;
                const valueCell  = isHolding ? `<span style="font-weight:600;">${fmtMoney(s.value || 0)}</span>` : `<span style="color:#64748b">-</span>`;
                const budgetLine = (!isHolding && s.budget > 0) ? `<div style="color:#60a5fa;font-size:0.75rem;margin-top:2px">💰 ${fmtMoney(s.budget)}</div>` : '';
                satHtmlBuffer += `<tr>
                    <td><b>${s.name}</b><span style="color:#64748b;font-size:0.78rem;margin-left:5px">${s.ticker}</span></td>
                    <td>${priceCell}</td>
                    <td>${sharesCell}</td>
                    <td><div>${valueCell}</div>${_pnlCell(s.shares, s.avg_price, s.price)}${budgetLine}</td>
                    <td><span class="badge" onclick="showStatusModalEncoded('${s.name}', '${encodeURIComponent(sMsg)}')" style="cursor:pointer; ${_positionBadgeStyle(sText)}">${sText}</span></td>
                </tr>`;
            });
        } else {
            satHtmlBuffer = '<tr><td colspan="5" class="muted-center">Growth 종목 탐색 중...</td></tr>';
        }
        satTbody.innerHTML = satHtmlBuffer;


        if (data.logs && data.logs.length > 0) {
            const recent = data.logs.slice(-6);
            let logHtmlBuffer = '';
            recent.forEach(log => {
                logHtmlBuffer += `<div class="mini-log-entry"><span class="log-time">[${log.time}]</span>${log.message}</div>`;
            });
            miniLog.innerHTML = logHtmlBuffer;
            miniLog.scrollTop = miniLog.scrollHeight;
        }
    }

    // Expose to outer-scope window.* handlers (saveAccountSettings, saveCoreStocks, toggleMode)
    window.fetchStatus = fetchStatus;
    window.updateUI    = updateUI;

    fetchStatus();
    setInterval(fetchStatus, 5000);
});

window.toggleMode = async function () {
    const cb = document.getElementById('modeSwitch');
    const isMock = cb.checked ? 1 : 0;
    cb.disabled = true;

    const lblReal = document.getElementById('label-real');
    const lblUs = document.getElementById('label-us');
    if (isMock) {
        lblUs.classList.add('mode-active');
        lblReal.classList.remove('mode-active');
    } else {
        lblReal.classList.add('mode-active');
        lblUs.classList.remove('mode-active');
    }

    try {
        const res = await fetch('/api/settings/mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_mock: isMock })
        });
        const result = await res.json();
        if (result.status === 'success') {
            showToast(isMock ? 'US 모드로 전환됨' : 'KR 모드로 전환됨', 'info');
            fetch('/api/status').then(r => r.json()).then(data => {
                updateUI(data);
                // fetchPnl 제거됨
            });
        } else {
            showToast('모드 변경 실패', 'error');
            cb.checked = !cb.checked;
        }
    } catch (e) {
        showToast('서버 오류', 'error');
        cb.checked = !cb.checked;
    } finally {
        cb.disabled = false;
    }
}

window.switchSettingsTab = function (n) {
    [1, 2].forEach(i => {
        const panel = document.getElementById('stab-panel-' + i);
        const btn   = document.getElementById('stab-' + i);
        const active = (i === n);
        panel.style.display = active ? 'block' : 'none';
        if (active) {
            btn.style.background = i === 1 ? 'rgba(16,185,129,0.2)' : 'rgba(99,102,241,0.2)';
            btn.style.borderColor = i === 1 ? 'rgba(16,185,129,0.5)' : 'rgba(99,102,241,0.5)';
            btn.style.color = i === 1 ? '#10b981' : '#818cf8';
        } else {
            btn.style.background = 'transparent';
            btn.style.borderColor = 'rgba(255,255,255,0.1)';
            btn.style.color = '#8b949e';
        }
    });
}

window.openSettingsModal = async function () {
    document.getElementById('settingsModal').style.display = 'block';
    switchSettingsTab(1);   // 항상 탭1 부터 열기

    // 저장된 뉴스 API 키 불러오기 (마스킹된 값 표시)
    try {
        const res = await fetch('/api/settings/news_keys');
        if (res.ok) {
            const keys = await res.json();
            if (keys.dart_api_key)        document.getElementById('dartApiKey').placeholder        = keys.dart_api_key + ' (저장됨)';
            if (keys.naver_client_id)     document.getElementById('naverClientId').placeholder     = keys.naver_client_id + ' (저장됨)';
            if (keys.naver_client_secret) document.getElementById('naverClientSecret').placeholder = keys.naver_client_secret + ' (저장됨)';
        }
    } catch (e) { /* 조회 실패 시 무시 */ }
    // 저장된 섹터 가이드 불러오기
    try {
        const res2 = await fetch('/api/settings/sector_guide');
        if (res2.ok) {
            const data = await res2.json();
            if (data.sector_guide) document.getElementById('sectorGuideText').value = data.sector_guide;
        }
    } catch (e) { /* 무시 */ }
}
window.closeSettingsModal = function () {
    document.getElementById('settingsModal').style.display = 'none';
}

window.toggleCoreDCA = async function(ticker, name, currentlyOn) {
    const enable = !currentlyOn;
    const action = enable ? 'ON' : 'OFF';
    if (!confirm(`${name} 적립식 DCA를 ${action}으로 변경할까요?\n\n` +
        (enable
            ? '✅ 예수금 입금 감지 시 + 평단 -3% 눌림 시 자동 적립 매수\n(진입 점수/RSI 신호 무관)'
            : '❌ DCA 적립을 중단합니다.'))) return;
    try {
        const res = await fetch('/api/set_core_dca', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ ticker, dca: enable })
        });
        const d = await res.json();
        if (d.status === 'ok') {
            showToast(`💰 ${d.message}`, 'success');
        } else {
            showToast(`오류: ${d.message}`, 'error');
        }
    } catch(e) {
        showToast('DCA 설정 실패', 'error');
    }
}

window.openCoreModal = function () {
    document.getElementById('coreModal').style.display = 'block';
    _coreStockList = [...(window.cachedCoreStocks || [])];
    renderCoreTags();
    document.getElementById('coreSearchResults').innerHTML = '';
    document.getElementById('coreSearchInput').value = '';
}
window.closeCoreModal = function () {
    document.getElementById('coreModal').style.display = 'none';
}

window.onclick = function (event) {
    if (event.target == document.getElementById('settingsModal')) closeSettingsModal();
    if (event.target == document.getElementById('coreModal')) closeCoreModal();
    if (event.target == document.getElementById('strategyModal')) closeStrategyModal();
}

let strategyAnimReq = null;

function animateStrategy(strategyName) {
    const canvas = document.getElementById('strat-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let W = canvas.width;
    let H = canvas.height;

    if (strategyAnimReq) cancelAnimationFrame(strategyAnimReq);

    let t = 0;
    function render() {
        ctx.clearRect(0, 0, W, H);
        // 테마와 무관하게 항상 어두운 배경 고정
        ctx.fillStyle = 'rgba(15,23,42,0.94)';
        ctx.fillRect(0, 0, W, H);
        ctx.strokeStyle = 'rgba(255,255,255,0.05)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (let i = 0; i < W; i += 20) { ctx.moveTo(i, 0); ctx.lineTo(i, H); }
        for (let i = 0; i < H; i += 20) { ctx.moveTo(0, i); ctx.lineTo(W, i); }
        ctx.stroke();

        const timeOffset = t * 0.02;
        if (strategyName.includes("크로스") || strategyName.includes("MACD")) {
            ctx.lineWidth = 2;
            ctx.beginPath();
            ctx.strokeStyle = '#94a3b8';
            for (let x = 0; x <= W; x += 2) {
                let y = H / 2 + Math.sin(x * 0.01 + timeOffset * 0.5) * 20;
                if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }
            ctx.stroke();
            ctx.beginPath();
            ctx.strokeStyle = '#60a5fa';
            let crossX = -1, crossY = -1, crossType = '';
            let prevDiff = 0;
            for (let x = 0; x <= W; x += 2) {
                let yLong = H / 2 + Math.sin(x * 0.01 + timeOffset * 0.5) * 20;
                let yShort = H / 2 + Math.sin(x * 0.015 + timeOffset) * 40;
                if (x === 0) ctx.moveTo(x, yShort); else ctx.lineTo(x, yShort);
                let diff = yShort - yLong;
                if (x > 20 && x < W - 20) {
                    if (prevDiff > 0 && diff <= 0) { crossX = x; crossY = yShort; crossType = 'BUY'; }
                    if (prevDiff < 0 && diff >= 0) { crossX = x; crossY = yShort; crossType = 'SELL'; }
                }
                prevDiff = diff;
            }
            ctx.stroke();
            if (crossX !== -1) {
                ctx.fillStyle = crossType === 'BUY' ? '#ef4444' : '#3b82f6';
                ctx.beginPath(); ctx.arc(crossX, crossY, 5, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = 'white'; ctx.font = 'bold 11px sans-serif';
                ctx.fillText(crossType, crossX - 12, crossY - 10);
            }
        } else if (strategyName.includes("RSI") || strategyName.includes("Williams") || strategyName.includes("Stochastic") || strategyName.includes("CCI")) {
            ctx.fillStyle = 'rgba(255,255,255,0.05)';
            ctx.fillRect(0, H * 0.3, W, H * 0.4);
            ctx.strokeStyle = 'rgba(255,255,255,0.2)';
            ctx.setLineDash([4, 4]);
            ctx.beginPath(); ctx.moveTo(0, H * 0.3); ctx.lineTo(W, H * 0.3); ctx.stroke();
            ctx.beginPath(); ctx.moveTo(0, H * 0.7); ctx.lineTo(W, H * 0.7); ctx.stroke();
            ctx.setLineDash([]);
            ctx.fillStyle = '#94a3b8'; ctx.font = '9px sans-serif';
            ctx.fillText('과매수 (Overbought)', 5, H * 0.3 - 5);
            ctx.fillText('과매도 (Oversold)', 5, H * 0.7 + 12);
            ctx.beginPath();
            ctx.strokeStyle = '#c084fc';
            ctx.lineWidth = 2;
            let markerX = -1, markerY = -1, mType = '';
            for (let x = 0; x <= W; x += 2) {
                let y = H / 2 + Math.sin(x * 0.02 + timeOffset) * 50;
                if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                if (x === Math.floor(W / 2)) {
                    if (y > H * 0.7) { markerX = x; markerY = y; mType = 'BUY'; }
                    if (y < H * 0.3) { markerX = x; markerY = y; mType = 'SELL'; }
                }
            }
            ctx.stroke();
            if (markerX !== -1) {
                ctx.fillStyle = mType === 'BUY' ? '#ef4444' : '#3b82f6';
                ctx.beginPath(); ctx.arc(markerX, markerY, 5, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = 'white'; ctx.font = 'bold 11px sans-serif';
                ctx.fillText(mType, markerX - 12, markerY + (mType === 'BUY' ? -10 : 15));
            }
        } else if (strategyName.includes("볼린저")) {
            ctx.lineWidth = 1;
            let midY = [], upY = [], loY = [];
            for (let x = 0; x <= W; x += 2) {
                let my = H / 2 + Math.sin(x * 0.01 + timeOffset * 0.5) * 15;
                let std = 30 + Math.sin(x * 0.02 + timeOffset) * 10;
                midY.push(my); upY.push(my - std); loY.push(my + std);
            }
            ctx.strokeStyle = 'rgba(255,255,255,0.1)';
            ctx.beginPath(); midY.forEach((y, i) => { if (i === 0) ctx.moveTo(i * 2, y); else ctx.lineTo(i * 2, y); }); ctx.stroke();
            ctx.fillStyle = 'rgba(96, 165, 250, 0.1)';
            ctx.beginPath();
            upY.forEach((y, i) => { if (i === 0) ctx.moveTo(i * 2, y); else ctx.lineTo(i * 2, y); });
            for (let i = loY.length - 1; i >= 0; i--) { ctx.lineTo(i * 2, loY[i]); }
            ctx.fill();
            ctx.strokeStyle = 'rgba(96, 165, 250, 0.5)';
            ctx.beginPath(); upY.forEach((y, i) => { if (i === 0) ctx.moveTo(i * 2, y); else ctx.lineTo(i * 2, y); }); ctx.stroke();
            ctx.beginPath(); loY.forEach((y, i) => { if (i === 0) ctx.moveTo(i * 2, y); else ctx.lineTo(i * 2, y); }); ctx.stroke();
            ctx.strokeStyle = '#f8fafc';
            ctx.lineWidth = 2;
            ctx.beginPath();
            let bx = -1, by = -1, btype = '';
            for (let x = 0; x <= W; x += 2) {
                let px = Math.floor(x / 2);
                let py = midY[px] + Math.sin(x * 0.03 + timeOffset * 1.5) * 35;
                if (x === 0) ctx.moveTo(x, py); else ctx.lineTo(x, py);
                if (x === Math.floor(W / 2)) {
                    if (py > loY[px]) { bx = x; by = py; btype = 'BUY'; }
                    if (py < upY[px]) { bx = x; by = py; btype = 'SELL'; }
                }
            }
            ctx.stroke();
            if (bx !== -1) {
                ctx.fillStyle = btype === 'BUY' ? '#ef4444' : '#3b82f6';
                ctx.beginPath(); ctx.arc(bx, by, 5, 0, Math.PI * 2); ctx.fill();
                ctx.fillStyle = 'white'; ctx.font = 'bold 11px sans-serif';
                ctx.fillText(btype, bx - 12, by - 10);
            }
        } else {
            // 기본: 어두운 배경 채우고 모멘텀 바 애니메이션
            ctx.fillStyle = 'rgba(15,23,42,0.92)';
            ctx.fillRect(0, 0, W, H);

            // 격자선
            ctx.strokeStyle = 'rgba(255,255,255,0.04)';
            ctx.lineWidth = 1;
            ctx.beginPath();
            for (let i = 0; i < W; i += 20) { ctx.moveTo(i, 0); ctx.lineTo(i, H); }
            for (let i = 0; i < H; i += 20) { ctx.moveTo(0, i); ctx.lineTo(W, i); }
            ctx.stroke();

            // 모멘텀 바 (수직 바 7개, 물결치는 높이)
            const barCount = 7;
            const barW = 22;
            const gap = (W - barCount * barW) / (barCount + 1);
            for (let i = 0; i < barCount; i++) {
                const x = gap + i * (barW + gap);
                const heightRatio = 0.3 + 0.55 * Math.abs(Math.sin(t * 0.04 + i * 0.7));
                const bh = H * 0.75 * heightRatio;
                const by = (H - bh) / 2;
                const alpha = 0.5 + 0.5 * heightRatio;
                ctx.fillStyle = `rgba(167,139,250,${alpha.toFixed(2)})`;
                ctx.beginPath();
                ctx.roundRect ? ctx.roundRect(x, by, barW, bh, 4) : ctx.rect(x, by, barW, bh);
                ctx.fill();
            }

            // 중앙 텍스트
            ctx.fillStyle = 'rgba(248,250,252,0.92)';
            ctx.font = 'bold 13px Inter, sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('AI 시뮬레이션 최적 타점 탐색 중...', W / 2, H - 14);
            ctx.textAlign = 'left';
        }
        t++;
        strategyAnimReq = requestAnimationFrame(render);
    }
    render();
}

window.showStrategyInfo = function (strategyName) {
    const titleEl = document.getElementById('strat-title');
    const descEl = document.getElementById('strat-desc');
    titleEl.textContent = strategyName;
    let desc = "이 전략은 단기적인 모멘텀과 시장 심리를 분석하여 최적의 타점에서 매수/매도를 진행하도록 AI가 13가지 백테스트 후 가장 성과가 좋은 기법으로 자동 선정했습니다.";
    if (strategyName.includes("EMA 5/20 크로스")) desc = "최근 5일(단기) 지수이동평균선(EMA)이 20일(장기) 지수이동평균선을 상향 돌파(골든크로스)할 때 매수하고, 하향 돌파(데드크로스)할 때 매도하는 추세 추종 전략입니다.";
    else if (strategyName.includes("SMA 3/20 크로스")) desc = "3일 단순이동평균선(SMA)과 20일 단순이동평균선의 교차를 활용하여, 단기적으로 빠른 추세 변화를 포착해 진입하는 전략입니다.";
    else if (strategyName.includes("RSI(14)")) desc = "RSI(상대강도지수)가 30 이하로 떨어지면 과매도 구간으로 판단하여 매수하고, 70 이상으로 올라가면 과매수 구간으로 판단해 매도하는 대표적인 역추세 매매 기법입니다.";
    else if (strategyName.includes("MACD")) desc = "MACD 선이 Signal 선을 상향 돌파할 때 매수하고, 하향 돌파할 때 매도하여 상승 모멘텀이 시작되는 초입을 노리는 기법입니다.";
    else if (strategyName.includes("볼린저")) desc = "주가가 볼린저 밴드 하단에 도달했을 때 반등을 예상하여 매수하고, 중심선 또는 상단선에서 매도하는 변동성 돌파 전략입니다.";
    descEl.textContent = desc;
    document.getElementById('strategyModal').style.display = 'block';
    animateStrategy(strategyName);
}

window.closeStrategyModal = function () {
    document.getElementById('strategyModal').style.display = 'none';
    if (strategyAnimReq) { cancelAnimationFrame(strategyAnimReq); strategyAnimReq = null; }
}

let _coreStockList = [];
function renderCoreTags() {
    const container = document.getElementById('coreTagList');
    if (!container) return;
    if (_coreStockList.length === 0) {
        container.innerHTML = '<span style="color:#94a3b8; font-size:0.8rem;">비어있음 (기본값 사용)</span>';
        return;
    }
    container.innerHTML = _coreStockList.map((s, i) => `
        <span class="core-tag">
            ${s.name} <span style="color:#94a3b8; font-size:0.75rem;">${s.ticker}</span>
            <span class="remove-core" onclick="removeCoreStock(${i})">✕</span>
        </span>
    `).join('');
}

window.removeCoreStock = function (idx) { _coreStockList.splice(idx, 1); renderCoreTags(); }

window.searchCoreStock = async function () {
    const q = document.getElementById('coreSearchInput').value.trim();
    if (!q) return;
    const resultsEl = document.getElementById('coreSearchResults');
    resultsEl.innerHTML = '<div style="color:#94a3b8; font-size:0.85rem; padding:8px;">검색 중...</div>';
    try {
        const res = await fetch(`/api/search/stock?q=${encodeURIComponent(q)}`);
        const data = await res.json();
        if (!data.results || data.results.length === 0) {
            resultsEl.innerHTML = '<div style="color:#94a3b8; font-size:0.85rem; padding:8px;">검색 결과 없음</div>';
            return;
        }
        resultsEl.innerHTML = data.results.map(s => `
            <div class="search-result-item" onclick="addCoreStock('${s.ticker}','${s.name}')">
                <div>
                    <div class="stock-name">${s.name}</div>
                    <div class="stock-code">${s.ticker}</div>
                </div>
                <button class="btn-add-core">+ 추가</button>
            </div>
        `).join('');
    } catch (e) { resultsEl.innerHTML = '<div style="color:#ef4444; font-size:0.85rem; padding:8px;">검색 오류</div>'; }
}

window.addCoreStock = function (ticker, name) {
    if (_coreStockList.find(s => s.ticker === ticker)) { alert('이미 추가된 종목입니다.'); return; }
    _coreStockList.push({ ticker, name });
    renderCoreTags();
    document.getElementById('coreSearchResults').innerHTML = '';
    document.getElementById('coreSearchInput').value = '';
}

window.saveCoreStocks = async function () {
    const isMock = 1;
    const coreJsonStr = JSON.stringify(_coreStockList);

    const data = {
        real_app_key: document.getElementById('realAppKey').value,
        real_app_secret: document.getElementById('realAppSecret').value,
        real_account_no: document.getElementById('realAccountNo').value,
        us_app_key: document.getElementById('usAppKey').value,
        us_app_secret: document.getElementById('usAppSecret').value,
        us_account_no: document.getElementById('usAccountNo').value,

        telegram_token: document.getElementById('teleToken').value,
        telegram_chat_id: document.getElementById('teleChatId').value,
        claude_api_key: document.getElementById('claudeApiKey').value,
        us_core_stocks: coreJsonStr,
        is_mock: isMock,
    };
    try {
        const res = await fetch('/api/settings/keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        const result = await res.json();
        if (result.status === 'success') {
            closeCoreModal();
            showToast('코어 종목이 변경되었습니다. 시스템에 반영 중입니다.', 'success');
            fetchStatus();
        } else { showToast('저장 실패: ' + (result.message || '오류'), 'error'); }
    } catch (e) { showToast('서버 통신 오류', 'error'); }
}

window.saveAccountSettings = async function () {
    const isMock = 1;
    const coreJsonStr = JSON.stringify(_coreStockList);

    const data = {
        real_app_key: document.getElementById('realAppKey').value,
        real_app_secret: document.getElementById('realAppSecret').value,
        real_account_no: document.getElementById('realAccountNo').value,
        us_app_key: document.getElementById('usAppKey').value,
        us_app_secret: document.getElementById('usAppSecret').value,
        us_account_no: document.getElementById('usAccountNo').value,

        telegram_token: document.getElementById('teleToken').value,
        telegram_chat_id: document.getElementById('teleChatId').value,
        claude_api_key: document.getElementById('claudeApiKey').value,
        us_core_stocks: coreJsonStr,
        is_mock: isMock,
        // initial_cash 제거: KR/US 모두 실계좌 잔고 자동 감지 (수동 입력값으로 덮어쓰기 금지)
    };
    try {
        const res = await fetch('/api/settings/keys', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        const result = await res.json();
        if (result.status === 'success') {
            closeSettingsModal();
            showToast('계좌 설정이 저장되었습니다. 페이지를 새로고침합니다...', 'success');
            // 저장 후 페이지 새로고침: Claude API 키 등 서버 렌더링 요소 반영
            setTimeout(() => window.location.reload(), 1200);
        } else { showToast('저장 실패', 'error'); }
    } catch (e) { showToast('서버 통신 오류', 'error'); }
}

window.saveNewsKeys = async function () {
    const dartKey    = document.getElementById('dartApiKey').value.trim();
    const naverId    = document.getElementById('naverClientId').value.trim();
    const naverSec   = document.getElementById('naverClientSecret').value.trim();
    // 아무것도 입력 안 했으면 무시
    if (!dartKey && !naverId && !naverSec) {
        showToast('저장할 키를 입력해 주세요.', 'error'); return;
    }
    try {
        const res = await fetch('/api/settings/news_keys', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ dart_api_key: dartKey, naver_client_id: naverId, naver_client_secret: naverSec })
        });
        const result = await res.json();
        if (result.status === 'success') {
            const msg = document.getElementById('newsKeysSavedMsg');
            msg.style.display = 'block';
            setTimeout(() => { msg.style.display = 'none'; }, 3000);
            // placeholder 갱신 (입력창 비우기)
            if (dartKey)  { document.getElementById('dartApiKey').value = '';        document.getElementById('dartApiKey').placeholder        = dartKey.slice(0,8) + '**** (저장됨)'; }
            if (naverId)  { document.getElementById('naverClientId').value = '';     document.getElementById('naverClientId').placeholder     = naverId.slice(0,4) + '**** (저장됨)'; }
            if (naverSec) { document.getElementById('naverClientSecret').value = ''; document.getElementById('naverClientSecret').placeholder = naverSec.slice(0,4) + '**** (저장됨)'; }
        } else { showToast('뉴스 키 저장 실패', 'error'); }
    } catch (e) { showToast('서버 통신 오류', 'error'); }
}

// 섹터 가이드 파일 업로드: .md/.txt 파일을 복수 선택하면 내용을 textarea에 자동 추가
window.appendSectorGuideFiles = function (input) {
    const files = Array.from(input.files);
    if (!files.length) return;
    const ta = document.getElementById('sectorGuideText');
    const msg = document.getElementById('sectorGuideFileMsg');
    let remaining = files.length;
    files.forEach(file => {
        const reader = new FileReader();
        reader.onload = function (e) {
            const separator = '\n\n---\n<!-- 파일: ' + file.name + ' -->\n';
            ta.value = (ta.value.trimEnd() ? ta.value.trimEnd() + separator : '') + e.target.result.trimEnd();
            remaining--;
            if (remaining === 0) {
                msg.textContent = '✅ ' + files.length + '개 파일 추가 완료 — 저장 버튼을 눌러주세요.';
                msg.style.display = 'inline';
                setTimeout(() => { msg.style.display = 'none'; }, 5000);
            }
        };
        reader.onerror = function () {
            remaining--;
            showToast(file.name + ' 읽기 실패', 'error');
        };
        reader.readAsText(file, 'UTF-8');
    });
    // 같은 파일 재선택 가능하도록 value 초기화
    input.value = '';
}

window.saveSectorGuide = async function () {
    const guide = document.getElementById('sectorGuideText').value.trim();
    try {
        const res = await fetch('/api/settings/sector_guide', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sector_guide: guide })
        });
        const result = await res.json();
        if (result.status === 'success') {
            const msg = document.getElementById('sectorGuideSavedMsg');
            msg.style.display = 'block';
            setTimeout(() => { msg.style.display = 'none'; }, 3000);
        } else { showToast('저장 실패', 'error'); }
    } catch (e) { showToast('서버 통신 오류', 'error'); }
}

window.openReportModal = async function () {
    document.getElementById('reportModal').style.display = 'block';
    document.getElementById('report-content').innerHTML = '리포트 데이터를 불러오는 중...';
    document.getElementById('report-time-tabs').innerHTML = '';
    try {
        const res = await fetch('/api/daily_report');
        const json = await res.json();
        if (json.status === 'success' && json.data) {
            const data = json.data;
            const times = ['15:40'];
            let tabsHtml = '';
            let latestTime = null;
            let latestContent = null;

            // report_markdown: 단일 텍스트 (아직 시간별 분리 전)
            if (data.report_markdown && !data['15:40']) {
                latestContent = data.report_markdown;
            } else {
                // 현재 KST 시간으로 생성 가능 여부 판단
                const nowKst = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Seoul' }));
                const hhmm = nowKst.getHours().toString().padStart(2,'0') + ':' + nowKst.getMinutes().toString().padStart(2,'0');

                times.forEach(t => {
                    const content = data[t];
                    const hasDone = !!content;
                    // 아직 시간이 안 됐고 내용도 없으면 비활성
                    const notYet = !hasDone && hhmm < t;
                    if (hasDone) {
                        tabsHtml += `<button style="padding:6px 14px; border-radius:6px; font-size:0.85rem; font-weight:bold; background:rgba(59,130,246,0.2); color:#60a5fa; border:1px solid #3b82f6; cursor:pointer;" onclick="renderReportText('${encodeURIComponent(content)}', this)">${t}</button>`;
                        latestTime = t;
                        latestContent = content;
                    } else if (notYet) {
                        tabsHtml += `<button style="padding:6px 14px; border-radius:6px; font-size:0.85rem; font-weight:bold; background:rgba(255,255,255,0.04); color:#4b5563; border:1px solid rgba(255,255,255,0.08); cursor:not-allowed;" disabled>${t}</button>`;
                    } else {
                        // 시간은 됐는데 아직 미생성 → 클릭 가능하지만 내용 없음 안내
                        const noContent = encodeURIComponent('📋 이 시간대 리포트는 아직 생성되지 않았습니다.\n\nAI 리포트가 활성화되어 있으면 잠시 후 자동으로 발행됩니다.');
                        tabsHtml += `<button style="padding:6px 14px; border-radius:6px; font-size:0.85rem; font-weight:bold; background:rgba(59,130,246,0.08); color:#64748b; border:1px solid rgba(59,130,246,0.25); cursor:pointer;" onclick="renderReportText('${noContent}', this)">${t} ⏳</button>`;
                    }
                });
                if (!latestContent && data.report_markdown) latestContent = data.report_markdown;
            }

            document.getElementById('report-time-tabs').innerHTML = tabsHtml;

            // 가장 최근 탭 활성화
            if (latestTime) {
                setTimeout(() => {
                    const btns = document.getElementById('report-time-tabs').querySelectorAll('button:not([disabled])');
                    btns.forEach(b => {
                        if (b.textContent.startsWith(latestTime)) {
                            b.style.background = '#3b82f6'; b.style.color = '#fff';
                        }
                    });
                }, 50);
            }

            renderReportText(encodeURIComponent(latestContent || '📋 오늘 생성된 리포트가 없습니다.\n\n리포트는 AI 설정이 있을 때 11:00 / 15:30 / 20:00 KST에 자동 발행됩니다.'), null);
        } else {
            document.getElementById('report-content').innerHTML = json.message || '리포트가 아직 생성되지 않았습니다.';
        }
    } catch (e) { document.getElementById('report-content').innerHTML = '오류: 리포트를 불러올 수 없습니다.'; }
}

window.renderReportText = function (encodedText, btnEl) {
    const text = decodeURIComponent(encodedText);
    let htmlText = text
        .replace(/### (.*)/g, '<h3>$1</h3>')
        .replace(/#### (.*)/g, '<h4 style="color:var(--accent-blue); margin-top:20px; border-bottom:1px solid #334155; padding-bottom:5px;">$1</h4>')
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/> (.*)/g, '<div style="background:rgba(59,130,246,0.1); padding:10px; border-left:4px solid var(--accent-blue); margin:10px 0; border-radius:4px;">$1</div>')
        .replace(/- (.*)/g, '<li style="margin-bottom:8px;">$1</li>')
        .replace(/\n/g, '<br>');
    document.getElementById('report-content').innerHTML = htmlText;

    if (btnEl) {
        const btns = document.getElementById('report-time-tabs').querySelectorAll('button');
        btns.forEach(b => {
            if (!b.disabled) { b.style.background = 'rgba(59,130,246,0.2)'; b.style.color = '#60a5fa'; }
        });
        btnEl.style.background = '#3b82f6';
        btnEl.style.color = '#fff';
    }
}

window.closeReportModal = function () { document.getElementById('reportModal').style.display = 'none'; }
window.hideReportToday = function () {
    const today = new Date().toISOString().split('T')[0];
    localStorage.setItem('hideReportDate', today);
    closeReportModal();
}

window.checkDailyReport = function () {
    const today = new Date().toISOString().split('T')[0];
    const hiddenDate = localStorage.getItem('hideReportDate');
    if (hiddenDate !== today) {
        fetch('/api/daily_report').then(res => res.json()).then(json => {
            if (json.status === 'success' && json.data && json.data.date === today) {
                setTimeout(() => openReportModal(), 1500);
            }
        });
    }
}

let _aiChatOpen = false;
let _aiIsLoading = false;
window.toggleAiChat = function () {
    const fab = document.getElementById('ai-chat-fab');
    const panel = document.getElementById('ai-chat-panel');
    _aiChatOpen = !_aiChatOpen;
    if (_aiChatOpen) {
        fab.classList.add('open'); panel.classList.add('open');
        document.getElementById('ai-new-badge').classList.remove('visible');
        setTimeout(() => document.getElementById('ai-chat-input').focus(), 350);
        const el = document.getElementById('chat-messages'); if (el) el.scrollTop = el.scrollHeight;
    } else { fab.classList.remove('open'); panel.classList.remove('open'); }
}

function markdownToHtml(text) {
    return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/### (.*?)(\n|$)/g, '<h4>$1</h4>').replace(/## (.*?)(\n|$)/g, '<h3>$1</h3>').replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>').replace(/\*(.*?)\*/g, '<em>$1</em>').replace(/`([^`]+)`/g, '<code>$1</code>').replace(/^- (.*?)(\n|$)/gm, '<li>$1</li>').replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>').replace(/\n/g, '<br>');
}

window.sendAiMessage = async function () {
    if (_aiIsLoading) return;
    const input = document.getElementById('ai-chat-input');
    const message = input.value.trim(); if (!message) return;
    input.value = ''; input.style.height = '42px';
    _aiIsLoading = true; document.getElementById('ai-chat-send').disabled = true;
    const messages = document.getElementById('chat-messages');
    const uWrapper = document.createElement('div'); uWrapper.className = 'chat-msg user';
    const uBubble = document.createElement('div'); uBubble.className = 'chat-bubble'; uBubble.textContent = message;
    const uTime = document.createElement('span'); uTime.className = 'chat-msg-time'; uTime.textContent = new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
    uWrapper.appendChild(uBubble); uWrapper.appendChild(uTime); messages.appendChild(uWrapper);
    messages.scrollTop = messages.scrollHeight;
    const indicator = document.createElement('div'); indicator.id = 'chat-typing-indicator'; indicator.className = 'chat-typing'; indicator.innerHTML = '<div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div>';
    messages.appendChild(indicator); messages.scrollTop = messages.scrollHeight;
    try {
        const res = await fetch('/api/ai_chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }) });
        const data = await res.json();
        const indicatorEl = document.getElementById('chat-typing-indicator'); if (indicatorEl) indicatorEl.remove();
        const reply = data.reply || data.message || '응답을 받을 수 없습니다.';
        const aWrapper = document.createElement('div'); aWrapper.className = 'chat-msg ai';
        const aBubble = document.createElement('div'); aBubble.className = 'chat-bubble'; aBubble.innerHTML = markdownToHtml(reply);
        const aTime = document.createElement('span'); aTime.className = 'chat-msg-time'; aTime.textContent = `라씨 AI · ${new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}`;
        aWrapper.appendChild(aBubble); aWrapper.appendChild(aTime); messages.appendChild(aWrapper);
        messages.scrollTop = messages.scrollHeight;
        if (!_aiChatOpen) document.getElementById('ai-new-badge').classList.add('visible');

        // ── 봇 명령 실행 결과 표시 ──────────────────────────────────
        if (data.applied_commands && data.applied_commands.length > 0) {
            const cmdWrapper = document.createElement('div'); cmdWrapper.className = 'chat-msg ai';
            const cmdBubble  = document.createElement('div'); cmdBubble.className = 'chat-bubble';
            cmdBubble.style.cssText = 'background:rgba(59,130,246,0.15);border:1px solid rgba(59,130,246,0.35);color:#93c5fd;';
            cmdBubble.innerHTML = '🤖 <strong>봇 설정 자동 적용 완료</strong><br>' +
                data.applied_commands.map(c => markdownToHtml(c)).join('<br>');
            const cmdTime = document.createElement('span'); cmdTime.className = 'chat-msg-time';
            cmdTime.textContent = `라씨 AI · ${new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}`;
            cmdWrapper.appendChild(cmdBubble); cmdWrapper.appendChild(cmdTime);
            messages.appendChild(cmdWrapper); messages.scrollTop = messages.scrollHeight;
        }
    } catch (e) {
        const indicatorEl = document.getElementById('chat-typing-indicator'); if (indicatorEl) indicatorEl.remove();
        const aWrapper = document.createElement('div'); aWrapper.className = 'chat-msg ai';
        const aBubble = document.createElement('div'); aBubble.className = 'chat-bubble'; aBubble.textContent = '⚠️ 서버 통신 오류가 발생했습니다.';
        aWrapper.appendChild(aBubble); messages.appendChild(aWrapper); messages.scrollTop = messages.scrollHeight;
    } finally { _aiIsLoading = false; document.getElementById('ai-chat-send').disabled = false; input.focus(); }
}

window.sendChip = function (text) { document.getElementById('ai-chat-input').value = text; sendAiMessage(); }
window.handleChatKey = function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAiMessage(); } }
window.autoResizeTextarea = function (el) { el.style.height = 'auto'; el.style.height = Math.min(el.scrollHeight, 120) + 'px'; }
window.resetAiChat = async function () {
    if (!confirm('대화 기록을 초기화할까요?\n(서버 메모리 + DB 저장 기록 모두 삭제됩니다)')) return;
    try { await fetch('/api/ai_chat/reset', { method: 'POST' }); } catch (e) { }
    const messages = document.getElementById('chat-messages');
    messages.innerHTML = `<div class="chat-msg ai"><div class="chat-bubble">대화 기록이 초기화되었습니다. 새 대화를 시작해보세요! 😊</div><span class="chat-msg-time">라씨 AI · ${new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}</span></div>`;
}

window.resetInitialCash = async function () {
    const totalValEl = document.getElementById('total-value');
    const currentTotal = totalValEl ? parseInt(totalValEl.textContent.replace(/[^0-9]/g, '')) : 0;
    const input = prompt(
        '수익률 기준 원금을 재설정합니다.\n' +
        '현재 총평가금액: ' + (currentTotal ? currentTotal.toLocaleString() + '원' : '알 수 없음') + '\n\n' +
        '재설정할 원금을 입력하세요 (원 단위, 기본 10,000,000):',
        '10000000'
    );
    if (input === null) return;
    const amount = parseInt(input.replace(/[^0-9]/g, '') || '0');
    if (isNaN(amount) || amount < 0) { alert('올바른 금액을 입력해주세요. (0원 입력 가능)'); return; }
    try {
        const res = await fetch('/api/reset_initial_cash', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ amount })
        });
        const data = await res.json();
        if (data.status === 'ok') {
            alert('✅ ' + data.message + '\n페이지를 새로고침하면 수익률이 정확하게 반영됩니다.');
        } else {
            alert('❌ ' + (data.message || '오류 발생'));
        }
    } catch (e) {
        alert('❌ 서버 통신 오류: ' + e.message);
    }
}

// ── 위성 종목 수 조절 (+/- 버튼) ──────────────────────────────────────
window.adjustSat = function (delta) {
    const el = document.getElementById('sat-num-display');
    if (!el) return;
    let val = parseInt(el.textContent) + delta;
    if (val < 1) val = 1;
    if (val > 3) val = 3;
    el.textContent = val;

    fetch('/api/settings/satellites', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ count: val })
    }).then(r => r.json()).then(res => {
        if (res.status === 'success') {
            console.log('위성 슬롯 →', res.num_satellites);
        } else {
            console.warn('위성 슬롯 변경 실패:', res.message);
        }
    }).catch(e => console.error('adjustSat 오류:', e));
};

