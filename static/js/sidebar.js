// ==================== Sidebar Section Collapse ====================
function toggleSection(sectionId) {
    const section = document.getElementById(sectionId);
    section.classList.toggle('collapsed');
}

function toggleAllSections() {
    const btn = document.getElementById('toggleAllBtn');
    const sections = document.querySelectorAll('.sidebar-section');
    const allCollapsed = Array.from(sections).every(s => s.classList.contains('collapsed'));

    if (allCollapsed) {
        sections.forEach(s => s.classList.remove('collapsed'));
        btn.textContent = '全部折叠';
    } else {
        sections.forEach(s => s.classList.add('collapsed'));
        btn.textContent = '全部展开';
    }
}

// ==================== Active Flows ====================
async function updateActiveFlows() {
    try {
        const response = await fetch('/flows/active');
        if (!response.ok) return;
        const flows = await response.json();

        const flowList = document.getElementById('flowList');
        if (!flows || flows.length === 0) {
            flowList.innerHTML = `
                <div class="switch-item">
                    <div class="switch-info">
                        <div class="switch-name">无活跃流</div>
                    </div>
                </div>
            `;
        } else {
            flowList.innerHTML = flows.map(f => {
                const pathStr = f.path.join(' → ');
                const isQos = f.type === 'qos';
                const label = isQos && f.name
                    ? `${f.name} (${f.src_ip} → ${f.dst_ip})`
                    : `${f.src_ip} → ${f.dst_ip}`;
                return `
                    <div class="switch-item" onclick="highlightFlowPath(${JSON.stringify(f.path_dpids).replace(/"/g, '&quot;')})" style="cursor: pointer;">
                        <div class="switch-icon" style="background:#238636">→</div>
                        <div class="switch-info">
                            <div class="switch-name">${label}</div>
                            <div class="switch-dpid">${pathStr}</div>
                        </div>
                    </div>
                `;
            }).join('');
        }
    } catch (e) {
        console.log('Failed to fetch active flows:', e);
    }
}

// ==================== Multicast Active Flows ====================
async function updateMulticastActiveFlows() {
    try {
        const response = await fetch('/multicast/active_flows');
        if (!response.ok) return;
        const flows = await response.json();

        const flowList = document.getElementById('multicastFlowList');
        if (!flows || flows.length === 0) {
            flowList.innerHTML = `
                <div class="switch-item">
                    <div class="switch-info">
                        <div class="switch-name">无组播活跃流</div>
                    </div>
                </div>
            `;
        } else {
            flowList.innerHTML = flows.map(f => {
                const pathStr = f.path.join(' → ');
                const edgesJson = JSON.stringify(f.path_edges).replace(/"/g, '&quot;');
                return `
                    <div class="switch-item" onclick="highlightMulticastPath(${edgesJson})" style="cursor: pointer;">
                        <div class="switch-icon" style="background:#9b59b6">★</div>
                        <div class="switch-info">
                            <div class="switch-name">${f.multicast_addr}</div>
                            <div class="switch-dpid">${f.source_ip} → ${pathStr}</div>
                        </div>
                    </div>
                `;
            }).join('');
        }
    } catch (e) {
        console.log('Failed to fetch multicast active flows:', e);
    }
}