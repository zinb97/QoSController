// ==================== Global State ====================
var network;
var nodes = new vis.DataSet();
var edges = new vis.DataSet();
let lastTopology = "";
let topologyIntervalId = null;
let cachedSwitches = [];
let showHosts = true;
let showBandwidth = false;
let showDelay = false;
let showLoss = false;
let showLoad = false;
let fixTopology = false;
let currentHosts = [];
let currentLinks = [];
let sliceTopology = null;  // 存储切片拓扑数据

// Flow path animation state
let currentHighlightedPath = null;
let flowAnimationInterval = null;
let animationState = false;
let originalEdgeStates = {};

// ==================== Init ====================
window.onload = function() {
    initNetwork();
    updateTopology();
    updateActiveFlows();
    updateMulticastWeights();
    updateMulticastActiveFlows();
    updateQosRules();
    loadRoutingAlgorithm();
    loadLinkEventState();
    loadMulticastRerouteState();
    topologyIntervalId = setInterval(updateTopology, 2000);
    setInterval(updateActiveFlows, 2000);
    setInterval(updateMulticastWeights, 5000);
    setInterval(updateMulticastActiveFlows, 2000);
    setInterval(updateQosRules, 5000);
};

// ==================== List Scroll Cap ====================
// 交换机列表 / 主机列表 / 活跃流表：展开时最多显示 5 条，
// 超出部分在列表内部滚动（不撑高侧栏）。
const LIST_MAX_ITEMS = 5;

function capListHeight(listEl, maxItems) {
    if (!listEl) return;
    const limit = maxItems || LIST_MAX_ITEMS;
    const items = listEl.children;

    if (items.length <= limit) {
        listEl.classList.remove('list-scroll');
        listEl.style.maxHeight = '';
        delete listEl.dataset.capH;
        return;
    }
    // 列表所在分区被折叠（display:none）时读不到布局信息：
    // 沿用上次测得的高度；没有缓存就稍后重试（等用户展开该分区）
    if (!items[0].offsetHeight) {
        const cached = listEl.dataset.capH;
        if (cached) {
            listEl.style.maxHeight = cached + 'px';
            listEl.classList.add('list-scroll');
        } else if (!listEl.dataset.capRetry) {
            listEl.dataset.capRetry = '1';
            setTimeout(function () {
                delete listEl.dataset.capRetry;
                capListHeight(listEl, limit);
            }, 500);
        }
        return;
    }

    const first = items[0];
    const last = items[limit - 1];
    // 前 limit 条所占高度（含条目之间的 gap）
    const h = (last.offsetTop + last.offsetHeight) - first.offsetTop;
    listEl.dataset.capH = String(h);
    listEl.style.maxHeight = h + 'px';
    listEl.classList.add('list-scroll');
}

// ==================== Loading Overlay ====================
function showLoading(message) {
    document.getElementById('loadingText').textContent = message || '请稍候';
    document.getElementById('loadingOverlay').classList.add('active');
}

function hideLoading() {
    document.getElementById('loadingOverlay').classList.remove('active');
}