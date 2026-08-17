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
        'low-loss': 'sliceLowLoss'
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

    // 勾选基础/低时延/低丢包切片 → 先乐观更新 UI，再请求后端
    const endpointMap = {
        'basic': '/topology/slice/basic',
        'low-latency': '/topology/slice/low-latency',
        'low-loss': '/topology/slice/low-loss'
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
    'low-loss': '低丢包切片'
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

function openSliceConfigModal() {
    document.getElementById('sliceConfigSuccess').style.display = 'none';
    document.getElementById('sliceConfigError').style.display = 'none';

    document.getElementById('sliceLowLatencyNodes').value = '';
    document.getElementById('sliceLowLossNodes').value = '';
    document.getElementById('sliceLowLatencyThreshold').value = '1.0';
    document.getElementById('sliceLowLossThreshold').value = '1.0';

    document.getElementById('modalSliceType').textContent = ' - ' + (sliceTypeLabels[currentSlice] || currentSlice);
    document.getElementById('sliceConfigModal').classList.add('active');
}

function closeSliceConfigModal() {
    document.getElementById('sliceConfigModal').classList.remove('active');
}

async function saveSliceConfig() {
    const lowLatencyText = document.getElementById('sliceLowLatencyNodes').value;
    const lowLossText = document.getElementById('sliceLowLossNodes').value;
    const lowLatencyThreshold = parseFloat(document.getElementById('sliceLowLatencyThreshold').value);
    const lowLossThreshold = parseFloat(document.getElementById('sliceLowLossThreshold').value);

    if (isNaN(lowLatencyThreshold) || lowLatencyThreshold < 0 || lowLatencyThreshold > 1) {
        document.getElementById('sliceConfigError').textContent = '时延阈值必须在 [0, 1] 之间';
        document.getElementById('sliceConfigError').style.display = 'block';
        return;
    }
    if (isNaN(lowLossThreshold) || lowLossThreshold < 0 || lowLossThreshold > 1) {
        document.getElementById('sliceConfigError').textContent = '丢包阈值必须在 [0, 1] 之间';
        document.getElementById('sliceConfigError').style.display = 'block';
        return;
    }

    const lowLatencyParsed = parseSwitchList(lowLatencyText, cachedSwitches);
    const lowLossParsed = parseSwitchList(lowLossText, cachedSwitches);

    if (lowLatencyParsed.invalid.length > 0) {
        document.getElementById('sliceConfigError').textContent =
            `低时延切片节点不存在：${lowLatencyParsed.invalid.join(', ')}`;
        document.getElementById('sliceConfigError').style.display = 'block';
        return;
    }
    if (lowLossParsed.invalid.length > 0) {
        document.getElementById('sliceConfigError').textContent =
            `低丢包切片节点不存在：${lowLossParsed.invalid.join(', ')}`;
        document.getElementById('sliceConfigError').style.display = 'block';
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
            document.getElementById('sliceConfigError').textContent = data.error || '切片配置保存失败';
            document.getElementById('sliceConfigError').style.display = 'block';
        }
    } catch (e) {
        hideLoading();
        document.getElementById('sliceConfigError').textContent = '切片配置保存失败';
        document.getElementById('sliceConfigError').style.display = 'block';
    }
}