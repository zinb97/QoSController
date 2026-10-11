"""
OpenFlow 拓扑事件 handlers：
- switch_features_handler：交换机连接初始化（清空流表 + 默认上送/丢弃规则）
- switch_enter_handler / switch_leave_handler：交换机上下线
- link_add_handler / link_delete_handler：链路增删（含 _execute_link_delete 延迟删除）
- host_add_handler：主机加入

State ownership:
- self.inter_switch_ports：被 paths.update_topology 重建
- self._link_delete_pending：链路删除去抖定时器映射
- self.link_event_enabled：链路事件处理开关

依赖：
- handlers.paths（调用 update_topology）
- handlers.multicast（_install_multicast_flow_rules）
- handlers.qos_rules（_recalculate_affected_flows、_recalculate_affected_multicast_flows）
"""

from ryu.lib import hub

import handlers.multicast as multicast
import handlers.paths as paths
import handlers.qos_rules as qos_rules


def switch_features_handler(self, ev):
    """交换机连接初始化：清空旧流表 + 表项0默认上送 + 默认丢弃 UDP 组播"""
    datapath = ev.msg.datapath
    ofproto = datapath.ofproto
    parser = datapath.ofproto_parser

    # 清空旧流表
    mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE,
                            out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY)
    datapath.send_msg(mod)
    self.logger.info(f"[Switch Init] Init flows on Switch {paths.sw_name(self, datapath.id)}")

    # 表项0：默认上送控制器
    match = parser.OFPMatch()
    actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
    paths.add_flow(self, datapath, 0, match, actions, idle_timeout=0)

    # 表项：默认丢弃所有 UDP 组播报文（优先级低于具体组播组规则）
    match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=("224.0.0.0", "240.0.0.0"))
    actions = []  # 空动作 = 丢弃
    paths.add_flow(self, datapath, 1, match, actions, idle_timeout=0)


def switch_enter_handler(self, ev):
    """交换机上线：注册 datapath 并为未安装组播组补装流表"""
    try:
        self.logger.info(f"交换机上线: {paths.sw_name(self, ev.switch.dp.id)} DPID={ev.switch.dp.id:016x}")
        with self._lock:
            self.datapaths[ev.switch.dp.id] = ev.switch.dp
            paths.update_topology(self)
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
            multicast._install_multicast_flow_rules(self, multicast_addr)
            # 每个组播组之间间隔 5 秒
            # time.sleep(5)
    except Exception as e:
        self.logger.info(f'Event SwitchEnter {paths.sw_name(self, ev.switch.dp.id)} {e}')


def switch_leave_handler(self, ev):
    """交换机离线：从 datapaths 移除并重算受影响流"""
    dpid = ev.switch.dp.id
    self.logger.info(f"交换机离线: {dpid} DPID={dpid}")
    with self._lock:
        self.datapaths.pop(dpid, None)
        paths.update_topology(self)
    # 重新计算受影响的流
    qos_rules._recalculate_affected_flows(self)
    # 重新计算受影响的组播流
    qos_rules._recalculate_affected_multicast_flows(self)


def link_add_handler(self, ev):
    """链路新增：取消 pending delete、更新拓扑、检查更优路径、补装组播流"""
    if not self.link_event_enabled:
        self.logger.debug(f"[Link Event] Link add ignored (disabled): {paths.sw_name(self, ev.link.src.dpid)}:{ev.link.src.port_no} <-> {paths.sw_name(self, ev.link.dst.dpid)}:{ev.link.dst.port_no}")
        return

    link_key = (ev.link.src.dpid, ev.link.src.port_no, ev.link.dst.dpid, ev.link.dst.port_no)

    # 如果链路之前触发过 pending delete，取消删除
    if link_key in self._link_delete_pending:
        timer = self._link_delete_pending.pop(link_key)
        self.logger.info(f"链路恢复，取消删除: {paths.sw_name(self, ev.link.src.dpid)}:{ev.link.src.port_no} <-> "
                        f"{paths.sw_name(self, ev.link.dst.dpid)}:{ev.link.dst.port_no}")
        return

    self.logger.info(f"新增链路: {paths.sw_name(self, ev.link.src.dpid)}:{ev.link.src.port_no} <-> "
                     f"{paths.sw_name(self, ev.link.dst.dpid)}:{ev.link.dst.port_no}")
    with self._lock:
        paths.update_topology(self)
    # 检查是否有更优路径可更新
    qos_rules.check_better_path_for_active_flows(self)
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
        multicast._install_multicast_flow_rules(self, multicast_addr)
        # 每个组播组之间间隔 5 秒
        # time.sleep(5)


def _execute_link_delete(self, src_dpid, src_port, dst_dpid, dst_port):
    """真正执行链路删除：更新拓扑 + 重算单播和组播流"""
    self.logger.info(f"删除链路: {paths.sw_name(self, src_dpid)}:{src_port} <-> {paths.sw_name(self, dst_dpid)}:{dst_port}")
    with self._lock:
        paths.update_topology(self)
    qos_rules._recalculate_affected_flows(self)
    qos_rules._recalculate_affected_multicast_flows(self, deleted_link=(src_dpid, dst_dpid))


def link_delete_handler(self, ev):
    """链路删除：去抖延迟后真正删除（避免 LLDP 抖动）"""
    if not self.link_event_enabled:
        self.logger.debug(f"[Link Event] Link delete ignored (disabled): {paths.sw_name(self, ev.link.src.dpid)}:{ev.link.src.port_no} <-> {paths.sw_name(self, ev.link.dst.dpid)}:{ev.link.dst.port_no}")
        return

    link_key = (ev.link.src.dpid, ev.link.src.port_no, ev.link.dst.dpid, ev.link.dst.port_no)

    # 如果已经在 pending 状态，不再重复处理
    if link_key in self._link_delete_pending:
        return

    self.logger.warning(f"链路可能断开，等待确认: {paths.sw_name(self, ev.link.src.dpid)}:{ev.link.src.port_no} <-> "
                       f"{paths.sw_name(self, ev.link.dst.dpid)}:{ev.link.dst.port_no}")

    # 延迟删除，给链路恢复留出时间
    def delayed_delete():
        if link_key in self._link_delete_pending:
            del self._link_delete_pending[link_key]
            _execute_link_delete(self, ev.link.src.dpid, ev.link.src.port_no,
                                 ev.link.dst.dpid, ev.link.dst.port_no)

    self._link_delete_pending[link_key] = hub.spawn_after(self._link_delete_delay, delayed_delete)


def host_add_handler(self, ev):
    """主机加入：触发拓扑重建"""
    self.logger.info(f"主机加入: MAC={ev.host.mac}, IP={ev.host.ipv4}, "
                     f"Switch={paths.sw_name(self, ev.host.port.dpid)}:{ev.host.port.port_no}")
    paths.update_topology(self)