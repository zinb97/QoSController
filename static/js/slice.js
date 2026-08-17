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