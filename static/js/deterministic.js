/**
 * 确定性时延分析功能
 * 对应研究内容 2.4.1
 */

// 存储交换机数据
let deterministicSwitches = [];

// 更新交换机下拉列表
function updateDeterministicSwitches(switches) {
    // 如果传入参数则更新缓存
    if (switches && Array.isArray(switches)) {
        deterministicSwitches = switches;
    }

    const srcSelect = document.getElementById('detSrcSwitch');
    const dstSelect = document.getElementById('detDstSwitch');

    if (!srcSelect || !dstSelect) return;

    // 保存当前选择
    const currentSrc = srcSelect.value;
    const currentDst = dstSelect.value;

    // 清空选项
    srcSelect.innerHTML = '<option value="">请选择...</option>';
    dstSelect.innerHTML = '<option value="">请选择...</option>';

    // 填充交换机选项
    deterministicSwitches.forEach(sw => {
        const dpid = sw.dpid;
        const name = sw.name || `Switch ${dpid}`;
        const dpidHex = '0x' + dpid.toString(16).padStart(16, '0');

        // 添加到源交换机列表
        const srcOption = document.createElement('option');
        srcOption.value = dpid;
        srcOption.textContent = `${name} (${dpidHex})`;
        srcSelect.appendChild(srcOption);

        // 添加到目标交换机列表
        const dstOption = document.createElement('option');
        dstOption.value = dpid;
        dstOption.textContent = `${name} (${dpidHex})`;
        dstSelect.appendChild(dstOption);
    });

    // 恢复之前的选择
    if (currentSrc) srcSelect.value = currentSrc;
    if (currentDst) dstSelect.value = currentDst;
}

// 从服务器加载交换机列表
async function loadDeterministicSwitches() {
    try {
        const response = await fetch('/topology/data');
        const topo = await response.json();

        if (topo && topo.switches && Array.isArray(topo.switches)) {
            updateDeterministicSwitches(topo.switches);
        }
    } catch (error) {
        console.error('加载交换机列表失败:', error);
    }
}

// 验证确定性时延
async function verifyDeterministic() {
    const srcDpid = document.getElementById('detSrcSwitch').value;
    const dstDpid = document.getElementById('detDstSwitch').value;
    const slaMs = parseFloat(document.getElementById('detSla').value);
    const rateMbps = parseFloat(document.getElementById('detRate').value);
    const burstKb = parseFloat(document.getElementById('detBurst').value);

    const resultDiv = document.getElementById('detResult');
    const resultContent = document.getElementById('detResultContent');

    // 验证输入
    if (!srcDpid || !dstDpid) {
        showDeterministicError('请选择源和目标交换机');
        return;
    }

    if (srcDpid === dstDpid) {
        showDeterministicError('源和目标交换机不能相同');
        return;
    }

    if (!slaMs || slaMs <= 0) {
        showDeterministicError('请输入有效的 SLA 要求');
        return;
    }

    if (!rateMbps || rateMbps <= 0 || !burstKb || burstKb <= 0) {
        showDeterministicError('请输入有效的流量参数');
        return;
    }

    // 显示加载状态
    resultDiv.style.display = 'block';
    resultContent.innerHTML = '<div style="text-align: center; padding: 12px;"><span style="color: #667eea;">⏳ 计算中...</span></div>';

    try {
        const response = await fetch('/routing/deterministic/verify', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                src_dpid: srcDpid,
                dst_dpid: dstDpid,
                sla_ms: slaMs,
                traffic: {
                    rate_mbps: rateMbps,
                    burst_kb: burstKb
                }
            })
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            showDeterministicError(data.error || '验证失败');
            return;
        }

        // 显示结果
        displayDeterministicResult(data);

    } catch (error) {
        console.error('确定性验证错误:', error);
        showDeterministicError('网络请求失败: ' + error.message);
    }
}

// 显示验证结果
function displayDeterministicResult(data) {
    const resultContent = document.getElementById('detResultContent');

    const satisfied = data.satisfied;
    const delayBound = data.delay_bound_ms;
    const sla = data.sla_ms;
    const margin = data.margin_ms;
    const hops = data.hops;
    const path = data.path || [];

    // 将 DPID 转换为交换机名称
    let pathNames = [];
    if (deterministicSwitches && deterministicSwitches.length > 0) {
        pathNames = path.map(dpidHex => {
            const dpid = parseInt(dpidHex.replace('0x', ''), 16);
            const sw = deterministicSwitches.find(s => s.dpid === dpid);
            return sw ? (sw.name || `Switch ${dpid}`) : dpidHex.substring(0, 10) + '...';
        });
    } else {
        pathNames = path.map(dpidHex => dpidHex.substring(0, 10) + '...');
    }

    // 构建结果 HTML
    let html = '';

    // 结果状态
    if (satisfied) {
        html += `
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; padding: 8px; background: #d4edda; border-radius: 6px;">
                <span style="font-size: 20px;">✅</span>
                <div style="flex: 1;">
                    <div style="font-weight: 600; color: #155724; font-size: 13px;">满足 SLA 要求</div>
                    <div style="font-size: 11px; color: #155724; margin-top: 2px;">时延上界小于 SLA</div>
                </div>
            </div>
        `;
    } else {
        html += `
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 12px; padding: 8px; background: #f8d7da; border-radius: 6px;">
                <span style="font-size: 20px;">⚠️</span>
                <div style="flex: 1;">
                    <div style="font-weight: 600; color: #721c24; font-size: 13px;">不满足 SLA 要求</div>
                    <div style="font-size: 11px; color: #721c24; margin-top: 2px;">时延上界超出 SLA</div>
                </div>
            </div>
        `;
    }

    // 关键指标
    html += `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px;">
            <div style="padding: 8px; background: white; border: 1px solid #e0e4e8; border-radius: 6px;">
                <div style="font-size: 11px; color: #666;">时延上界</div>
                <div style="font-size: 16px; font-weight: 600; color: ${satisfied ? '#28a745' : '#dc3545'}; margin-top: 2px;">${delayBound.toFixed(2)} ms</div>
            </div>
            <div style="padding: 8px; background: white; border: 1px solid #e0e4e8; border-radius: 6px;">
                <div style="font-size: 11px; color: #666;">SLA 要求</div>
                <div style="font-size: 16px; font-weight: 600; color: #667eea; margin-top: 2px;">${sla.toFixed(2)} ms</div>
            </div>
        </div>
    `;

    // 余量和跳数
    html += `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px;">
            <div style="padding: 8px; background: white; border: 1px solid #e0e4e8; border-radius: 6px;">
                <div style="font-size: 11px; color: #666;">${satisfied ? '余量' : '超出'}</div>
                <div style="font-size: 16px; font-weight: 600; color: ${satisfied ? '#28a745' : '#dc3545'}; margin-top: 2px;">${Math.abs(margin).toFixed(2)} ms</div>
            </div>
            <div style="padding: 8px; background: white; border: 1px solid #e0e4e8; border-radius: 6px;">
                <div style="font-size: 11px; color: #666;">路径跳数</div>
                <div style="font-size: 16px; font-weight: 600; color: #667eea; margin-top: 2px;">${hops}</div>
            </div>
        </div>
    `;

    // 路径信息（可点击显示）
    if (path.length > 0) {
        const pathStr = pathNames.join(' → ');
        const pathDpidsJson = JSON.stringify(path.map(dpidHex => parseInt(dpidHex.replace('0x', ''), 16))).replace(/"/g, '&quot;');

        html += `
            <div style="padding: 8px; background: white; border: 1px solid #e0e4e8; border-radius: 6px; cursor: pointer; transition: all 0.2s;"
                 onclick="highlightFlowPath(${pathDpidsJson})"
                 onmouseover="this.style.background='#f0f7ff'; this.style.borderColor='#667eea'"
                 onmouseout="this.style.background='white'; this.style.borderColor='#e0e4e8'">
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <div style="flex: 1;">
                        <div style="font-size: 11px; color: #666; margin-bottom: 4px;">路径 (点击在拓扑中显示)</div>
                        <div style="font-size: 11px; color: #333; word-break: break-all;">
                            ${pathStr}
                        </div>
                    </div>
                    <div style="font-size: 16px; color: #667eea;">👁️</div>
                </div>
            </div>
        `;
    }

    // 说明
    html += `
        <div style="margin-top: 12px; padding: 8px; background: #e7f3ff; border-radius: 6px; font-size: 11px; color: #004085;">
            <div style="font-weight: 600; margin-bottom: 4px;">💡 说明</div>
            <div>时延上界基于网络演算计算，是数学保证的最坏情况值，不是统计平均值。</div>
        </div>
    `;

    resultContent.innerHTML = html;
}

// 显示错误信息
function showDeterministicError(message) {
    const resultDiv = document.getElementById('detResult');
    const resultContent = document.getElementById('detResultContent');

    resultDiv.style.display = 'block';
    resultContent.innerHTML = `
        <div style="display: flex; align-items: center; gap: 8px; padding: 8px; background: #f8d7da; border-radius: 6px;">
            <span style="font-size: 20px;">❌</span>
            <div style="flex: 1;">
                <div style="font-weight: 600; color: #721c24; font-size: 13px;">验证失败</div>
                <div style="font-size: 11px; color: #721c24; margin-top: 2px;">${message}</div>
            </div>
        </div>
    `;
}

// 初始化：在页面加载后加载交换机列表
if (typeof window.addEventListener !== 'undefined') {
    window.addEventListener('load', function() {
        // 初始加载
        loadDeterministicSwitches();

        // 定期刷新（与拓扑更新同步）
        setInterval(loadDeterministicSwitches, 5000);
    });
}

// 导出函数供全局使用
window.verifyDeterministic = verifyDeterministic;
window.updateDeterministicSwitches = updateDeterministicSwitches;
window.loadDeterministicSwitches = loadDeterministicSwitches;
