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

    // ── P&L Chart (일/주/월/년 탭 점그래프) ──
    let pnlChart = null;
    let activePnlTab = 'daily';
    let pnlDataCache = null;

    const PNL_TAB_LABELS = { daily: '일별', weekly: '주별', monthly: '월별', yearly: '연별' };

    function buildChartPoints(labels, values) {
        // scatter용 {x, y} 배열 + 색상 배열 생성
        const points = labels.map((l, i) => ({ x: l, y: values[i] }));
        const ptColors = values.map(v => v >= 0 ? 'rgba(248,81,73,0.85)' : 'rgba(88,166,255,0.85)');
        const lineColor = values.reduce((s, v) => s + v, 0) >= 0 ? 'rgba(248,81,73,0.35)' : 'rgba(88,166,255,0.35)';
        return { points, ptColors, lineColor };
    }

    function renderPnlChart(labels, values, tabKey) {
        const ctx = document.getElementById('pnl-chart').getContext('2d');
        const empty = document.getElementById('chart-empty');

        if (!labels || labels.length === 0) {
            empty.style.display = 'flex';
            if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
            return;
        }
        empty.style.display = 'none';

        const { points, ptColors, lineColor } = buildChartPoints(labels, values);
        const tabLabel = PNL_TAB_LABELS[tabKey] || '일별';

        const chartCfg = {
            type: 'scatter',
            data: {
                datasets: [
                    // 연결선
                    {
                        type: 'line',
                        label: '',
                        data: points,
                        parsing: false,
                        borderColor: lineColor,
                        borderWidth: 1.5,
                        pointRadius: 0,
                        tension: 0.35,
                        fill: false,
                    },
                    // 점
                    {
                        type: 'scatter',
                        label: `${tabLabel} 손익 (원)`,
                        data: points,
                        parsing: false,
                        backgroundColor: ptColors,
                        borderColor: ptColors,
                        pointRadius: 6,
                        pointHoverRadius: 9,
                        pointStyle: 'circle',
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        filter: item => item.datasetIndex === 1,
                        callbacks: {
                            title: items => items[0]?.raw?.x || '',
                            label: item => {
                                const v = item.raw.y;
                                return ` ${v >= 0 ? '+' : ''}${v.toLocaleString()}원`;
                            }
                        },
                        backgroundColor: 'rgba(22,27,34,0.95)',
                        titleColor: '#8b949e',
                        bodyColor: '#e6edf3',
                        borderColor: 'rgba(255,255,255,0.1)',
                        borderWidth: 1,
                        padding: 10,
                    }
                },
                scales: {
                    x: {
                        type: 'category',
                        labels: labels,
                        grid: { color: 'rgba(255,255,255,0.05)' },
                        ticks: { color: '#8b949e', font: { size: 10 },
                                 maxTicksLimit: 10, maxRotation: 45 }
                    },
                    y: {
                        grid: { color: 'rgba(255,255,255,0.05)' },
                        ticks: {
                            color: '#8b949e', font: { size: 11 },
                            callback: v => (v >= 0 ? '+' : '') + v.toLocaleString() + '원'
                        }
                    }
                }
            }
        };

        if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
        pnlChart = new Chart(ctx, chartCfg);
    }

    function switchPnlTab(tabKey) {
        activePnlTab = tabKey;
        document.querySelectorAll('.pnl-tab-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tab === tabKey);
            applyTabStyle(btn, btn.dataset.tab === tabKey);
        });
        if (!pnlDataCache) return;
        const seg = pnlDataCache[tabKey] || { labels: [], values: [] };
        renderPnlChart(seg.labels, seg.values, tabKey);
    }

    // 탭 버튼 이벤트 연결 (DOM 로드 후)
    document.querySelectorAll('.pnl-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => switchPnlTab(btn.dataset.tab));
    });

    // 탭 active 스타일 헬퍼
    function applyTabStyle(btn, isActive) {
        if (isActive) {
            btn.style.background = 'rgba(248,81,73,0.22)';
            btn.style.borderColor = 'rgba(248,81,73,0.5)';
            btn.style.color = '#e6edf3';
        } else {
            btn.style.background = 'transparent';
            btn.style.borderColor = 'rgba(255,255,255,0.1)';
            btn.style.color = '#8b949e';
        }
    }
    document.querySelectorAll('.pnl-tab-btn').forEach(btn => {
        applyTabStyle(btn, btn.dataset.tab === activePnlTab);
    });

    // 구형 initChart 호환 래퍼
    function initChart(labels, values) {
        renderPnlChart(labels, values, activePnlTab);
    }

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

    function fetchPnl() {
        fetch('/api/pnl')
            .then(r => r.json())
            .then(data => {
                pnlDataCache = data;
                const seg = data[activePnlTab] || { labels: data.labels || [], values: data.values || [] };
                renderPnlChart(seg.labels, seg.values, activePnlTab);

                const formatPnl = v => (v >= 0 ? '+' : '') + v.toLocaleString() + '원';
                const colorPnl  = v => v >= 0 ? '#f85149' : '#58a6ff';

                // 월별·연별·누적 요약
                const monthly = (data.monthly?.values || []).reduce((s, v) => {
                    const now = new Date().toISOString().slice(0, 7);
                    return s; // 아래에서 재계산
                }, 0);
                const nowMonth = new Date().toISOString().slice(0, 7);
                const nowYear  = new Date().toISOString().slice(0, 4);

                let totalAcc = 0, monthlyAcc = 0, yearlyAcc = 0;
                (data.daily?.labels || data.labels || []).forEach((d, i) => {
                    const v = (data.daily?.values || data.values || [])[i] || 0;
                    totalAcc += v;
                    if (d.startsWith(nowMonth)) monthlyAcc += v;
                    if (d.startsWith(nowYear))  yearlyAcc  += v;
                });

                const elMonth = document.getElementById('chart-monthly-pnl');
                const elYear  = document.getElementById('chart-yearly-pnl');
                const elTotal = document.getElementById('chart-total-pnl');
                // data-pnl 속성 함께 설정: warm-beige 테마에서 !important CSS 덮어쓰기용
                const setPnlEl = (el, val, label) => {
                    if (!el) return;
                    el.textContent = `${label}: ${formatPnl(val)}`;
                    el.style.color = colorPnl(val);
                    el.dataset.pnl = val > 0 ? 'profit' : (val < 0 ? 'loss' : 'neutral');
                };
                setPnlEl(elMonth, monthlyAcc, '이번달');
                setPnlEl(elYear,  yearlyAcc,  '올해');
                setPnlEl(elTotal, totalAcc,   '누적');
            });
    }

    // ── 모멘텀 슬롯 카드 렌더링 ──
    function renderMomentumSlots(momentumList) {
        const slots = momentumList || [null, null, null];
        slots.forEach((mp, i) => {
            const el = document.getElementById(`mslot-${i}`);
            if (!el) return;
            if (!mp) {
                el.className = 'momentum-slot-card';
                el.innerHTML = `
                    <div class="mslot-label">슬롯 #${i + 1}</div>
                    <div class="mslot-empty">⏳ 스캔 중...<br>빈 슬롯</div>`;
            } else {
                const pnl     = mp.pnl_pct || 0;
                const pnlSign = pnl >= 0 ? '+' : '';
                const pnlClr  = pnl > 0 ? '#f85149' : (pnl < 0 ? '#58a6ff' : '#8b949e');
                const avgPStr = mp.avg_price > 0 ? Math.round(mp.avg_price).toLocaleString() : '-';
                const curPStr = mp.price > 0 ? Math.round(mp.price).toLocaleString() : '-';
                el.className  = 'momentum-slot-card occupied' + (pnl > 0 ? ' profit' : pnl < 0 ? ' loss' : '');
                el.innerHTML  = `
                    <div class="mslot-label">슬롯 #${i + 1} · 🚀 보유 중</div>
                    <div class="mslot-name">${mp.name} <span style="color:#64748b;font-size:0.75rem;">${mp.ticker}</span></div>
                    <div class="mslot-pnl pnl-rate" data-pnl="${pnl > 0 ? 'profit' : pnl < 0 ? 'loss' : 'neutral'}" style="color:${pnlClr}">${pnlSign}${pnl.toFixed(2)}%
                        <span style="font-size:0.75rem;font-weight:400;color:#94a3b8;margin-left:6px;">${Math.round(mp.value || 0).toLocaleString()}원</span>
                    </div>
                    <div class="mslot-meta">
                        ${(mp.shares || 0).toLocaleString()}주 · 단가 ${avgPStr}원 → 현재 ${curPStr}원<br>
                        ${mp.elapsed || ''} · ${mp.reason || ''}
                    </div>`;
            }
        });
    }

    // 🟢 팝업창(모달)을 띄우는 함수
    window.showStatusModal = function (name, message) {
        document.getElementById('modalTickerName').innerText = `[${name}] 진행 상황`;
        document.getElementById('modalStatusMsg').innerText = message;
        document.getElementById('statusModal').style.display = 'flex';
    }

    // ── Main UI Update ──
    function updateUI(data) {
        if (data.initial_cash !== undefined) {
            USER_INVESTED_CAPITAL = data.initial_cash;
            const inputEl = document.getElementById('initialCash');
            if (inputEl && document.activeElement !== inputEl) {
                inputEl.value = data.initial_cash;
            }
        }

        if (data.mock_total_asset !== undefined) {
            const totalValEl = document.getElementById('total-value');
            if (totalValEl) {
                totalValEl.textContent = Math.round(data.mock_total_asset).toLocaleString() + '원';
                // 수익 여부에 따라 색상: 이익 → 빨강, 손실 → 파랑, 중립 → 기본
                // data-pnl 속성도 함께 설정: warm-beige 테마 CSS 덮어쓰기용 (브라우저가 hex→rgb 정규화해서 [style*=] 방식이 안됨)
                if (data.mock_pnl !== undefined) {
                    const pnlState = data.mock_pnl > 0 ? 'profit' : (data.mock_pnl < 0 ? 'loss' : 'neutral');
                    totalValEl.style.color = data.mock_pnl > 0 ? '#f85149' : (data.mock_pnl < 0 ? '#58a6ff' : '');
                    totalValEl.dataset.pnl = pnlState;
                }
            }
        }
        if (data.mock_pnl !== undefined && data.mock_pnl_rt !== undefined) {
            const pnlEl = document.getElementById('total-pnl');
            if (pnlEl) {
                const sign = data.mock_pnl >= 0 ? '+' : '';
                const pnlState = data.mock_pnl > 0 ? 'profit' : (data.mock_pnl < 0 ? 'loss' : 'neutral');
                const color = data.mock_pnl > 0 ? '#f85149' : (data.mock_pnl < 0 ? '#58a6ff' : '#8b949e');
                pnlEl.style.color = color;
                pnlEl.style.fontWeight = '700';
                pnlEl.dataset.pnl = pnlState;
                pnlEl.textContent = `수익: ${sign}${Math.round(data.mock_pnl).toLocaleString()}원 (${sign}${data.mock_pnl_rt.toFixed(2)}%)`;
            }
        }

        // 예수금 표시
        if (data.available_cash !== undefined) {
            const cashValEl = document.getElementById('available-cash-val');
            if (cashValEl) {
                cashValEl.textContent = Math.round(data.available_cash).toLocaleString() + '원';
            }
        }

        const isLive = (data.is_mock === false || data.is_mock === 0);

        const cb = document.getElementById('modeSwitch');
        const lblReal = document.getElementById('label-real');
        const lblMock = document.getElementById('label-mock');

        if (cb && data.is_mock !== undefined) {
            cb.checked = !!data.is_mock;
            if (lblReal && lblMock) {
                if (data.is_mock) {
                    lblMock.classList.add('mode-active');
                    lblReal.classList.remove('mode-active');
                } else {
                    lblReal.classList.add('mode-active');
                    lblMock.classList.remove('mode-active');
                }
            }
        }

        // 모멘텀 슬롯 렌더링
        renderMomentumSlots(data.momentum_list);

        if (isLive) {
            document.body.classList.remove('theme-warm-beige');
        } else {
            document.body.classList.add('theme-warm-beige');
        }

        const pnlTitle = document.getElementById('pnl-title');
        if (pnlTitle && data.is_mock !== undefined) {
            pnlTitle.textContent = data.is_mock ? '모의투자 수익률' : '실전투자 수익률';
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
        const satCard = topCardsContainer.lastElementChild;
        document.querySelectorAll('.core-card').forEach(e => e.remove());

        const fragment = document.createDocumentFragment();
        let totalCoreValue = 0;

        cores.forEach((core) => {
            totalCoreValue += (core.value || 0);

            const sText = core.status || "감시 중 👀";
            const sMsg = core.status_msg || "지표 점검 중...";
            let badgeStyle = "background:rgba(255,255,255,0.1); color:#94a3b8; border:1px solid rgba(255,255,255,0.2);";
            if (sText.includes('AI') || sText.includes('심사')) badgeStyle = "background:rgba(168,85,247,0.2); color:#c084fc; border:1px solid rgba(168,85,247,0.4); animation:pulse 2s infinite;";
            if (sText.includes('주문') || sText.includes('대기')) badgeStyle = "background:rgba(245,158,11,0.2); color:#fcd34d; border:1px solid rgba(245,158,11,0.4); animation:pulse 2s infinite;";
            if (sText.includes('거절') || sText.includes('손절') || sText.includes('청산')) badgeStyle = "background:rgba(239,68,68,0.2); color:#fca5a5; border:1px solid rgba(239,68,68,0.4);";

            // 코어 수익률 계산
            const coreAvgP = core.avg_price || 0;
            const coreCurP = core.price || 0;
            let corePnlHtml = '';
            if (core.shares > 0 && coreAvgP > 0 && coreCurP > 0) {
                const corePnlPct = ((coreCurP / coreAvgP) - 1) * 100;
                const corePnlState = corePnlPct > 0 ? 'profit' : (corePnlPct < 0 ? 'loss' : 'neutral');
                const corePnlClr  = corePnlPct > 0 ? '#f85149' : (corePnlPct < 0 ? '#58a6ff' : '#8b949e');
                const corePnlSign = corePnlPct >= 0 ? '+' : '';
                corePnlHtml = `<div class="pnl-rate" data-pnl="${corePnlState}" style="font-size:0.85rem;font-weight:700;margin-top:3px;color:${corePnlClr};">${corePnlSign}${corePnlPct.toFixed(2)}%</div>`;
            } else if (core.shares > 0) {
                corePnlHtml = `<div class="pnl-rate" data-pnl="neutral" style="font-size:0.8rem;color:#8b949e;margin-top:3px;">수익률 계산 중...</div>`;
            }

            const div = document.createElement('div');
            div.className = 'info-card glass-card core-card';
            div.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <h3 style="margin: 0; display:flex; align-items:center; gap:8px;">
                        💎 ${core.name} (Core)
                        <span onclick="showStatusModal('${core.name}', '${sMsg.replace(/'/g, "\\'")}')" class="badge" style="cursor:pointer; ${badgeStyle}">${sText}</span>
                    </h3>
                    <button onclick="openCoreModal()" style="background:none; border:none; color:var(--text-dim); cursor:pointer; font-size:1.1rem;" title="코어 설정 변경">⚙️</button>
                </div>
                <div class="card-value highlight">${(core.shares || 0).toLocaleString()} 주</div>
                <div class="card-subvalue">
                    평가금액 ${Math.round(core.value || 0).toLocaleString()}원<br>
                    <span style="color:#64748b;font-size:0.8rem;">(배정 예산: ${(core.budget || 0).toLocaleString()}원)</span>
                </div>
                ${corePnlHtml}
                <div class="card-subvalue" style="color:#f59e0b;font-size:0.8rem;margin-top:4px">🔒 floor: ${core.floor}주 보호</div>
            `;
            fragment.appendChild(div);
        });
        topCardsContainer.insertBefore(fragment, satCard);

        if (sats.length > 0) {
            let satHtmlBuffer = '';
            sats.forEach(s => {
                const isHolding = s.shares > 0;

                const sText = s.status || "감시 중 👀";
                const sMsg = s.status_msg || "지표 점검 중...";
                let badgeStyle = "background:rgba(255,255,255,0.1); color:#94a3b8; border:1px solid rgba(255,255,255,0.2);";
                if (sText.includes('AI') || sText.includes('심사')) badgeStyle = "background:rgba(168,85,247,0.2); color:#c084fc; border:1px solid rgba(168,85,247,0.4); animation:pulse 2s infinite;";
                if (sText.includes('주문') || sText.includes('대기')) badgeStyle = "background:rgba(245,158,11,0.2); color:#fcd34d; border:1px solid rgba(245,158,11,0.4); animation:pulse 2s infinite;";
                if (sText.includes('거절') || sText.includes('손절') || sText.includes('청산') || sText.includes('보류')) badgeStyle = "background:rgba(239,68,68,0.2); color:#fca5a5; border:1px solid rgba(239,68,68,0.4);";

                const statusBadge = `<span class="badge" onclick="showStatusModal('${s.name}', '${sMsg.replace(/'/g, "\\'")}')" style="cursor:pointer; ${badgeStyle}">${sText}</span>`;

                const stratBadge = s.strategy
                    ? `<span class="badge badge-strategy" style="cursor:pointer;" onclick="showStrategyInfo('${s.strategy}')" title="클릭하여 전략 상세 설명 보기">${s.strategy}</span>`
                    : '<span style="color:#8b949e">-</span>';
                const sharesCell = isHolding ? `${s.shares.toLocaleString()}주` : `<span style="color:#64748b">-</span>`;

                // 평가금액: 실시간 live_prices 기반 (3초 폴링)
                const valueCell = isHolding
                    ? `<span style="font-weight:600;">${Math.round(s.value || 0).toLocaleString()}원</span>`
                    : `<span style="color:#64748b">-</span>`;

                // 수익률: avg_price 기반 계산 (고점 갱신대기 대체)
                // data-pnl 속성 사용 — warm-beige td { -webkit-text-fill-color: !important } 상속 극복
                let pnlCell = '';
                if (isHolding) {
                    const avgP = s.avg_price || 0;
                    const curP = s.price || 0;
                    if (avgP > 0 && curP > 0) {
                        const pnlPct = ((curP / avgP) - 1) * 100;
                        const pnlState = pnlPct > 0 ? 'profit' : (pnlPct < 0 ? 'loss' : 'neutral');
                        const pnlColor = pnlPct > 0 ? '#f85149' : (pnlPct < 0 ? '#58a6ff' : '#8b949e');
                        const pnlSign = pnlPct >= 0 ? '+' : '';
                        pnlCell = `<div class="pnl-rate" data-pnl="${pnlState}" style="font-size:0.75rem;color:${pnlColor};margin-top:3px;font-weight:700;">${pnlSign}${pnlPct.toFixed(2)}%</div>`;
                    } else {
                        pnlCell = `<div class="pnl-rate" data-pnl="neutral" style="font-size:0.75rem;color:#64748b;margin-top:3px;">수익률 계산 중...</div>`;
                    }
                }

                satHtmlBuffer += `
                    <tr>
                        <td><b>${s.name}</b>
                            <span style="color:#64748b;font-size:0.78rem;margin-left:5px">${s.ticker}</span>
                        </td>
                        <td>${stratBadge}</td>
                        <td>${sharesCell}</td>
                        <td>
                            <div>${valueCell}</div>
                            ${pnlCell}
                        </td>
                        <td>${statusBadge}</td>
                    </tr>`;
            });
            satTbody.innerHTML = satHtmlBuffer;
        }

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
    window.fetchPnl    = fetchPnl;
    window.updateUI    = updateUI;

    fetchStatus();
    fetchPnl();
    setInterval(fetchStatus, 5000);
    setInterval(fetchPnl, 15000);
});

window.toggleMode = async function () {
    const cb = document.getElementById('modeSwitch');
    const isMock = cb.checked ? 1 : 0;
    cb.disabled = true;

    const lblReal = document.getElementById('label-real');
    const lblMock = document.getElementById('label-mock');
    if (isMock) {
        lblMock.classList.add('mode-active');
        lblReal.classList.remove('mode-active');
    } else {
        lblReal.classList.add('mode-active');
        lblMock.classList.remove('mode-active');
    }

    try {
        const res = await fetch('/api/settings/mode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_mock: isMock })
        });
        const result = await res.json();
        if (result.status === 'success') {
            showToast(isMock ? '모의투자 모드로 전환됨' : '실전투자 모드로 전환됨', 'info');
            fetch('/api/status').then(r => r.json()).then(data => {
                updateUI(data);
                fetchPnl();
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
}
window.closeSettingsModal = function () {
    document.getElementById('settingsModal').style.display = 'none';
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
            ctx.fillStyle = '#a78bfa'; ctx.font = '14px sans-serif';
            ctx.fillText('AI 시뮬레이션 최적 타점 탐색 중...', W / 2 - 90, H / 2);
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
    const isMock = document.getElementById('modeSwitch').checked ? 1 : 0;
    const coreJsonStr = JSON.stringify(_coreStockList);

    const data = {
        real_app_key: document.getElementById('realAppKey').value,
        real_app_secret: document.getElementById('realAppSecret').value,
        real_account_no: document.getElementById('realAccountNo').value,
        mock_app_key: document.getElementById('mockAppKey').value,
        mock_app_secret: document.getElementById('mockAppSecret').value,
        mock_account_no: document.getElementById('mockAccountNo').value,

        telegram_token: document.getElementById('teleToken').value,
        telegram_chat_id: document.getElementById('teleChatId').value,
        claude_api_key: document.getElementById('claudeApiKey').value,
        core_stocks: coreJsonStr,
        is_mock: isMock,
        initial_cash: document.getElementById('initialCash').value
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
    const isMock = document.getElementById('modeSwitch').checked ? 1 : 0;
    const coreJsonStr = JSON.stringify(_coreStockList);

    const data = {
        real_app_key: document.getElementById('realAppKey').value,
        real_app_secret: document.getElementById('realAppSecret').value,
        real_account_no: document.getElementById('realAccountNo').value,
        mock_app_key: document.getElementById('mockAppKey').value,
        mock_app_secret: document.getElementById('mockAppSecret').value,
        mock_account_no: document.getElementById('mockAccountNo').value,

        telegram_token: document.getElementById('teleToken').value,
        telegram_chat_id: document.getElementById('teleChatId').value,
        claude_api_key: document.getElementById('claudeApiKey').value,
        core_stocks: coreJsonStr,
        is_mock: isMock
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

window.openReportModal = async function () {
    document.getElementById('reportModal').style.display = 'block';
    document.getElementById('report-content').innerHTML = '리포트 데이터를 불러오는 중...';
    document.getElementById('report-time-tabs').innerHTML = '';
    try {
        const res = await fetch('/api/daily_report');
        const json = await res.json();
        if (json.status === 'success' && json.data) {
            const data = json.data;
            const times = ['11:00', '15:30', '20:00'];
            let tabsHtml = '';
            let latestTime = null;
            let latestContent = null;

            // report_markdown: 단일 텍스트 (아직 시간별 분리 전)
            if (data.report_markdown && !data['11:00'] && !data['15:30'] && !data['20:00']) {
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

window.updateTestModeLabel = function () {}

window.runTestOrder = async function (side) {
    const ticker = document.getElementById('testOrderTicker').value.trim();
    const resultEl = document.getElementById('testOrderResult');
    if (!ticker) { resultEl.textContent = '⚠️ 종목코드를 입력하세요.'; resultEl.style.color = '#f59e0b'; return; }
    const modeEl = document.querySelector('input[name="testMode"]:checked');
    const useReal = modeEl && modeEl.value === 'real';
    if (useReal && !confirm(`⚠️ 실전 계좌로 ${ticker} 1주 ${side === 'BUY' ? '매수' : '매도'} 주문을 접수합니다.\n계속하시겠습니까?`)) return;
    resultEl.textContent = '주문 전송 중...'; resultEl.style.color = '#94a3b8';
    try {
        const res = await fetch('/api/test_order', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ticker, side, use_real: useReal })
        });
        const data = await res.json();
        if (data.status === 'success') {
            resultEl.textContent = '✅ ' + data.message; resultEl.style.color = '#3fb950';
        } else {
            resultEl.textContent = '❌ ' + data.message; resultEl.style.color = '#f85149';
        }
    } catch (e) { resultEl.textContent = '❌ 서버 통신 오류'; resultEl.style.color = '#f85149'; }
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
    if (!confirm('대화 기록을 초기화할까요?')) return;
    try { await fetch('/api/ai_reset', { method: 'POST' }); } catch (e) { }
    const messages = document.getElementById('chat-messages');
    messages.innerHTML = `<div class="chat-msg ai"><div class="chat-bubble">대화 기록이 초기화되었습니다.</div><span class="chat-msg-time">라씨 AI · ${new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' })}</span></div>`;
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
    const amount = parseInt(input.replace(/[^0-9]/g, ''));
    if (!amount || amount <= 0) { alert('올바른 금액을 입력해주세요.'); return; }
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
