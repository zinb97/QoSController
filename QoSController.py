"""
RYU SDN 控制器壳类。

本文件只包含 QoSController(RyuApp) 类的核心生命周期：
- __init__：初始化状态、WSGI 注册、启动后台 hub 线程、静默化 WSGI 日志
- 10 个 @set_ev_cls 事件 handlers：一行委托给 handlers.* 模块
- 5 个后台 hub 循环：循环体内部分派给 handlers.* 模块

所有业务逻辑已迁移到 handlers/ 子包。

启动方式保持不变：
    ryu-manager QoSController.py --wsapi-port 8080 --observe-links
"""

import json
import os
import time

from ryu.app.wsgi import WSGIApplication
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.lib import hub
from ryu.topology import event

# REST 控制器（保持向后兼容路径）
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

# handlers/ 子包（业务逻辑拆分）
import handlers.forwarding as forwarding
import handlers.multicast as multicast
import handlers.paths as paths
import handlers.qos_rules as qos_rules
import handlers.stats as stats
import handlers.topology as topology

# Prometheus 指标（re-export 自 handlers.metrics，保持外部抓取兼容）
from handlers.metrics import (
    qos_switches_count,
    qos_links_count,
    qos_hosts_count,
    qos_switch_packet_in,
    qos_switch_flows_installed,
    qos_link_bandwidth_usage,
    qos_link_delay,
    qos_link_loss,
    qos_network_total_bandwidth_bits,
    qos_rules_total,
    qos_rules_active,
    qos_multicast_groups,
    qos_multicast_flows,
    qos_controller_uptime,
    qos_topology_updates_total,
    qos_topology_update_duration,
)
from handlers.state import init_state

topology_api_name = 'topology_api'


class QoSController(app_manager.RyuApp):
    OFP_VERSIONS = [ofp_event.OFP_VERSION]

    _CONTEXTS = {
        'wsgi': WSGIApplication
    }

    def __init__(self, *args, **kwargs):
        super(QoSController, self).__init__(*args, **kwargs)

        # 初始化所有 self.* 状态属性
        init_state(self)

        # 抑制 WSGI HTTP 请求日志（避免淹没 topology 线程日志）
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

        # 启动 5 个后台 hub 线程
        self._topo_thread = hub.spawn(self._topology_loop)
        self._stats_thread = hub.spawn(self._stats_loop)
        self._mcast_load_thread = hub.spawn(self._multicast_load_loop)
        self._link_load_thread = hub.spawn(self._link_load_loop)
        self._reroute_thread = hub.spawn(self._reroute_multicast_loop)

    # ============ 5 个后台 hub 循环 ============

    def _topology_loop(self):
        """每 3s 重建 DiGraph 并刷新 Prometheus 指标"""
        while True:
            try:
                with self._lock:
                    paths.update_topology(self)
                    from handlers import metrics as metrics_mod
                    metrics_mod.update_prometheus_metrics(self)
            except Exception as e:
                self.logger.warning(f"Topology loop update failed: {e}")
            hub.sleep(3)

    def _stats_loop(self):
        """每 2s 请求端口统计并触发 load_balance 规则的重路由检查"""
        while True:
            try:
                with self._lock:
                    stats.request_port_stats(self)
                    qos_rules._check_and_update_load_balance_rules(self)
            except Exception as e:
                self.logger.warning(f"Port stats request failed: {e}")
            hub.sleep(2)

    def _multicast_load_loop(self):
        """每 5s 请求组播组源交换机的流表统计，计算组播组负载"""
        while True:
            try:
                with self._lock:
                    for multicast_addr, flow_info in self.multicast_active_flows.items():
                        src_dpid = flow_info.get('source_dpid')
                        src_ip = flow_info.get('source_ip')
                        if src_dpid and src_dpid in self.datapaths and src_ip:
                            stats.request_flow_stats(self, src_dpid, multicast_addr, src_ip)
            except Exception as e:
                self.logger.warning(f"Multicast load stats request failed: {e}")
            hub.sleep(5)

    def _link_load_loop(self):
        """每 5s 检查链路负载，连续 3 次 >80% 标记组播组为高负载"""
        while True:
            try:
                with self._lock:
                    stats._check_link_load(self)
            except Exception as e:
                self.logger.warning(f"Link load check failed: {e}")
            hub.sleep(5)

    def _reroute_multicast_loop(self):
        """每 10s 为高负载组播组重新计算路由并安装流表"""
        while True:
            try:
                with self._lock:
                    stats._reroute_high_load_multicast(self)
            except Exception as e:
                self.logger.warning(f"Reroute multicast failed: {e}")
            hub.sleep(10)

    # ============ 10 个 @set_ev_cls 事件 handlers（委托） ============

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features_handler(self, ev):
        topology.switch_features_handler(self, ev)

    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        topology.switch_enter_handler(self, ev)

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        topology.switch_leave_handler(self, ev)

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        topology.link_add_handler(self, ev)

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        topology.link_delete_handler(self, ev)

    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev):
        topology.host_add_handler(self, ev)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        stats.port_stats_reply_handler(self, ev)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply_handler(self, ev):
        stats.flow_stats_reply_handler(self, ev)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed_handler(self, ev):
        stats.flow_removed_handler(self, ev)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        forwarding.packet_in_handler(self, ev)