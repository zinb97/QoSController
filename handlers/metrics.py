"""
Prometheus 指标定义与更新函数。

State ownership:
- 全局 Prometheus metric 对象（模块级，prometheus_client 注册到全局 registry）
- 本模块的 update_prometheus_metrics(self) 读取以下 self.*：
  self.graph, self.hosts, self.qos_rules, self.multicast_groups,
  self.multicast_flows, self.flow_stats, self.datapaths,
  self._start_time
- 通过 paths.sw_name(self, dpid) 获取交换机名

注意：metric 对象必须只在 modules 顶层实例化一次，否则会触发
prometheus_client.DuplicatedTimeseries 错误。其他模块如需引用，
通过 `from handlers.metrics import qos_xxx_count` 共享。
"""

import time

from prometheus_client import Counter, Gauge, Histogram

import handlers.paths as paths

# ==================== Prometheus Metrics ====================
# 拓扑指标
qos_switches_count = Gauge('qos_switches_count', 'Number of switches in topology')
qos_links_count = Gauge('qos_links_count', 'Number of links in topology')
qos_hosts_count = Gauge('qos_hosts_count', 'Number of hosts in topology')

# 交换机指标
qos_switch_packet_in = Counter('qos_switch_packet_in_total', 'Total packet-in events', ['dpid'])
qos_switch_flows_installed = Gauge('qos_switch_flows_installed', 'Number of installed flows', ['dpid'])

# 链路指标
qos_link_bandwidth_usage = Gauge('qos_link_bandwidth_usage_bits', 'Link bandwidth usage in bits/s', ['src_dpid', 'dst_dpid'])
qos_link_delay = Gauge('qos_link_delay_ms', 'Link delay in milliseconds', ['src_dpid', 'dst_dpid'])
qos_link_loss = Gauge('qos_link_loss_percent', 'Link packet loss percentage', ['src_dpid', 'dst_dpid'])

# 网络总带宽指标
qos_network_total_bandwidth_bits = Gauge('qos_network_total_bandwidth_bits', 'Total network bandwidth usage in bits/s')

# QoS 规则指标
qos_rules_total = Gauge('qos_rules_total', 'Total number of QoS rules')
qos_rules_active = Gauge('qos_rules_active', 'Number of active QoS rules')

# 组播指标
qos_multicast_groups = Gauge('qos_multicast_groups', 'Number of active multicast groups')
qos_multicast_flows = Gauge('qos_multicast_flows', 'Number of multicast flows installed')

# 控制器性能指标
qos_controller_uptime = Gauge('qos_controller_uptime_seconds', 'Controller uptime in seconds')
qos_topology_updates_total = Counter('qos_topology_updates_total', 'Total topology updates')
qos_topology_update_duration = Histogram('qos_topology_update_duration_seconds', 'Topology update duration')


def update_prometheus_metrics(self):
    """更新 Prometheus 指标（在 topology_loop 中每 3s 调用）"""
    # 更新控制器运行时间
    qos_controller_uptime.set(time.time() - self._start_time)
    qos_topology_updates_total.inc()

    # 更新拓扑指标
    qos_switches_count.set(self.graph.number_of_nodes())
    qos_links_count.set(self.graph.number_of_edges())
    qos_hosts_count.set(len(self.hosts))

    # 更新链路指标
    total_bandwidth = 0
    for src, dst, attrs in self.graph.edges(data=True):
        src_name = paths.sw_name(self, src)
        dst_name = paths.sw_name(self, dst)
        link_bandwidth = attrs.get('total_bps', 0)  # Mbps
        qos_link_bandwidth_usage.labels(src_dpid=src_name, dst_dpid=dst_name).set(link_bandwidth)
        qos_link_delay.labels(src_dpid=src_name, dst_dpid=dst_name).set(attrs.get('delay', 0))
        qos_link_loss.labels(src_dpid=src_name, dst_dpid=dst_name).set(attrs.get('loss', 0))
        total_bandwidth += link_bandwidth

    # 更新网络总带宽指标
    qos_network_total_bandwidth_bits.set(total_bandwidth)

    # 更新 QoS 规则指标
    qos_rules_total.set(len(self.qos_rules))
    qos_rules_active.set(sum(1 for r in self.qos_rules.values() if r.get('enabled', True)))

    # 更新组播指标
    qos_multicast_groups.set(len(self.multicast_groups))
    qos_multicast_flows.set(len(self.multicast_flows))

    # 更新交换机流表数量指标
    for dpid in self.datapaths:
        flow_count = sum(1 for flows in self.flow_stats.get(dpid, {}).values())
        qos_switch_flows_installed.labels(dpid=paths.sw_name(self, dpid)).set(flow_count)