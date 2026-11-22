import json
import os
import sys
import threading
import time
from collections import deque
from typing import NamedTuple, Optional

import networkx as nx
import numpy as np
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.controller.ofp_event import EventOFPFlowRemoved
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp, tcp, udp, igmp
from ryu.ofproto import ofproto_v1_3, ether
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from webob import Response

import utils

# 算法包已集成到 QoSController 目录中，直接导入即可。
# 算法包缺失时控制器仍须能跑：降级为原来的单目标 Dijkstra，而不是启动失败。
# 打印一条 WARN 让运维知道「智能路由没生效」，避免静默退化。
try:
    import algo.predict as algo_predict
    import algo.routing as algo_routing
    ALGO_AVAILABLE = True
except Exception as _algo_err:      # noqa: BLE001 - 导入失败原因要原样打出
    algo_predict = None
    algo_routing = None
    ALGO_AVAILABLE = False
    print(f'[WARN] algo package unavailable ({_algo_err}); '
          f'smart routing / load prediction disabled, falling back to hop count')

from controllers.QoSRuleController import QoSRuleController
from controllers.SliceController import SliceController
from controllers.StaticController import StaticController
from controllers.TopologyController import TopologyController
from controllers.LinkEventController import LinkEventController
from controllers.MulticastRerouteController import MulticastRerouteController
from controllers.ActiveFlowsController import ActiveFlowsController
from controllers.MulticastWeightsController import MulticastWeightsController
from controllers.RoutingController import RoutingController
from controllers.MetricsController import MetricsController
from client import RL_multicast_tree
from utils import WeightPref

topology_api_name = 'topology_api'

# ==================== Prometheus Metrics ====================
# 拓扑指标
qos_switches_count = Gauge('qos_switches_count', 'Number of switches in topology')
qos_links_count = Gauge('qos_links_count', 'Number of links in topology')
qos_hosts_count = Gauge('qos_hosts_count', 'Number of hosts in topology')

# 交换机指标
qos_switch_packet_in = Counter('qos_switch_packet_in_total', 'Total packet-in events', ['dpid'])
qos_switch_flows_installed = Gauge('qos_switch_flows_installed', 'Number of installed flows', ['dpid'])

# 链路指标
# qos_link_bandwidth_usage_bits：**单向（src→dst）实测吞吐**。
# 口径变更：早期版本取的是 src 端口的 rx+tx（双向混在一起），同一条物理链路的
# 两个方向各报一条，求和时同一份流量被算了两遍。现在一个方向只报一个值，
# 对全部有向边求和 = 每条物理链路的双向总流量，无重复计数。
qos_link_bandwidth_usage = Gauge('qos_link_bandwidth_usage_bits', 'Link one-way throughput src->dst in bits/s', ['src_dpid', 'dst_dpid'])
# 额定速率：来自 OVS Interface 的 link_speed（网卡驱动上报的协商速率）。
# 消费端用它替代「全网按固定 1000 Mbps 算」的假设来算带宽余量。
qos_link_capacity = Gauge('qos_link_capacity_bits', 'Link rated capacity from OVS link_speed in bits/s', ['src_dpid', 'dst_dpid'])
# 时延：netem 的**配置值**。被动采集拿不到 RTT，实测时延需要主动探测（尚未实现）。
qos_link_delay = Gauge('qos_link_delay_ms', 'Link delay from netem configuration in milliseconds', ['src_dpid', 'dst_dpid'])
# 丢包率：**实测**（tc -s 累计计数差分）。
# 语义变更：早期版本报的是 netem 的配置值（恒定不变，与实际丢包无关）。
qos_link_loss = Gauge('qos_link_loss_percent', 'Measured link packet loss percentage', ['src_dpid', 'dst_dpid'])
# 丢包率配置值（netem 参数），保留用于与实测值对照，验证损伤是否真的生效
qos_link_loss_configured = Gauge('qos_link_loss_configured_percent', 'Configured netem packet loss percentage', ['src_dpid', 'dst_dpid'])

# 网络总带宽指标
qos_network_total_bandwidth_bits = Gauge('qos_network_total_bandwidth_bits', 'Total network throughput in bits/s (sum of per-direction link throughput)')
qos_network_total_capacity_bits = Gauge('qos_network_total_capacity_bits', 'Total network rated capacity in bits/s (sum of per-direction link capacity)')

# QoS 规则指标
qos_rules_total = Gauge('qos_rules_total', 'Total number of QoS rules')
qos_rules_active = Gauge('qos_rules_active', 'Number of active QoS rules')
# 带 SLA 数值目标的规则数（target_bps 已配置）——用于区分「只选路」与「真的限速」
qos_rules_with_sla = Gauge('qos_rules_with_sla', 'QoS rules with an explicit rate SLA (meter installed)')
# 已下发 meter 的规则数（meter 安装成功才算）
qos_rules_metered = Gauge('qos_rules_metered', 'QoS rules whose meter was installed successfully')
# SLA 兑现状态：1=兑现 0=未达 0.5=无数据
qos_rule_sla_compliance = Gauge('qos_rule_sla_compliance', 'SLA compliance ratio (measured/committed, capped at 1)', ['rule', 'sla_type'])
# 实测速率对承诺速率的比值（不带上限，<1 表示没达标）
qos_rule_sla_ratio = Gauge('qos_rule_sla_ratio', 'Measured rate / committed rate for a QoS rule', ['rule'])

# ---- 多目标联合路由（2.4.2）----
# 每次选路的耗时，按偏好类型分（可看出联合优化比单目标贵多少）
qos_routing_decision_seconds = Histogram(
    'qos_routing_decision_seconds', 'Path computation duration', ['preference'])
# 选路次数，按偏好类型分
qos_routing_decisions_total = Counter(
    'qos_routing_decisions_total', 'Path computations', ['preference'])
# 算力占用（0~1），由外部调度器经 REST 注入；label=交换机名
qos_compute_load = Gauge(
    'qos_compute_load_ratio', 'Injected compute load per switch (0=idle, 1=saturated)', ['switch'])
# 当前有效权重（便于从 Prometheus 回看是哪套参数在起作用）
qos_routing_weight = Gauge(
    'qos_routing_weight', 'Active weight of each routing metric', ['metric'])

# ---- 反馈式负载均衡（2.4.3）----
# 每轮评估/重路由/跳过计数。评估数 >> 重路由数，就是「降低了反馈开销」的证据
qos_lb_evaluated_total = Counter(
    'qos_lb_evaluated_total', 'Load-balance rule evaluations')
qos_lb_reroutes_total = Counter(
    'qos_lb_reroutes_total', 'Load-balance reroutes actually performed')
qos_lb_skipped_total = Counter(
    'qos_lb_skipped_total', 'Load-balance evaluations skipped (below threshold / cooldown)')
# 上一轮反馈周期耗时与规模（Gauge 而非 Counter，看「最近一轮」）
qos_lb_cycle_duration = Gauge(
    'qos_lb_cycle_duration_seconds', 'Duration of the last load-balance feedback cycle')
qos_lb_cycle_rules = Gauge(
    'qos_lb_cycle_rules', 'Number of rules seen in the last feedback cycle')
# 预测负载：比值（预测吞吐/额定），label=有向边
qos_link_predicted_util = Gauge(
    'qos_link_predicted_util_ratio', 'Predicted link utilization (0~1)', ['src_dpid', 'dst_dpid'])

# 组播指标
qos_multicast_groups = Gauge('qos_multicast_groups', 'Number of active multicast groups')
qos_multicast_flows = Gauge('qos_multicast_flows', 'Number of multicast flows installed')

# 控制器性能指标
qos_controller_uptime = Gauge('qos_controller_uptime_seconds', 'Controller uptime in seconds')
qos_topology_updates_total = Counter('qos_topology_updates_total', 'Total topology updates')
qos_topology_update_duration = Histogram('qos_topology_update_duration_seconds', 'Topology update duration')


class LinkSample(NamedTuple):
    """一次链路采样结果。

    - stats: 该接口的 tc 采样（配置值 + 实测累计计数）
    - sampled_at: 采样时刻
    - loss_measured_pct: 用本次与上次的累计计数差分算出的**实测**丢包率（%）。
      None 表示还没有差分样本（新链路刚发现、或上一轮计数器被重置）。
    """
    stats: utils.LinkTcStats
    sampled_at: float
    loss_measured_pct: Optional[float]


class QoSController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    _CONTEXTS = {
        'wsgi': WSGIApplication
    }

    def __init__(self, *args, **kwargs):
        super(QoSController, self).__init__(*args, **kwargs)

        # 核心数据存储
        self.hosts = {}                 # mac -> {ip, dpid, port, mac}
        self.ip_to_mac = {}             # ip -> mac
        self.inter_switch_ports = {}    # {dpid: set(port_no, ...)}
        self.datapaths = {}             # 保存所有交换机

        # 组播组管理: {组播组地址: {dpid: {MAC地址: (src_ip, in_port)}}}
        self.multicast_groups = {}
        # 组播流规则记录: {组播组地址: [(dpid, match_description), ...]}
        self.multicast_flows = {}
        # 组播流规则安装标记集合: 标记已安装流规则的组播组地址
        self.multicast_flow_installed = set()
        # 组播源节点记录: {组播组地址: 源节点IP}
        self.multicast_sources = {}
        # 组播权重配置: {组播组地址: {'latency': 1.0, 'bandwidth': 1.0, 'loss': 1.0}}
        self.multicast_weights = {}
        # 路由算法配置: 'dijkstra'=迪杰斯特拉算法, 'smart'=智能算法(默认)
        self.routing_algorithm = 'smart'

        # QoS 规则配置: {rule_id: {name, src_ip, dst_ip, protocol, src_port, dst_port, dscp, priority, guarantee, enabled, src_dpid, dst_dpid, path, path_switches}}
        self.qos_rules = {}
        self.qos_rule_counter = 0

        # ---- 多目标联合路由（2.4.2）----
        # 实例属性而非只靠模块全局：REST 层拿到的只有 app 实例，
        # 读不到 QoSController 模块的 ALGO_AVAILABLE，必须在这里显式挂一份，
        # 否则 /routing/weights 会一直报 available=false（误判为算法包缺失）。
        self.algo_available = ALGO_AVAILABLE
        # 权重可由 REST 调整（PUT /routing/weights），默认值见 algo.routing
        self.routing_weights = dict(algo_routing.DEFAULT_WEIGHTS) if ALGO_AVAILABLE else {}
        # 每个交换机的算力占用（0~1，1=算力耗尽），由外部调度器经
        # PUT /routing/compute 注入；缺省为空 = 不影响选路
        self.compute_load = {}
        # 选路决策计数（按偏好类型）
        self._routing_decisions = {}

        # ---- 反馈式负载均衡（2.4.3）----
        # 每条有向边的负载预测器: {(u, v): TrendPredictor}
        self._link_load_predictors = {}
        # 每条规则的重路由状态: {rule_id: {'hot': 连续超阈值次数, 'last': 上次重路由时刻}}
        self._lb_rule_state = {}
        # 负载均衡阈值与去抖参数（可用环境变量覆盖，便于测试）
        self._lb_high_watermark = float(os.getenv('LB_HIGH_WATERMARK', '0.75'))
        self._lb_hot_cycles = int(os.getenv('LB_HOT_CYCLES', '3'))
        self._lb_cooldown_s = float(os.getenv('LB_COOLDOWN_S', '8'))
        self._lb_min_gain = float(os.getenv('LB_MIN_GAIN', '0.05'))
        # 上一轮统计（导成指标，用来证明「不是每轮都在全量重算」）
        self._lb_last_cycle = {'evaluated': 0, 'triggered': 0, 'skipped': 0,
                               'ts': 0.0, 'duration': 0.0}

        # 拓扑相关 - 使用 networkx DiGraph
        self.graph = nx.DiGraph()        # 保存完整拓扑（节点=交换机，边=链路）
        self.adj_matrix_nodes = []       # 邻接矩阵的节点顺序列表（索引->dpid映射）
        self.host_ip_to_dpid = {}        # 主机IP -> 所在交换机DPID 映射
        self.topology_data = {"switches": [], "links": [], "hosts": []}

        self._topology_stable = False
        self._start_time = time.time()

        # 活跃流表追踪
        # {(src_ip, dst_ip): {'path': [dpid1, dpid2, ...], 'path_switches': [...]}}
        self.active_flows = {}

        # 组播活跃流追踪
        # {multicast_addr: {
        #     'source_ip': str,           # 组播源IP
        #     'source_dpid': int,         # 源交换机DPID
        #     'route_matrix': np.array,   # 组播树邻接矩阵
        #     'member_ips': [str, ...],   # 组成员IP列表
        #     'path': [(dpid1, dpid2), ...],  # 组播树路径列表
        #     'installed_time': float      # 安装时间戳
        # }}
        self.multicast_active_flows = {}

        # 端口统计（用于计算链路负载）
        self.port_stats = {}            # {dpid: {port_no: {'rx_bytes': ..., 'tx_bytes': ..., 'timestamp': ...}}}
        self.port_stats_prev = {}       # {dpid: {port_no: {'rx_bytes': ..., 'tx_bytes': ..., 'timestamp': ...}}}
        # 组播组负载统计: {multicast_addr: {'load': float(Mbps), 'timestamp': float}}
        self.multicast_load = {}
        # 流规则统计: {dpid: {match_key: {'byte_count': ..., 'packet_count': ..., 'timestamp': ...}}}
        self.flow_stats = {}
        self.flow_stats_prev = {}
        # 链路连续高负载计数: {(src_dpid, dst_dpid): 连续高负载次数}
        self.link_high_load_count = {}
        # 组播组高负载标记: {multicast_addr: True/False}
        self.multicast_high_load = {}
        # 不可替代节点（出入度和为 1 或 2 的节点）
        self.critical_nodes = set()

        # RLock 而非 Lock：update_topology() 自身也要加锁，
        # 而它会被已持锁的后台循环调用，可重入锁避免潜在死锁
        self._lock = threading.RLock()

        # 链路 tc 指标缓存: {eth_name: LinkSample}
        # tc 采样需要 fork 子进程，由独立循环负责并放在锁外执行，
        # 避免占用 update_topology 的临界区阻塞 packet-in
        self._link_metrics_cache = {}
        # 上一次 tc 采样，用于差分算实测丢包率
        self._link_metrics_prev = {}
        # OVS 接口额定速率缓存: {接口名: bit/s}（OVS Interface 的 link_speed）
        self._ovs_link_speeds = {}
        self._ovs_link_speeds_ts = 0.0
        self._ovs_link_speeds_ttl = 60
        self._link_metrics_refresh_interval = 3

        self.switch_name = utils.get_switch_name()

        # 链路抖动去抖动机制
        self._link_delete_pending = {}  # {(src_dpid, src_port, dst_dpid, dst_port): pending_timer}
        self._link_delete_delay = 0.01     # 延迟秒数再删除链路

        # 链路事件处理开关
        self.link_event_enabled = True  # 默认开启
        # 组播组重路由开关
        self.multicast_reroute_enabled = False  # 默认关闭

        # 抑制 WSGI HTTP 请求日志
        # 原始实现使用 LoggingWrapper 写日志到 LOG.info
        _original_serve_forever = hub.WSGIServer.serve_forever
        def silent_serve_forever(self):
            class SilentLoggingWrapper:
                def write(self, message):
                    pass  # 完全静默，不输出任何日志
            self.logger = SilentLoggingWrapper()
            hub.eventlet.wsgi.server(self.server, self.handle, self.logger)
        hub.WSGIServer.serve_forever = silent_serve_forever

        # 注册 Web API
        wsgi = kwargs['wsgi']
        wsgi.register(StaticController, {topology_api_name: self})
        wsgi.register(TopologyController, {topology_api_name: self})
        wsgi.register(LinkEventController, {topology_api_name: self})
        wsgi.register(MulticastRerouteController, {topology_api_name: self})
        wsgi.register(ActiveFlowsController, {topology_api_name: self})
        wsgi.register(MulticastWeightsController, {topology_api_name: self})
        wsgi.register(RoutingController, {topology_api_name: self})
        wsgi.register(QoSRuleController, {topology_api_name: self})
        wsgi.register(SliceController, {topology_api_name: self})
        wsgi.register(MetricsController, {topology_api_name: self})

        self._topo_thread = hub.spawn(self._topology_loop)
        self._link_metrics_thread = hub.spawn(self._link_metrics_loop)
        self._stats_thread = hub.spawn(self._stats_loop)
        self._mcast_load_thread = hub.spawn(self._multicast_load_loop)
        self._link_load_thread = hub.spawn(self._link_load_loop)
        self._reroute_thread = hub.spawn(self._reroute_multicast_loop)

    def _topology_loop(self):
        while True:
            try:
                # update_topology 自行管理加锁，避免长时间持有锁阻塞 packet-in
                self.update_topology()
                with self._lock:
                    self._update_prometheus_metrics()
            except Exception as e:
                self.logger.warning(f"Topology loop update failed: {e}")
            hub.sleep(3)

    def _link_metrics_loop(self):
        """刷新链路 tc 指标（带宽/时延/丢包）

        与拓扑更新同周期，但单独成循环且不持有 _lock：
        tc 采样需 fork 子进程，若放在 update_topology 临界区内会阻塞 packet-in。
        """
        while True:
            try:
                self._refresh_link_metrics()
            except Exception as e:
                self.logger.warning(f"Link metrics refresh failed: {e}")
            hub.sleep(self._link_metrics_refresh_interval)

    def _refresh_link_metrics(self):
        """采集缓存中已有设备的 tc 指标并更新缓存（不持有 _lock）

        同时用两次采样的累计计数做差分，算出**实测**丢包率：
        内核 qdisc 丢包只累加 drops、**不计入 Sent**，所以分母是 Δsent + Δdropped。
        """
        self._refresh_ovs_link_speeds()
        for dev in list(self._link_metrics_cache.keys()):
            stats = utils.get_tc_stats_optimized(dev)
            prev = self._link_metrics_prev.get(dev)
            loss_measured = None
            if prev is not None:
                d_sent = stats.packets_sent - prev.packets_sent
                d_drop = stats.packets_dropped - prev.packets_dropped
                # 计数回退说明设备/qdisc 被重建，这一轮不参与差分
                if d_sent >= 0 and d_drop >= 0 and (d_sent + d_drop) > 0:
                    loss_measured = round(d_drop * 100.0 / (d_sent + d_drop), 4)
            self._link_metrics_prev[dev] = stats
            self._link_metrics_cache[dev] = LinkSample(stats, time.time(), loss_measured)

    def _refresh_ovs_link_speeds(self, force=False):
        """刷新 OVS 接口额定速率缓存（默认 60s 一次）。

        链路的协商速率不会频繁变化，但 OVS 里没有现成的「速率变更」事件，
        故用 TTL 定期重读。重读失败时保留旧值，不清空——宁可沿用上次的
        额定值，也不要让带宽余量突然变成「未知」。
        """
        now = time.time()
        if not force and now - self._ovs_link_speeds_ts < self._ovs_link_speeds_ttl:
            return
        speeds = utils.get_ovs_interface_speeds()
        if speeds:
            self._ovs_link_speeds = speeds
        self._ovs_link_speeds_ts = now

    def _prime_link_metrics(self, link_list):
        """补齐尚未采样的链路设备指标，并清理已消失设备的缓存

        采样在锁外执行：子进程调用不应阻塞事件处理。
        """
        now = time.time()
        live_devices = set()
        for l in link_list:
            src_dpid, src_port = l.src.dpid, l.src.port_no
            eth_name = f'{self.switch_name.get(f"{src_dpid:016x}")}-eth{src_port}'
            live_devices.add(eth_name)
            if eth_name not in self._link_metrics_cache:
                stats = utils.get_tc_stats_optimized(eth_name)
                # 首次采样只作为差分的基准，本轮没有实测丢包率
                self._link_metrics_prev[eth_name] = stats
                self._link_metrics_cache[eth_name] = LinkSample(stats, now, None)

        # 已下线的设备不应继续被低频刷新任务采样
        for dev in list(self._link_metrics_cache.keys()):
            if dev not in live_devices:
                del self._link_metrics_cache[dev]
                self._link_metrics_prev.pop(dev, None)

    def _stats_loop(self):
        """定期请求交换机端口统计信息，并根据负载更新 load_balance QoS 规则"""
        while True:
            try:
                with self._lock:
                    self._request_port_stats()
                    self._check_and_update_load_balance_rules()
            except Exception as e:
                self.logger.warning(f"Port stats request failed: {e}")
            hub.sleep(2)

    def _check_and_update_load_balance_rules(self):
        """反馈式负载均衡（2.4.3）。

        与改造前的区别，逐条对应研究内容里的痛点：

        1. **从「当前负载」改成「预测负载」**：用每条边历史的 EWMA+斜率外推
           下一步利用率，在链路真的过载前动作。
        2. **从「每次都全量重算」改成「先判有没有必要」**：只有预测利用率
           连续 N 次越过水位线、且不在冷却期，才真的去算新路径。评估是
           常数级的（查预测器 + 比数值），重算才是 Dijkstra —— 这就是
           「降低反馈开销」的落点，并导成 `qos_lb_evaluated_total` /
           `qos_lb_reroutes_total` 两个计数，比值本身就是证据。
        3. **加增益门槛与冷却**：新路径的联合代价必须比当前路径好
           `_lb_min_gain` 以上才切，避免来回抖振（原来只要路径不同就切）。
        4. **不再静默失效**：预测器没数据时退回当前实测负载（而不是当成 0），
           额定带宽未知时跳过该边（而不是把比值算成 0）。

        路径为什么切换，日志里给的是联合代价前后的数值，不再是「path changed」。
        """
        started = time.time()
        evaluated = triggered = skipped = 0

        for rule_id, rule in list(self.qos_rules.items()):
            if rule.get('guarantee') != 'load_balance':
                continue

            src_ip = rule.get('src_ip')
            dst_ip = rule.get('dst_ip')
            src_dpid = self.host_ip_to_dpid.get(src_ip)
            dst_dpid = self.host_ip_to_dpid.get(dst_ip)
            if not src_dpid or not dst_dpid:
                continue

            evaluated += 1
            current_path = rule.get('path', [])
            state = self._lb_rule_state.setdefault(
                rule_id, {'hot': 0, 'last': 0.0})

            # ---- 1) 预测当前路径的瓶颈利用率（O(跳数)，不做搜索）----
            util, bottleneck = self._predict_path_utilization(current_path)
            if util is None:
                # 没有任何可用的链路数据：不猜，跳过并留计数
                skipped += 1
                state['hot'] = 0
                continue

            if util < self._lb_high_watermark:
                # 正常水位：清零连击，不要因为历史问题继续折腾
                state['hot'] = 0
                skipped += 1
                continue

            state['hot'] += 1
            if state['hot'] < self._lb_hot_cycles:
                # 单次尖峰不算数（去抖）
                skipped += 1
                continue

            now = time.time()
            if now - state['last'] < self._lb_cooldown_s:
                skipped += 1
                continue

            # ---- 2) 确实需要重算，才跑联合优化 ----
            new_path, new_cost = self._joint_route(src_dpid, dst_dpid)
            if not new_path or len(new_path) < 2 or new_path == current_path:
                skipped += 1
                state['hot'] = 0
                continue

            old_cost = algo_routing.path_cost(
                self.graph, current_path,
                weights=self.routing_weights, node_penalty=self.compute_load) \
                if ALGO_AVAILABLE else float('inf')

            # 只有新路径显著更优才切（相对改善量，避免代价量纲影响阈值）
            gain = (old_cost - new_cost) / old_cost if old_cost > 0 else 0.0
            if gain < self._lb_min_gain:
                self.logger.info(
                    f"[LoadBalance] {rule.get('name')}: hot ({util:.0%} at {self.sw_name(bottleneck[0])}"
                    f"->{self.sw_name(bottleneck[1])}) but no better path "
                    f"(cost {old_cost:.3f} -> {new_cost:.3f}, gain {gain:.1%})")
                skipped += 1
                state['hot'] = 0
                continue

            # ---- 3) 切换 ----
            self.logger.info(
                f"[LoadBalance] {rule.get('name')}: reroute "
                f"(predicted util {util:.0%} at {self.sw_name(bottleneck[0])}"
                f"->{self.sw_name(bottleneck[1])}, cost {old_cost:.3f} -> {new_cost:.3f})")
            self.logger.info(f"[LoadBalance] old path: {' -> '.join(self.sw_name(p) for p in current_path)}")
            self.logger.info(f"[LoadBalance] new path: {' -> '.join(self.sw_name(p) for p in new_path)}")

            src_host = self.hosts.get(self.ip_to_mac.get(src_ip), {})
            dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip), {})
            path_switches = self.get_path_switches(
                new_path,
                src_host.get('dpid', new_path[0]),
                src_host.get('port', 1),
                dst_host.get('dpid', new_path[-1]),
                dst_host.get('port', 1)
            )

            self.delete_qos_flow(rule)
            rule['path'] = new_path
            rule['path_switches'] = path_switches

            self.active_flows[rule_id] = {
                'type': 'qos',
                'path': new_path,
                'path_switches': path_switches,
                'rule': rule,
                'src_host': src_host,
                'dst_host': dst_host,
            }

            self.install_qos_flow(rule)
            triggered += 1
            state['hot'] = 0
            state['last'] = now
            self.logger.info(f"[LoadBalance] {rule.get('name')}: flow table updated")

        # 清理已删除规则的状态，避免字典无限增长
        for rid in list(self._lb_rule_state.keys()):
            if rid not in self.qos_rules:
                self._lb_rule_state.pop(rid, None)

        if evaluated:
            qos_lb_evaluated_total.inc(evaluated)
        if triggered:
            qos_lb_reroutes_total.inc(triggered)
        if skipped:
            qos_lb_skipped_total.inc(skipped)
        self._lb_last_cycle = {
            'evaluated': evaluated, 'triggered': triggered,
            'skipped': skipped, 'ts': time.time(),
            'duration': time.time() - started,
        }

    def _predict_path_utilization(self, path):
        """预测一条路径的瓶颈利用率 → (util, (u, v)) 或 (None, None)

        取路径上各跳**预测利用率的最大值**作为瓶颈。预测器样本不足时
        退回该边的当前实测利用率（`oneway_bps / bw`），而不是当 0 ——
        把未知当空闲，会导致过载链路被判定为「健康」而永不触发。
        """
        if not path or len(path) < 2:
            return None, None

        worst_util = None
        worst_edge = None
        for u, v in zip(path, path[1:]):
            if not self.graph.has_edge(u, v):
                return None, None
            attrs = self.graph[u][v]
            bw = attrs.get('bw') or 0
            if bw <= 0:
                # 额定未知：这条边不参与判断（不能当 0 也不能当 1）
                continue

            util = None
            if ALGO_AVAILABLE:
                pred = self._link_load_predictors.get((u, v))
                if pred is not None and pred.ready:
                    predicted = pred.predict(horizon=1)
                    if predicted is not None:
                        util = predicted / bw
            if util is None:
                util = (attrs.get('oneway_bps') or 0) / bw

            if worst_util is None or util > worst_util:
                worst_util, worst_edge = util, (u, v)

        return worst_util, worst_edge

    def _joint_route(self, src, dst):
        """跑一次联合优化路由 → (path, cost)；算法包缺失时退回跳数最短"""
        if ALGO_AVAILABLE:
            return algo_routing.best_path(
                self.graph, src, dst,
                weights=self.routing_weights,
                node_penalty=self.compute_load)
        path = self.get_shortest_path(src, dst)
        return path, 0.0

    def _multicast_load_loop(self):
        """定期请求组播组源交换机的流表统计，计算组播组负载"""
        while True:
            try:
                with self._lock:
                    for multicast_addr, flow_info in self.multicast_active_flows.items():
                        src_dpid = flow_info.get('source_dpid')
                        src_ip = flow_info.get('source_ip')
                        if src_dpid and src_dpid in self.datapaths and src_ip:
                            self._request_flow_stats(src_dpid, multicast_addr, src_ip)
            except Exception as e:
                self.logger.warning(f"Multicast load stats request failed: {e}")
            hub.sleep(5)

    def _link_load_loop(self):
        """定期检查链路负载，如果某条链路连续三次超过80%负载且在组播路径中，标记组播组为高负载"""
        while True:
            try:
                with self._lock:
                    self._check_link_load()
            except Exception as e:
                self.logger.warning(f"Link load check failed: {e}")
            hub.sleep(5)

    def _reroute_multicast_loop(self):
        """定期为高负载组播组重新计算路由并安装流表"""
        while True:
            try:
                with self._lock:
                    self._reroute_high_load_multicast()
            except Exception as e:
                self.logger.warning(f"Reroute multicast failed: {e}")
            hub.sleep(10)

    def _reroute_high_load_multicast(self):
        """为高负载组播组重新计算路由
        1. 获取带宽矩阵、可用带宽矩阵、当前组播负载、当前路由矩阵
        2. 更新可用带宽矩阵：释放当前组播占用的带宽
        3. 剔除高负载链路后检查连通性
        4. 如果连通，重新计算路由并安装
        """
        if not self.multicast_reroute_enabled:
            return

        if not self.multicast_high_load:
            return

        for multicast_addr, is_high_load in list(self.multicast_high_load.items()):
            if not is_high_load:
                continue

            self.logger.info(f"[Reroute] Reinstalling multicast flow for high-load group {multicast_addr}")

            # 获取组播组当前负载
            current_load = self.multicast_load.get(multicast_addr, {}).get('load', 0)
            if current_load <= 0:
                self.logger.warning(f"[Reroute] No valid load for group {multicast_addr}, skipping reroute")
                continue

            # 获取当前路由矩阵
            if multicast_addr not in self.multicast_active_flows:
                self.logger.warning(f"[Reroute] No active flow found for group {multicast_addr}, skipping reroute")
                continue

            current_flow = self.multicast_active_flows[multicast_addr]
            route_matrix = current_flow.get('route_matrix')
            if route_matrix is None:
                self.logger.warning(f"[Reroute] No route matrix for group {multicast_addr}, skipping reroute")
                continue

            src_ip = current_flow.get('source_ip')
            src_switch_dpid = current_flow.get('source_dpid')
            if not src_ip or not src_switch_dpid:
                self.logger.warning(f"[Reroute] Invalid source info for group {multicast_addr}, skipping reroute")
                continue

            # 收集组成员信息
            member_ips = []
            member_dpid_set = set()
            if multicast_addr in self.multicast_groups:
                for group_dpid, mac_dict in self.multicast_groups[multicast_addr].items():
                    member_dpid_set.add(group_dpid)
                    for mac, (member_ip, member_port) in mac_dict.items():
                        member_ips.append(member_ip)

            # 构建矩阵
            adj_matrix_nodes = sorted(self.graph.nodes())
            net_size = len(adj_matrix_nodes)

            # 构建邻接矩阵、带宽矩阵、延迟矩阵、丢包矩阵
            adj_matrix = nx.to_numpy_array(self.graph, nodelist=adj_matrix_nodes, dtype=np.float32)
            bandwidth_matrix = nx.attr_matrix(self.graph, edge_attr='bw', rc_order=adj_matrix_nodes, dtype=np.float32)
            latency_matrix = nx.attr_matrix(self.graph, edge_attr='delay', rc_order=adj_matrix_nodes, dtype=np.float32)
            loss_matrix = nx.attr_matrix(self.graph, edge_attr='loss', rc_order=adj_matrix_nodes, dtype=np.float32)
            load_matrix = nx.attr_matrix(self.graph, edge_attr='tx_bps', rc_order=adj_matrix_nodes, dtype=np.float32)
            available_bw_matrix = bandwidth_matrix - load_matrix

            # 释放当前组播路由占用的带宽：遍历路由矩阵，将对应链路的可用带宽加上当前负载
            src_idx = adj_matrix_nodes.index(src_switch_dpid)
            visited = set()
            queue = deque([src_idx])
            visited.add(src_idx)

            while queue:
                i = queue.popleft()
                for j in range(net_size):
                    if route_matrix[i, j] == 1:
                        # 释放这条链路的带宽（但不超过链路带宽）
                        if available_bw_matrix[i, j] + current_load <= bandwidth_matrix[i, j]:
                            available_bw_matrix[i, j] += current_load
                        else:
                            available_bw_matrix[i, j] = bandwidth_matrix[i, j]
                        if j not in visited:
                            queue.append(j)
                            visited.add(j)

            self.logger.info(f"[Reroute] Released {current_load} Mbps from current route")

            # 剔除高负载链路：如果链路 (可用带宽-当前组播负载) / 带宽 <= 1 - 阈值(0.8)，则剔除
            high_threshold = 0.8
            filtered_adj_matrix = adj_matrix.copy()
            for i in range(net_size):
                for j in range(net_size):
                    if adj_matrix[i, j] > 0:  # 链路存在
                        potential_load = available_bw_matrix[i, j] - current_load
                        if bandwidth_matrix[i, j] > 0 and potential_load / bandwidth_matrix[i, j] <= 1 - high_threshold:
                            filtered_adj_matrix[i, j] = 0
                            self.logger.debug(f"[Reroute] Filtered out link {adj_matrix_nodes[i]}->{adj_matrix_nodes[j]} due to high load")

            # 检查源和所有目的地是否连通
            src_idx = adj_matrix_nodes.index(src_switch_dpid)
            member_idx_set = [adj_matrix_nodes.index(dpid) for dpid in member_dpid_set]

            # 使用 BFS 检查从源能否到达所有成员
            reachable = set()
            queue = deque([src_idx])
            reachable.add(src_idx)
            while queue:
                i = queue.popleft()
                for j in range(net_size):
                    if filtered_adj_matrix[i, j] > 0 and j not in reachable:
                        queue.append(j)
                        reachable.add(j)

            all_reachable = all(idx in reachable for idx in member_idx_set)
            if not all_reachable:
                self.logger.warning(f"[Reroute] Source cannot reach all members after filtering, skipping reroute for {multicast_addr}")
                continue

            self.logger.info(f"[Reroute] Source can reach all members, proceeding with reroute for {multicast_addr}")

            # 删除旧的流表规则
            self._delete_multicast_flows(multicast_addr)

            # 删除旧的 multicast_active_flows 记录
            if multicast_addr in self.multicast_active_flows:
                del self.multicast_active_flows[multicast_addr]

            # 清除高负载标记
            self.multicast_high_load[multicast_addr] = False

            # 使用过滤后的邻接矩阵重新计算路由
            # 重新构建延迟、可用带宽、丢包矩阵（基于过滤后的邻接矩阵）
            # 将filtered_adj_matrix中为0的位置，带宽也设为0
            filtered_available_bw = available_bw_matrix * (filtered_adj_matrix > 0).astype(np.float32)

            # 索引转换（1-based）
            src_switch_id = src_idx + 1
            member_id_list = [idx + 1 for idx in member_idx_set]

            # 获取权重配置
            weights = self.multicast_weights.get(multicast_addr, {'latency': 1.0, 'bandwidth': 1.0, 'loss': 1.0})
            self.logger.info(f"[Reroute] Using weights: latency={weights['latency']}, bandwidth={weights['bandwidth']}, loss={weights['loss']}")

            # 调用路由算法
            is_success, new_route_matrix = RL_multicast_tree(
                filtered_adj_matrix, latency_matrix, filtered_available_bw, loss_matrix,
                source_node=src_switch_id,
                destination_group=member_id_list,
                latency_weight=weights['latency'],
                bandwidth_weight=weights['bandwidth'],
                loss_weight=weights['loss'],
                algorithm_type=self.routing_algorithm
            )

            if not is_success:
                self.logger.warning(f"[Reroute] Failed to find new route for group {multicast_addr}")
                continue

            # 安装新的流表规则
            self.multicast_flow_installed.add(multicast_addr)

            # 将邻接矩阵的双向性变成单向性矩阵（BFS）
            visited = set()
            queue = deque([src_idx])
            visited.add(src_idx)

            while queue:
                i = queue.popleft()
                for j in range(net_size):
                    if new_route_matrix[i, j] == 1:
                        new_route_matrix[j, i] = 0
                        if j not in visited:
                            queue.append(j)
                            visited.add(j)

            # 安装流规则
            installed_flows = []
            for i in range(net_size):
                src_node_dpid = adj_matrix_nodes[i]
                if src_node_dpid not in self.datapaths:
                    continue

                src_datapath = self.datapaths[src_node_dpid]
                src_parser = src_datapath.ofproto_parser

                out_actions = []

                # 如果该交换机上有组成员主机，转发到主机端口
                if src_node_dpid in member_dpid_set:
                    for mac, (member_ip, member_port) in self.multicast_groups[multicast_addr].get(src_node_dpid, {}).items():
                        out_actions.append(src_parser.OFPActionOutput(member_port))
                        self.logger.info(f"[Reroute] SW:{self.sw_name(src_node_dpid)} -> host port {member_port}")

                for j in range(net_size):
                    if new_route_matrix[i][j] == 1:
                        dst_node_dpid = adj_matrix_nodes[j]
                        out_port = self.get_path_out_port(src_node_dpid, dst_node_dpid)
                        if out_port:
                            out_actions.append(src_parser.OFPActionOutput(out_port))
                            self.logger.info(f"[Reroute] SW:{self.sw_name(src_node_dpid)} -> port {out_port} (next hop SW:{self.sw_name(dst_node_dpid)})")

                if out_actions:
                    match = src_parser.OFPMatch(eth_type=0x0800, ipv4_src=src_ip, ipv4_dst=multicast_addr)
                    self.add_flow(src_datapath, 100, match, out_actions, idle_timeout=0, hard_timeout=0)
                    installed_flows.append((src_node_dpid, f"ipv4_src={src_ip}, ipv4_dst={multicast_addr} -> {len(out_actions)} port(s)"))

            # 记录已安装的流规则
            if multicast_addr not in self.multicast_flows:
                self.multicast_flows[multicast_addr] = []
            self.multicast_flows[multicast_addr].extend(installed_flows)

            # 提取路径列表
            path_list = []
            for i in range(net_size):
                for j in range(net_size):
                    if new_route_matrix[i, j] == 1:
                        path_list.append((adj_matrix_nodes[i], adj_matrix_nodes[j]))

            # 获取源主机与源交换机相连的端口号
            src_switch_in_port = None
            for mac, host_info in self.hosts.items():
                if host_info.get('ip') == src_ip and host_info.get('dpid') == src_switch_dpid:
                    src_switch_in_port = host_info.get('port')
                    break

            # 更新 multicast_active_flows
            self.multicast_active_flows[multicast_addr] = {
                'source_ip': src_ip,
                'source_dpid': src_switch_dpid,
                'source_in_port': src_switch_in_port,
                'route_matrix': new_route_matrix,
                'member_ips': member_ips,
                'path': path_list,
                'installed_time': time.time()
            }

            self.logger.info(f"[Reroute] Successfully reinstalled multicast flow for group {multicast_addr}, installed {len(installed_flows)} flow rules")

            # 重路由成功则直接退出 for 循环，下一个组播组重路由在下一轮任务中再执行，不同组播组高负载重路由之间形成间隔
            break

    def _check_link_load(self):
        """检查所有链路负载，更新高负载计数和组播组标记"""
        high_threshold = 0.8  # 80% 阈值

        for src_dpid, dst_dpid, attrs in self.graph.edges(data=True):
            bw = attrs.get('bw', 0)
            tx_bps = attrs.get('tx_bps', 0)
            load_ratio = tx_bps / bw if bw > 0 else 0

            link_key = (src_dpid, dst_dpid)

            if load_ratio > high_threshold:
                # 增加连续高负载计数
                self.link_high_load_count[link_key] = self.link_high_load_count.get(link_key, 0) + 1
                self.logger.debug(f"[Link Load] {self.sw_name(src_dpid)}->{self.sw_name(dst_dpid)} load={load_ratio:.2%}, count={self.link_high_load_count[link_key]}")
            else:
                # 负载恢复正常，重置计数
                self.link_high_load_count[link_key] = 0

            # 如果连续三次超过80%且该链路在组播路径中，标记组播组为高负载
            if self.link_high_load_count.get(link_key, 0) >= 3:
                self._mark_multicast_high_load(src_dpid, dst_dpid)

    def _mark_multicast_high_load(self, src_dpid, dst_dpid):
        """
        如果链路在组播路径中，标记对应组播组为高负载
        但如果组播组的负载占链路带宽的比例超过阈值（0.8），则不标记为高负载
        如果链路任意一端节点是不可替代节点（度=1），代表这条链路也为不可替代链路，则组播组也不标记为高负载
        """
        high_threshold = 0.8

        # 检查链路两端是否都是非临界节点
        if src_dpid in self.critical_nodes or dst_dpid in self.critical_nodes:
            self.logger.debug(f"[Multicast High Load] Skipping link {self.sw_name(src_dpid)}->{self.sw_name(dst_dpid)}: "
                             f"one or both nodes are critical (degree=1)")
            return

        for multicast_addr, flow_info in self.multicast_active_flows.items():
            path = flow_info.get('path', [])
            # 检查 (src_dpid, dst_dpid) 或 (dst_dpid, src_dpid) 是否在路径中
            if (src_dpid, dst_dpid) in path or (dst_dpid, src_dpid) in path:
                # 获取组播组的负载和链路带宽
                mcast_load = self.multicast_load.get(multicast_addr, {}).get('load', 0)
                # 获取链路带宽
                if self.graph.has_edge(src_dpid, dst_dpid):
                    link_bw = self.graph[src_dpid][dst_dpid].get('bw', 0)
                elif self.graph.has_edge(dst_dpid, src_dpid):
                    link_bw = self.graph[dst_dpid][src_dpid].get('bw', 0)
                else:
                    link_bw = 0

                if link_bw > 0:
                    load_ratio = mcast_load / link_bw
                    if load_ratio > high_threshold:
                        self.logger.debug(f"[Multicast High Load] Group {multicast_addr} NOT marked as high load "
                                         f"(mcast_load={mcast_load:.2f}Mbps, link_bw={link_bw:.2f}Mbps, ratio={load_ratio:.2%} > {high_threshold:.2%})")
                        continue

                self.multicast_high_load[multicast_addr] = True
                self.logger.info(f"[Multicast High Load] Group {multicast_addr} marked as high load "
                                 f"(link {self.sw_name(src_dpid)}->{self.sw_name(dst_dpid)}, "
                                 f"mcast_load={mcast_load:.2f}Mbps, link_bw={link_bw:.2f}Mbps)")


    def _update_affected_flows(self, flow_key):
        flow_info = self.active_flows[flow_key]

        if not flow_info:
            self.logger.info(f"[ERROR] {flow_key} 为空")
            return

        if flow_info.get('type') == 'flow':
            src_ip, dst_ip = flow_key
            self.logger.info(f"[Route] update flow: {src_ip}->{dst_ip}")
            src_host, dst_host = flow_info['src_host'], flow_info['dst_host']
            self._delete_flow(src_ip, dst_ip)

            # 重新计算路径
            new_path = self.get_shortest_path(src_host['dpid'], dst_host['dpid'])
            if not new_path or len(new_path) < 2:
                self.logger.warning(f"[ERROR] 重新计算路径失败，删除流 {src_ip} -> {dst_ip}")
                del self.active_flows[flow_key]
                return

            self.logger.info(f"[Route] new path: {' -> '.join(self.switch_name.get(f'{p:016x}', f'{p}') for p in new_path)}")
            path_switches = self.get_path_switches(new_path, src_host['dpid'], src_host['port'], dst_host['dpid'], dst_host['port'])
            self._add_path_flow(src_ip, dst_ip, path_switches)
            # 更新活跃流信息
            self.active_flows[flow_key] = {
                'type': 'flow',
                'path': new_path,
                'path_switches': path_switches,
                'src_host': src_host,
                'dst_host': dst_host
            }
            self.logger.info(f"[Route] 流表更新完成: {src_ip} -> {dst_ip}")

        if flow_info.get('type') == 'qos':
            # QoS 规则的重新计算
            rule_id = flow_key
            rule = flow_info.get('rule')
            # 判空必须前置到解引用之前，且命中后立即 return。
            # 原写法先解引用 rule 再判空：rule 为 None 时直接 TypeError，
            # 把整条流的重算带崩；而判空分支又没有 return，会继续往下执行。
            if not rule or rule_id is None:
                self.logger.info(f"[ERROR] {rule_id} 为空或规则缺失，跳过重算")
                return

            self.logger.info(f"[QoS] update qos: {rule['name']}")
            self.delete_qos_flow(rule)

            src_ip, dst_ip = rule['src_ip'], rule['dst_ip']
            guarantee = rule['guarantee']
            src_dpid = self.host_ip_to_dpid.get(src_ip)
            dst_dpid = self.host_ip_to_dpid.get(dst_ip)

            if not src_dpid or not dst_dpid:
                self.logger.warning(f"[ERROR] IP {src_ip}/{src_ip} not find")
                return

            pref_map = {
                'best_effort': None,                      # 跳数最短（Dijkstra）
                'bandwidth': WeightPref.Bandwidth,        # 带宽最大（Dijkstra）
                'latency': WeightPref.Latency,            # 时延最低（Dijkstra）
                'loss': WeightPref.LossRate,              # 丢包率最低（Dijkstra）
                'bandwidth_guarantee': WeightPref.Bandwidth,  # 带宽保障（需配置 target_bps）
                'load_balance': WeightPref.LoadBalance,   # 负载均衡
                'smart_algorithm': WeightPref.SmartAlgorithm  # 多目标优化
            }
            preference = pref_map.get(guarantee)
            new_path = self.get_shortest_path(src_dpid, dst_dpid, preference)

            if not new_path or len(new_path) < 2:
                self.logger.warning(f"[QoS ERROR] {rule.get('name')}重新计算路径失败")
                return

            rule['path'] = new_path
            src_host = self.hosts.get(self.ip_to_mac.get(src_ip), {})
            dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip), {})
            path_switches = self.get_path_switches(
                new_path,
                src_host.get('dpid', new_path[0]),
                src_host.get('port', 1),
                dst_host.get('dpid', new_path[-1]),
                dst_host.get('port', 1)
            )
            rule['path_switches'] = path_switches

            # 更新 qos_rules active_flows 中的记录
            self.qos_rules[rule_id] = rule
            self.active_flows[rule_id] = {
                'type': 'qos',
                'path': new_path,
                'path_switches': path_switches,
                'rule': rule,
                'src_host': src_host,
                'dst_host': dst_host,
            }
            self.install_qos_flow(rule)

    def _recalculate_affected_flows(self):
        """重新计算受拓扑变化影响的流"""
        for flow_key, flow_info in list(self.active_flows.items()):
            path = flow_info['path']
            need_recalc = False

            # 检查路径上的交换机是否都还在
            for dpid in path:
                if dpid not in self.datapaths:
                    need_recalc = True
                    break

            # 检查路径上的链路是否都还在
            if not need_recalc:
                for i in range(len(path) - 1):
                    src_sw = path[i]
                    dst_sw = path[i + 1]
                    if not self.graph.has_edge(src_sw, dst_sw):
                        need_recalc = True
                        break

            if need_recalc:
                self._update_affected_flows(flow_key)

    def check_better_path_for_active_flows(self):
        """检查所有活跃流是否有更优路径可走"""
        for flow_key, flow_info in list(self.active_flows.items()):
            is_qos_flow = flow_info.get('type') == 'qos'
            if is_qos_flow:
                rule = flow_info.get('rule')
                rule_id = flow_key
                src_ip, dst_ip = rule.get('src_ip'), rule.get('dst_ip')
                src_host = self.hosts.get(self.ip_to_mac.get(src_ip), {})
                dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip), {})
                pref_map = {
                    'best_effort': None,                      # 跳数最短（Dijkstra）
                    'bandwidth': WeightPref.Bandwidth,        # 带宽最大（Dijkstra）
                    'latency': WeightPref.Latency,            # 时延最低（Dijkstra）
                    'loss': WeightPref.LossRate,              # 丢包率最低（Dijkstra）
                    'bandwidth_guarantee': WeightPref.Bandwidth,  # 带宽保障（需配置 target_bps）
                    'load_balance': WeightPref.LoadBalance,   # 负载均衡
                    'smart_algorithm': WeightPref.SmartAlgorithm  # 多目标优化
                }
                guarantee = rule.get('guarantee')
                preference = pref_map.get(guarantee)

            else:
                src_ip, dst_ip = flow_key
                src_host = flow_info['src_host']
                dst_host = flow_info['dst_host']
                preference = None

            current_path = flow_info['path']

            # 检查源和目的主机是否可达
            if src_host['dpid'] not in self.datapaths or dst_host['dpid'] not in self.datapaths:
                continue

            # 计算新最优路径
            new_path = self.get_shortest_path(src_host['dpid'], dst_host['dpid'], preference)

            if not new_path or len(new_path) < 2:
                continue
            # 如果路径相同，跳过
            if new_path == current_path:
                continue

            if is_qos_flow:
                self.logger.info(f"[QoS] {rule['name']} 发现更优路径: {src_ip} -> {dst_ip}")
                self.delete_qos_flow(rule)

                src_host = self.hosts.get(self.ip_to_mac.get(src_ip))
                dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip))
                path_switches = self.get_path_switches(new_path, src_host['dpid'], src_host['port'],  dst_host['dpid'], dst_host['port'])
                rule['path'] = new_path
                rule['path_switches'] = path_switches

                self.qos_rules[rule_id] = rule
                self.active_flows[rule_id] = {
                    'type': 'qos',
                    'path': new_path,
                    'path_switches': path_switches,
                    'rule': rule,
                    'src_host': src_host,
                    'dst_host': dst_host,
                }
                self.install_qos_flow(rule)
            else:
                self.logger.info(f"[Route] 发现更优路径: {src_ip} -> {dst_ip}")
                self.logger.info(f"[Route] 新路径: {' -> '.join(self.sw_name(p) for p in new_path)}")

                # 删除旧流表
                self._delete_flow(src_ip, dst_ip)
                # 构建新路径的 path_switches
                path_switches = self.get_path_switches(new_path, src_host['dpid'], src_host['port'], dst_host['dpid'], dst_host['port'])

                # 下发新流表
                self._add_path_flow(src_ip, dst_ip, path_switches)

                # 更新活跃流信息
                self.active_flows[flow_key] = {
                    'type': 'flow',
                    'path': new_path,
                    'path_switches': path_switches,
                    'src_host': src_host,
                    'dst_host': dst_host
                }
                self.logger.info(f"[Route] 流表更新完成: {src_ip} -> {dst_ip}")

    def _recalculate_affected_multicast_flows(self, deleted_link=None):
        """重新计算受拓扑变化影响的组播流"""
        affected_groups = []

        for multicast_addr, flow_info in list(self.multicast_active_flows.items()):
            path_edges = flow_info.get('path', [])
            need_recalc = False

            # 检查路径上的链路是否都还在
            if deleted_link:
                # 如果指定了删除的链路，检查该链路是否在组播树路径中
                src_dpid, dst_dpid = deleted_link
                for (u, v) in path_edges:
                    if (u == src_dpid and v == dst_dpid) or (u == dst_dpid and v == src_dpid):
                        need_recalc = True
                        break
            else:
                # 如果没有指定链路，检查所有边是否都在图中
                for (u, v) in path_edges:
                    if not self.graph.has_edge(u, v):
                        need_recalc = True
                        break

            if need_recalc:
                affected_groups.append(multicast_addr)

        if affected_groups:
            self.logger.info(f"[Multicast Route] 需要重新计算的组播流: {len(affected_groups)}")

        # 重新计算每条受影响的组播流
        for multicast_addr in affected_groups:
            # 检查源和成员是否还可达
            if multicast_addr not in self.multicast_sources:
                self.logger.warning(f"[Multicast Route] 组播组 {multicast_addr} 源 IP 不可达，删除")
                self._delete_multicast_flows(multicast_addr)
                if multicast_addr in self.multicast_active_flows:
                    del self.multicast_active_flows[multicast_addr]
                continue

            if multicast_addr not in self.multicast_groups or not self.multicast_groups[multicast_addr]:
                self.logger.warning(f"[Multicast Route] 组播组 {multicast_addr} 无成员，删除")
                self._delete_multicast_flows(multicast_addr)
                if multicast_addr in self.multicast_active_flows:
                    del self.multicast_active_flows[multicast_addr]
                continue

            # 删除旧流表
            self._delete_multicast_flows(multicast_addr)
            self.multicast_flow_installed.discard(multicast_addr)

            # 重新安装流规则
            success = self._install_multicast_flow_rules(multicast_addr)
            if success:
                self.logger.info(f"[Multicast Route] 组播流重新下发完成: {multicast_addr}")
                # 组播组之间间隔5秒
                # time.sleep(5)
            else:
                self.logger.warning(f"[Multicast Route] 组播流重新下发失败: {multicast_addr}")
                if multicast_addr in self.multicast_active_flows:
                    del self.multicast_active_flows[multicast_addr]

    def _lookup_path_switches(self, flow_key):
        """取已下发流的 path_switches，缓存不存在或路径上有交换机已离线时返回 None"""
        cached = self.active_flows.get(flow_key)
        if not cached or cached.get('type') != 'flow':
            return None
        path_switches = cached.get('path_switches')
        if not path_switches:
            return None
        if any(ps['dpid'] not in self.datapaths for ps in path_switches):
            return None
        return path_switches

    # ==================== SLA / 确定性保障 ====================
    # 一条 QoS 规则可以带三项数值目标，全部可选：
    #   target_bps        承诺速率（Mbps），落到 OFPMeterMod 限速 + 实测校验
    #   target_delay_ms   时延上限（ms），落到选路约束 + netem 施加
    #   target_loss_pct   丢包上限（%），落到选路约束 + netem 施加
    # 什么都没有时行为与改造前完全一致（纯选路），不影响旧调用方。

    def _rule_sla(self, rule):
        """取规则的 SLA 约束目标（全部转成 float，缺省 None）

        **只包含约束**：这三项决定「能不能选这条路径」，不会改变链路本身。
        过去的实现把 target_delay_ms 同时当成 netem 施加值，相当于「承诺
        时延不超过 20ms，然后主动给它加 20ms」——语义自相矛盾。损伤注入
        已拆到 `_rule_injection`。
        """
        return {
            'target_bps': self._positive_num(rule, 'target_bps'),
            'target_delay_ms': self._positive_num(rule, 'target_delay_ms'),
            'target_loss_pct': self._positive_num(rule, 'target_loss_pct'),
        }

    def _rule_injection(self, rule):
        """取规则的损伤注入参数（与 SLA 约束完全独立）

        inject_* 是**测试/演示用**的显式动作：把指定的时延/丢包施加到本规则
        路径的首跳接口上，用来构造「这条路径不满足 SLA」的场景，供其它规则
        的选路避让。没有这两个字段时控制器不会碰任何链路的 tc 配置。
        """
        return {
            'inject_delay_ms': self._positive_num(rule, 'inject_delay_ms'),
            'inject_loss_pct': self._positive_num(rule, 'inject_loss_pct'),
        }

    @staticmethod
    def _positive_num(rule, key):
        """取正值字段 → float 或 None（0/负数/非数字一律视为未设置）"""
        value = rule.get(key)
        if value is None or value == '':
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    def _sla_block_reason(self, attrs, sla):
        """判断一条链路是否违反 SLA 约束 → 违反原因字符串，None 表示通过。

        **只用实测值判断，不用配置值**：
        - 带宽用 oneway_bps（OpenFlow 端口统计差分，实测）
        - 丢包用 loss_measured（tc -s 差分，实测）
        - 时延没有被动实测手段，只能退到 netem 配置值 delay——这一点在
          返回的原因里标明 `(configured)`，避免被当成实测结果引用。

        没有任何 SLA 目标时恒返回 None，即「不改变原有选路行为」。
        """
        if all(v is None for v in sla.values()):
            return None

        if sla['target_delay_ms'] is not None:
            d = attrs.get('delay') or 0
            if d > sla['target_delay_ms']:
                return f"delay {d:.1f}ms (configured) > {sla['target_delay_ms']:.1f}ms"

        if sla['target_loss_pct'] is not None:
            lm = attrs.get('loss_measured')
            if lm is not None and lm > sla['target_loss_pct']:
                return f"loss {lm:.3f}% (measured) > {sla['target_loss_pct']:.3f}%"

        if sla['target_bps'] is not None:
            bw = attrs.get('bw') or 0
            used = attrs.get('oneway_bps') or 0
            # 额定带宽未知（bw=0）时不做判断：宁可不拦，也不要凭猜测拒绝一条
            # 可能是唯一连通的路径
            if bw > 0:
                free = bw - used
                if free < sla['target_bps']:
                    return (f"free bw {free:.1f}Mbps < target "
                            f"{sla['target_bps']:.1f}Mbps")
        return None

    def get_sla_path(self, src, dst, sla):
        """带 SLA 约束的选路 → (path, reason)。

        先按约束剪掉**已知会违反**的边再求最短路；若剪完不可达，退回
        不剪边的原始最短路并在 reason 里说明「无满足约束的路径，已退回
        尽力而为」。这样规则总能创建成功（不会因为瞬时指标抖动导致建
        规则失败），但调用方能从 reason 看到这次有没有真的满足承诺。
        """
        if src == dst:
            return [src], None
        if src not in self.graph or dst not in self.graph:
            return [], 'source or destination not in graph'
        if all(v is None for v in sla.values()):
            return self.get_shortest_path(src, dst), None

        # 用边过滤器剪枝：违反任一项 SLA 的边剔除。
        # 注意 networkx 的 filter_edge 签名为 `(u, v)` —— **只有两个参数**，
        # 边属性要从宿主图 G[u][v] 里取。写成 `(u, v, attrs)` 不是「更啰嗦」
        # 而是直接 TypeError（networkx 3.1 实测），而 subgraph_view 异常又
        # 容易被外层的宽泛 except 吞掉，表现为「约束静默失效、总是退回尽力
        # 而为」，比不实现还难查。
        graph = self.graph

        def _ok(u, v):
            return self._sla_block_reason(graph[u][v], sla) is None

        try:
            sub = nx.subgraph_view(graph, filter_edge=_ok)
            if src in sub and dst in sub and nx.has_path(sub, src, dst):
                return list(nx.shortest_path(sub, src, dst)), None
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            pass
        except Exception as e:
            # 只有真正的意外才走到这里；把类型带上，避免被当成普通的「无路径」
            self.logger.warning(f"[SLA] Constraint filtering failed "
                                f"({type(e).__name__}): {e}")

        path = self.get_shortest_path(src, dst)
        return path, 'no path satisfies SLA constraints, fell back to best-effort'

    def _rule_meter_id(self, rule):
        """规则 → 稳定的 meter_id（同一规则重装流表时复用同一个 meter）

        取值范围 1..0xFEFFFF（OFPMeterMod 的 meter_id 是 32 位，OFPM_MAX
        之后留给虚拟端口，这里只用低段）。rule['id'] 从 1 开始，直接复用。
        """
        rid = rule.get('id')
        if not isinstance(rid, int) or rid <= 0:
            return None
        return rid

    def _install_meter(self, datapath, meter_id, rate_kbps, burst_kbits=0):
        """在交换机上下发一个 drop 型 meter（限速）→ ok

        用 `OFPMF_KBPS` 单位而不是 `OFPMF_PKTPS`：SLA 承诺的是字节速率。
        `command=OFPMC_ADD`；若该 id 已存在（重装同一条规则）先删再加，
        避免 "meter already exists" 被交换机静默丢掉——那样限速就失效了，
        而流表看起来依然正常。
        """
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        # 先删旧 meter（不存在时 OVS 返回错误，忽略即可）
        del_mod = parser.OFPMeterMod(
            datapath=datapath,
            command=ofproto.OFPMC_DELETE,
            flags=ofproto.OFPMF_KBPS,
            meter_id=meter_id,
            bands=[],
        )
        datapath.send_msg(del_mod)

        bands = [parser.OFPMeterBandDrop(rate=int(rate_kbps),
                                         burst_size=int(burst_kbits))]
        mod = parser.OFPMeterMod(
            datapath=datapath,
            command=ofproto.OFPMC_ADD,
            flags=ofproto.OFPMF_KBPS,
            meter_id=meter_id,
            bands=bands,
        )
        datapath.send_msg(mod)
        return True

    def _delete_meter(self, datapath, meter_id):
        """删除交换机上的 meter → ok"""
        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto
        mod = parser.OFPMeterMod(
            datapath=datapath,
            command=ofproto.OFPMC_DELETE,
            flags=ofproto.OFPMF_KBPS,
            meter_id=meter_id,
            bands=[],
        )
        datapath.send_msg(mod)
        return True

    def _install_netem_for_rule(self, rule, injection):
        """按规则的 inject_* 把时延/丢包施加到路径首跳接口 → [(dev, ok, err)]

        参数是 `_rule_injection()` 的结果，**不是 SLA 约束**。只有显式配置了
        inject_delay_ms / inject_loss_pct 的规则才会改动链路，普通业务规则
        不会因为承诺了某个时延就被加上相应延迟。

        只施加在首跳出接口，不是全路径——避免「注入 20ms 实际 20ms × 跳数」。
        带宽不在这里做：带宽靠 meter 限速，netem 没有 rate 能力。

        dev 的取法：路径首跳交换机 DPID + 出端口 → `<sw名>-eth<port>`，
        再用 utils.find_injector_device 找到真正可挂 qdisc 的那一端。
        """
        results = []
        if not injection:
            return results
        delay = injection.get('inject_delay_ms')
        loss = injection.get('inject_loss_pct')
        if delay is None and loss is None:
            return results

        path_switches = rule.get('path_switches') or []
        if not path_switches:
            return results

        ps = path_switches[0]
        dev = f'{self.sw_name(ps["dpid"])}-eth{ps["out_port"]}'
        target = utils.find_injector_device(dev)
        if target is None:
            results.append((dev, False, 'no tc-capable device found'))
            return results

        ok, err = utils.set_netem(target, delay_ms=delay, loss_pct=loss)
        if ok:
            # netem 改了就必须让 tc 采样立刻重读，否则指标要等一个刷新周期
            # （默认 3s）才反映这次写入，看起来像「没生效」
            self._link_metrics_prev.pop(target, None)
        results.append((target, ok, err))
        return results

    def _remove_netem_for_rule(self, rule):
        """规则删除/更新时清掉它施加过的 netem → [(dev, ok, err)]"""
        out = []
        for dev in (rule.get('netem_devs') or []):
            ok, err = utils.clear_netem(dev)
            if ok:
                self._link_metrics_prev.pop(dev, None)
            out.append((dev, ok, err))
        return out

    def _add_path_flow(self, src_ip, dst_ip, path_switches):
        # 路径流表
        priority = 10
        idle_timeout = 300
        for ps in path_switches:
            sw_dp = self.datapaths.get(ps['dpid'])
            if sw_dp:
                parser = sw_dp.ofproto_parser
                match = parser.OFPMatch(in_port=ps['in_port'], eth_type=ether.ETH_TYPE_IP,
                                        ipv4_src=src_ip, ipv4_dst=dst_ip)
                actions = [parser.OFPActionOutput(ps['out_port'])]
                self.add_flow(sw_dp, priority, match, actions, idle_timeout=idle_timeout)

                # 反向流表
                match = parser.OFPMatch(in_port=ps['out_port'], eth_type=ether.ETH_TYPE_IP,
                                        ipv4_src=dst_ip, ipv4_dst=src_ip)
                actions = [parser.OFPActionOutput(ps['in_port'])]
                self.add_flow(sw_dp, priority, match, actions, idle_timeout=idle_timeout)

    def _delete_flow(self, src_ip, dst_ip):
        """删除指定流的流表

        删除请求的 match 必须是目标表项 match 字段的**子集**（含相同取值）。
        `_add_path_flow` 装表时没有指定 ip_dscp（该字段在表项里是通配），
        若删除请求反而带上 `ip_dscp=0`，按 OpenFlow 1.3 语义该请求匹配不上
        通配的表项 —— 删除会静默落空，旧表项残留到 idle_timeout 过期，
        期间与新装的重路由表项并存，流量可能仍走旧路径。

        所以这里的 match 必须与安装时的字段集保持一致（不含 ip_dscp）。
        """
        flow_info = self.active_flows.get((src_ip, dst_ip))
        if not flow_info:
            return

        for ps in flow_info['path_switches']:
            sw_dp = self.datapaths.get(ps['dpid'])
            if sw_dp:
                parser = sw_dp.ofproto_parser
                ofproto = sw_dp.ofproto
                # 删除正向流
                match = parser.OFPMatch(eth_type=ether.ETH_TYPE_IP, ipv4_src=src_ip, ipv4_dst=dst_ip)
                del_flow = parser.OFPFlowMod(
                    datapath=sw_dp,
                    command=ofproto.OFPFC_DELETE,
                    table_id=0,
                    priority=0,
                    out_port=ofproto.OFPP_ANY,
                    out_group=ofproto.OFPG_ANY,
                    match=match,
                )
                sw_dp.send_msg(del_flow)
                # 删除反向流
                match = parser.OFPMatch(eth_type=ether.ETH_TYPE_IP, ipv4_src=dst_ip, ipv4_dst=src_ip)
                del_flow = parser.OFPFlowMod(
                    datapath=sw_dp,
                    command=ofproto.OFPFC_DELETE,
                    table_id=0,
                    priority=0,
                    out_port=ofproto.OFPP_ANY,
                    out_group=ofproto.OFPG_ANY,
                    match=match,
                )
                sw_dp.send_msg(del_flow)

    def install_qos_flow(self, rule):
        """为 QoS 规则安装流表
        rule: {name, src_ip, dst_ip, protocol, src_port, dst_port, dscp, priority,
               guarantee, src_dpid, dst_dpid, path, path_switches}

        DSCP 在两类交换机上扮演**不同角色**，不能一律写进 match：

        - **首跳（打标点）**：正向是 path_switches[0]（贴着源主机），反向是
          path_switches[-1]（贴着目的主机）。这一跳的 match **不含 ip_dscp**，
          动作里先用 `OFPActionSetField(ip_dscp=...)` 打标再转发。
        - **其余跳（认标点）**：报文已被上游打好标，match **带 ip_dscp**，
          只认这条标记流，避免同五元组的其它业务被误送进 QoS 路径。

        为什么必须这样分：业务主机的原始报文 DSCP 通常为 0。若首跳也按
        `ip_dscp` 匹配，规则就只能命中「进入本机前已自带该标记」的流量——
        而正常情况下没人会先打标，等于规则永远匹配不到真实业务流（表项
        装得上，但计数器一直为 0，且不会报任何错）。

        dscp=0 表示不打标，此时全路径 match 与动作都与改动前完全一致。
        """
        src_ip = rule['src_ip']
        dst_ip = rule['dst_ip']
        protocol = rule.get('protocol')
        src_port = rule.get('src_port')
        dst_port = rule.get('dst_port')
        dscp = rule.get('dscp', 0)
        priority = rule.get('priority', 100)
        path = rule.get('path')
        path_switches = rule.get('path_switches')

        if not path_switches:
            self.logger.warning(f"[QoS] No path_switches for rule {rule.get('name')}")
            return False

        def _build_match(in_port, ip_src, ip_dst, sport, dport):
            """按协议填 L4 字段：sport 属于 ip_src 一侧，dport 属于 ip_dst 一侧"""
            kwargs = {
                'in_port': in_port,
                'eth_type': ether.ETH_TYPE_IP,
                'ipv4_src': ip_src,
                'ipv4_dst': ip_dst,
            }
            if protocol == 'tcp':
                kwargs['ip_proto'] = 6
                if dport:
                    kwargs['tcp_dst'] = dport
                if sport:
                    kwargs['tcp_src'] = sport
            elif protocol == 'udp':
                kwargs['ip_proto'] = 17
                if dport:
                    kwargs['udp_dst'] = dport
                if sport:
                    kwargs['udp_src'] = sport
            elif protocol == 'icmp':
                kwargs['ip_proto'] = 1
            return kwargs

        last_idx = len(path_switches) - 1
        marking_hops = 0

        # ---------- SLA：限速 meter（仅首跳，贴着源） ----------
        # 只在源侧交换机限速就够了：这是业务进入网络的入口点，在入口把
        # 超额的包丢掉，下游链路自然不会被这条业务挤爆。中间每跳都限速
        # 反而会重复计数（同一条流被同速率截断多次无意义，还多占 meter）。
        sla = self._rule_sla(rule)
        meter_id = None
        meter_ok = False
        if sla['target_bps']:
            meter_id = self._rule_meter_id(rule)
            src_dp = self.datapaths.get(path_switches[0]['dpid'])
            if meter_id and src_dp is not None:
                # target_bps 是 Mbps，meter 的 OFPMF_KBPS 单位是 kbps
                rate_kbps = int(sla['target_bps'] * 1000)
                burst_kbits = max(1, int(rate_kbps))  # 约 1 秒的突发量
                meter_ok = self._install_meter(src_dp, meter_id, rate_kbps, burst_kbits)
                self.logger.info(
                    f"[SLA] meter {meter_id} on {self.sw_name(path_switches[0]['dpid'])}: "
                    f"{rate_kbps} kbps ({sla['target_bps']:.1f} Mbps) for rule {rule.get('name')}")

        for idx, ps in enumerate(path_switches):
            sw_dp = self.datapaths.get(ps['dpid'])
            if not sw_dp:
                continue

            parser = sw_dp.ofproto_parser

            # ---------- 正向：src -> dst ----------
            # 只有贴近源主机的那一跳负责打标，其余跳只认标
            forward_marking = bool(dscp) and idx == 0
            match_kwargs = _build_match(ps['in_port'], src_ip, dst_ip, src_port, dst_port)
            if dscp and not forward_marking:
                match_kwargs['ip_dscp'] = dscp

            actions = []
            if forward_marking:
                actions.append(parser.OFPActionSetField(ip_dscp=dscp))
            actions.append(parser.OFPActionOutput(ps['out_port']))
            match = parser.OFPMatch(**match_kwargs)
            # 首跳且 meter 装成功时，把 meter 指令挂在正向流上
            if meter_ok and idx == 0:
                self._add_flow_with_meter(sw_dp, priority, match, actions, meter_id)
            else:
                self.add_flow(sw_dp, priority, match, actions)

            # ---------- 反向：dst -> src ----------
            # 反向的首跳是最靠近目的主机（即正向末跳）的那台交换机
            reverse_marking = bool(dscp) and idx == last_idx
            reverse_match_kwargs = _build_match(ps['out_port'], dst_ip, src_ip, dst_port, src_port)
            if dscp and not reverse_marking:
                reverse_match_kwargs['ip_dscp'] = dscp

            reverse_actions = []
            if reverse_marking:
                reverse_actions.append(parser.OFPActionSetField(ip_dscp=dscp))
            reverse_actions.append(parser.OFPActionOutput(ps['in_port']))
            self.add_flow(sw_dp, priority, parser.OFPMatch(**reverse_match_kwargs), reverse_actions)

            marking_hops += int(forward_marking) + int(reverse_marking)

        # ---------- 损伤注入（与 SLA 约束无关，仅 inject_* 触发） ----------
        injection = self._rule_injection(rule)
        netem_results = self._install_netem_for_rule(rule, injection)
        # 记下来，规则删除/更新时好清理
        rule['netem_devs'] = [dev for dev, ok, _ in netem_results if ok]
        rule['meter_id'] = meter_id if meter_ok else None
        for dev, ok, err in netem_results:
            if ok:
                self.logger.info(f"[Inject] netem applied on {dev}: "
                                 f"delay={injection.get('inject_delay_ms')}ms "
                                 f"loss={injection.get('inject_loss_pct')}%")
            else:
                self.logger.warning(f"[Inject] netem FAILED on {dev}: {err}")

        self.logger.info(f"[QoS] Installed flow for rule: {rule.get('name')} ({src_ip} -> {dst_ip}, {protocol}, dscp={dscp}, priority={priority})")
        self.logger.info(f"[QoS] path: {' -> '.join(self.sw_name(p) for p in path)}")
        if dscp:
            # 打标点数量恒为 2（单交换机路径时为同一台设备的两个端口），
            # 少于此数说明路径首末跳的 datapath 缺失，报文不会被标记
            self.logger.info(f"[QoS] DSCP {dscp} marked at {marking_hops}/2 hop(s) "
                             f"(forward at {self.sw_name(path_switches[0]['dpid'])}, "
                             f"reverse at {self.sw_name(path_switches[-1]['dpid'])})")
        return True

    def _add_flow_with_meter(self, datapath, priority, match, actions, meter_id):
        """装表并在 instructions 里前置 OFPInstructionMeter

        OpenFlow 1.3 规定 meter 指令**必须排在 apply-actions 之前**：
        `instructions = [meter, apply_actions]`。顺序反了交换机返回
        OFPET_BAD_INSTRUCTION，流表装不上——而且这个错误是异步回来的，
        控制器的 send_msg 不会抛异常，只能靠交换机日志发现。
        所以这里显式构造，不复用 add_flow（它只写 apply_actions）。
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [
            parser.OFPInstructionMeter(meter_id=meter_id),
            parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions),
        ]
        mod = parser.OFPFlowMod(
            datapath=datapath, priority=priority, match=match,
            instructions=inst, idle_timeout=0, hard_timeout=0,
            flags=ofproto.OFPFF_SEND_FLOW_REM,
        )
        datapath.send_msg(mod)

    def delete_qos_flow(self, rule):
        """删除 QoS 规则的流表

        删除请求的 match **不能带 ip_dscp**，即便规则配了 dscp 也一样。
        OpenFlow 1.3 的 OFPFC_DELETE 语义是「请求里的 match 字段 ⊆ 表项的
        match 字段」才命中，即请求越通配删得越多。而 install_qos_flow 装下的
        表项分两类：打标点（首跳）match 不含 ip_dscp、认得点 match 含 ip_dscp。
        带上 ip_dscp 去删只能删掉后者，打标点那两条会静默残留到 idle_timeout
        （QoS 规则表项 idle_timeout=0，永不过期），继续为该五元组分流并
        覆盖后续重新下发的路径 —— 表现为「删了规则流量还在老路径上」。

        用通配（不含 ip_dscp）即可同时覆盖两类表项，是唯一安全的选择。
        """
        src_ip = rule['src_ip']
        dst_ip = rule['dst_ip']
        protocol = rule.get('protocol', 'any')
        src_port = rule.get('src_port')
        dst_port = rule.get('dst_port')
        path_switches = rule.get('path_switches', [])

        for ps in path_switches:
            sw_dp = self.datapaths.get(ps['dpid'])
            if not sw_dp:
                continue

            parser = sw_dp.ofproto_parser
            ofproto = sw_dp.ofproto

            # 构建匹配字段（不带 in_port / ip_dscp，见上方说明）
            match_kwargs = {
                'eth_type': ether.ETH_TYPE_IP,
                'ipv4_src': src_ip,
                'ipv4_dst': dst_ip,
            }

            if protocol == 'tcp':
                match_kwargs['ip_proto'] = 6
                if dst_port:
                    match_kwargs['tcp_dst'] = dst_port
                if src_port:
                    match_kwargs['tcp_src'] = src_port
            elif protocol == 'udp':
                match_kwargs['ip_proto'] = 17
                if dst_port:
                    match_kwargs['udp_dst'] = dst_port
                if src_port:
                    match_kwargs['udp_src'] = src_port
            elif protocol == 'icmp':
                match_kwargs['ip_proto'] = 1

            match = parser.OFPMatch(**match_kwargs)
            del_flow = parser.OFPFlowMod(
                datapath=sw_dp,
                command=ofproto.OFPFC_DELETE,
                table_id=0,
                priority=0,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=match,
            )
            sw_dp.send_msg(del_flow)

            # 删除反向流
            reverse_match_kwargs = {
                'eth_type': ether.ETH_TYPE_IP,
                'ipv4_src': dst_ip,
                'ipv4_dst': src_ip,
            }
            if protocol == 'tcp':
                reverse_match_kwargs['ip_proto'] = 6
                if dst_port:
                    reverse_match_kwargs['tcp_src'] = dst_port
                if src_port:
                    reverse_match_kwargs['tcp_dst'] = src_port
            elif protocol == 'udp':
                reverse_match_kwargs['ip_proto'] = 17
                if dst_port:
                    reverse_match_kwargs['udp_src'] = dst_port
                if src_port:
                    reverse_match_kwargs['udp_dst'] = src_port
            elif protocol == 'icmp':
                reverse_match_kwargs['ip_proto'] = 1

            reverse_match = parser.OFPMatch(**reverse_match_kwargs)
            del_flow = parser.OFPFlowMod(
                datapath=sw_dp,
                command=ofproto.OFPFC_DELETE,
                table_id=0,
                priority=0,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=reverse_match,
            )
            sw_dp.send_msg(del_flow)

        # ---------- SLA 清理 ----------
        # 先删 meter 再清 netem：meter 挂在首跳的流表上，流表已在上面的
        # 循环里删掉；meter 本身不会随流表消失，必须显式删，否则规则删除
        # 后该 meter_id 仍占着且在交换机里累积，重复创建会报 id 冲突。
        meter_id = rule.get('meter_id')
        if meter_id:
            src_sw = (rule.get('path_switches') or [{}])[0].get('dpid')
            src_dp = self.datapaths.get(src_sw) if src_sw is not None else None
            if src_dp is not None:
                self._delete_meter(src_dp, meter_id)
                self.logger.info(f"[SLA] meter {meter_id} removed on {self.sw_name(src_sw)}")

        for dev, ok, err in self._remove_netem_for_rule(rule):
            if ok:
                self.logger.info(f"[SLA] netem cleared on {dev}")
            else:
                self.logger.warning(f"[SLA] netem clear FAILED on {dev}: {err}")
        rule['meter_id'] = None
        rule['netem_devs'] = []

        self.logger.info(f"[QoS] Deleted flow for rule: {rule.get('name')} ({src_ip} -> {dst_ip})")

    def get_path_switches(self, path, src_dpid, src_port, dst_dpid, dst_port):
        # 构建路径上每个交换机的转发信息
        path_switches = []
        # 源交换机
        path_switches.append({
            'dpid': src_dpid,
            'in_port': src_port,
            'out_port': self.graph[path[0]][path[1]].get("src_port"),
        })
        # 中间交换机
        for i in range(1, len(path) - 1):
            pre_sw = path[i - 1]
            curr_sw = path[i]
            next_sw = path[i + 1]
            in_edge = self.graph[pre_sw][curr_sw]
            out_edge = self.graph[curr_sw][next_sw]
            path_switches.append({
                'dpid': curr_sw,
                'in_port': in_edge.get("dst_port"),
                'out_port': out_edge.get("src_port"),
            })
        # 目的交换机
        path_switches.append({
            'dpid': dst_dpid,
            'in_port': self.graph[path[-2]][path[-1]].get("dst_port"),
            'out_port': dst_port,
        })
        return path_switches

    # ==================== 交换机连接初始化 ====================
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 清空旧流表
        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE,
                                out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY)
        datapath.send_msg(mod)
        self.logger.info(f"[Switch Init] Init flows on Switch {self.sw_name(datapath.id)}")

        # 表项0：默认上送控制器
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions, idle_timeout=0)

        # 表项：默认丢弃所有 UDP 组播报文（优先级低于具体组播组规则）
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=("224.0.0.0", "240.0.0.0"))
        actions = []  # 空动作 = 丢弃
        self.add_flow(datapath, 1, match, actions, idle_timeout=0)

    # ==================== 拓扑事件回调 ====================
    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        try:
            self.logger.info(f"交换机上线: {self.sw_name(ev.switch.dp.id)} DPID={ev.switch.dp.id:016x}")
            with self._lock:
                self.datapaths[ev.switch.dp.id] = ev.switch.dp
            self.update_topology()
            # 为那些还没有安装组播流规则的组播组安装组播流规则
            for multicast_addr in list(self.multicast_groups.keys()):
                # 检查是否已安装过流规则
                if multicast_addr in self.multicast_flow_installed:
                    continue
                # 检查是否有源IP
                if multicast_addr not in self.multicast_sources:
                    continue
                # 检查是否有组成员
                if not self.multicast_groups[multicast_addr]:
                    continue
                # 安装组播流规则
                self._install_multicast_flow_rules(multicast_addr)
                # 每个组播组之间间隔 5 秒
                # time.sleep(5)
        except Exception as e:
            self.logger.info(f'Event SwitchEnter {self.sw_name(ev.switch.dp.id)} {e}')

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self.logger.info(f"交换机离线: {dpid} DPID={dpid}")
        with self._lock:
            self.datapaths.pop(dpid, None)
        self.update_topology()
        # 重新计算受影响的流
        self._recalculate_affected_flows()
        # 重新计算受影响的组播流
        self._recalculate_affected_multicast_flows()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        if not self.link_event_enabled:
            self.logger.debug(f"[Link Event] Link add ignored (disabled): {self.sw_name(ev.link.src.dpid)}:{ev.link.src.port_no} <-> {self.sw_name(ev.link.dst.dpid)}:{ev.link.dst.port_no}")
            return

        link_key = (ev.link.src.dpid, ev.link.src.port_no, ev.link.dst.dpid, ev.link.dst.port_no)

        # 如果链路之前触发过 pending delete，取消删除
        if link_key in self._link_delete_pending:
            timer = self._link_delete_pending.pop(link_key)
            self.logger.info(f"链路恢复，取消删除: {self.sw_name(ev.link.src.dpid)}:{ev.link.src.port_no} <-> "
                            f"{self.sw_name(ev.link.dst.dpid)}:{ev.link.dst.port_no}")
            return

        self.logger.info(f"新增链路: {self.sw_name(ev.link.src.dpid)}:{ev.link.src.port_no} <-> "
                         f"{self.sw_name(ev.link.dst.dpid)}:{ev.link.dst.port_no}")
        self.update_topology()
        # 检查是否有更优路径可更新
        self.check_better_path_for_active_flows()
        # 为那些还没有安装组播流规则的组播组安装组播流规则
        for multicast_addr in list(self.multicast_groups.keys()):
            # 检查是否已安装过流规则
            if multicast_addr in self.multicast_flow_installed:
                continue
            # 检查是否有源IP
            if multicast_addr not in self.multicast_sources:
                continue
            # 检查是否有组成员
            if not self.multicast_groups[multicast_addr]:
                continue
            # 安装组播流规则
            self._install_multicast_flow_rules(multicast_addr)
            # 每个组播组之间间隔 5 秒
            # time.sleep(5)

    def _execute_link_delete(self, src_dpid, src_port, dst_dpid, dst_port):
        """真正执行链路删除"""
        self.logger.info(f"删除链路: {self.sw_name(src_dpid)}:{src_port} <-> {self.sw_name(dst_dpid)}:{dst_port}")
        self.update_topology()
        self._recalculate_affected_flows()
        self._recalculate_affected_multicast_flows(deleted_link=(src_dpid, dst_dpid))

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        if not self.link_event_enabled:
            self.logger.debug(f"[Link Event] Link delete ignored (disabled): {self.sw_name(ev.link.src.dpid)}:{ev.link.src.port_no} <-> {self.sw_name(ev.link.dst.dpid)}:{ev.link.dst.port_no}")
            return

        link_key = (ev.link.src.dpid, ev.link.src.port_no, ev.link.dst.dpid, ev.link.dst.port_no)

        # 如果已经在 pending 状态，不再重复处理
        if link_key in self._link_delete_pending:
            return

        self.logger.warning(f"链路可能断开，等待确认: {self.sw_name(ev.link.src.dpid)}:{ev.link.src.port_no} <-> "
                           f"{self.sw_name(ev.link.dst.dpid)}:{ev.link.dst.port_no}")

        # 延迟删除，给链路恢复留出时间
        def delayed_delete():
            if link_key in self._link_delete_pending:
                del self._link_delete_pending[link_key]
                self._execute_link_delete(ev.link.src.dpid, ev.link.src.port_no,
                                         ev.link.dst.dpid, ev.link.dst.port_no)

        self._link_delete_pending[link_key] = hub.spawn_after(self._link_delete_delay, delayed_delete)

    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev):
        self.logger.info(f"主机加入: MAC={ev.host.mac}, IP={ev.host.ipv4}, "
                         f"Switch={self.sw_name(ev.host.port.dpid)}:{ev.host.port.port_no}")
        self.update_topology()

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """处理端口统计应答，计算链路负载"""
        msg = ev.msg
        dpid = msg.datapath.id
        now = time.time()

        self.port_stats[dpid] = {}

        for stat in msg.body:
            port_no = stat.port_no
            if port_no > ofproto_v1_3.OFPP_MAX:
                continue

            self.port_stats[dpid][port_no] = {
                'rx_bytes': stat.rx_bytes,
                'tx_bytes': stat.tx_bytes,
                'rx_packets': stat.rx_packets,
                'tx_packets': stat.tx_packets,
                'rx_errors': stat.rx_errors,
                'tx_errors': stat.tx_errors,
                'timestamp': now
            }

            # 计算与上次的差值和负载
            if dpid in self.port_stats_prev and port_no in self.port_stats_prev[dpid]:
                prev = self.port_stats_prev[dpid][port_no]
                time_delta = now - prev['timestamp']
                if time_delta > 0:
                    rx_bytes_delta = stat.rx_bytes - prev['rx_bytes']
                    tx_bytes_delta = stat.tx_bytes - prev['tx_bytes']
                    rx_bps = (rx_bytes_delta * 8) / time_delta / 1_000_000
                    tx_bps = (tx_bytes_delta * 8) / time_delta / 1_000_000
                    self.port_stats[dpid][port_no]['rx_bps'] = round(rx_bps, 2)
                    self.port_stats[dpid][port_no]['tx_bps'] = round(tx_bps, 2)
                    self.port_stats[dpid][port_no]['total_bps'] = round(rx_bps + tx_bps, 2)

        self.port_stats_prev[dpid] = self.port_stats[dpid]

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        """处理流表统计应答，计算组播组负载"""
        msg = ev.msg
        dpid = msg.datapath.id
        now = time.time()

        self.flow_stats[dpid] = {}

        for stat in msg.body:
            # 生成 match 的唯一标识
            match = stat.match
            ipv4_src = match.get('ipv4_src')
            ipv4_dst = match.get('ipv4_dst')
            if not ipv4_src or not ipv4_dst:
                continue
            flow_key = f"{ipv4_src}:{ipv4_dst}"

            self.flow_stats[dpid][flow_key] = {
                'byte_count': stat.byte_count,
                'packet_count': stat.packet_count,
                'timestamp': now
            }

            # 计算与上次的差值和负载
            if dpid in self.flow_stats_prev and flow_key in self.flow_stats_prev[dpid]:
                prev = self.flow_stats_prev[dpid][flow_key]
                time_delta = now - prev['timestamp']
                if time_delta > 0:
                    byte_delta = stat.byte_count - prev['byte_count']
                    load_mbps = (byte_delta * 8) / time_delta / 1_000_000

                    # 查找对应的组播组并记录负载
                    for multicast_addr, flow_info in self.multicast_active_flows.items():
                        if flow_info.get('source_ip') == ipv4_src:
                            self.multicast_load[multicast_addr] = {
                                'load': round(load_mbps, 2),
                                'timestamp': now
                            }
                            self.logger.debug(f"[Multicast Load] Group {multicast_addr}: {round(load_mbps, 2)} Mbps (flow {flow_key})")

        self.flow_stats_prev[dpid] = self.flow_stats[dpid]

    # ==================== 流表过期处理 ====================
    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed_handler(self, ev):
        """处理交换机流表因 idle_timeout 过期而被删除的事件"""
        try:
            msg = ev.msg
            datapath = msg.datapath
            ofproto = datapath.ofproto
            if msg.reason not in (ofproto.OFPRR_IDLE_TIMEOUT, ofproto.OFPRR_HARD_TIMEOUT):
                return

            dpid = datapath.id
            match = msg.match
            ipv4_src = match.get('ipv4_src')
            ipv4_dst = match.get('ipv4_dst')
            if not ipv4_src or not ipv4_dst:
                return

            flow_key = (ipv4_src, ipv4_dst)
            if flow_key in self.active_flows:
                del self.active_flows[flow_key]
                self.logger.info(f"[FlowRemoved] Flow expired on SW:{self.sw_name(dpid)}: {ipv4_src} -> {ipv4_dst}, removed from active_flows")
        except Exception as e:
            self.logger.info(f'Event OFPFlowRemoved {e}')

    # ==================== 核心：PacketIn 处理 ====================
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        # 忽略 LLDP / IPv6
        if eth.ethertype in (ether.ETH_TYPE_LLDP, ether.ETH_TYPE_IPV6):
            return

        # 更新 Prometheus packet-in 指标
        qos_switch_packet_in.labels(dpid=self.sw_name(datapath.id)).inc()

        arp_pkt = pkt.get_protocol(arp.arp)
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)

        # IGMP 处理
        if ipv4_pkt and ipv4_pkt.proto == 2:  # IGMP protocol number
            igmp_pkt = pkt.get_protocol(igmp.igmp)
            if igmp_pkt:
                self.handle_igmp(datapath, in_port, eth, ipv4_pkt, igmp_pkt)
                return

        # ARP 处理
        if arp_pkt:
            self.handle_arp(datapath, in_port, msg, eth, arp_pkt)
            return

        # IPv4 处理
        if ipv4_pkt:
            self.handle_ipv4(datapath, in_port, msg, eth, ipv4_pkt, pkt)
            return

    # ==================== ARP 处理 ====================
    def handle_arp(self, datapath, in_port, msg, eth_pkt, arp_pkt):
        dpid = datapath.id
        src_ip_arp, dst_ip_arp = arp_pkt.src_ip, arp_pkt.dst_ip
        src_eth, dst_eth = eth_pkt.src, eth_pkt.dst
        src_mac, dst_mac = arp_pkt.src_mac, arp_pkt.dst_mac
        arp_opcode = arp_pkt.opcode
        arp_htype = arp_pkt.hwtype
        arp_ptype = arp_pkt.proto

        arp_op_name = {1: "ARP Request", 2: "ARP Reply", 3: "RARP Request", 4: "RARP Reply"}.get(arp_opcode, f"ARP Opcode {arp_opcode}")

        # 只处理以太网 IPv4 ARP
        if arp_htype != 1 or arp_ptype != 0x0800:
            self.logger.debug(f"[PacketIn] 非以太网IPv4 ARP，忽略: HTYPE={arp_htype}, PTYPE={hex(arp_ptype)}")
            return
        self.logger.debug(f"[PacketIn] {self.sw_name(dpid)} {arp_op_name} ETH:{src_eth}-->{dst_eth} "
                          f"| ARP:{src_mac}({src_ip_arp})-->{dst_mac}({dst_ip_arp})")

        self._learn_host(dpid, in_port, src_eth, src_ip_arp, proto_tag="ARP")

        # ARP 请求
        if arp_opcode == arp.ARP_REQUEST:
            if dst_ip_arp in self.ip_to_mac:
                target_mac = self.ip_to_mac[dst_ip_arp]
                self.send_arp_reply(datapath, eth_pkt, src_mac, src_ip_arp, target_mac, dst_ip_arp, in_port)
                self.logger.info(f"[ARP Reply] {self.sw_name(dpid)} Reply {dst_ip_arp} is {target_mac}")
            else:
                self.flood_arp_to_all_switches(msg.data)
                self.logger.info(f"[ARP Flood] unknown IP {dst_ip_arp}")

        # ARP 响应
        elif arp_opcode == arp.ARP_REPLY:
            if dst_mac in self.hosts:
                target_dpid = self.hosts[dst_mac]['dpid']
                target_port = self.hosts[dst_mac]['port']
                if target_dpid in self.datapaths:
                    target_dp = self.datapaths[target_dpid]
                    self.send_packet_out(target_dp, target_port, msg.data)
                    self.logger.info(f"[ARP Forward] ARP Reply → Host {dst_mac} (SW:{target_dpid}, Port:{target_port})")
                else:
                    self.logger.warning(f"[ARP Forward] 目标交换机不存在: {target_dpid}")
            else:
                self.logger.warning(f"[ARP Forward] 未知目标主机: {dst_mac}，丢弃")

    # ==================== IPv4 报文处理 ====================
    def handle_ipv4(self, datapath, in_port, msg, eth, ipv4_pkt, pkt):
        """ 统一处理 IPv4 报文（TCP/UDP/ICMP） """
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        dpid = datapath.id
        sw_name = self.sw_name(dpid)
        dst_eth, src_eth = eth.dst, eth.src
        src_ip, dst_ip = ipv4_pkt.src, ipv4_pkt.dst
        proto = ipv4_pkt.proto

        # 解析四层协议
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)
        icmp_pkt = pkt.get_protocol(icmp.icmp)

        self._learn_host(dpid, in_port, src_eth, src_ip, proto_tag="IPv4")

        # 日志输出（逐包日志，降为 debug 以免打流时淹没日志）
        if tcp_pkt:
            self.logger.debug(f"[PacketIn] SW:{sw_name} TCP {src_ip}:{tcp_pkt.src_port}-->{dst_ip}:{tcp_pkt.dst_port}")
        elif udp_pkt:
            self.logger.debug(f"[PacketIn] SW:{sw_name} UDP {src_ip}:{udp_pkt.src_port}-->{dst_ip}:{udp_pkt.dst_port}")
        elif icmp_pkt:
            icmp_type = icmp_pkt.type
            if icmp_type == icmp.ICMP_ECHO_REQUEST:
                self.logger.debug(f"[PacketIn] SW:{sw_name} ICMP Echo Request {src_ip}-->{dst_ip}")
            elif icmp_type == icmp.ICMP_ECHO_REPLY:
                self.logger.debug(f"[PacketIn] SW:{sw_name} ICMP Echo Reply {src_ip}-->{dst_ip}")
            else:
                self.logger.debug(f"[PacketIn] SW:{sw_name} ICMP Type:{icmp_type} {src_ip}-->{dst_ip}")
        else:
            self.logger.debug(f"[PacketIn] SW:{sw_name} proto:{str(proto)} {src_ip}-->{dst_ip}")

        # UDP 组播报文直接丢弃
        if udp_pkt and self._is_multicast_ip(dst_ip):
            return

        if dst_ip.endswith("255"):
            return

        if dst_ip in self.ip_to_mac:
            dst_eth = self.ip_to_mac[dst_ip]
        else:
            self.logger.debug(f"[PacketIn] SW:{sw_name} {dst_ip} not in ip_to_mac")

        # 路由与转发决策
        if dst_eth not in self.hosts:
            self.send_arp_request_for_ip(datapath, src_eth, src_ip, dst_ip)
            self.logger.debug(f"[ARP Flood] 模拟 {src_ip}({src_eth}) 发送 ARP 请求查询 {dst_ip}")
            self.flood_packet_to_all_host_ports(msg.data)
            return

        src_dpid, src_port = self.hosts[src_eth]['dpid'], self.hosts[src_eth]['port']
        dst_dpid, dst_port = self.hosts[dst_eth]['dpid'], self.hosts[dst_eth]['port']

        # 同交换机直接转发
        if dpid == dst_dpid:
            # 流表匹配规则
            flow_match = parser.OFPMatch(in_port=in_port, eth_type=ether.ETH_TYPE_IP, ipv4_dst=dst_ip)
            actions = [parser.OFPActionOutput(dst_port)]
            # 下发流表
            if flow_match:
                self.add_flow(datapath, 101, flow_match, actions)
            # 发送当前报文
            data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            out = parser.OFPPacketOut(
                datapath=datapath, buffer_id=msg.buffer_id,
                in_port=in_port, actions=actions, data=data
            )
            datapath.send_msg(out)
            return

        # 跨交换机：优先复用已下发的路径，仅在无缓存或缓存失效时重算
        flow_key = (src_ip, dst_ip)
        path_switches = self._lookup_path_switches(flow_key)

        if path_switches is None:
            path = self.get_shortest_path(src_dpid, dst_dpid)
            if not path or len(path) < 2:
                self.logger.warning(f"[Route] No path found from {self.sw_name(src_dpid)} to {self.sw_name(dst_dpid)}")
                return

            path_switches = self.get_path_switches(path, src_dpid, src_port, dst_dpid, dst_port)
            self.logger.info(f"[Route] {src_ip}->{dst_ip} Path: " +
                             ' -> '.join(f'{p["in_port"]}:{self.sw_name(p["dpid"])}:{p["out_port"]}'
                                         for p in path_switches))

            self._add_path_flow(src_ip, dst_ip, path_switches)

            # 保存活跃流信息
            self.active_flows[flow_key] = {
                'type': 'flow',
                'path': path,
                'path_switches': path_switches,
                'src_host': {'mac': src_eth, 'dpid': src_dpid, 'port': src_port},
                'dst_host': {'mac': dst_eth, 'dpid': dst_dpid, 'port': dst_port}
            }
        else:
            self.logger.debug(f"[Route] reuse cached path for {src_ip}->{dst_ip}")

        # 发送当前报文：取入端口所在交换机的出端口
        out_port = next((ps['out_port'] for ps in path_switches if ps['dpid'] == dpid), None)
        if out_port is None:
            self.logger.warning(f"[Route] SW:{sw_name} not on cached path of {src_ip}->{dst_ip}, drop packet")
            return
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=[parser.OFPActionOutput(out_port)], data=data
        )
        datapath.send_msg(out)

    # ==================== IGMP 处理 ====================
    def handle_igmp(self, datapath, in_port, eth, ipv4_pkt, igmp_pkt):
        """
        处理 IGMP 报文（加入/离开组播组），如果某组播组已安装流表，同时有组播成员加入/离开，则删除该组播组安装的所有流表
        """
        dpid = datapath.id
        src_ip = ipv4_pkt.src
        src_mac = eth.src
        msgtype = igmp_pkt.msgtype

        # 跳过 IGMPv3 报文 (msgtype 0x22)
        if msgtype == 0x22:
            self.logger.info(f"[IGMP] IGMPv3 Report ignored, SW:{dpid}")
            return

        multicast_addr = igmp_pkt.address

        igmp_type_names = {
            0x11: "Membership Query",
            0x12: "Membership Report (v1)",
            0x16: "Membership Report (v2)",
            0x17: "Leave Group (v2)"
        }
        type_name = igmp_type_names.get(msgtype, "Unknown")

        self.logger.info(f"[IGMP] SW:{dpid} IGMP {type_name} from {src_ip} for group {multicast_addr}")

        if msgtype in [0x11]:
            # 将源 IP 地址作为该组播组的源节点记录
            self.logger.info(f"[IGMP Query] Source IP: {src_ip}, Multicast Group: {multicast_addr}")
            if multicast_addr == '224.0.0.0':
                # 224.0.0.0 代表删除源IP的信号，用 src_ip 查找并删除对应关系
                groups_to_delete = [g for g, s in self.multicast_sources.items() if s == src_ip]
                for g in groups_to_delete:
                    del self.multicast_sources[g]
                    # 删除该组播组的所有流规则
                    if g in self.multicast_flow_installed:
                        self._delete_multicast_flows(g)
                        self.multicast_flow_installed.discard(g)
                    self.logger.info(f"[IGMP Query] Received 224.0.0.0, removed source {src_ip} for group {g}")
                if not groups_to_delete:
                    self.logger.info(f"[IGMP Query] Received 224.0.0.0, but no group found with source {src_ip}")
            elif multicast_addr != '0.0.0.0':  # Specific Query
                if multicast_addr not in self.multicast_sources:
                    # 组播组还没有源IP，将当前源 ip 和该组播组进行关联。如果组播组中还有组播成员，则安装组播流规则
                    self.multicast_sources[multicast_addr] = src_ip
                    self.logger.info(f"[IGMP Query] Recorded source node {src_ip} for multicast group {multicast_addr}")
                    # 如果组播组已有成员，安装流规则
                    if multicast_addr in self.multicast_groups and self.multicast_groups[multicast_addr]:
                        self._install_multicast_flow_rules(multicast_addr)
                else:
                    # 组播组已有源IP，不允许更改
                    self.logger.info(f"[IGMP Query] Source node for {multicast_addr} already recorded as {self.multicast_sources[multicast_addr]}, skipping")
            return

        # 检查是否重复加入或重复离开
        if msgtype in [0x12, 0x16]:  # Join
            if multicast_addr in self.multicast_groups:
                if dpid in self.multicast_groups[multicast_addr]:
                    if src_mac in self.multicast_groups[multicast_addr][dpid]:
                        self.logger.info(f"[IGMP] Host {src_ip} already in group {multicast_addr}, skipping")
                        return
        elif msgtype == 0x17:  # Leave
            if multicast_addr not in self.multicast_groups:
                self.logger.info(f"[IGMP] Group {multicast_addr} does not exist, skipping leave")
                return
            if dpid not in self.multicast_groups[multicast_addr]:
                self.logger.info(f"[IGMP] Switch {dpid} has no members in group {multicast_addr}, skipping leave")
                return
            if src_mac not in self.multicast_groups[multicast_addr][dpid]:
                self.logger.info(f"[IGMP] Host {src_ip} not in group {multicast_addr}, skipping leave")
                return

        # 检查该组播组是否已安装过流表
        if multicast_addr in self.multicast_flow_installed:
            self.logger.info(f"[IGMP] Group {multicast_addr} has existing flow rules, deleting them first")
            self._delete_multicast_flows(multicast_addr)

        # msgtype 0x12/0x16 = Join (加入组播组)
        if msgtype in [0x12, 0x16]:
            self.add_member_to_group(dpid, src_mac, src_ip, multicast_addr, in_port)
            if multicast_addr in self.multicast_sources:
                self._install_multicast_flow_rules(multicast_addr)
        # msgtype 0x17 = Leave (离开组播组)
        elif msgtype == 0x17:
            self.remove_member_from_group(dpid, src_mac, multicast_addr)

    # ==================== 组播组管理 ====================
    def _is_multicast_ip(self, ip):
        """检查是否为组播 IP 地址 (224.0.0.0 - 239.255.255.255)"""
        ip_parts = [int(x) for x in ip.split('.')]
        return ip_parts[0] >= 224 and ip_parts[0] <= 239

    def add_member_to_group(self, dpid, mac, src_ip, multicast_addr, in_port):
        """
        将主机加入组播组
        """
        if multicast_addr not in self.multicast_groups:
            self.multicast_groups[multicast_addr] = {}

        if dpid not in self.multicast_groups[multicast_addr]:
            self.multicast_groups[multicast_addr][dpid] = {}

        self.multicast_groups[multicast_addr][dpid][mac] = (src_ip, in_port)
        self.logger.info(f"[Multicast Group] Host joined: Group={multicast_addr}, MAC={mac}, IP={src_ip}, SW={dpid}, Port={in_port}")
        self.print_multicast_groups()

    def remove_member_from_group(self, dpid, mac, multicast_addr):
        """
        将主机从组播组删除
        """
        if multicast_addr in self.multicast_groups:
            if dpid in self.multicast_groups[multicast_addr]:
                if mac in self.multicast_groups[multicast_addr][dpid]:
                    del self.multicast_groups[multicast_addr][dpid][mac]
                    self.logger.info(f"[Multicast Group] Host left: Group={multicast_addr}, MAC={mac}")

                    # 如果该组播组在该交换机上没有成员了，删除该交换机的记录
                    if not self.multicast_groups[multicast_addr][dpid]:
                        del self.multicast_groups[multicast_addr][dpid]

                    # 如果该组播组没有组播成员了
                    if not self.multicast_groups[multicast_addr]:
                        # 且该组播组没有对应的源IP，则删除该组播组
                        # 否则不做操作，保留该组播组（后续有新成员加入时可复用源IP）
                        if multicast_addr not in self.multicast_sources:
                            del self.multicast_groups[multicast_addr]
                    else:
                        # 如果该组播组删除主机后还有组播成员，且有源IP，则重新安装流规则
                        if multicast_addr in self.multicast_sources:
                            self._install_multicast_flow_rules(multicast_addr)
                else:
                    self.logger.info(f"[Multicast Group] Host not in group: MAC={mac}")
            else:
                self.logger.info(f"[Multicast Group] Switch has no members in group")
        else:
            self.logger.info(f"[Multicast Group] Group does not exist")
        self.print_multicast_groups()

    def print_multicast_groups(self):
        """
        打印当前所有组播组信息（成员变动时才产生，降为 debug）
        """
        self.logger.debug("========== Multicast Group State ==========")
        if not self.multicast_groups:
            self.logger.debug("  No multicast members")
        for group, dpid_dict in self.multicast_groups.items():
            self.logger.debug(f"  Group {group}:")
            for dpid, mac_dict in dpid_dict.items():
                self.logger.debug(f"    Switch {dpid}:")
                for mac, (src_ip, port) in mac_dict.items():
                    self.logger.debug(f"      MAC={mac}, IP={src_ip}, Port={port}")
        self.logger.debug("=========================================")

    # ==================== 组播流规则安装/删除 ====================
    def _install_multicast_flow_rules(self, multicast_addr):
        """
        为指定组播组安装组播流规则
        根据 multicast_sources 获取源 IP，根据 multicast_groups 获取组成员信息
        """
        if multicast_addr not in self.multicast_sources:
            self.logger.info(f"[Multicast Flow] Group {multicast_addr} has no source IP, skipping flow installation")
            return False

        if multicast_addr not in self.multicast_groups or not self.multicast_groups[multicast_addr]:
            self.logger.info(f"[Multicast Flow] Group {multicast_addr} has no members, skipping flow installation")
            return False

        self.logger.info(f"[Multicast Flow] multicast_weights content: {self.multicast_weights}")

        src_ip = self.multicast_sources[multicast_addr]
        src_switch_dpid = self.host_ip_to_dpid.get(src_ip)
        if not src_switch_dpid:
            self.logger.warning(f"[Multicast Route] Cannot find source switch for IP: {src_ip}")
            return False

        try:
            # 标记该组播组已安装流规则
            self.multicast_flow_installed.add(multicast_addr)

            # 收集所有成员信息（去重）
            member_ips = []
            member_dpid_set = set()
            for group_dpid, mac_dict in self.multicast_groups[multicast_addr].items():
                member_dpid_set.add(group_dpid)
                for mac, (member_ip, member_port) in mac_dict.items():
                    member_ips.append(member_ip)

            # 将 self.graph 转换为邻接矩阵
            with self._lock:
                self.adj_matrix_nodes = sorted(self.graph.nodes())
                # 构建邻接矩阵节点索引和交换机 DPID 之间的的对应关系
                adj_matrix = nx.to_numpy_array(self.graph, nodelist=self.adj_matrix_nodes, dtype=np.float32)
                # 获取延迟、带宽、丢包矩阵
                latency_matrix = nx.attr_matrix(self.graph, edge_attr='delay', rc_order=self.adj_matrix_nodes, dtype=np.float32)
                bandwidth_matrix = nx.attr_matrix(self.graph, edge_attr='bw', rc_order=self.adj_matrix_nodes, dtype=np.float32)
                load_matrix = nx.attr_matrix(self.graph, edge_attr='total_bps', rc_order=self.adj_matrix_nodes, dtype=np.float32)
                available_bw_matrix = bandwidth_matrix - load_matrix
                loss_matrix = nx.attr_matrix(self.graph, edge_attr='loss', rc_order=self.adj_matrix_nodes, dtype=np.float32)

            src_switch_idx = self.adj_matrix_nodes.index(src_switch_dpid)
            src_switch_id = src_switch_idx + 1
            member_idx_set = [self.adj_matrix_nodes.index(dpid) for dpid in member_dpid_set]
            member_id_list = [idx + 1 for idx in member_idx_set]

            self.logger.info(f"[Multicast Route] Source: {src_ip} -> SW:{src_switch_id}")
            self.logger.info(f"[Multicast Route] Member Hosts in Destination Group: {member_ips}")
            self.logger.info(f"[Multicast Route] Member Switches: {member_id_list}")

            # 调用智能组播路由构建算法构建组播路由, 1-based:source_node,destination_group
            # 获取该组播组的权重配置，默认为 1.0
            weights = self.multicast_weights.get(multicast_addr,
                                                 {'latency': 1.0, 'bandwidth': 1.0, 'loss': 1.0})
            self.logger.info(f"[Multicast Route] Using weights for {multicast_addr}: latency={weights['latency']}, bandwidth={weights['bandwidth']}, loss={weights['loss']}")
            is_success, route_matrix = RL_multicast_tree(adj_matrix, latency_matrix, available_bw_matrix, loss_matrix,
                                                         source_node=src_switch_id,
                                                         destination_group=member_id_list,
                                                         latency_weight=weights['latency'],
                                                         bandwidth_weight=weights['bandwidth'],
                                                         loss_weight=weights['loss'],
                                                         algorithm_type=self.routing_algorithm)
            if is_success:
                # 将邻接矩阵的双向性变成从源到目的集合的单向性矩阵（BFS）
                src_idx = self.adj_matrix_nodes.index(src_switch_dpid)
                visited = set()
                queue = deque([src_idx])
                visited.add(src_idx)
                net_size = len(self.adj_matrix_nodes)

                while queue:
                    i = queue.popleft()
                    for j in range(net_size):
                        if route_matrix[i, j] == 1:
                            route_matrix[j, i] = 0  # 删除反向边，确保单向性
                            if j not in visited:
                                queue.append(j)
                                visited.add(j)

                installed_flows = []
                for i in range(net_size):
                    src_node_dpid = self.adj_matrix_nodes[i]
                    if src_node_dpid not in self.datapaths:
                        continue

                    src_datapath = self.datapaths[src_node_dpid]
                    src_parser = src_datapath.ofproto_parser

                    # 收集该节点需要转发到的所有下一跳
                    out_actions = []

                    # 如果该交换机上有组成员主机，转发到主机端口（目的交换机）
                    if src_node_dpid in member_dpid_set:
                        for mac, (member_ip, member_port) in self.multicast_groups[multicast_addr].get(src_node_dpid, {}).items():
                            out_actions.append(src_parser.OFPActionOutput(member_port))
                            self.logger.info(f"[Multicast Flow] SW:{self.sw_name(src_node_dpid)} -> host port {member_port}")

                    for j in range(net_size):
                        if route_matrix[i][j] == 1:
                            dst_node_dpid = self.adj_matrix_nodes[j]
                            out_port = self.get_path_out_port(src_node_dpid, dst_node_dpid)
                            if out_port:
                                out_actions.append(src_parser.OFPActionOutput(out_port))
                                self.logger.info(f"[Multicast Flow] SW:{self.sw_name(src_node_dpid)} -> port {out_port} (next hop SW:{self.sw_name(dst_node_dpid)})")

                    if out_actions:
                        # 源交换机需要匹配 in_port + src_ip + 组播地址，其他交换机只匹配 src_ip + 组播地址
                        if src_node_dpid == src_switch_dpid:
                            # 源交换机需要知道入端口，这里用 OFPP_LOCAL 暂时处理
                            match = src_parser.OFPMatch(
                                eth_type=0x0800,
                                ipv4_src=src_ip,
                                ipv4_dst=multicast_addr
                            )
                        else:
                            match = src_parser.OFPMatch(
                                eth_type=0x0800,
                                ipv4_src=src_ip,
                                ipv4_dst=multicast_addr
                            )
                        self.add_flow(src_datapath, 100, match, out_actions, idle_timeout=0, hard_timeout=0)
                        installed_flows.append((src_node_dpid, f"ipv4_src={src_ip}, ipv4_dst={multicast_addr} -> {len(out_actions)} port(s)"))

                # 记录已安装的流规则
                if multicast_addr not in self.multicast_flows:
                    self.multicast_flows[multicast_addr] = []
                self.multicast_flows[multicast_addr].extend(installed_flows)
                self.logger.info(f"[Multicast Flow] Group {multicast_addr} installed {len(installed_flows)} flow rules")

                # 提取组播树路径列表
                path_list = []
                for i in range(net_size):
                    for j in range(net_size):
                        if route_matrix[i, j] == 1:
                            path_list.append((self.adj_matrix_nodes[i], self.adj_matrix_nodes[j]))

                # 获取源主机与源交换机相连的端口号
                src_switch_in_port = None
                for mac, host_info in self.hosts.items():
                    if host_info.get('ip') == src_ip and host_info.get('dpid') == src_switch_dpid:
                        src_switch_in_port = host_info.get('port')
                        break

                self.multicast_active_flows[multicast_addr] = {
                    'source_ip': src_ip,
                    'source_dpid': src_switch_dpid,
                    'source_in_port': src_switch_in_port,
                    'route_matrix': route_matrix,
                    'member_ips': member_ips,
                    'path': path_list,
                    'installed_time': time.time()
                }
                self.logger.info(f"[Multicast Active Flow] Recorded active flow for group {multicast_addr}")

                return True

            else:
                # 组播组流规则安装失败，删除标记
                self.multicast_flow_installed.discard(multicast_addr)
                self.logger.info(f"[Multicast Route] Route Matrix: failed")
                return False

        except Exception as e:
            # 组播组流规则安装出现异常，删除标记
            self.multicast_flow_installed.discard(multicast_addr)
            self.logger.error(f"[Multicast Route] Exception: {e}")
            return False

    def _delete_multicast_flows(self, multicast_addr):
        """
        删除指定组播组的所有流表规则
        """
        if multicast_addr not in self.multicast_flows or not self.multicast_flows[multicast_addr]:
            return

        for dpid, match_desc in self.multicast_flows[multicast_addr]:
            if dpid not in self.datapaths:
                continue
            datapath = self.datapaths[dpid]
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser

            # 构造匹配 ipv4_dst 的 match
            match = parser.OFPMatch(eth_type=0x0800, ipv4_dst=multicast_addr)
            # 删除该组播组的流表
            mod = parser.OFPFlowMod(
                datapath=datapath,
                command=ofproto.OFPFC_DELETE,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=match
            )
            datapath.send_msg(mod)
            self.logger.info(f"[Multicast Flow] Deleted flow on SW:{dpid} for group {multicast_addr}")

        # 清空该组播组的流表记录
        self.multicast_flows[multicast_addr] = []
        self.multicast_flow_installed.discard(multicast_addr)

        # 从组播活跃流中移除
        if multicast_addr in self.multicast_active_flows:
            del self.multicast_active_flows[multicast_addr]
            self.logger.info(f"[Multicast Active Flow] Removed active flow for group {multicast_addr}")

    # ==================== 主机学习 ====================
    def _learn_host(self, dpid, in_port, src_eth, src_ip, proto_tag=""):
        """
        通用主机学习核心逻辑
        :param dpid: 交换机 DPID
        :param in_port: 入端口号
        :param src_eth: 源 MAC 地址
        :param src_ip: 源 IP 地址
        :param proto_tag: 日志标签 (如 "ARP" 或 "IPv4")
        """
        # 只在非交换机级联端口学习主机
        is_inter_switch_port = dpid in self.inter_switch_ports and in_port in self.inter_switch_ports[dpid]
        if is_inter_switch_port:
            return

        # 学习源MAC + 源IP
        if src_eth not in self.hosts:
            self.hosts[src_eth] = {
                'mac': src_eth,
                'ip': src_ip,
                'dpid': dpid,
                'port': in_port
            }
            self.logger.info(f"[Host Learn {proto_tag}] MAC:{src_eth} IP:{src_ip} at Switch {dpid} Port {in_port}")
        else:
            # 更新位置或IP
            old = self.hosts[src_eth]
            if old['dpid'] != dpid or old['port'] != in_port:
                self.hosts[src_eth]['dpid'] = dpid
                self.hosts[src_eth]['port'] = in_port
                self.logger.info(f"[Host Move {proto_tag}] MAC:{src_eth} at Switch {dpid} Port {in_port}")
            # 更新IP（如果之前没有）
            if src_ip and not self.hosts[src_eth]['ip']:
                self.hosts[src_eth]['ip'] = src_ip

        # 维护 IP → MAC 映射
        if src_ip:
            # 如果该 IP 之前属于别人，先删掉旧的记录（防止 IP 冲突漂移）
            if src_ip in self.ip_to_mac and self.ip_to_mac[src_ip] != src_eth:
                old_mac = self.ip_to_mac[src_ip]
                del self.hosts[old_mac]
                self.logger.warning(f"[IP Conflict] IP {src_ip} moved from {old_mac} to {src_eth}")
            self.ip_to_mac[src_ip] = src_eth

    # ==================== 数据包发送 ====================
    def send_arp_reply(self, datapath, eth_pkt, src_mac, src_ip, dst_mac, dst_ip, in_port):
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=eth_pkt.ethertype, src=dst_mac, dst=src_mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=dst_mac, src_ip=dst_ip, dst_mac=src_mac, dst_ip=src_ip))
        pkt.serialize()
        self.send_packet_out(datapath, in_port, pkt.data)

    def send_packet_out(self, datapath, port, pkt_data):
        # 向交换机发送指令，令交换机从指定端口发送数据包
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        actions = [parser.OFPActionOutput(port)]
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER, actions=actions, data=pkt_data
        )
        datapath.send_msg(out)

    def send_arp_request_for_ip(self, datapath, src_mac, src_ip, target_ip):
        """
        模拟源主机构造并发送 ARP Request，查询目标 IP 的 MAC
        :param datapath: 收到 IPv4 报文的交换机（用于获取 ofproto/parser）
        :param src_mac: 源主机 MAC
        :param src_ip: 源主机 IP
        :param target_ip: 需要查询的目标 IP
        """
        # 构造 ARP 请求报文
        pkt = packet.Packet()
        # 1. 以太网头：广播地址
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether.ETH_TYPE_ARP,
            src=src_mac,
            dst='ff:ff:ff:ff:ff:ff'
        ))
        # 2. ARP 头：Request 类型
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REQUEST,
            src_mac=src_mac,
            src_ip=src_ip,
            dst_mac='00:00:00:00:00:00',
            dst_ip=target_ip
        ))
        pkt.serialize()
        # 调用通用泛洪函数发送
        self.flood_packet_to_all_host_ports(pkt.data)

    # ==================== ARP 全网泛洪 ====================
    def flood_arp_to_all_switches(self, data):
        self.flood_packet_to_all_host_ports(data)

    def flood_packet_to_all_host_ports(self, data):
        """
        通用泛洪：将数据包发送到所有交换机的非级联端口（主机侧端口）
        :param data: 要发送的序列化报文数据
        """
        for dpid, dp in self.datapaths.items():
            ofproto = dp.ofproto
            parser = dp.ofproto_parser

            inter_ports = self.inter_switch_ports.get(dpid, set())
            flood_ports = []

            if dpid in self.graph:
                sw_ports = self.graph.nodes[dpid].get('ports', [])
                flood_ports = [p for p in sw_ports if p not in inter_ports]

            if not flood_ports:
                continue

            actions = [parser.OFPActionOutput(p) for p in flood_ports]
            out = parser.OFPPacketOut(
                datapath=dp, buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=ofproto.OFPP_CONTROLLER, actions=actions, data=data
            )
            dp.send_msg(out)

    def flood_arp_for_ip(self, target_ip):
        """
        构造针对目标IP的ARP请求并全网泛洪
        :param target_ip: 需要查询MAC的目标IP地址
        """
        # 控制器虚拟MAC和IP（用于发送ARP请求，可根据实际网络调整）
        controller_mac = '00:00:00:00:00:cc'
        controller_ip = '10.0.0.254'

        # 构造ARP请求报文
        pkt = packet.Packet()
        # 以太网层：广播目的MAC
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ether.ETH_TYPE_ARP,
            src=controller_mac,
            dst='ff:ff:ff:ff:ff:ff'
        ))
        # ARP层：请求 target_ip 的MAC
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REQUEST,
            src_mac=controller_mac,
            src_ip=controller_ip,
            dst_mac='00:00:00:00:00:00',
            dst_ip=target_ip
        ))
        pkt.serialize()

        # 调用现有泛洪函数发送
        self.flood_arp_to_all_switches(pkt.data)
        self.logger.info(f"[ARP Flood Trigger] 已泛洪针对 IP {target_ip} 的ARP请求")

    # ==================== 拓扑更新 ====================
    def update_topology(self):
        if not self._topology_stable:
            if time.time() - self._start_time > 5:
                self.logger.info('Controller Init')
                self._topology_stable = True
            return

        switch_list = get_switch(self)
        link_list = get_link(self)
        host_list = get_host(self)

        # 新链路的首次 tc 采样放在锁外完成，之后由低频刷新任务维护
        self._prime_link_metrics(link_list)

        with self._lock:
            self._rebuild_graph(switch_list, link_list, host_list)

    def _rebuild_graph(self, switch_list, link_list, host_list):
        """重建 DiGraph 与派生数据，调用方需持有 _lock"""
        # 重建 DiGraph
        self.graph = nx.DiGraph()
        self.inter_switch_ports = {s.dp.id: set() for s in switch_list}

        # 添加交换机节点
        for s in switch_list:
            dpid_hex = format(s.dp.id, '016x')
            level = int(dpid_hex[8:10], 16) if len(dpid_hex) >= 10 else 3
            sw_name = self.switch_name.get(f'{s.dp.id:016x}', f"sw-{s.dp.id}")
            ports = [p.port_no for p in s.ports]
            self.graph.add_node(s.dp.id, name=sw_name, level=level, ports=ports)

        # 添加链路边
        for l in list(link_list):
            src_dpid = l.src.dpid
            src_port = l.src.port_no
            dst_dpid = l.dst.dpid
            dst_port = l.dst.port_no

            eth_name = f'{self.switch_name.get(f"{src_dpid:016x}")}-eth{src_port}'
            sample = self._link_metrics_cache.get(eth_name)
            stats = sample.stats if sample is not None else utils.LinkTcStats()
            bw, delay, loss = stats.bw_mbps, stats.delay_ms, stats.loss_configured_pct
            # 实测丢包率（tc -s 计数差分）；还没有差分样本时为 None
            loss_measured = sample.loss_measured_pct if sample is not None else None

            # 获取链路负载（从端口统计计算）
            rx_bps = 0
            tx_bps = 0
            total_bps = 0
            # 单向 src→dst 的实测吞吐（Mbps）。
            # src 端口的 tx 就是「从 src 发往 dst」的字节速率；dst 端口的 rx
            # 是同一批流量的另一视角——两者是同一份数据，**不能相加**。
            # （旧口径取 rx+tx，把双向混在一起，导致双向边求和时重复计数。）
            oneway_bps = 0.0
            if src_dpid in self.port_stats and src_port in self.port_stats[src_dpid]:
                rx_bps = self.port_stats[src_dpid][src_port].get('rx_bps', 0)
                tx_bps = self.port_stats[src_dpid][src_port].get('tx_bps', 0)
                total_bps = self.port_stats[src_dpid][src_port].get('total_bps', 0)
                oneway_bps = tx_bps
            elif dst_dpid in self.port_stats and dst_port in self.port_stats[dst_dpid]:
                rx_bps = self.port_stats[dst_dpid][dst_port].get('rx_bps', 0)
                tx_bps = self.port_stats[dst_dpid][dst_port].get('tx_bps', 0)
                total_bps = self.port_stats[dst_dpid][dst_port].get('total_bps', 0)
                # 只有 dst 侧有统计时，用 dst 的 rx（即 src→dst 方向）作为单向值
                oneway_bps = rx_bps

            # 添加双向边（保存端口和链路状态信息）
            self.graph.add_edge(src_dpid, dst_dpid,
                               src_port=src_port, dst_port=dst_port,
                               bw=bw, delay=delay, loss=loss,
                               loss_measured=loss_measured,
                               # 额定速率（bit/s）：OVS Interface 的 link_speed
                               capacity_bps=self._ovs_link_speeds.get(eth_name, 0),
                               rx_bps=rx_bps, tx_bps=tx_bps, total_bps=total_bps,
                               oneway_bps=oneway_bps)

            # 负载历史：每条有向边一个预测器，供反馈式负载均衡外推
            # （只用 oneway_bps 这个单向实测值，与链路指标同口径）
            if ALGO_AVAILABLE:
                pred = self._link_load_predictors.get((src_dpid, dst_dpid))
                if pred is None:
                    pred = algo_predict.TrendPredictor(maxlen=10, alpha=0.5)
                    self._link_load_predictors[(src_dpid, dst_dpid)] = pred
                pred.push(oneway_bps)

            # 记录级联端口
            self.inter_switch_ports[src_dpid].add(src_port)
            self.inter_switch_ports[dst_dpid].add(dst_port)

        # 主机信息更新
        self.host_ip_to_dpid = {}  # 清空并重建
        for h in host_list:
            if not h.port:
                continue
            mac = h.mac
            ip = next(iter(h.ipv4), None) if getattr(h, "ipv4", None) else None

            if mac in self.hosts:
                self.hosts[mac]['dpid'] = h.port.dpid
                self.hosts[mac]['port'] = h.port.port_no
                if ip:
                    self.hosts[mac]['ip'] = ip
            else:
                self.hosts[mac] = {'mac': mac, 'ip': ip, 'dpid': h.port.dpid, 'port': h.port.port_no}

            # 更新主机IP -> 交换机DPID 映射
            if ip:
                self.host_ip_to_dpid[ip] = h.port.dpid

        # 从 graph 提取 topology_data
        self.topology_data = self._build_topology_data()

        # 找到出入度和为 1 或 2 的节点（不可替代节点）
        self.critical_nodes = {node for node, degree in self.graph.degree() if (degree == 1 or degree == 2)}
        if self.critical_nodes:
            self.logger.debug(f"Critical nodes (degree=1): {[self.sw_name(n) for n in self.critical_nodes]}")

        self.logger.debug(f"Topology updated: {self.graph.number_of_nodes()} switches, {self.graph.number_of_edges()} links")

    def _build_topology_data(self):
        """从 DiGraph 提取前端需要的拓扑数据"""
        # 交换机信息
        switches = []
        for dpid, attrs in self.graph.nodes(data=True):
            switches.append({
                'dpid': dpid,
                'name': attrs.get('name', f'sw-{dpid}'),
                'level': attrs.get('level', 3),
                'ports': attrs.get('ports', [])
            })

        # 链路信息
        links = []
        for src_dpid, dst_dpid, attrs in self.graph.edges(data=True):
            links.append({
                'src': {'dpid': src_dpid, 'port': attrs.get('src_port')},
                'dst': {'dpid': dst_dpid, 'port': attrs.get('dst_port')},
                'bw': attrs.get('bw', 0),
                'delay': attrs.get('delay', 0),
                # loss 是 netem 的**配置值**
                'loss': attrs.get('loss', 0),
                # loss_measured 是**实测**丢包率（tc -s 计数差分），可能为 null
                'loss_measured': attrs.get('loss_measured'),
                # capacity_bps 是 OVS link_speed 给出的额定速率（bit/s）
                'capacity_bps': attrs.get('capacity_bps', 0),
                'load': {
                    'rx_bps': attrs.get('rx_bps', 0),
                    'tx_bps': attrs.get('tx_bps', 0),
                    'total_bps': attrs.get('total_bps', 0),
                    # 单向 src→dst 吞吐（Mbps），与 Prometheus 口径一致
                    'oneway_bps': attrs.get('oneway_bps', 0)
                }
            })

        # 主机信息
        hosts = []
        for mac, attrs in self.hosts.items():
            hosts.append({
                'mac': mac,
                'ip': attrs.get('ip'),
                'dpid': attrs['dpid'],
                'port': attrs['port']
            })

        return {'switches': switches, 'links': links, 'hosts': hosts}

    # ==================== 流表下发 ====================
    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id, priority=priority,
                                    match=match, instructions=inst,
                                    idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                                    flags=ofproto.OFPFF_SEND_FLOW_REM)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst,
                                    idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                                    flags=ofproto.OFPFF_SEND_FLOW_REM)
        datapath.send_msg(mod)

    # ==================== 最短路径（使用 networkx） ====================
    def get_shortest_path(self, src, dst, preference=None):
        """路径计算入口。

        单目标偏好（bandwidth / loss / latency / load_balance）保持原语义，
        便于与历史结果对照；`smart_algorithm` 走多目标联合优化
        （`algo.routing`），也是算力路由（2.4.2）的实际落点。

        **负载口径修正**：load_balance 原来用 `total_bps`（rx+tx，双向之和）
        与 `bw`（单向额定）相比，比值天然偏大，同一份流量在两个方向各算
        一次，导致「看起来一直很挤」。现在改用 `oneway_bps`（单向 src→dst
        实测吞吐），与链路指标导出同口径。
        """
        if src == dst:
            return [src]
        if src not in self.graph or dst not in self.graph:
            return []

        label = getattr(preference, 'name', 'default')
        start = time.time()
        try:
            path = self._compute_path(src, dst, preference)
        finally:
            # 选路耗时按偏好分开统计：联合优化比单目标贵多少一目了然
            try:
                qos_routing_decision_seconds.labels(preference=label).observe(
                    time.time() - start)
                qos_routing_decisions_total.labels(preference=label).inc()
            except Exception:
                pass
        return path

    def _compute_path(self, src, dst, preference):
        """实际求解（由 get_shortest_path 包一层指标）"""
        try:
            if preference == WeightPref.SmartAlgorithm and ALGO_AVAILABLE:
                # 多目标联合优化：时延/丢包/带宽/负载/算力余量加权
                path, _cost = algo_routing.best_path(
                    self.graph, src, dst,
                    weights=self.routing_weights,
                    node_penalty=self.compute_load)
                return path
            if preference == WeightPref.Bandwidth:
                def weight_bw(u, v, d):
                    bw = d.get("bw", 0)
                    return 1.0 / (bw + 0.001)
                return nx.shortest_path(self.graph, src, dst, weight=weight_bw)
            if preference == WeightPref.LossRate:
                return nx.shortest_path(self.graph, src, dst, weight='loss')
            if preference == WeightPref.LoadBalance:
                def weight_load(u, v, d):
                    bw = d.get("bw", 0.1)
                    # oneway_bps 是该方向实测吞吐（Mbps）；缺失时退回 0
                    load = d.get("oneway_bps") or 0
                    return 1 / (bw - load) if bw > load else 999
                return nx.shortest_path(self.graph, src, dst, weight=weight_load)
            if preference == WeightPref.Latency:
                return nx.shortest_path(self.graph, src, dst, weight='delay')
            return nx.shortest_path(self.graph, src, dst)
        except nx.NetworkXNoPath:
            return []
        except Exception as e:
            # 联合优化出问题时退回跳数最短，别让选路整体失败
            if preference == WeightPref.SmartAlgorithm:
                self.logger.warning(f"[Routing] smart path failed ({type(e).__name__}: {e}), "
                                    f"falling back to hop count")
                try:
                    return nx.shortest_path(self.graph, src, dst)
                except nx.NetworkXNoPath:
                    return []
            raise

    def explain_path(self, path):
        """逐项拆解一条路径的选路代价（供 REST 展示「为什么选它」）"""
        if not ALGO_AVAILABLE:
            return {'path': list(path or []), 'error': 'algo package unavailable'}
        return algo_routing.explain_path(
            self.graph, path, weights=self.routing_weights,
            node_penalty=self.compute_load)

    def get_path_out_port(self, src, dst):
        """获取 src → dst 这条链路的出端口

        调用方传入的 (src, dst) 均为路由树上的边，本就是图中的边，
        因此直接查边属性即可，无需再跑一次最短路计算。
        """
        if self.graph.has_edge(src, dst):
            return self.graph[src][dst].get('src_port')
        return None

    def _request_port_stats(self):
        """向所有交换机发送端口统计请求"""
        for dpid, datapath in self.datapaths.items():
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            req = parser.OFPPortStatsRequest(datapath, port_no=ofproto.OFPP_ANY)
            datapath.send_msg(req)

    def _request_flow_stats(self, dpid, multicast_addr=None, src_ip=None):
        """向指定交换机发送流表统计请求"""
        if dpid not in self.datapaths:
            return
        datapath = self.datapaths[dpid]
        parser = datapath.ofproto_parser

        # 如果提供了源IP和组播地址，构建精确的匹配条件
        if src_ip and multicast_addr:
            match = parser.OFPMatch(
                eth_type=0x0800,
                ipv4_src=src_ip,
                ipv4_dst=multicast_addr
            )
        else:
            match = parser.OFPMatch(eth_type=0x0800)

        ofproto = datapath.ofproto
        req = parser.OFPFlowStatsRequest(
            datapath,
            table_id=ofproto.OFPTT_ALL,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            cookie=0,
            cookie_mask=0,
            match=match
        )
        datapath.send_msg(req)

    def _update_prometheus_metrics(self):
        """更新 Prometheus 指标"""
        # 更新控制器运行时间
        qos_controller_uptime.set(time.time() - self._start_time)
        qos_topology_updates_total.inc()

        # 更新拓扑指标
        qos_switches_count.set(self.graph.number_of_nodes())
        qos_links_count.set(self.graph.number_of_edges())
        qos_hosts_count.set(len(self.hosts))

        # 更新链路指标
        total_bandwidth = 0
        total_capacity = 0
        for src, dst, attrs in self.graph.edges(data=True):
            src_name = self.sw_name(src)
            dst_name = self.sw_name(dst)
            # oneway_bps 在内部（图属性）一律是 Mbps；
            # prometheus 指标名与 help 都约定 bits/s，导出时在此统一 ×1e6，
            # 否则消费端（backen 按 bit/s ÷1e6 换算 Mbps）会差 6 个数量级。
            # 口径：单向 src→dst 的实测吞吐。
            oneway_bps = attrs.get('oneway_bps', 0) * 1e6
            qos_link_bandwidth_usage.labels(src_dpid=src_name, dst_dpid=dst_name).set(oneway_bps)
            # 额定速率：OVS Interface link_speed（bit/s），读不到时为 0
            capacity_bps = attrs.get('capacity_bps', 0)
            qos_link_capacity.labels(src_dpid=src_name, dst_dpid=dst_name).set(capacity_bps)
            # 时延：netem 配置值（被动采集拿不到 RTT）
            qos_link_delay.labels(src_dpid=src_name, dst_dpid=dst_name).set(attrs.get('delay', 0))
            # 丢包率：实测值（tc -s 计数差分）。本轮没有差分样本时保持上次值，
            # 不用 0 覆盖——「没有新样本」不等于「没有丢包」。
            loss_measured = attrs.get('loss_measured')
            if loss_measured is not None:
                qos_link_loss.labels(src_dpid=src_name, dst_dpid=dst_name).set(loss_measured)
            # 配置值（netem 参数），保留用于与实测值对照
            qos_link_loss_configured.labels(src_dpid=src_name, dst_dpid=dst_name).set(attrs.get('loss', 0))
            total_bandwidth += oneway_bps
            total_capacity += capacity_bps

        # 更新网络总带宽指标（Σ 有向边单向吞吐 = 每条物理链路的双向总流量）
        qos_network_total_bandwidth_bits.set(total_bandwidth)
        qos_network_total_capacity_bits.set(total_capacity)

        # 更新 QoS 规则指标
        qos_rules_total.set(len(self.qos_rules))
        qos_rules_active.set(sum(1 for r in self.qos_rules.values() if r.get('enabled', True)))

        # ---- 选路与负载均衡的状态导出 ----
        # 权重：让 Prometheus 侧能回看「当时用的是哪套参数」，否则同一指标
        # 在不同时间段不可比
        for key, value in (self.routing_weights or {}).items():
            qos_routing_weight.labels(metric=key).set(value)
        # 算力占用：0 表示未注入（不影响选路）。显式导出 0 而不是不导出，
        # 这样「没配」和「配了 0」在时序上可区分（后者会有写入历史）
        for dpid, load in (self.compute_load or {}).items():
            qos_compute_load.labels(switch=self.sw_name(dpid)).set(max(0.0, min(float(load), 1.0)))
        # 上一轮反馈周期规模与耗时
        cyc = self._lb_last_cycle or {}
        qos_lb_cycle_rules.set(cyc.get('evaluated', 0))
        qos_lb_cycle_duration.set(cyc.get('duration', 0.0))
        # 预测利用率：逐条有向边导出，预测器没热身的边退回当前实测比值
        for (u, v), pred in list(self._link_load_predictors.items()):
            if not self.graph.has_edge(u, v):
                continue
            attrs = self.graph[u][v]
            bw = attrs.get('bw') or 0
            if bw <= 0:
                continue
            value = None
            if pred.ready:
                value = pred.predict(horizon=1)
            if value is None:
                value = attrs.get('oneway_bps') or 0
            qos_link_predicted_util.labels(
                src_dpid=self.sw_name(u), dst_dpid=self.sw_name(v)
            ).set(min(max(value / bw, 0.0), 1.0))

        # SLA 兑现度：承诺速率 vs 实测速率。
        # 实测值取规则**首跳出接口**的 oneway_bps（OpenFlow 端口统计差分，
        # 与链路指标同口径），单位 Mbps —— 与该链路上这条业务的实际吞吐
        # 是同一份数据，不是估算。
        #
        # 局限（写进 detail/指标说明，别当精确值读）：
        # 首跳出接口上可能同时跑着别的流量，oneway_bps 是**整条链路**的
        # 吞吐而不是单条规则的。所以这个比值是「该规则所在链路的饱和度」，
        # 用来看 SLA 有没有被挤爆够用，用来看单条流的精确速率不够。
        # 要拿到单条流的字节数，得按 match 查 flow_stats —— 属后续工作。
        sla_rules = [r for r in self.qos_rules.values() if r.get('target_bps')]
        metered = [r for r in sla_rules if r.get('meter_id')]
        qos_rules_with_sla.set(len(sla_rules))
        qos_rules_metered.set(len(metered))

        # 规则会被删，旧标签值必须显式清掉，否则 Prometheus 里残留
        # 已不存在规则的比值（Gauge 的 label 组合不会自动回收）
        live_labels = set()
        for rule in sla_rules:
            name = str(rule.get('name') or rule.get('id'))
            live_labels.add(name)
            target = rule.get('target_bps')
            if not target:
                continue
            measured = 0.0
            ps = rule.get('path_switches') or []
            if ps and self.graph.has_node(ps[0]['dpid']):
                u, out_port = ps[0]['dpid'], ps[0]['out_port']
                # 找到首跳出端口对应的那条边，取它的单向实测吞吐
                for nxt in self.graph.successors(u):
                    edge_attrs = self.graph[u][nxt]
                    if edge_attrs.get('src_port') == out_port:
                        measured = edge_attrs.get('oneway_bps') or 0
                        break
            ratio = measured / target if target > 0 else 0
            qos_rule_sla_ratio.labels(rule=name).set(ratio)
            qos_rule_sla_compliance.labels(rule=name, sla_type='bandwidth').set(min(ratio, 1.0))

        for stale in getattr(self, '_sla_label_cache', set()) - live_labels:
            qos_rule_sla_ratio.labels(rule=stale).set(0)
            qos_rule_sla_compliance.labels(rule=stale, sla_type='bandwidth').set(0)
        self._sla_label_cache = live_labels

        # 更新组播指标
        qos_multicast_groups.set(len(self.multicast_groups))
        qos_multicast_flows.set(len(self.multicast_flows))

        # 更新交换机流表数量指标
        for dpid in self.datapaths:
            flow_count = sum(1 for flows in self.flow_stats.get(dpid, {}).values())
            qos_switch_flows_installed.labels(dpid=self.sw_name(dpid)).set(flow_count)

    def sw_name(self, dpid):
        return f'{self.switch_name.get(f"{dpid:016x}", f"{dpid}")}'


