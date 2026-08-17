// ==================== Global State ====================
var network;
var nodes = new vis.DataSet();
var edges = new vis.DataSet();
let lastTopology = "";
let topologyIntervalId = null;
let showHosts = true;
let showBandwidth = false;
let showDelay = false;
let showLoss = false;
let showLoad = false;
let fixTopology = false;
let currentHosts = [];
let currentLinks = [];

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

// ==================== Loading Overlay ====================
function showLoading(message) {
    document.getElementById('loadingText').textContent = message || '请稍候';
    document.getElementById('loadingOverlay').classList.add('active');
}

function hideLoading() {
    document.getElementById('loadingOverlay').classList.remove('active');
}