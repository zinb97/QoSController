// ==================== Switch Flow Table Modal ====================
let currentSwitchDpid = null;

function openFlowModal(dpid) {
    currentSwitchDpid = dpid;
    document.getElementById('flowModal').classList.add('active');
    document.getElementById('modalSwitchDpid').textContent = '0x' + dpid.toString(16).padStart(16, '0');
    fetchFlowTable(dpid);
}

function closeFlowModal() {
    document.getElementById('flowModal').classList.remove('active');
    currentSwitchDpid = null;
}

function refreshFlowTable() {
    if (currentSwitchDpid) {
        fetchFlowTable(currentSwitchDpid);
    }
}

async function fetchFlowTable(dpid) {
    const modalBody = document.getElementById('modalBody');
    modalBody.innerHTML = '<div class="loading">加载中...</div>';

    try {
        const response = await fetch(`/stats/flow/${dpid}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();

        let flows = [];
        if (Array.isArray(data)) {
            flows = data;
        } else if (data && typeof data === 'object') {
            const key = Object.keys(data)[0];
            if (key && Array.isArray(data[key])) {
                flows = data[key];
            }
        }

        if (!flows || flows.length === 0) {
            modalBody.innerHTML = '<div class="no-flows">No flow entries found</div>';
            return;
        }

        let tableHtml = `
            <div style="margin-bottom: 12px; color: #79c0ff; font-size: 13px;">
                共 ${flows.length} 条流表规则
            </div>
            <table class="flow-table">
                <thead>
                    <tr>
                        <th>#</th>
                        <th>优先级</th>
                        <th>匹配项</th>
                        <th>动作</th>
                        <th>持续时间</th>
                        <th>超时</th>
                        <th>统计</th>
                    </tr>
                </thead>
                <tbody>
        `;

        flows.forEach((flow, index) => {
            const priority = flow.priority || 0;
            const durationSec = flow.duration_sec || 0;
            const idleTimeout = flow.idle_timeout || 0;
            const hardTimeout = flow.hard_timeout || 0;
            const packetCount = flow.packet_count || 0;
            const byteCount = flow.byte_count || 0;

            let matchStr = '';
            if (flow.match) {
                const matchParts = [];
                const match = flow.match;
                for (const [key, value] of Object.entries(match)) {
                    if (value !== undefined && value !== null && value !== 0) {
                        let displayValue = value;
                        if (typeof value === 'number' && key.includes('type')) {
                            displayValue = '0x' + value.toString(16);
                        }
                        matchParts.push(`${key}=${displayValue}`);
                    }
                }
                matchStr = matchParts.join('<br>');
            }

            let actionsStr = '';
            if (flow.actions) {
                const actionParts = [];
                const actions = Array.isArray(flow.actions) ? flow.actions : [flow.actions];
                actions.forEach(action => {
                    if (typeof action === 'object' && action !== null) {
                        if (action.port !== undefined) {
                            actionParts.push(`output:${action.port}`);
                        } else if (action.field) {
                            actionParts.push(`set_field:${JSON.stringify(action.field)}`);
                        } else if (action.type !== undefined) {
                            const typeNames = {
                                0: 'OUTPUT', 1: 'COPY_TTL_OUT', 2: 'COPY_TTL_IN',
                                3: 'SET_MPLS_TTL', 4: 'DEC_MPLS_TTL', 5: 'PUSH_VLAN',
                                6: 'POP_VLAN', 7: 'PUSH_MPLS', 8: 'POP_MPLS',
                                9: 'SET_QUEUE', 10: 'GROUP', 11: 'SET_FIELD',
                                12: 'PUSH_PBB', 13: 'SET_IPV4_SRC', 14: 'SET_IPV4_DST',
                                15: 'DEC_IPV4_TTL', 16: 'SET_IPV6_SRC', 17: 'SET_IPV6_DST',
                                18: 'DEC_IPV6_HOP', 22: 'SET_IPV6_FLABEL'
                            };
                            const typeName = typeNames[action.type] || `TYPE_${action.type}`;
                            if (action.value !== undefined) {
                                actionParts.push(`${typeName}:${action.value}`);
                            } else {
                                actionParts.push(typeName);
                            }
                        } else {
                            actionParts.push(JSON.stringify(action));
                        }
                    } else {
                        actionParts.push(String(action));
                    }
                });
                actionsStr = actionParts.join('<br>');
            } else if (flow.instructions) {
                const actionParts = [];
                const instructions = Array.isArray(flow.instructions) ? flow.instructions : [flow.instructions];
                instructions.forEach(inst => {
                    if (inst.actions) {
                        inst.actions.forEach(action => {
                            if (action.port !== undefined) {
                                actionParts.push(`output:${action.port}`);
                            } else {
                                actionParts.push(JSON.stringify(action));
                            }
                        });
                    }
                });
                actionsStr = actionParts.join('<br>');
            }

            tableHtml += `
                <tr>
                    <td class="index">${index + 1}</td>
                    <td class="priority">${priority}</td>
                    <td class="match">${matchStr || '-'}</td>
                    <td class="actions">${actionsStr || '-'}</td>
                    <td class="duration">${formatDuration(durationSec)}</td>
                    <td class="timeouts">空闲超时:${idleTimeout}<br>硬超时:${hardTimeout}</td>
                    <td class="stats">包数:${packetCount}<br>字节:${formatBytes(byteCount)}</td>
                </tr>
            `;
        });

        tableHtml += '</tbody></table>';
        modalBody.innerHTML = tableHtml;

    } catch (error) {
        modalBody.innerHTML = `<div class="error">加载流表失败：<br>${error.message}</div>`;
    }
}

function formatBytes(bytes) {
    if (bytes === 0) return '0';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatDuration(seconds) {
    if (seconds < 60) return seconds + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm ' + (seconds % 60) + 's';
    const hours = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return hours + 'h ' + mins + 'm' + secs + 's';
}

// ==================== Multicast Weight Modal ====================
let currentWeightGroup = null;

function isValidMulticastAddress(ip) {
    const parts = ip.split('.');
    if (parts.length !== 4) return false;
    for (const part of parts) {
        const num = parseInt(part, 10);
        if (isNaN(num) || num < 0 || num > 255 || part === '' || part !== String(num)) {
            return false;
        }
    }
    const first = parseInt(parts[0], 10);
    return first >= 224 && first <= 239;
}

async function updateMulticastWeights() {
    try {
        const response = await fetch('/multicast/weights');
        if (!response.ok) return;
        const weights = await response.json();

        const list = document.getElementById('multicastWeightList');
        const weightKeys = Object.keys(weights);

        let html = `
            <div class="switch-item" onclick="openWeightModal()" style="cursor: pointer; border-style: dashed; border-color: #9b59b6;">
                <div class="switch-icon" style="background:#9b59b6">+</div>
                <div class="switch-info">
                    <div class="switch-name">新增组播权重</div>
                    <div class="switch-dpid">点击新增组播组</div>
                </div>
            </div>
        `;

        if (weightKeys.length === 0) {
            list.innerHTML = html;
        } else {
            list.innerHTML = html + weightKeys.map(group => {
                const w = weights[group];
                return `
                    <div class="switch-item" onclick="openWeightModal('${group}')" style="cursor: pointer;">
                        <div class="switch-icon" style="background:#9b59b6">⚙</div>
                        <div class="switch-info">
                            <div class="switch-name">${group}</div>
                            <div class="switch-dpid">时延:${w.latency} 带宽:${w.bandwidth} 丢包:${w.loss}</div>
                        </div>
                    </div>
                `;
            }).join('');
        }
    } catch (e) {
        console.log('Failed to fetch multicast weights:', e);
    }
}

async function openWeightModal(group) {
    currentWeightGroup = group || '';
    document.getElementById('modalWeightGroup').textContent = group ? ' - ' + group : '';
    document.getElementById('weightSuccess').style.display = 'none';
    document.getElementById('weightError').style.display = 'none';

    const groupInput = document.getElementById('weightGroupInput');
    const groupField = document.getElementById('weightGroup');
    if (group) {
        groupInput.style.display = 'none';
        groupField.value = group;
    } else {
        groupInput.style.display = 'block';
        groupField.value = '';
    }

    if (group) {
        try {
            const response = await fetch('/multicast/weights/' + encodeURIComponent(group));
            if (response.ok) {
                const w = await response.json();
                document.getElementById('weightLatency').value = w.latency;
                document.getElementById('weightBandwidth').value = w.bandwidth;
                document.getElementById('weightLoss').value = w.loss;
            }
        } catch (e) {
            console.log('Failed to load weight:', e);
        }
    } else {
        document.getElementById('weightLatency').value = 1.0;
        document.getElementById('weightBandwidth').value = 1.0;
        document.getElementById('weightLoss').value = 1.0;
    }

    document.getElementById('weightModal').classList.add('active');
}

function closeWeightModal() {
    document.getElementById('weightModal').classList.remove('active');
    currentWeightGroup = null;
}

async function saveWeight() {
    const group = document.getElementById('weightGroup').value.trim();
    if (!group) {
        document.getElementById('weightError').textContent = '请输入组播组地址';
        document.getElementById('weightError').style.display = 'block';
        return;
    }

    if (!isValidMulticastAddress(group)) {
        document.getElementById('weightError').textContent = '组播地址无效。范围必须在 224.0.0.0 - 239.255.255.255 之间';
        document.getElementById('weightError').style.display = 'block';
        return;
    }

    let latency = parseFloat(document.getElementById('weightLatency').value);
    let bandwidth = parseFloat(document.getElementById('weightBandwidth').value);
    let loss = parseFloat(document.getElementById('weightLoss').value);
    if (isNaN(latency)) latency = 1.0;
    if (isNaN(bandwidth)) bandwidth = 1.0;
    if (isNaN(loss)) loss = 1.0;

    if (latency === 1.0 && bandwidth === 1.0 && loss === 1.0) {
        document.getElementById('weightError').textContent = '自定义权重必须与默认值 (1.0, 1.0, 1.0) 不同';
        document.getElementById('weightError').style.display = 'block';
        return;
    }

    try {
        showLoading('正在保存组播权重...');
        const response = await fetch('/multicast/weights/' + encodeURIComponent(group), {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                latency_weight: latency,
                bandwidth_weight: bandwidth,
                loss_weight: loss
            })
        });
        hideLoading();

        if (response.ok) {
            document.getElementById('weightSuccess').style.display = 'block';
            setTimeout(() => {
                closeWeightModal();
                updateMulticastWeights();
            }, 1000);
        } else {
            const data = await response.json();
            document.getElementById('weightError').textContent = data.error || '权重保存失败';
            document.getElementById('weightError').style.display = 'block';
        }
    } catch (e) {
        hideLoading();
        document.getElementById('weightError').textContent = '权重保存失败';
        document.getElementById('weightError').style.display = 'block';
    }
}

async function deleteWeight() {
    if (!currentWeightGroup) {
        closeWeightModal();
        return;
    }

    try {
        showLoading('正在删除组播权重...');
        const response = await fetch('/multicast/weights/' + encodeURIComponent(currentWeightGroup), {
            method: 'DELETE'
        });
        hideLoading();

        if (response.ok) {
            closeWeightModal();
            updateMulticastWeights();
        }
    } catch (e) {
        hideLoading();
        console.log('Failed to delete weight:', e);
    }
}

// ==================== QoS Rules Modal ====================
let currentQosRuleId = null;

async function updateQosRules() {
    try {
        const response = await fetch('/qos/rules');
        if (!response.ok) return;
        const rules = await response.json();

        const list = document.getElementById('qosRuleList');

        let html = `
            <div class="switch-item" onclick="openQosModal()" style="cursor: pointer; border-style: dashed; border-color: #f39c12;">
                <div class="switch-icon" style="background:#f39c12">+</div>
                <div class="switch-info">
                    <div class="switch-name">新增 QoS 规则</div>
                    <div class="switch-dpid">点击创建新规则</div>
                </div>
            </div>
        `;

        if (!rules || rules.length === 0) {
            list.innerHTML = html;
        } else {
            list.innerHTML = html + rules.map(rule => {
                const guaranteeIcon = {
                    'default': '→',
                    'bandwidth': '↑',
                    'latency': '⏱',
                    'loss_rate': '↓',
                    'load_balance': '↔',
                    'smart_algorithm': '✦'
                }[rule.guarantee] || '→';
                return `
                    <div class="switch-item" onclick="openQosModal(${rule.id})" style="cursor: pointer;">
                        <div class="switch-icon" style="background:#f39c12">${guaranteeIcon}</div>
                        <div class="switch-info">
                            <div class="switch-name">${rule.name}</div>
                            <div class="switch-dpid">${rule.src_ip} → ${rule.dst_ip} | ${rule.protocol} | DSCP:${rule.dscp} | ${({default:'默认', bandwidth:'带宽优先', latency:'时延优先', loss_rate:'丢包率优先', load_balance:'负载均衡', smart_algorithm:'智能算法'})[rule.guarantee] || rule.guarantee}</div>
                        </div>
                    </div>
                `;
            }).join('');
        }
    } catch (e) {
        console.log('Failed to fetch QoS rules:', e);
    }
}

function openQosModal(ruleId) {
    currentQosRuleId = ruleId || null;
    document.getElementById('modalQosTitle').textContent = ruleId ? ' - 编辑规则' : ' - 新建规则';
    document.getElementById('qosSuccess').style.display = 'none';
    document.getElementById('qosError').style.display = 'none';
    document.getElementById('qosDeleteBtn').style.display = ruleId ? 'inline-block' : 'none';

    if (ruleId) {
        fetch('/qos/rules/' + ruleId)
            .then(r => r.json())
            .then(rule => {
                document.getElementById('qosName').value = rule.name || '';
                document.getElementById('qosSrcIp').value = rule.src_ip || '';
                document.getElementById('qosDstIp').value = rule.dst_ip || '';
                document.getElementById('qosProtocol').value = rule.protocol || 'any';
                document.getElementById('qosSrcPort').value = rule.src_port || '';
                document.getElementById('qosDstPort').value = rule.dst_port || '';
                document.getElementById('qosDscp').value = rule.dscp || 0;
                document.getElementById('qosPriority').value = rule.priority || 100;
                document.getElementById('qosGuarantee').value = rule.guarantee || 'default';
            })
            .catch(e => console.log('Failed to load QoS rule:', e));
    } else {
        document.getElementById('qosName').value = '';
        document.getElementById('qosSrcIp').value = '';
        document.getElementById('qosDstIp').value = '';
        document.getElementById('qosProtocol').value = 'any';
        document.getElementById('qosSrcPort').value = '';
        document.getElementById('qosDstPort').value = '';
        document.getElementById('qosDscp').value = 0;
        document.getElementById('qosPriority').value = 100;
        document.getElementById('qosGuarantee').value = 'default';
    }

    document.getElementById('qosModal').classList.add('active');
}

function closeQosModal() {
    document.getElementById('qosModal').classList.remove('active');
    currentQosRuleId = null;
}

async function saveQosRule() {
    const name = document.getElementById('qosName').value.trim();
    const srcIp = document.getElementById('qosSrcIp').value.trim();
    const dstIp = document.getElementById('qosDstIp').value.trim();
    const protocol = document.getElementById('qosProtocol').value;
    const srcPort = document.getElementById('qosSrcPort').value;
    const dstPort = document.getElementById('qosDstPort').value;
    const dscp = parseInt(document.getElementById('qosDscp').value) || 0;
    const priority = parseInt(document.getElementById('qosPriority').value) || 100;
    const guarantee = document.getElementById('qosGuarantee').value;

    if (!srcIp || !dstIp) {
        document.getElementById('qosError').textContent = '请填写名称、源 IP 和目的 IP';
        document.getElementById('qosError').style.display = 'block';
        return;
    }

    const ruleData = {
        name: name,
        src_ip: srcIp,
        dst_ip: dstIp,
        protocol: protocol,
        src_port: srcPort ? parseInt(srcPort) : null,
        dst_port: dstPort ? parseInt(dstPort) : null,
        dscp: dscp,
        priority: priority,
        guarantee: guarantee
    };

    try {
        showLoading('正在保存 QoS 规则...');
        const url = currentQosRuleId ? `/qos/rules/${currentQosRuleId}` : '/qos/rules';
        const method = currentQosRuleId ? 'PUT' : 'POST';

        const response = await fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(ruleData)
        });

        hideLoading();

        if (response.ok) {
            document.getElementById('qosSuccess').style.display = 'block';
            setTimeout(() => {
                closeQosModal();
                updateQosRules();
            }, 1000);
        } else {
            const data = await response.json();
            document.getElementById('qosError').textContent = data.error || 'QoS 规则保存失败';
            document.getElementById('qosError').style.display = 'block';
        }
    } catch (e) {
        hideLoading();
        document.getElementById('qosError').textContent = 'QoS 规则保存失败';
        document.getElementById('qosError').style.display = 'block';
    }
}

async function deleteQosRule() {
    if (!currentQosRuleId) {
        closeQosModal();
        return;
    }

    try {
        showLoading('正在删除 QoS 规则...');
        const response = await fetch(`/qos/rules/${currentQosRuleId}`, {
            method: 'DELETE'
        });
        hideLoading();

        if (response.ok) {
            closeQosModal();
            updateQosRules();
        }
    } catch (e) {
        hideLoading();
        console.log('Failed to delete QoS rule:', e);
    }
}

// ==================== Modal Event Bindings ====================
document.getElementById('flowModal').addEventListener('click', function(e) {
    if (e.target === this) {
        closeFlowModal();
    }
});

const weightModal = document.getElementById('weightModal');
if (weightModal) {
    weightModal.addEventListener('click', function(e) {
        if (e.target === this) {
            closeWeightModal();
        }
    });
}

const qosModal = document.getElementById('qosModal');
if (qosModal) {
    qosModal.addEventListener('click', function(e) {
        if (e.target === this) {
            closeQosModal();
        }
    });
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        if (document.getElementById('flowModal').classList.contains('active')) {
            closeFlowModal();
        }
        const wm = document.getElementById('weightModal');
        if (wm && wm.classList.contains('active')) {
            closeWeightModal();
        }
        const qm = document.getElementById('qosModal');
        if (qm && qm.classList.contains('active')) {
            closeQosModal();
        }
        const scm = document.getElementById('sliceConfigModal');
        if (scm && scm.classList.contains('active')) {
            closeSliceConfigModal();
        }
    }
});

const sliceConfigModal = document.getElementById('sliceConfigModal');
if (sliceConfigModal) {
    sliceConfigModal.addEventListener('click', function(e) {
        if (e.target === this) {
            closeSliceConfigModal();
        }
    });
}