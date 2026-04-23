from typing import List

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.ofproto import ofproto_v1_3, ether
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp, tcp, udp, ipv6, igmp
from collections import deque

import json
import os
import time
import numpy as np
import networkx as nx
from webob import Response

import utils
from client import RL_multicast_tree

topology_api_name = 'topology_api'


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

        # 拓扑相关 - 使用 networkx DiGraph
        self.graph = nx.DiGraph()        # 保存完整拓扑（节点=交换机，边=链路）
        self.adj_matrix_nodes = []       # 邻接矩阵的节点顺序列表（索引->dpid映射）
        self.host_ip_to_dpid = {}        # 主机IP -> 所在交换机DPID 映射
        self.topology_data = {"switches": [], "links": [], "hosts": []}

        # 活跃流表追踪
        # {(src_ip, dst_ip, dscp): {'path': [dpid1, dpid2, ...], 'path_switches': [...]}}
        self.active_flows = {}

        # 端口统计（用于计算链路负载）
        self.port_stats = {}            # {dpid: {port_no: {'rx_bytes': ..., 'tx_bytes': ..., 'timestamp': ...}}}
        self.port_stats_prev = {}       # {dpid: {port_no: {'rx_bytes': ..., 'tx_bytes': ..., 'timestamp': ...}}}

        self.switch_name = utils.get_switch_name()

        # 注册 Web API
        wsgi = kwargs['wsgi']
        wsgi.register(TopologyController, {topology_api_name: self})

        self._topo_thread = hub.spawn(self._topology_loop)
        self._stats_thread = hub.spawn(self._stats_loop)

    def _topology_loop(self):
        while True:
            try:
                self.update_topology()
            except Exception as e:
                self.logger.warning(f"Topology loop update failed: {e}")
            hub.sleep(3)

    def _stats_loop(self):
        """定期请求交换机端口统计信息"""
        while True:
            try:
                self._request_port_stats()
            except Exception as e:
                self.logger.warning(f"Port stats request failed: {e}")
            hub.sleep(2)

    def _recalculate_affected_flows(self):
        """重新计算受拓扑变化影响的流"""
        affected_flows = []
        for (src_ip, dst_ip, dscp), flow_info in list(self.active_flows.items()):
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
                affected_flows.append((src_ip, dst_ip, dscp))

        if affected_flows:
            self.logger.info(f"[Route] 需要重新计算的流: {len(affected_flows)}")

        # 重新计算每条受影响的流
        for src_ip, dst_ip, dscp in affected_flows:
            flow_info = self.active_flows.get((src_ip, dst_ip, dscp))
            if not flow_info:
                continue

            dst_host = flow_info['dst_host']
            src_host = flow_info['src_host']

            # 检查目的主机是否可达
            if dst_host['dpid'] not in self.datapaths:
                self.logger.warning(f"[Route] 目的主机不可达，删除流 {src_ip} -> {dst_ip}")
                del self.active_flows[(src_ip, dst_ip, dscp)]
                continue

            # 删除旧流表
            self._delete_flow(src_ip, dst_ip, dscp)

            # 重新计算路径
            new_path = self.get_shortest_path(src_host['dpid'], dst_host['dpid'], dscp)
            if not new_path or len(new_path) < 2:
                self.logger.warning(f"[Route] 重新计算路径失败，删除流 {src_ip} -> {dst_ip}")
                del self.active_flows[(src_ip, dst_ip, dscp)]
                continue

            self.logger.info(f"[Route] 重新计算路径: {' -> '.join(self.switch_name.get(f'{p:016x}', f'{p}') for p in new_path)}")

            # 重新构建 path_switches
            path_switches = self.get_path_switches(new_path, src_host['dpid'], src_host['port'],
                                                   dst_host['dpid'], dst_host['port'])
            self._add_path_flow(src_ip, dst_ip, dscp, path_switches)
            # 更新活跃流信息
            self.active_flows[(src_ip, dst_ip, dscp)] = {
                'path': new_path,
                'path_switches': path_switches,
                'src_host': src_host,
                'dst_host': dst_host
            }
            self.logger.info(f"[Route] 流表重新下发完成: {src_ip} -> {dst_ip} {dscp}")

    def _add_path_flow(self, src_ip, dst_ip, dscp, path_switches):
        # 重新路径流表
        priority = 101 if dscp else 10
        for ps in path_switches:
            sw_dp = self.datapaths.get(ps['dpid'])
            if sw_dp:
                parser = sw_dp.ofproto_parser
                if dscp:
                    match = parser.OFPMatch(in_port=ps['in_port'], eth_type=ether.ETH_TYPE_IP,
                                            ipv4_src=src_ip, ipv4_dst=dst_ip, ip_dscp=dscp)
                else:
                    match = parser.OFPMatch(in_port=ps['in_port'], eth_type=ether.ETH_TYPE_IP,
                                            ipv4_src=src_ip, ipv4_dst=dst_ip)
                actions = [parser.OFPActionOutput(ps['out_port'])]
                self.add_flow(sw_dp, priority, match, actions, idle_timeout=120)

                # 反向流表
                if dscp:
                    match = parser.OFPMatch(in_port=ps['out_port'], eth_type=ether.ETH_TYPE_IP,
                                            ipv4_src=dst_ip, ipv4_dst=src_ip, ip_dscp=dscp)
                else:
                    match = parser.OFPMatch(in_port=ps['out_port'], eth_type=ether.ETH_TYPE_IP,
                                            ipv4_src=dst_ip, ipv4_dst=src_ip)
                actions = [parser.OFPActionOutput(ps['in_port'])]
                self.add_flow(sw_dp, priority, match, actions, idle_timeout=120)
                # self.logger.info(f"[Flow] : -> {ps['in_port']} : {self.sw_name(ps['dpid'])} : {ps['out_port']} ->")

    def _delete_flow(self, src_ip, dst_ip, dscp=None):
        """删除指定流的流表"""
        flow_key = (src_ip, dst_ip, dscp) if dscp else (src_ip, dst_ip)
        flow_info = self.active_flows.get(flow_key)
        if not flow_info:
            return

        for ps in flow_info['path_switches']:
            sw_dp = self.datapaths.get(ps['dpid'])
            if sw_dp:
                parser = sw_dp.ofproto_parser
                ofproto = sw_dp.ofproto
                # 删除正向流
                if dscp:
                    match = parser.OFPMatch(eth_type=ether.ETH_TYPE_IP, ipv4_src=src_ip, ipv4_dst=dst_ip, ip_dscp=dscp)
                else:
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
                if dscp:
                    match = parser.OFPMatch(eth_type=ether.ETH_TYPE_IP, ipv4_src=dst_ip, ipv4_dst=src_ip, ip_dscp=dscp)
                else:
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

    def _check_update_paths(self):
        """检查所有活跃流是否有更优路径可走"""
        for (src_ip, dst_ip, dscp), flow_info in list(self.active_flows.items()):
            src_host = flow_info['src_host']
            dst_host = flow_info['dst_host']
            current_path = flow_info['path']

            # 检查源和目的主机是否可达
            if src_host['dpid'] not in self.datapaths or dst_host['dpid'] not in self.datapaths:
                continue

            # 计算新最优路径
            new_path = self.get_shortest_path(src_host['dpid'], dst_host['dpid'], dscp)
            if not new_path or len(new_path) < 2:
                continue

            # 如果路径相同，跳过
            if new_path == current_path:
                continue
            else:
                self.logger.info(f"[Route] 发现更优路径: {src_ip} -> {dst_ip} {dscp}")
                self.logger.info(f"[Route] 新路径: {' -> '.join(self.sw_name(p) for p in new_path)}")

                # 删除旧流表
                self._delete_flow(src_ip, dst_ip, dscp)

                # 构建新路径的 path_switches
                path_switches = self.get_path_switches(new_path, src_host['dpid'], src_host['port'],
                                                       dst_host['dpid'], dst_host['port'])

                # 下发新流表
                self._add_path_flow(src_ip, dst_ip, dscp, path_switches)

                # 更新活跃流信息
                self.active_flows[(src_ip, dst_ip, dscp)] = {
                    'path': new_path,
                    'path_switches': path_switches,
                    'src_host': src_host,
                    'dst_host': dst_host
                }
                self.logger.info(f"[Route] 流表更新完成: {src_ip} -> {dst_ip}")

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

    # ==================== 拓扑事件回调 ====================
    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self.logger.info(f"交换机上线: DPID={ev.switch.dp.id}")
        self.datapaths[ev.switch.dp.id] = ev.switch.dp
        self.update_topology()

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self.logger.info(f"交换机离线: DPID={dpid}")
        self.datapaths.pop(dpid, None)
        self.update_topology()
        # 重新计算受影响的流
        self._recalculate_affected_flows()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        self.logger.info(f"新增链路: {ev.link.src.dpid}:{ev.link.src.port_no} <-> {ev.link.dst.dpid}:{ev.link.dst.port_no}")
        self.update_topology()
        # 检查是否有更优路径可更新
        self._check_update_paths()

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        self.logger.info(f"删除链路: {ev.link.src.dpid}:{ev.link.src.port_no} <-> {ev.link.dst.dpid}:{ev.link.dst.port_no}")
        self.update_topology()
        # 重新计算受影响的流
        self._recalculate_affected_flows()

    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev):
        self.logger.info(f"主机加入: MAC={ev.host.mac}, IP={ev.host.ipv4}, Switch={ev.host.port.dpid}:{ev.host.port.port_no}")
        self.update_topology()

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply_handler(self, ev):
        """处理端口统计应答，计算链路负载"""
        msg = ev.msg
        dpid = msg.datapath.id
        now = time.time()

        # 保存当前统计
        self.port_stats[dpid] = {}

        for stat in msg.body:
            port_no = stat.port_no
            # 跳过特殊端口
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

                    # 计算负载 (Mbps)
                    rx_bps = (rx_bytes_delta * 8) / time_delta / 1_000_000
                    tx_bps = (tx_bytes_delta * 8) / time_delta / 1_000_000

                    self.port_stats[dpid][port_no]['rx_bps'] = round(rx_bps, 2)
                    self.port_stats[dpid][port_no]['tx_bps'] = round(tx_bps, 2)
                    self.port_stats[dpid][port_no]['total_bps'] = round(rx_bps + tx_bps, 2)

        # 保存本次统计作为下一次的前一个
        self.port_stats_prev[dpid] = self.port_stats[dpid]

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
        self.logger.info(f"[Switch Init] Clear all flow entries on Switch {datapath.id}")

        # 表项0：默认上送控制器
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions, idle_timeout=0)

        for dscp in [4, 8, 16, 32]:
            match = parser.OFPMatch(eth_type=0x0800, ip_dscp=dscp)
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
            self.add_flow(datapath, 100, match, actions, idle_timeout=0)

        # 表项：默认丢弃所有 UDP 组播报文（优先级低于具体组播组规则）
        match = parser.OFPMatch(eth_type=0x0800, ip_proto=17, ipv4_dst=("224.0.0.0", "240.0.0.0"))
        actions = []  # 空动作 = 丢弃
        self.add_flow(datapath, 1, match, actions, idle_timeout=0)

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

        # IPv4 处理（独立函数）
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
        self.logger.info(f"[PacketIn] SW:{dpid} {arp_op_name} ETH:{src_eth}-->{dst_eth} | ARP:{src_mac}({src_ip_arp})-->{dst_mac}({dst_ip_arp})")

        self._learn_host(dpid, in_port, src_eth, src_ip_arp, proto_tag="ARP")

        # ARP 请求
        if arp_opcode == arp.ARP_REQUEST:
            if dst_ip_arp in self.ip_to_mac:
                target_mac = self.ip_to_mac[dst_ip_arp]
                self.send_arp_reply(datapath, eth_pkt, src_mac, src_ip_arp, target_mac, dst_ip_arp, in_port)
                self.logger.info(f"[ARP Reply] SW:{dpid} Reply {dst_ip_arp} is {target_mac}")
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
        """
        统一处理 IPv4 报文（TCP/UDP/ICMP）
        """
        ofproto, parser = datapath.ofproto, datapath.ofproto_parser
        dpid = datapath.id
        sw_name = self.sw_name(dpid)
        dst_eth, src_eth = eth.dst, eth.src
        src_ip, dst_ip = ipv4_pkt.src, ipv4_pkt.dst
        proto = ipv4_pkt.proto
        tos = ipv4_pkt.tos
        dscp = tos >> 2

        # 解析四层协议
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)
        icmp_pkt = pkt.get_protocol(icmp.icmp)

        self._learn_host(dpid, in_port, src_eth, src_ip, proto_tag="IPv4")

        # 日志输出
        if tcp_pkt:
            self.logger.info(f"[PacketIn] SW:{sw_name} TCP {src_ip}:{tcp_pkt.src_port}-->{dst_ip}:{tcp_pkt.dst_port} "
                             f"{f'dscp:{dscp}' if dscp else ''}")
        elif udp_pkt:
            self.logger.info(f"[PacketIn] SW:{sw_name} UDP {src_ip}:{udp_pkt.src_port}-->{dst_ip}:{udp_pkt.dst_port} "
                             f"{f'dscp:{dscp}' if dscp else ''}")
        elif icmp_pkt:
            icmp_type = icmp_pkt.type
            if icmp_type == icmp.ICMP_ECHO_REQUEST:
                self.logger.info(f"[PacketIn] SW:{sw_name} ICMP Echo Request {src_ip}-->{dst_ip} "
                                 f"{f'dscp:{dscp}' if dscp else ''}")
            elif icmp_type == icmp.ICMP_ECHO_REPLY:
                self.logger.info(f"[PacketIn] SW:{sw_name} ICMP Echo Reply {src_ip}-->{dst_ip} "
                                 f"{f'dscp:{dscp}' if dscp else ''}")
            else:
                self.logger.info(f"[PacketIn] SW:{sw_name} ICMP Type:{icmp_type} {src_ip}-->{dst_ip} "
                                 f"{f'dscp:{dscp}' if dscp else ''}")
        else:
            self.logger.info(f"[PacketIn] SW:{sw_name} proto:{str(proto)} {src_ip}-->{dst_ip} "
                             f"{f'dscp:{dscp}' if dscp else ''}")

        # UDP 组播报文直接丢弃
        if udp_pkt and self._is_multicast_ip(dst_ip):
            return

        if dst_ip in self.ip_to_mac:
            dst_eth = self.ip_to_mac[dst_ip]
        else:
            self.logger.info(f"[PacketIn] SW:{sw_name} {dst_ip} not in ip_to_mac")

        # 路由与转发决策
        if dst_eth not in self.hosts:
            self.send_arp_request_for_ip(datapath, src_eth, src_ip, dst_ip)
            self.logger.info(f"[ARP Flood] 模拟 {src_ip}({src_eth}) 发送 ARP 请求查询 {dst_ip}")
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

        # 跨交换机：计算完整路径并下发双向流表
        path = self.get_shortest_path(src_dpid, dst_dpid, dscp)
        if not path or len(path) < 2:
            self.logger.warning(f"[Route] No path found from {self.sw_name(src_dpid)} to {self.sw_name(dst_dpid)}")
            return

        path_switches = self.get_path_switches(path, src_dpid, src_port, dst_dpid, dst_port)
        self.logger.info(f"[Route] {src_ip}->{dst_ip} Path: " +
                         ' -> '.join(f'{p["in_port"]}:{self.sw_name(p["dpid"])}:{p["out_port"]}'
                                     for p in path_switches))

        self._add_path_flow(src_ip, dst_ip, dscp, path_switches)

        # 保存活跃流信息
        flow_key = (src_ip, dst_ip, dscp)
        self.active_flows[flow_key] = {
            'path': path,
            'path_switches': path_switches,
            'src_host': {'mac': src_eth, 'dpid': src_dpid, 'port': src_port},
            'dst_host': {'mac': dst_eth, 'dpid': dst_dpid, 'port': dst_port}
        }

        # 发送当前报文
        out_port = path_switches[0]['out_port']
        for ps in path_switches:
            if dpid == ps.get('dpid'):
                out_port = ps.get('out_port')
                break
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
        multicast_addr = igmp_pkt.address
        msgtype = igmp_pkt.msgtype

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
            # 当组播组有对应的源IP时，安装组播流规则
            if multicast_addr in self.multicast_sources:
                self._install_multicast_flow_rules(multicast_addr)
        # msgtype 0x17 = Leave (离开组播组)
        elif msgtype == 0x17:
            self.remove_member_from_group(dpid, src_mac, multicast_addr)

    def _install_multicast_flow_rules(self, multicast_addr):
        """
        为指定组播组安装组播流规则
        根据 multicast_sources 获取源 IP，根据 multicast_groups 获取组成员信息
        """
        # 检查该组播组是否有源IP和成员
        if multicast_addr not in self.multicast_sources:
            self.logger.info(f"[Multicast Flow] Group {multicast_addr} has no source IP, skipping flow installation")
            return False

        if multicast_addr not in self.multicast_groups or not self.multicast_groups[multicast_addr]:
            self.logger.info(f"[Multicast Flow] Group {multicast_addr} has no members, skipping flow installation")
            return False

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
            self.adj_matrix_nodes = sorted(self.graph.nodes())
            # 构建邻接矩阵节点索引和交换机 DPID 之间的的对应关系
            adj_matrix = nx.to_numpy_array(self.graph, nodelist=self.adj_matrix_nodes, dtype=np.float32)
            src_switch_idx = self.adj_matrix_nodes.index(src_switch_dpid)
            src_switch_id = src_switch_idx + 1
            member_idx_set = [self.adj_matrix_nodes.index(dpid) for dpid in member_dpid_set]
            member_id_list = [idx + 1 for idx in member_idx_set]

            self.logger.info(f"[Multicast Route] Source: {src_ip} -> SW:{src_switch_id}")
            self.logger.info(f"[Multicast Route] Member Hosts in Destination Group: {member_ips}")
            self.logger.info(f"[Multicast Route] Member Switches: {member_id_list}")

            net_size = len(self.adj_matrix_nodes)

            # 获取延迟、带宽、丢包矩阵
            latency_matrix = nx.attr_matrix(self.graph, edge_attr='delay', rc_order=self.adj_matrix_nodes, dtype=np.float32)
            bandwidth_matrix = nx.attr_matrix(self.graph, edge_attr='bw', rc_order=self.adj_matrix_nodes, dtype=np.float32)
            loss_matrix = nx.attr_matrix(self.graph, edge_attr='loss', rc_order=self.adj_matrix_nodes, dtype=np.float32)

            # 调用智能组播路由构建算法构建组播路由, 1-based:source_node,destination_group
            is_success, route_matrix = RL_multicast_tree(adj_matrix, latency_matrix, bandwidth_matrix, loss_matrix,
                                                         source_node=src_switch_id,
                                                         destination_group=member_id_list)
            if is_success:
                # 将邻接矩阵的双向性变成从源到目的集合的单向性矩阵（BFS）
                src_idx = self.adj_matrix_nodes.index(src_switch_dpid)
                visited = set()
                queue = deque([src_idx])
                visited.add(src_idx)

                while queue:
                    i = queue.popleft()
                    for j in range(net_size):
                        if route_matrix[i, j] == 1:
                            route_matrix[j, i] = 0  # 删除反向边，确保单向性
                            if j not in visited:
                                queue.append(j)
                                visited.add(j)

                # 根据单向性矩阵安装组播流规则
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

                    # 遍历 route_matrix[i][j] == 1，转发到下一跳交换机（源/中间交换机）
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

    # ==================== 组播组管理 ====================
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

                    # 如果所有交换机都没有该组播组的成员了，删除该组播组
                    if not self.multicast_groups[multicast_addr]:
                        del self.multicast_groups[multicast_addr]
                        # 清理组播源记录
                        if multicast_addr in self.multicast_sources:
                            del self.multicast_sources[multicast_addr]
                        # 清理组播流规则
                        if multicast_addr in self.multicast_flow_installed:
                            self._delete_multicast_flows(multicast_addr)
                            self.multicast_flow_installed.discard(multicast_addr)
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
        打印当前所有组播组信息
        """
        self.logger.info("========== Multicast Group State ==========")
        if not self.multicast_groups:
            self.logger.info("  No multicast members")
        for group, dpid_dict in self.multicast_groups.items():
            self.logger.info(f"  Group {group}:")
            for dpid, mac_dict in dpid_dict.items():
                self.logger.info(f"    Switch {dpid}:")
                for mac, (src_ip, port) in mac_dict.items():
                    self.logger.info(f"      MAC={mac}, IP={src_ip}, Port={port}")
        self.logger.info("=========================================")

    def _is_multicast_ip(self, ip):
        """检查是否为组播 IP 地址 (224.0.0.0 - 239.255.255.255)"""
        ip_parts = [int(x) for x in ip.split('.')]
        return ip_parts[0] >= 224 and ip_parts[0] <= 239

    # ==================== 【新增】通用主机学习函数 ====================
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

            # 从 graph 获取交换机端口信息
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
        try:
            switch_list = get_switch(self, None)
            link_list = get_link(self, None)
            host_list = get_host(self, None)
        except TypeError:
            switch_list = get_switch(self)
            link_list = get_link(self)
            host_list = get_host(self)

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
        for l in link_list:
            src_dpid = l.src.dpid
            src_port = l.src.port_no
            dst_dpid = l.dst.dpid
            dst_port = l.dst.port_no

            eth_name = f'{self.switch_name.get(f"{src_dpid:016x}")}-eth{src_port}'
            bw, delay, loss = utils.get_tc_stats_optimized(eth_name)

            # 获取链路负载（从端口统计计算）
            rx_bps = 0
            tx_bps = 0
            total_bps = 0
            if src_dpid in self.port_stats and src_port in self.port_stats[src_dpid]:
                rx_bps = self.port_stats[src_dpid][src_port].get('rx_bps', 0)
                tx_bps = self.port_stats[src_dpid][src_port].get('tx_bps', 0)
                total_bps = self.port_stats[src_dpid][src_port].get('total_bps', 0)
            elif dst_dpid in self.port_stats and dst_port in self.port_stats[dst_dpid]:
                rx_bps = self.port_stats[dst_dpid][dst_port].get('rx_bps', 0)
                tx_bps = self.port_stats[dst_dpid][dst_port].get('tx_bps', 0)
                total_bps = self.port_stats[dst_dpid][dst_port].get('total_bps', 0)

            # 添加双向边（保存端口和链路状态信息）
            self.graph.add_edge(src_dpid, dst_dpid,
                               src_port=src_port, dst_port=dst_port,
                               bw=bw, delay=delay, loss=loss,
                               rx_bps=rx_bps, tx_bps=tx_bps, total_bps=total_bps)

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
                'loss': attrs.get('loss', 0),
                'load': {
                    'rx_bps': attrs.get('rx_bps', 0),
                    'tx_bps': attrs.get('tx_bps', 0),
                    'total_bps': attrs.get('total_bps', 0)
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
                                    idle_timeout=idle_timeout, hard_timeout=hard_timeout)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst,
                                    idle_timeout=idle_timeout, hard_timeout=hard_timeout)
        datapath.send_msg(mod)

        # ==================== 最短路径（使用 networkx） ====================

    def get_shortest_path(self, src, dst, dscp=None) -> List[int]:
        """使用 networkx Dijkstra 算法计算最短路径
        dscp: 1=时延优先, 2=负载优先, 3=丢包率优先, 4=带宽优先, None=跳数
        """
        if src == dst:
            return [src]
        if src not in self.graph or dst not in self.graph:
            return []
        try:
            if dscp == 4:
                def weight_bw(u, v, d):
                    bw = d.get("bw", 0)
                    return 1.0 / (bw + 0.001)
                path = nx.shortest_path(self.graph, src, dst, weight=weight_bw)
            elif dscp == 8:
                path = nx.shortest_path(self.graph, src, dst, weight='loss')
            elif dscp == 16:
                def weight_load(u, v, d):
                    bw = d.get("bw", 0.1)
                    load= d.get("total_bps", 0)
                    return 1 / (bw - load) if bw != load else 999

                path = nx.shortest_path(self.graph, src, dst, weight=weight_load)
            elif dscp == 32:
                path = nx.shortest_path(self.graph, src, dst, weight='delay')
            else:
                path = nx.shortest_path(self.graph, src, dst)
            return path
        except nx.NetworkXNoPath:
            return []

    def get_path_out_port(self, src, dst, dscp=None):
        """获取最短路径上下一个交换机的出端口"""
        path = self.get_shortest_path(src, dst, dscp)
        if not path or len(path) <= 1:
            return None
        next_hop = path[1]
        # 从 src 到 next_hop 的边的 src_port
        if self.graph.has_edge(src, next_hop):
            return self.graph[src][next_hop].get('src_port')
        return None

    def _request_port_stats(self):
        """向所有交换机发送端口统计请求"""
        for dpid, datapath in self.datapaths.items():
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            req = parser.OFPPortStatsRequest(datapath, port_no=ofproto.OFPP_ANY)
            datapath.send_msg(req)

    def sw_name(self, dpid):
        return f'{self.switch_name.get(f"{dpid:016x}", f"{dpid}")}'


# ===================== Web 服务 =====================
class TopologyController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(TopologyController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('topology', '/topology/data', methods=['GET'])
    def get_topo_data(self, req):
        body = json.dumps(self.switch_app.topology_data, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/flows/active', methods=['GET'])
    def get_active_flows(self, req):
        """获取所有活跃流"""
        flows = []
        for (src_ip, dst_ip, dscp), flow_info in self.switch_app.active_flows.items():
            path_names = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in flow_info['path']]
            flows.append({
                'src_ip': src_ip,
                'dst_ip': dst_ip,
                'dscp': dscp,
                'path': path_names,
                'path_dpids': [int(p) for p in flow_info['path']],
                'src_host': flow_info['src_host'],
                'dst_host': flow_info['dst_host']
            })
        body = json.dumps(flows, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/', methods=['GET'])
    def index(self, req):
        try:
            html_path = os.path.join(os.path.dirname(__file__), 'topology_web.html')
            with open(html_path, 'r', encoding='utf-8') as f:
                html = f.read()
            return Response(content_type='text/html; charset=UTF-8', body=html.encode('utf-8'))
        except Exception as e:
            error_msg = f"<html><body><h1>Error loading topology page</h1><p>{str(e)}</p></body></html>"
            return Response(content_type='text/html; charset=UTF-8', body=error_msg.encode('utf-8'), status=500)