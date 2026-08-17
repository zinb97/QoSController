// ==================== Level Colors ====================
const levelColors = {
    1: { bg: '#e74c3c', border: '#c0392b', shadow: 'rgba(231, 76, 60, 0.3)' },
    2: { bg: '#f39c12', border: '#d68910', shadow: 'rgba(243, 156, 18, 0.3)' },
    3: { bg: '#27ae60', border: '#1e8449', shadow: 'rgba(39, 174, 96, 0.3)' },
    4: { bg: '#3498db', border: '#2471a3', shadow: 'rgba(52, 152, 219, 0.3)' },
    5: { bg: '#9b59b6', border: '#7d3c98', shadow: 'rgba(155, 89, 182, 0.3)' },
    6: { bg: '#95a5a6', border: '#7f8c8d', shadow: 'rgba(149, 165, 166, 0.3)' },
};

function getSwitchColor(level) {
    const colors = levelColors[level] || levelColors[3];
    return {
        background: colors.bg,
        border: colors.border,
        highlight: { background: colors.bg, border: colors.border }
    };
}

function getLinkLoadColor(link) {
    if (!link.load || !link.bw || link.bw === 0) {
        return { color: '#58a6ff', highlight: '#79c0ff' };
    }
    const loadMbps = link.load.tx_bps;
    if (loadMbps < 0.5) {
        return { color: '#4a5568', highlight: '#718096' };
    }
    const ratio = loadMbps / link.bw;
    let r, g, b;

    if (ratio < 0.3) {
        const t = ratio / 0.3;
        r = Math.round(34 - 34 * t);
        g = Math.round(197 - 23 * t);
        b = Math.round(197 + 23 * t);
    } else if (ratio < 0.6) {
        const t = (ratio - 0.3) / 0.3;
        r = Math.round(237 + 6 * t);
        g = Math.round(201 - 45 * t);
        b = Math.round(38 - 20 * t);
    } else if (ratio < 0.8) {
        const t = (ratio - 0.6) / 0.2;
        r = Math.round(243 - 12 * t);
        g = Math.round(156 - 80 * t);
        b = Math.round(18 - 0 * t);
    } else {
        const t = Math.min(1, (ratio - 0.8) / 0.2);
        r = Math.round(231 - 31 * t);
        g = Math.round(76 - 56 * t);
        b = Math.round(60 - 45 * t);
    }
    const color = `rgb(${r}, ${g}, ${b})`;
    const highlight = `rgb(${Math.min(r + 40, 255)}, ${Math.min(g + 40, 255)}, ${Math.min(b + 40, 255)})`;
    return { color, highlight };
}

// ==================== Network Init ====================
function initNetwork() {
    var container = document.getElementById('mynetwork');
    var data = { nodes: nodes, edges: edges };
    var options = {
        nodes: {
            shape: 'box',
            font: { face: 'JetBrains Mono', size: 12, color: '#f0f6fc' },
            margin: 12
        },
        edges: {
            width: 2,
            color: { color: '#58a6ff', highlight: '#79c0ff' },
            arrows: { to: { enabled: true, scaleFactor: 0.8 } },
            smooth: { type: 'continuous' }
        },
        physics: { enabled: true, stabilization: { iterations: 100 } }
    };
    network = new vis.Network(container, data, options);

    network.on('click', function(params) {
        if (params.nodes.length > 0) {
            const clickedId = params.nodes[0];
            if (!clickedId.toString().startsWith('host_')) {
                openFlowModal(clickedId);
            }
        } else {
            clearFlowAnimation();
        }
    });

    document.getElementById('showHosts').addEventListener('change', function(e) {
        showHosts = e.target.checked;
        updateHostVisibility();
    });

    document.getElementById('showBandwidth').addEventListener('change', function(e) {
        showBandwidth = e.target.checked;
        updateLinkLabels();
    });

    document.getElementById('showDelay').addEventListener('change', function(e) {
        showDelay = e.target.checked;
        updateLinkLabels();
    });

    document.getElementById('showLoss').addEventListener('change', function(e) {
        showLoss = e.target.checked;
        updateLinkLabels();
    });

    document.getElementById('showLoad').addEventListener('change', function(e) {
        showLoad = e.target.checked;
        updateLinkLabels();
    });

    document.getElementById('fixTopology').addEventListener('change', function(e) {
        fixTopology = e.target.checked;
        if (fixTopology) {
            network.setOptions({ physics: { enabled: false } });
        } else {
            network.setOptions({ physics: { enabled: true } });
        }
    });
}

// ==================== Topology Refresh ====================
function hasTopologyChanged(topo) {
    const simpleData = {
        switches: topo.switches.map(s => s.dpid).sort(),
        links: topo.links.map(l => ({src: l.src.dpid, dst: l.dst.dpid})),
        hosts: topo.hosts.map(h => h.mac).sort()
    };
    const now = JSON.stringify(simpleData);
    if (now === lastTopology) return false;
    lastTopology = now;
    return true;
}

function updateLinkDataOnly(topo) {
    currentLinks = topo.links;
    updateLinkLabels();
    updateTime();
}

function updateTime() {
    const now = new Date();
    document.getElementById('lastUpdate').textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
}

function updateSidebar(topo) {
    document.getElementById('statSwitches').textContent = topo.switches.length;
    document.getElementById('statLinks').textContent = topo.links.length;
    document.getElementById('statHosts').textContent = topo.hosts.length;
    document.getElementById('switchCount').textContent = topo.switches.length;

    const list = document.getElementById('switchList');
    list.innerHTML = topo.switches.map(s => {
        const levelColor = levelColors[s.level] || levelColors[3];
        return `
        <div class="switch-item" onclick="openFlowModal(${s.dpid})" style="cursor: pointer;">
            <div class="switch-icon" style="background:${levelColor.bg}">⬡</div>
            <div class="switch-info">
                <div class="switch-name">交换机 ${s.name} (L${s.level})</div>
                <div class="switch-dpid">DPID: 0x${s.dpid.toString(16).padStart(16, '0')}</div>
            </div>
        </div>
    `}).join('');

    const hostList = document.getElementById('hostList');
    if (topo.hosts.length === 0) {
        hostList.innerHTML = `
            <div class="host-item">
                <div class="host-icon">▣</div>
                <div class="host-info">
                    <div class="host-name">未检测到主机</div>
                </div>
            </div>
        `;
    } else {
        hostList.innerHTML = topo.hosts.map(h => `
            <div class="host-item">
                <div class="host-icon">▣</div>
                <div class="host-info">
                    <div class="host-name">${h.ip || h.mac}</div>
                    <div class="host-ip">${h.mac}</div>
                </div>
            </div>
        `).join('');
    }
}

async function updateTopology() {
    try {
        let res = await fetch('/topology/data');
        let topo = await res.json();

        if (!hasTopologyChanged(topo)) {
            updateLinkDataOnly(topo);
            return;
        }

        renderTopology(topo);
        updateSidebar(topo);
        updateTime();

    } catch (e) {
        console.log('Fetch error:', e);
    }
}

// ==================== Topology Render ====================
function renderTopology(topo) {
    var savedPositions = {};
    if (fixTopology) {
        savedPositions = network.getPositions();
    }

    nodes.clear();
    edges.clear();

    topo.switches.forEach(s => {
        const colors = getSwitchColor(s.level);
        var nodeData = {
            id: s.dpid,
            label: s.name,
            color: colors,
            shadow: { enabled: true, color: colors.shadow || 'rgba(0,0,0,0.2)', size: 8, x: 0, y: 4 }
        };
        if (fixTopology && savedPositions[s.dpid]) {
            nodeData.x = savedPositions[s.dpid].x;
            nodeData.y = savedPositions[s.dpid].y;
        }
        nodes.add(nodeData);
    });

    currentLinks = topo.links;
    topo.links.forEach((link, idx) => {
        var edgeData = {
            id: 'link_' + idx,
            from: link.src.dpid,
            to: link.dst.dpid
        };
        var label = buildLinkLabel(link);
        if (label) {
            edgeData.label = label;
            edgeData.font = { face: 'JetBrains Mono', size: 10, color: '#8b949e', strokeWidth: 2, strokeColor: '#161b22' };
        }
        if (showLoad) {
            var loadColor = getLinkLoadColor(link);
            edgeData.color = loadColor;
            edgeData.highlight = loadColor;
        }
        edges.add(edgeData);
    });

    currentHosts = [];
    topo.hosts.forEach((h, idx) => {
        var hostId = 'host_' + h.mac.replace(/:/g, '');
        currentHosts.push({ id: hostId, mac: h.mac });
        var hostNodeData = {
            id: hostId,
            label: h.ip || h.mac,
            shape: 'ellipse',
            color: { background: '#2ea043', border: '#238636', highlight: { background: '#2ea043', border: '#3cbd54' } },
            font: { face: 'JetBrains Mono', size: 11, color: '#f0f6fc' },
            margin: 10,
            shadow: { enabled: true, color: 'rgba(46, 160, 67, 0.3)', size: 6, x: 0, y: 3 }
        };
        if (fixTopology && savedPositions[hostId]) {
            hostNodeData.x = savedPositions[hostId].x;
            hostNodeData.y = savedPositions[hostId].y;
        }
        nodes.add(hostNodeData);
        edges.add({ id: 'host_link_' + idx, from: hostId, to: h.dpid, color: { color: '#3fb950', highlight: '#56d364' }, arrows: { to: { enabled: false } } });
    });

    updateHostVisibility();
}

// ==================== Link Labels ====================
function updateHostVisibility() {
    currentHosts.forEach(host => {
        var node = nodes.get(host.id);
        if (node) {
            nodes.update({ id: host.id, hidden: !showHosts });
        }
    });
}

function buildLinkLabel(link) {
    let parts = [];
    if (showBandwidth && link.bw) parts.push(link.bw + ' Mbps');
    if (showDelay && link.delay) parts.push(link.delay + ' ms');
    if (showLoss && link.loss !== undefined) parts.push(link.loss + '%');
    if (showLoad && link.load) {
        const load = link.load;
        if (load.total_bps !== undefined) {
            parts.push('↑' + load.tx_bps + ' Mbps');
        }
    }
    return parts.length > 0 ? parts.join(' | ') : '';
}

function updateLinkLabels() {
    currentLinks.forEach((link, idx) => {
        var edgeId = 'link_' + idx;
        var label = buildLinkLabel(link);
        var edge = edges.get(edgeId);
        if (!edge) return;

        if (label) {
            var updateData = {
                id: edgeId,
                from: edge.from,
                to: edge.to,
                label: label,
                font: { face: 'JetBrains Mono', size: 10, color: '#8b949e', strokeWidth: 2, strokeColor: '#161b22' }
            };
            if (showLoad) {
                var loadColor = getLinkLoadColor(link);
                updateData.color = loadColor;
                updateData.highlight = loadColor;
            }
            edges.update(updateData);
        } else {
            edges.remove(edgeId);
            var newEdge = {
                id: edgeId,
                from: edge.from,
                to: edge.to
            };
            if (showLoad) {
                var loadColor = getLinkLoadColor(link);
                newEdge.color = loadColor;
                newEdge.highlight = loadColor;
            }
            edges.add(newEdge);
        }
    });
}

// ==================== Flow Path Animation ====================
function highlightFlowPath(pathDpids) {
    if (currentHighlightedPath &&
        currentHighlightedPath.length === pathDpids.length &&
        currentHighlightedPath.every((dpid, i) => dpid === pathDpids[i])) {
        clearFlowAnimation();
        return;
    }

    clearFlowAnimation();
    if (!pathDpids || pathDpids.length < 2) return;

    currentHighlightedPath = pathDpids;
    animationState = false;

    for (let i = 0; i < pathDpids.length - 1; i++) {
        const fromDpid = pathDpids[i];
        const toDpid = pathDpids[i + 1];

        currentLinks.forEach((link, idx) => {
            const edgeId = 'link_' + idx;
            if ((link.src.dpid === fromDpid && link.dst.dpid === toDpid) ||
                (link.src.dpid === toDpid && link.dst.dpid === fromDpid)) {
                originalEdgeStates[edgeId] = {
                    dashes: edges.get(edgeId).dashes || false
                };
            }
        });
    }

    flowAnimationInterval = setInterval(() => {
        animationState = !animationState;
        updateFlowPathAnimation(pathDpids, animationState);
    }, 400);

    updateFlowPathAnimation(pathDpids, animationState);
}

function updateFlowPathAnimation(pathDpids, state) {
    const dashPattern = state ? [12, 4, 2, 4] : [2, 4, 12, 4];

    for (let i = 0; i < pathDpids.length - 1; i++) {
        const fromDpid = pathDpids[i];
        const toDpid = pathDpids[i + 1];

        currentLinks.forEach((link, idx) => {
            const edgeId = 'link_' + idx;
            if ((link.src.dpid === fromDpid && link.dst.dpid === toDpid) ||
                (link.src.dpid === toDpid && link.dst.dpid === fromDpid)) {
                edges.update({
                    id: edgeId,
                    dashes: dashPattern
                });
            }
        });
    }
}

function clearFlowAnimation() {
    if (flowAnimationInterval) {
        clearInterval(flowAnimationInterval);
        flowAnimationInterval = null;
    }

    Object.keys(originalEdgeStates).forEach(edgeId => {
        const original = originalEdgeStates[edgeId];
        const edge = edges.get(edgeId);
        if (edge) {
            edges.update({
                id: edgeId,
                dashes: original.dashes
            });
        }
    });
    originalEdgeStates = {};
    currentHighlightedPath = null;
}

function highlightMulticastPath(pathEdges) {
    if (currentHighlightedPath &&
        currentHighlightedPath.length === pathEdges.length &&
        currentHighlightedPath.every((e, i) => e[0] === pathEdges[i][0] && e[1] === pathEdges[i][1])) {
        clearFlowAnimation();
        return;
    }

    clearFlowAnimation();
    if (!pathEdges || pathEdges.length === 0) return;

    const allEdges = pathEdges.map(e => [Number(e[0]), Number(e[1])]);

    allEdges.forEach(([fromDpid, toDpid]) => {
        currentLinks.forEach((link, idx) => {
            const edgeId = 'link_' + idx;
            const linkSrc = parseInt(link.src.dpid);
            const linkDst = parseInt(link.dst.dpid);
            if (linkSrc === fromDpid && linkDst === toDpid) {
                originalEdgeStates[edgeId] = {
                    dashes: edges.get(edgeId).dashes || false
                };
            }
        });
    });

    currentHighlightedPath = allEdges;
    animationState = false;

    flowAnimationInterval = setInterval(() => {
        animationState = !animationState;
        const dashPattern = animationState ? [12, 4, 2, 4] : [2, 4, 12, 4];

        allEdges.forEach(([fromDpid, toDpid]) => {
            currentLinks.forEach((link, idx) => {
                const edgeId = 'link_' + idx;
                const linkSrc = parseInt(link.src.dpid);
                const linkDst = parseInt(link.dst.dpid);
                if (linkSrc === fromDpid && linkDst === toDpid) {
                    edges.update({ id: edgeId, dashes: dashPattern });
                }
            });
        });
    }, 400);

    const dashPattern = animationState ? [12, 4, 2, 4] : [2, 4, 12, 4];
    allEdges.forEach(([fromDpid, toDpid]) => {
        currentLinks.forEach((link, idx) => {
            const edgeId = 'link_' + idx;
            const linkSrc = parseInt(link.src.dpid);
            const linkDst = parseInt(link.dst.dpid);
            if (linkSrc === fromDpid && linkDst === toDpid) {
                edges.update({ id: edgeId, dashes: dashPattern });
            }
        });
    });
}