// ==================== Network Slice Selector ====================
let currentSlice = 'all';

function showSliceError(message) {
    const overlay = document.getElementById('sliceErrorOverlay');
    if (overlay) {
        overlay.querySelector('.slice-error-message').textContent = message || '切片获取失败！';
        overlay.classList.add('active');
    }
    if (typeof nodes !== 'undefined' && nodes) {
        nodes.clear();
    }
    if (typeof edges !== 'undefined' && edges) {
        edges.clear();
    }
    if (topologyIntervalId !== null) {
        clearInterval(topologyIntervalId);
        topologyIntervalId = null;
    }
}

function hideSliceError() {
    const overlay = document.getElementById('sliceErrorOverlay');
    if (overlay) {
        overlay.classList.remove('active');
    }
    lastTopology = '';
    if (topologyIntervalId === null) {
        topologyIntervalId = setInterval(updateTopology, 2000);
    }
}

async function selectSlice(sliceType, checkbox) {
    const sliceIds = {
        'all': 'sliceAll',
        'basic': 'sliceBasic',
        'low-latency': 'sliceLowLatency',
        'low-loss': 'sliceLowLoss',
        'low-latency-loss': 'sliceLowLatencyLoss'
    };
    const allCb = document.getElementById(sliceIds['all']);

    // 取消勾选"全部" → 强制保持"全部"
    if (sliceType === 'all' && !checkbox.checked) {
        checkbox.checked = true;
        return;
    }

    // 取消勾选某个非"全部"切片 → 回退到"全部"
    if (sliceType !== 'all' && !checkbox.checked) {
        checkbox.closest('.slice-option').classList.remove('active');
        if (allCb) {
            allCb.checked = true;
            allCb.closest('.slice-option').classList.add('active');
            currentSlice = 'all';
        }
        hideSliceError();
        updateTopology();
        console.log('Selected slice:', currentSlice);
        return;
    }

    // 勾选"全部" → 纯本地操作
    if (sliceType === 'all') {
        document.querySelectorAll('.slice-option').forEach(opt => {
            const cb = opt.querySelector('input');
            if (cb !== checkbox) {
                cb.checked = false;
                opt.classList.remove('active');
            }
        });
        checkbox.closest('.slice-option').classList.add('active');
        currentSlice = 'all';
        hideSliceError();
        updateTopology();
        console.log('Selected slice:', currentSlice);
        return;
    }

    // 勾选基础/低时延/低丢包/低时延-丢包切片 → 先乐观更新 UI，再请求后端
    const endpointMap = {
        'basic': '/topology/slice/basic',
        'low-latency': '/topology/slice/low-latency',
        'low-loss': '/topology/slice/low-loss',
        'low-latency-loss': '/topology/slice/low-latency-loss'
    };
    const endpoint = endpointMap[sliceType];

    if (allCb && allCb.checked) {
        allCb.checked = false;
        allCb.closest('.slice-option').classList.remove('active');
    }
    document.querySelectorAll('.slice-option').forEach(opt => {
        const cb = opt.querySelector('input');
        if (cb !== checkbox && cb !== allCb) {
            cb.checked = false;
            opt.classList.remove('active');
        }
    });

    try {
        const response = await fetch(endpoint, {method: 'POST'});
        const data = await response.json();
        if (data.success) {
            checkbox.closest('.slice-option').classList.add('active');
            currentSlice = sliceType;
            sliceTopology = data.topology;  // 保存切片拓扑数据
            renderTopology(sliceTopology);
            updateSidebar(sliceTopology);
            updateTime();
            hideSliceError();
            console.log('Selected slice:', currentSlice);
        } else {
            showSliceError('切片获取失败！');
            console.warn(`Slice switch rejected: ${data.error || '后端拒绝切片切换'}`);
            console.log('Selected slice:', currentSlice);
        }
    } catch (e) {
        console.error('Slice switch request failed:', e);
        showSliceError('切片获取失败！');
        console.log('Selected slice:', currentSlice);
    }
}

// ==================== Slice Config Modal ====================
const sliceTypeLabels = {
    'all': '网络总拓扑',
    'basic': '基础切片',
    'low-latency': '低时延切片',
    'low-loss': '低丢包切片',
    'low-latency-loss': '低时延-丢包切片'
};

function parseSwitchList(text, switches) {
    const entries = text.split(/[,;]/).map(s => s.trim()).filter(s => s.length > 0);
    const validDpids = [];
    const invalid = [];
    const seen = new Set();

    for (const entry of entries) {
        let match = null;
        if (entry.toLowerCase().startsWith('0x')) {
            const hexStr = entry.slice(2).toLowerCase();
            match = switches.find(s => s.dpid.toString(16).padStart(16, '0') === hexStr.padStart(16, '0'));
        } else if (/^\d+$/.test(entry)) {
            match = switches.find(s => s.dpid.toString() === entry);
        } else {
            match = switches.find(s => s.name === entry);
        }

        if (match && !seen.has(match.dpid)) {
            validDpids.push(match.dpid);
            seen.add(match.dpid);
        } else if (!match) {
            invalid.push(entry);
        }
    }

    return { validDpids, invalid };
}

async function openSliceConfigModal() {
    // 切片配置必须在网络总拓扑视图下进行
    if (currentSlice !== 'all') {
        alert('切片配置必须在总拓扑视图下进行');
        return;
    }

    document.getElementById('sliceConfigSuccess').style.display = 'none';
    document.getElementById('sliceConfigError').style.display = 'none';

    document.getElementById('sliceLowLatencyNodes').value = '';
    document.getElementById('sliceLowLossNodes').value = '';
    document.getElementById('sliceLowLatencyLossNodes').value = '';
    document.getElementById('sliceLowLatencyThreshold').value = '1.0';
    document.getElementById('sliceLowLossThreshold').value = '1.0';
    document.getElementById('sliceLowLatencyLossLatencyThreshold').value = '1.0';
    document.getElementById('sliceLowLatencyLossLossThreshold').value = '1.0';

    // 尝试获取已有配置
    try {
        const response = await fetch('/topology/slice/config');
        const data = await response.json();
        if (data.success && data.config) {
            const config = data.config;
            // DPID 转交换机名称
            const dpidToName = (dpids) => {
                return dpids.map(dpid => {
                    const sw = cachedSwitches.find(s => s.dpid === dpid);
                    return sw ? sw.name : dpid.toString();
                }).join(', ');
            };
            if (config.low_latency) {
                document.getElementById('sliceLowLatencyNodes').value = dpidToName(config.low_latency.nodes);
                document.getElementById('sliceLowLatencyThreshold').value = config.low_latency.threshold;
            }
            if (config.low_loss) {
                document.getElementById('sliceLowLossNodes').value = dpidToName(config.low_loss.nodes);
                document.getElementById('sliceLowLossThreshold').value = config.low_loss.threshold;
            }
            if (config.low_latency_loss) {
                document.getElementById('sliceLowLatencyLossNodes').value = dpidToName(config.low_latency_loss.nodes);
                document.getElementById('sliceLowLatencyLossLatencyThreshold').value = config.low_latency_loss.latency_threshold;
                document.getElementById('sliceLowLatencyLossLossThreshold').value = config.low_latency_loss.loss_threshold;
            }
        }
    } catch (e) {
        console.error('Failed to load slice config:', e);
    }

    document.getElementById('modalSliceType').textContent = ' - ' + (sliceTypeLabels[currentSlice] || currentSlice);
    document.getElementById('sliceConfigModal').classList.add('active');
}

function closeSliceConfigModal() {
    document.getElementById('sliceConfigModal').classList.remove('active');
}

function showSliceConfigError(msg) {
    document.getElementById('sliceConfigError').textContent = msg;
    document.getElementById('sliceConfigError').style.display = 'block';
}

async function saveSliceConfig() {
    const lowLatencyText = document.getElementById('sliceLowLatencyNodes').value.trim();
    const lowLossText = document.getElementById('sliceLowLossNodes').value.trim();
    const lowLatencyLossText = document.getElementById('sliceLowLatencyLossNodes').value.trim();
    const lowLatencyThresholdRaw = document.getElementById('sliceLowLatencyThreshold').value.trim();
    const lowLossThresholdRaw = document.getElementById('sliceLowLossThreshold').value.trim();
    const lowLatencyLossLatencyRaw = document.getElementById('sliceLowLatencyLossLatencyThreshold').value.trim();
    const lowLatencyLossLossRaw = document.getElementById('sliceLowLatencyLossLossThreshold').value.trim();

    // 1. 所有字段必填
    if (lowLatencyText === '') {
        showSliceConfigError('低时延切片节点不能为空');
        return;
    }
    if (lowLossText === '') {
        showSliceConfigError('低丢包切片节点不能为空');
        return;
    }
    if (lowLatencyLossText === '') {
        showSliceConfigError('低时延-丢包切片节点不能为空');
        return;
    }
    if (lowLatencyThresholdRaw === '') {
        showSliceConfigError('低时延切片时延权重阈值不能为空');
        return;
    }
    if (lowLossThresholdRaw === '') {
        showSliceConfigError('低丢包切片丢包权重阈值不能为空');
        return;
    }
    if (lowLatencyLossLatencyRaw === '') {
        showSliceConfigError('低时延-丢包切片时延权重阈值不能为空');
        return;
    }
    if (lowLatencyLossLossRaw === '') {
        showSliceConfigError('低时延-丢包切片丢包权重阈值不能为空');
        return;
    }

    // 2. 阈值范围校验
    const lowLatencyThreshold = parseFloat(lowLatencyThresholdRaw);
    const lowLossThreshold = parseFloat(lowLossThresholdRaw);
    const lowLatencyLossLatency = parseFloat(lowLatencyLossLatencyRaw);
    const lowLatencyLossLoss = parseFloat(lowLatencyLossLossRaw);
    if (isNaN(lowLatencyThreshold) || lowLatencyThreshold < 0 || lowLatencyThreshold > 1) {
        showSliceConfigError('低时延切片时延阈值必须在 [0, 1] 之间');
        return;
    }
    if (isNaN(lowLossThreshold) || lowLossThreshold < 0 || lowLossThreshold > 1) {
        showSliceConfigError('低丢包切片丢包阈值必须在 [0, 1] 之间');
        return;
    }
    if (isNaN(lowLatencyLossLatency) || lowLatencyLossLatency < 0 || lowLatencyLossLatency > 1) {
        showSliceConfigError('低时延-丢包切片时延阈值必须在 [0, 1] 之间');
        return;
    }
    if (isNaN(lowLatencyLossLoss) || lowLatencyLossLoss < 0 || lowLatencyLossLoss > 1) {
        showSliceConfigError('低时延-丢包切片丢包阈值必须在 [0, 1] 之间');
        return;
    }

    // 3. 解析与存在性校验
    const lowLatencyParsed = parseSwitchList(lowLatencyText, cachedSwitches);
    const lowLossParsed = parseSwitchList(lowLossText, cachedSwitches);
    const lowLatencyLossParsed = parseSwitchList(lowLatencyLossText, cachedSwitches);
    if (lowLatencyParsed.invalid.length > 0) {
        showSliceConfigError(`低时延切片节点不存在：${lowLatencyParsed.invalid.join(', ')}`);
        return;
    }
    if (lowLossParsed.invalid.length > 0) {
        showSliceConfigError(`低丢包切片节点不存在：${lowLossParsed.invalid.join(', ')}`);
        return;
    }
    if (lowLatencyLossParsed.invalid.length > 0) {
        showSliceConfigError(`低时延-丢包切片节点不存在：${lowLatencyLossParsed.invalid.join(', ')}`);
        return;
    }

    // 4. 每个切片节点数 ≥ 2
    if (lowLatencyParsed.validDpids.length < 2) {
        showSliceConfigError('低时延切片节点至少需要 2 个');
        return;
    }
    if (lowLossParsed.validDpids.length < 2) {
        showSliceConfigError('低丢包切片节点至少需要 2 个');
        return;
    }
    if (lowLatencyLossParsed.validDpids.length < 2) {
        showSliceConfigError('低时延-丢包切片节点至少需要 2 个');
        return;
    }

    const params = {
        low_latency: {
            nodes: lowLatencyParsed.validDpids,
            threshold: lowLatencyThreshold
        },
        low_loss: {
            nodes: lowLossParsed.validDpids,
            threshold: lowLossThreshold
        },
        low_latency_loss: {
            nodes: lowLatencyLossParsed.validDpids,
            latency_threshold: lowLatencyLossLatency,
            loss_threshold: lowLatencyLossLoss
        }
    };

    try {
        showLoading('正在保存切片配置...');
        const response = await fetch('/topology/slice/config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(params)
        });
        hideLoading();

        if (response.ok) {
            document.getElementById('sliceConfigSuccess').style.display = 'block';
            setTimeout(() => {
                closeSliceConfigModal();
            }, 1000);
        } else {
            const data = await response.json();
            showSliceConfigError(data.error || '切片配置保存失败');
        }
    } catch (e) {
        hideLoading();
        showSliceConfigError('切片配置保存失败');
    }
}