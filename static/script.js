document.addEventListener('DOMContentLoaded', () => {

    // ── DOM refs ──
    const btnToggle = document.getElementById('btn-toggle');
    const toggleLabel = document.getElementById('toggle-label');
    const miniLog = document.getElementById('mini-log');
    const satTbody = document.getElementById('sat-tbody');

    // ── P&L Chart 초기화 ──
    let pnlChart = null;

    function initChart(labels, values) {
        const ctx = document.getElementById('pnl-chart').getContext('2d');
        const empty = document.getElementById('chart-empty');

        if (!labels || labels.length === 0) {
            empty.style.display = 'flex';
            return;
        }
        empty.style.display = 'none';

        const colors = values.map(v =>
            v >= 0 ? 'rgba(63,185,80,0.75)' : 'rgba(248,81,73,0.75)'
        );
        const borderColors = values.map(v =>
            v >= 0 ? 'rgba(63,185,80,1)' : 'rgba(248,81,73,1)'
        );

        if (pnlChart) {
            pnlChart.data.labels = labels;
            pnlChart.data.datasets[0].data = values;
            pnlChart.data.datasets[0].backgroundColor = colors;
            pnlChart.data.datasets[0].borderColor = borderColors;
            pnlChart.update('none');
            return;
        }

        pnlChart = new Chart(ctx, {
            type: 'bar',
            data: {
                labels,
                datasets: [{
                    label: '일별 손익 (원)',
                    data: values,
                    backgroundColor: colors,
                    borderColor: borderColors,
                    borderWidth: 1.5,
                    borderRadius: 6,
                    borderSkipped: false,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => {
                                const v = ctx.parsed.y;
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
                        grid: { color: 'rgba(255,255,255,0.05)' },
                        ticks: { color: '#8b949e', font: { size: 11 } }
                    },
                    y: {
                        grid: { color: 'rgba(255,255,255,0.05)' },
                        ticks: {
                            color: '#8b949e',
                            font: { size: 11 },
                            callback: v => (v >= 0 ? '+' : '') + v.toLocaleString() + '원'
                        }
                    }
                }
            }
        });
    }

    // ── Toggle Button ──
    btnToggle.addEventListener('click', () => {
        fetch('/api/toggle', { method: 'POST' })
            .then(async r => {
                if (!r.ok) {
                    const err = await r.json();
                    alert(err.message || '봇 시작 실패');
                }
                return r.json();
            })
            .then(() => fetchStatus())
            .catch(e => console.error('Toggle error', e));
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
                initChart(data.labels, data.values);

                const todayStr = new Date().toISOString().slice(0, 10);
                const monthStr = new Date().toISOString().slice(0, 7);
                const yearStr = new Date().toISOString().slice(0, 4);

                let total = 0, monthly = 0, yearly = 0;

                (data.values || []).forEach((val, i) => {
                    total += val;
                    const dateStr = data.labels[i];
                    if (dateStr.startsWith(monthStr)) monthly += val;
                    if (dateStr.startsWith(yearStr)) yearly += val;
                });

                const formatPnl = (val) => (val >= 0 ? '+' : '') + val.toLocaleString() + '원';
                const colorPnl = (val) => val >= 0 ? '#3fb950' : '#f85149';

                const elMonth = document.getElementById('chart-monthly-pnl');
                const elYear = document.getElementById('chart-yearly-pnl');
                const elTotal = document.getElementById('chart-total-pnl');

                if (elMonth) { elMonth.textContent = `이번달: ${formatPnl(monthly)}`; elMonth.style.color = colorPnl(monthly); }
                if (elYear) { elYear.textContent = `올해: ${formatPnl(yearly)}`; elYear.style.color = colorPnl(yearly); }
                if (elTotal) { elTotal.textContent = `누적: ${formatPnl(total)}`; elTotal.style.color = colorPnl(total); }
            });
    }

    // ── KIS Balance Fetch ──
    let kisBalanceInterval = null;

    function fetchKisBalance() {
        fetch('/api/kis_balance')
            .then(r => r.json())
            .then(res => {
                const kisTbody = document.getElementById('kis-tbody');
                const kisSummary = document.getElementById('kis-total-summary');
                if (res.status === 'success' && res.data) {
                    const d = res.data;

                    // D+2 예수금 + 주식평가금액 요약
                    if (kisSummary) kisSummary.textContent = `D+2 예수금: ${(d.total_cash || 0).toLocaleString()}원 | 주식평가: ${(d.total_value || 0).toLocaleString()}원`;

                    // 총 평가금액 카드: 예수금 + 주식평가
                    const totalAsset = (d.total_cash || 0) + (d.total_value || 0);
                    const totalValEl = document.getElementById('total-value');
                    if (totalValEl) {
                        totalValEl.textContent = totalAsset.toLocaleString() + '원';
                    }

                    // 평가손익 = 총 평가금액 - 매입금액 합계
                    const totalPurchase = d.total_purchase || 0;
                    const totalPnl = (d.total_value || 0) - totalPurchase;
                    const pnlRt = totalPurchase > 0 ? (totalPnl / totalPurchase * 100) : 0;

                    const pnlEl = document.getElementById('total-pnl');
                    if (pnlEl) {
                        if (totalPurchase > 0) {
                            const sign = totalPnl >= 0 ? '+' : '';
                            // 한국 증권 컨벤션: 이익 빨간색, 손실 파란색
                            const color = totalPnl > 0 ? '#f85149' : (totalPnl < 0 ? '#58a6ff' : '#8b949e');
                            pnlEl.style.color = color;
                            pnlEl.style.fontWeight = '700';
                            pnlEl.textContent = `수익: ${sign}${totalPnl.toLocaleString()}원 (${sign}${pnlRt.toFixed(2)}%)`;
                        } else {
                            pnlEl.style.color = '#8b949e';
                            pnlEl.textContent = '수익: 매입 내역 없음';
                        }
                    }

                    // 실제 보유 종목 표시 (보유수량 > 0인 것만)
                    const realStocks = (d.stocks || []).filter(s => s.shares > 0);
                    if (kisTbody) {
                        if (realStocks.length > 0) {
                            // 반복문 내부에서 직접 DOM을 건드리지 않고, 임시 문자열에 차곡차곡 모읍니다.
                            let htmlBuffer = '';
                            realStocks.forEach(s => {
                                const profitColor = s.profit_rt > 0 ? '#f85149' : (s.profit_rt < 0 ? '#58a6ff' : '#8b949e');
                                const profitSign = s.profit_rt > 0 ? '+' : '';
                                htmlBuffer += `
                                    <tr>
                                        <td><b>${s.name}</b> <span style="color:#64748b;font-size:0.78rem;">${s.ticker}</span></td>
                                        <td>${s.shares.toLocaleString()}주</td>
                                        <td>${Math.round(s.purchase_price).toLocaleString()}원</td>
                                        <td>${Math.round(s.current_price).toLocaleString()}원</td>
                                        <td>${Math.round(s.value).toLocaleString()}원</td>
                                        <td style="color: ${profitColor}; font-weight: 600;">${profitSign}${s.profit_rt.toFixed(2)}%</td>
                                    </tr>`;
                            });
                            // 조립이 완전히 끝난 무결점 상태의 HTML을 한방에 꽂아넣어 깜빡임을 원천 차단합니다.
                            kisTbody.innerHTML = htmlBuffer;
                        } else {
                            kisTbody.innerHTML = '<tr><td colspan="6" class="muted-center">보유 중인 주식이 없습니다.</td></tr>';
                        }
                    }
                } else {
                    const errMsg = res.message || '잔고 조회 실패';
                    if (kisTbody) kisTbody.innerHTML = `<tr><td colspan="6" class="muted-center">⚠️ ${errMsg}</td></tr>`;
                    if (kisSummary) kisSummary.textContent = 'API 오류';

                    const totalValEl = document.getElementById('total-value');
                    if (totalValEl) {
                        totalValEl.textContent = '연결 실패 (API 키 확인 필요)';
                    }
                    const pnlEl = document.getElementById('total-pnl');
                    if (pnlEl) {
                        pnlEl.textContent = '수익: 계좌 미연결';
                        pnlEl.style.color = '#8b949e';
                        pnlEl.style.fontWeight = 'normal';
                    }
                }
            })
            .catch(e => {
                console.error('kis balance fetch error', e);
                const kisTbody = document.getElementById('kis-tbody');
                const kisSummary = document.getElementById('kis-total-summary');
                if (kisTbody) kisTbody.innerHTML = '<tr><td colspan="6" class="muted-center">서버 통신 오류</td></tr>';
                if (kisSummary) kisSummary.textContent = '통신 오류';
            });
    }

    function startKisBalancePolling() {
        fetchKisBalance(); // 항상 즉시 1회 호출
        if (kisBalanceInterval) return; // 이미 인터벌 실행 중이면 중복 등록 방지
        kisBalanceInterval = setInterval(fetchKisBalance, 10000);
    }

    function stopKisBalancePolling() {
        if (kisBalanceInterval) {
            clearInterval(kisBalanceInterval);
            kisBalanceInterval = null;
        }
    }

    // ── Main UI Update ──
    function updateUI(data) {
        // ══ 실전/모의 모드 구분 ══
        const isLive = (data.is_mock === false || data.is_mock === 0);
        const realSection = document.getElementById('real-account-section');
        const mockSection = document.getElementById('mock-notice-section');

        // 다른 기기에서 바꾼 스위치 버튼과 글씨 하이라이트 불빛 물리적 연동
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

        // 🎨 [기능 개방 및 디자인 변경] 모의투자도 실전처럼 실시간 계좌 잔고를 완벽히 출력 및 동기화합니다.
        if (realSection && mockSection) {
            realSection.style.display = 'block'; // 실전/모의 상관없이 잔고 테이블 상시 노출
            mockSection.style.display = 'none';  // 단순 안내 문구는 숨김 처리

            if (!isLive) {
                // 모의투자 시 모의 자산 수치로 UI 강제 업데이트
                const totalAsset = data.mock_total_asset || 0;
                const totalValEl = document.getElementById('total-value');
                if (totalValEl) {
                    totalValEl.textContent = totalAsset.toLocaleString() + '원';
                }

                const totalPnl = data.mock_pnl || 0;
                const pnlRt = data.mock_pnl_rt || 0;
                const pnlEl = document.getElementById('total-pnl');
                if (pnlEl) {
                    const sign = totalPnl >= 0 ? '+' : '';
                    const color = totalPnl > 0 ? '#f85149' : (totalPnl < 0 ? '#58a6ff' : '#8b949e');
                    pnlEl.style.color = color;
                    pnlEl.style.fontWeight = '700';
                    pnlEl.textContent = `수익: ${sign}${totalPnl.toLocaleString()}원 (${sign}${pnlRt.toFixed(2)}%)`;
                }
            }
        }

        // 🔄 실전/모의 모드에 상관없이 실시간 폴링(10초 주기 계좌 동기화)을 상시 가동합니다.
        startKisBalancePolling();

        // 🎨 [가독성 개선 및 테마 스위칭] CSS 클래스로 전체 UI 테마를 제어합니다 (오인 매매 방지)
        if (isLive) {
            document.body.classList.remove('theme-warm-beige');
        } else {
            document.body.classList.add('theme-warm-beige');
        }

        // Mode Label Update
        const pnlTitle = document.getElementById('pnl-title');
        if (pnlTitle && data.is_mock !== undefined) {
            pnlTitle.textContent = data.is_mock ? '모의투자 수익률' : '실전투자 수익률';
        }

        // Toggle button state
        const running = data.is_running;
        if (running) {
            btnToggle.className = 'btn-toggle btn-running';
            toggleLabel.textContent = '⏹ Running';
        } else {
            btnToggle.className = 'btn-toggle btn-stopped';
            toggleLabel.textContent = 'Stopped';
        }

        // ── API Key Warning ──
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

        // Hot Sectors Badge
        const hotSectorsEl = document.getElementById('hot-sectors');
        if (data.hot_sectors && data.hot_sectors.length > 0) {
            hotSectorsEl.textContent = '🔥 현재 강세 섹터: ' + data.hot_sectors.join(', ');
        } else {
            hotSectorsEl.textContent = '🔥 분석 중이거나 강세 섹터가 없습니다.';
        }

        // ── Portfolio Cards ──
        const cores = data.cores || [];
        const sats = data.satellites || [];

        if (data.num_satellites !== undefined) {
            document.getElementById('sat-num-display').textContent = data.num_satellites;
        }

        // 봇 상태를 받아올 때 현재 DB에 보관 중인 코어 종목 명단을 실시간으로 전역 변수에 복사해 둡니다.
        if (data.cores) {
            window.cachedCoreStocks = data.cores.map(c => ({ ticker: c.ticker, name: c.name }));
        }

        // 코어 카드가 그려질 때 중간 탈착 과정이 화면에 노출되지 않도록 가상 임시 저장소(Fragment)를 씁니다.
        const topCardsContainer = document.getElementById('top-cards-container');
        const satCard = topCardsContainer.lastElementChild;
        document.querySelectorAll('.core-card').forEach(e => e.remove());

        const fragment = document.createDocumentFragment();
        let totalCoreValue = 0;

        cores.forEach((core) => {
            totalCoreValue += (core.value || 0);
            const div = document.createElement('div');
            // CSS 테마 클래스에 의해 코어 카드의 색상도 일괄 자동 변경됩니다.
            div.className = 'info-card glass-card core-card';
            div.innerHTML = `
                <div style="display: flex; justify-content: space-between; align-items: flex-start;">
                    <h3 style="margin: 0;">
                        💎 ${core.name} (Core) 
                        ${!core.shares ? '<span style="font-size:0.75rem; color:#f59e0b; background:rgba(245, 158, 11, 0.2); padding:2px 6px; border-radius:4px; margin-left:6px; vertical-align:middle;">[편입 예정]</span>' : ''}
                    </h3>
                    <button onclick="openCoreModal()" style="background:none; border:none; color:var(--text-dim); cursor:pointer; font-size:1.1rem;" title="코어 설정 변경">⚙️</button>
                </div>
                <div class="card-value highlight">${(core.shares || 0).toLocaleString()} 주</div>
                <div class="card-subvalue">
                    평가금액 ${(core.value || 0).toLocaleString()}원<br>
                    <span style="color:#64748b;font-size:0.8rem;">(배정 예산: ${(core.budget || 0).toLocaleString()}원)</span>
                </div>
                <div class="card-subvalue" style="color:#f59e0b;font-size:0.8rem;margin-top:4px">🔒 floor: ${core.floor}주 보호</div>
            `;
            fragment.appendChild(div);
        });
        topCardsContainer.insertBefore(fragment, satCard); // 단 한번만 물리 결합하여 카드 출렁임 완벽 방어

        // ── Satellite Table ──
        if (sats.length > 0) {
            let satHtmlBuffer = '';
            sats.forEach(s => {
                const isHolding = s.shares > 0;
                const statusBadge = isHolding
                    ? `<span class="badge badge-holding">보유중</span>`
                    : `<span class="badge" style="background:rgba(245,158,11,0.2);color:#f59e0b;border:1px solid rgba(245,158,11,0.4);">구매 예정</span>`;
                const stratBadge = s.strategy
                    ? `<span class="badge badge-strategy" style="cursor:pointer;" onclick="showStrategyInfo('${s.strategy}')" title="클릭하여 전략 상세 설명 보기">${s.strategy}</span>`
                    : '<span style="color:#8b949e">-</span>';
                const sharesCell = isHolding ? `${s.shares.toLocaleString()}주` : `<span style="color:#64748b">-</span>`;
                const valueCell = isHolding ? `${(s.value || 0).toLocaleString()}원` : `<span style="color:#64748b">-</span>`;

                // 🟢 [신규 추가] 백엔드에서 전달받은 최고가(max_price)를 포맷팅합니다.
                const maxPriceStr = (s.max_price && s.max_price > 0) ? `${s.max_price.toLocaleString()}원` : '갱신 대기';

                satHtmlBuffer += `
                    <tr>
                        <td><b>${s.name}</b>
                            <span style="color:#64748b;font-size:0.78rem;margin-left:5px">${s.ticker}</span>
                        </td>
                        <td>${stratBadge}</td>
                        <td>${sharesCell}</td>
                        <td>
                            <div>${valueCell}</div>
                            ${isHolding ? `<div style="font-size:0.75rem; color:#f59e0b; margin-top:3px;">고점: ${maxPriceStr}</div>` : ''}
                        </td>
                        <td>${statusBadge}</td>
                    </tr>`;
            });
            satTbody.innerHTML = satHtmlBuffer; // 원자적 단발성 주입
        }

        // ── Mini Log (Header) ──
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

    // ── Polling ──
    fetchStatus();
    fetchPnl();
    setInterval(fetchStatus, 3000);
    setInterval(fetchPnl, 10000);
});

// Global func for adjusting sat count
window.adjustSat = function (delta) {
    const el = document.getElementById('sat-num-display');
    let val = parseInt(el.textContent) + delta;
    if (val < 1) val = 1;
    if (val > 15) val = 15;
    el.textContent = val;

    fetch('/api/settings/satellites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ count: val })
    }).then(r => r.json()).then(res => {
        if (res.status === 'success') {
            console.log('Satellite count updated to ' + res.num_satellites);
        }
    });
}

// ─── 모드 토글 스위치 (실전/모의) ───
window.toggleMode = async function () {
    const cb = document.getElementById('modeSwitch');
    const isMock = cb.checked ? 1 : 0;

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
            // UI를 즉시 갱신하기 위해 상태를 다시 가져옴
            fetch('/api/status').then(r => r.json()).then(data => {
                const isLive = (data.is_mock === false || data.is_mock === 0);
                const realSection = document.getElementById('real-account-section');
                const mockSection = document.getElementById('mock-notice-section');

                if (realSection && mockSection) {
                    realSection.style.display = 'block';
                    mockSection.style.display = 'none';
                }

                if (isLive) {
                    document.body.classList.remove('theme-warm-beige');
                } else {
                    document.body.classList.add('theme-warm-beige');
                }

                const pnlTitle = document.getElementById('pnl-title');
                if (pnlTitle) pnlTitle.textContent = isLive ? '실전투자 수익률' : '모의투자 수익률';
            });
        } else {
            console.error('모드 변경 실패');
            cb.checked = !cb.checked;
        }
    } catch (e) {
        console.error('서버 오류:', e);
        cb.checked = !cb.checked;
    }
}

// ─── 계좌 설정 모달 ───
window.openSettingsModal = function () {
    document.getElementById('settingsModal').style.display = 'block';
}
window.closeSettingsModal = function () {
    document.getElementById('settingsModal').style.display = 'none';
}

// ─── 코어 종목 모달 ───
window.openCoreModal = function () {
    document.getElementById('coreModal').style.display = 'block';

    // 창을 열 때, 아까 백업해둔 기존 코어 종목 리스트를 화면(모달)으로 불러옵니다.
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

// ─── 전략 애니메이션 로직 ───
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
        gemini_api_key: document.getElementById('geminiApiKey').value,
        core_stocks: coreJsonStr,
        is_mock: isMock
    };
    try {
        const res = await fetch('/api/settings/keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        const result = await res.json();
        if (result.status === 'success') {
            alert('코어 종목이 변경되었습니다. 시스템에 반영 중입니다.');
            closeCoreModal();
            location.reload();
        } else { alert('저장 실패: ' + (result.message || '오류')); }
    } catch (e) { alert('서버 통신 오류'); }
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
        gemini_api_key: document.getElementById('geminiApiKey').value,
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
            alert('계좌 설정이 저장되었습니다.');
            closeSettingsModal();
            location.reload();
        } else { alert('저장 실패'); }
    } catch (e) { alert('서버 통신 오류'); }
}

window.openReportModal = async function () {
    document.getElementById('reportModal').style.display = 'block';
    document.getElementById('report-content').innerHTML = '리포트 데이터를 불러오는 중...';
    try {
        const res = await fetch('/api/daily_report');
        const json = await res.json();
        if (json.status === 'success' && json.data) {
            let htmlText = json.data.report_markdown
                .replace(/### (.*)/g, '<h3>$1</h3>')
                .replace(/#### (.*)/g, '<h4 style="color:var(--accent-blue); margin-top:20px; border-bottom:1px solid #334155; padding-bottom:5px;">$1</h4>')
                .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
                .replace(/> (.*)/g, '<div style="background:rgba(59,130,246,0.1); padding:10px; border-left:4px solid var(--accent-blue); margin:10px 0; border-radius:4px;">$1</div>')
                .replace(/- (.*)/g, '<li style="margin-bottom:8px;">$1</li>')
                .replace(/\n/g, '<br>');
            document.getElementById('report-content').innerHTML = htmlText;
        } else { document.getElementById('report-content').innerHTML = json.message || '리포트가 아직 생성되지 않았습니다.'; }
    } catch (e) { document.getElementById('report-content').innerHTML = '오류: 리포트를 불러올 수 없습니다.'; }
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