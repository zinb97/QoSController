// ==================== Link Event Toggle ====================
let linkEventEnabled = true;

async function toggleLinkEvent(enabled) {
    try {
        const response = await fetch('/link_event', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: enabled})
        });
        const data = await response.json();
        if (data.success) {
            linkEventEnabled = data.enabled;
            document.getElementById('linkEventLabel').textContent =
                data.enabled ? '链路事件: 开启' : '链路事件: 关闭';
            document.getElementById('linkEventToggle').checked = data.enabled;
        }
    } catch (e) {
        console.error('Failed to toggle link event:', e);
    }
}

async function loadLinkEventState() {
    try {
        const response = await fetch('/link_event');
        const data = await response.json();
        linkEventEnabled = data.enabled;
        document.getElementById('linkEventLabel').textContent =
            data.enabled ? '链路事件: 开启' : '链路事件: 关闭';
        document.getElementById('linkEventToggle').checked = data.enabled;
    } catch (e) {
        console.error('Failed to load link event state:', e);
    }
}

// ==================== Multicast Reroute Toggle ====================
async function toggleMulticastReroute(enabled) {
    try {
        const response = await fetch('/multicast_reroute', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({enabled: enabled})
        });
        const data = await response.json();
        document.getElementById('multicastRerouteLabel').textContent =
            data.enabled ? '组播重路由: 开启' : '组播重路由: 关闭';
    } catch (e) {
        console.error('Failed to toggle multicast reroute:', e);
    }
}

async function loadMulticastRerouteState() {
    try {
        const response = await fetch('/multicast_reroute');
        const data = await response.json();
        document.getElementById('multicastRerouteLabel').textContent =
            data.enabled ? '组播重路由: 开启' : '组播重路由: 关闭';
        document.getElementById('multicastRerouteToggle').checked = data.enabled;
    } catch (e) {
        console.error('Failed to load multicast reroute state:', e);
    }
}

// ==================== Routing Algorithm ====================
async function loadRoutingAlgorithm() {
    try {
        const response = await fetch('/routing/algorithm');
        if (response.ok) {
            const data = await response.json();
            updateAlgorithmUI(data.algorithm);
        }
    } catch (e) {
        console.log('Failed to load routing algorithm:', e);
    }
}

function updateAlgorithmUI(algorithm) {
    const badge = document.getElementById('algoBadge');
    if (algorithm === 'smart') {
        badge.textContent = '🧠 智能算法';
        badge.className = 'algo-badge';
    } else {
        badge.textContent = '📊 迪杰斯特拉算法';
        badge.className = 'algo-badge dijkstra';
    }

    document.querySelectorAll('.algo-option').forEach(el => {
        if (el.dataset.value === algorithm) {
            el.style.borderColor = '#58a6ff';
            el.style.background = '#1f2937';
        } else {
            el.style.borderColor = '#30363d';
            el.style.background = '#1c2128';
        }
    });
}

async function selectAlgorithm(algorithm) {
    try {
        showLoading(`正在切换到 ${algorithm === 'smart' ? '智能算法' : '迪杰斯特拉算法'}...`);
        const response = await fetch('/routing/algorithm', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ algorithm: algorithm })
        });
        const data = await response.json();
        hideLoading();
        if (response.ok) {
            if (data.unchanged) {
                console.log('Algorithm unchanged, no action taken');
            } else {
                updateAlgorithmUI(algorithm);
                updateMulticastActiveFlows();
            }
        }
    } catch (e) {
        hideLoading();
        console.log('Failed to change routing algorithm:', e);
    }
}