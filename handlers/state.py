"""
QoSController 实例状态初始化。

把所有 self.* 实例属性的初始化代码集中在这里，让壳类 __init__ 保持简洁。

State ownership（声明）：
- 核心数据：hosts, ip_to_mac, inter_switch_ports, datapaths
- 组播：multicast_groups, multicast_flows, multicast_flow_installed,
       multicast_sources, multicast_weights, multicast_active_flows,
       multicast_load, multicast_high_load, multicast_reroute_enabled
- QoS 规则：qos_rules, qos_rule_counter
- 拓扑：graph, adj_matrix_nodes, host_ip_to_dpid, topology_data,
       critical_nodes, switch_name
- 流追踪：active_flows, link_high_load_count
- 端口/流统计：port_stats, port_stats_prev, flow_stats, flow_stats_prev
- 杂项：_lock, _link_delete_pending, _link_delete_delay, link_event_enabled,
       routing_algorithm, _topology_stable, _start_time
"""

import threading
import time

import utils


def init_state(self):
    """初始化所有 self.* 实例属性（在 super().__init__() 之后调用）"""
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

    # 拓扑相关 - 使用 networkx DiGraph
    self.graph = None               # 由 paths.update_topology(self) 重建
    self.adj_matrix_nodes = []       # 邻接矩阵的节点顺序列表（索引->dpid映射）
    self.host_ip_to_dpid = {}        # 主机IP -> 所在交换机DPID 映射
    self.topology_data = {"switches": [], "links": [], "hosts": []}

    self._topology_stable = False
    self._start_time = time.time()

    # 活跃流表追踪
    # {(src_ip, dst_ip): {'path': [dpid1, dpid2, ...], 'path_switches': [...]}}
    self.active_flows = {}

    # 组播活跃流追踪
    # {multicast_addr: {...}}
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

    self._lock = threading.Lock()

    self.switch_name = utils.get_switch_name()

    # 链路抖动去抖动机制
    self._link_delete_pending = {}  # {(src_dpid, src_port, dst_dpid, dst_port): pending_timer}
    self._link_delete_delay = 0.01     # 延迟秒数再删除链路

    # 链路事件处理开关
    self.link_event_enabled = True  # 默认开启
    # 组播组重路由开关
    self.multicast_reroute_enabled = False  # 默认关闭