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
// ==================== End of Toggles ====================