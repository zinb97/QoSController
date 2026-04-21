from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.ofproto import ofproto_v1_3, ether
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp, tcp, udp, ipv6, igmp

import json
import os
import time
import networkx as nx
from webob import Response

import utils

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

        # 拓扑相关 - 使用 networkx DiGraph
        self.graph = nx.DiGraph()        # 保存完整拓扑（节点=交换机，边=链路）
        self.topology_data = {"switches": [], "links": [], "hosts": []}

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

    def _request_port_stats(self):
        """向所有交换机发送端口统计请求"""
        for dpid, datapath in self.datapaths.items():
            ofproto = datapath.ofproto
            parser = datapath.ofproto_parser
            req = parser.OFPPortStatsRequest(datapath, port_no=ofproto.OFPP_ANY)
            datapath.send_msg(req)

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

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        self.logger.info(f"新增链路: {ev.link.src.dpid}:{ev.link.src.port_no} <-> {ev.link.dst.dpid}:{ev.link.dst.port_no}")
        self.update_topology()

    @set_ev_cls(event.EventLinkDelete)
    def link_delete_handler(self, ev):
        self.logger.info(f"删除链路: {ev.link.src.dpid}:{ev.link.src.port_no} <-> {ev.link.dst.dpid}:{ev.link.dst.port_no}")
        self.update_topology()

    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev):
        self.logger.info(f"主机加入: MAC={ev.host.mac}, IP={ev.host.ipv4}, Switch={ev.host.port.dpid}:{ev.host.port.port_no}")
        self.update_topology()

    # ==================== 交换机连接初始化 ====================
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
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

    # ==================== 核心：PacketIn 处理 ====================
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

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

    # ==================== 独立函数：IPv4 报文处理 ====================
    def handle_ipv4(self, datapath, in_port, msg, eth, ipv4_pkt, pkt):
        """
        统一处理 IPv4 报文（TCP/UDP/ICMP）
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        src_ip = ipv4_pkt.src
        dst_ip = ipv4_pkt.dst
        proto = ipv4_pkt.proto
        dst_eth, src_eth = eth.dst, eth.src

        # 解析四层协议
        tcp_pkt = pkt.get_protocol(tcp.tcp)
        udp_pkt = pkt.get_protocol(udp.udp)
        icmp_pkt = pkt.get_protocol(icmp.icmp)

        self._learn_host(dpid, in_port, src_eth, src_ip, proto_tag="IPv4")

        # 日志输出
        if tcp_pkt:
            src_port = tcp_pkt.src_port
            dst_port = tcp_pkt.dst_port
            self.logger.info(f"[PacketIn] SW:{dpid} TCP {src_ip}:{src_port}-->{dst_ip}:{dst_port}")
        elif udp_pkt:
            src_port = udp_pkt.src_port
            dst_port = udp_pkt.dst_port
            self.logger.info(f"[PacketIn] SW:{dpid} UDP {src_ip}:{src_port}-->{dst_ip}:{dst_port}")
        elif icmp_pkt:
            icmp_type = icmp_pkt.type
            if icmp_type == icmp.ICMP_ECHO_REQUEST:
                self.logger.info(f"[PacketIn] SW:{dpid} ICMP Echo Request {src_ip}-->{dst_ip}")
            elif icmp_type == icmp.ICMP_ECHO_REPLY:
                self.logger.info(f"[PacketIn] SW:{dpid} ICMP Echo Reply {src_ip}-->{dst_ip}")
            else:
                self.logger.info(f"[PacketIn] SW:{dpid} ICMP Type:{icmp_type} {src_ip}-->{dst_ip}")
        else:
            self.logger.info(f"[PacketIn] SW:{dpid} proto:{str(proto)} {src_ip}-->{dst_ip}")

        # UDP 组播报文处理
        if udp_pkt and self._is_multicast_ip(dst_ip):
            self.handle_udp_multicast(datapath, in_port, msg, eth, ipv4_pkt, src_ip, dst_ip)
            return

        # 流表匹配规则
        flow_match = parser.OFPMatch(in_port=in_port, eth_type=ether.ETH_TYPE_IP, ipv4_dst=dst_ip)
        if dst_ip in self.ip_to_mac:
            dst_eth = self.ip_to_mac[dst_ip]

        # 路由与转发决策
        if dst_eth not in self.hosts:
            self.send_arp_request_for_ip(datapath, src_eth, src_ip, dst_ip)
            return

        dst_dpid = self.hosts[dst_eth]['dpid']
        dst_port = self.hosts[dst_eth]['port']

        # 同交换机直接转发
        if dpid == dst_dpid:
            out_port = dst_port
            actions = [parser.OFPActionOutput(out_port)]
        else:
            # 跨交换机最短路径
            out_port = self.get_path_out_port(dpid, dst_dpid)
            if not out_port:
                return
            actions = [parser.OFPActionOutput(out_port)]

        # 下发流表
        if out_port != ofproto.OFPP_FLOOD and flow_match:
            buffer_id = msg.buffer_id if msg.buffer_id != ofproto.OFP_NO_BUFFER else None
            self.add_flow(datapath, 1, flow_match, actions, buffer_id)

        # 发送当前报文
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data
        )
        datapath.send_msg(out)

    # ==================== IGMP 处理 ====================
    def handle_igmp(self, datapath, in_port, eth, ipv4_pkt, igmp_pkt):
        """
        处理 IGMP 报文（加入/离开组播组）
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

        # msgtype 0x12/0x16 = Join (加入组播组)
        if msgtype in [0x12, 0x16]:
            self.add_member_to_group(dpid, src_mac, src_ip, multicast_addr, in_port)
        # msgtype 0x17 = Leave (离开组播组)
        elif msgtype == 0x17:
            self.remove_member_from_group(dpid, src_mac, multicast_addr)

    # ==================== UDP 组播处理 ====================
    def handle_udp_multicast(self, datapath, in_port, msg, eth, ipv4_pkt, src_ip, dst_ip):
        """
        处理 UDP 组播报文，根据组播组成员决定是否转发
        """
        dpid = datapath.id
        src_mac = eth.src
        src_port = 0
        dst_port = 0

        self.logger.info(f"[UDP Multicast] SW:{dpid} UDP Multicast {src_ip}-->{dst_ip}")

        # 检查该组播组是否有成员
        if dst_ip in self.multicast_groups and self.multicast_groups[dst_ip]:
            # 收集所有成员信息
            member_ips = []
            for group_dpid, mac_dict in self.multicast_groups[dst_ip].items():
                for mac, (member_ip, member_port) in mac_dict.items():
                    member_ips.append(member_ip)

            self.logger.info(f"[Multicast Route] Source: {src_ip}, Destination Group: {member_ips}")

            # 转发到所有组播成员
            self.forward_to_multicast_members(datapath, in_port, msg, dst_ip)
        else:
            self.logger.info(f"[UDP Multicast] Group {dst_ip} has no members, dropping packet")

    def forward_to_multicast_members(self, datapath, in_port, msg, multicast_addr):
        """
        将组播数据转发到所有组播组成员
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        actions = []
        for group_dpid, mac_dict in self.multicast_groups[multicast_addr].items():
            for mac, (member_ip, member_port) in mac_dict.items():
                # 只转发到同一交换机上的成员，或者通过最短路径转发到其他交换机
                if group_dpid == dpid:
                    actions.append(parser.OFPActionOutput(member_port))
                else:
                    # 跨交换机转发 - 找到到目标交换机的出端口
                    out_port = self.get_path_out_port(dpid, group_dpid)
                    if out_port:
                        actions.append(parser.OFPActionOutput(out_port))

        if actions:
            out = parser.OFPPacketOut(
                datapath=datapath,
                buffer_id=msg.buffer_id,
                in_port=in_port,
                actions=actions,
                data=msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
            )
            datapath.send_msg(out)
            self.logger.info(f"[Multicast Forward] Forwarded to {len(actions)} member(s)")
        else:
            self.logger.info(f"[Multicast Forward] No valid path to members, dropping")

    def _is_multicast_ip(self, ip):
        """检查是否为组播 IP 地址 (224.0.0.0 - 239.255.255.255)"""
        ip_parts = [int(x) for x in ip.split('.')]
        return ip_parts[0] >= 224 and ip_parts[0] <= 239

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
                self.send_arp_reply(datapath, eth_pkt, arp_pkt, src_mac, src_ip_arp, target_mac, dst_ip_arp, in_port)
                self.logger.info(f"[ARP Reply] SW:{dpid} Reply {dst_ip_arp} is {target_mac}")
            else:
                self.flood_arp_to_all_switches(msg.data)
                self.logger.info(f"[ARP Flood] 未知IP {dst_ip_arp}，控制器向全网泛洪")

        # ARP 响应
        elif arp_opcode == arp.ARP_REPLY:
            if dst_mac in self.hosts:
                target_dpid = self.hosts[dst_mac]['dpid']
                target_port = self.hosts[dst_mac]['port']
                if target_dpid in self.datapaths:
                    target_dp = self.datapaths[target_dpid]
                    self.send_packet_out(target_dp, target_port, msg.data)
                    self.logger.info(f"[ARP Forward] 控制器转发ARP Reply → Host {dst_mac} (SW:{target_dpid}, Port:{target_port})")
                else:
                    self.logger.warning(f"[ARP Forward] 目标交换机不存在: {target_dpid}")
            else:
                self.logger.warning(f"[ARP Forward] 未知目标主机: {dst_mac}，丢弃")

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

    def send_arp_reply(self, datapath, eth_pkt, arp_pkt, src_mac, src_ip, dst_mac, dst_ip, in_port):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=eth_pkt.ethertype, src=dst_mac, dst=src_mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=dst_mac, src_ip=dst_ip, dst_mac=src_mac, dst_ip=src_ip))
        pkt.serialize()

        self.send_packet_out(datapath, in_port, pkt.data)

    def send_packet_out(self, datapath, port, pkt_data):
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
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

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

        self.logger.info(f"[ARP Probe] 模拟 {src_ip}({src_mac}) 发送 ARP 请求查询 {target_ip}")
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
    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=30, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

        if buffer_id:
            mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id, priority=priority,
                                    match=match, instructions=inst, idle_timeout=idle_timeout, hard_timeout=hard_timeout)
        else:
            mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                    match=match, instructions=inst, idle_timeout=idle_timeout, hard_timeout=hard_timeout)
        datapath.send_msg(mod)

        # ==================== 最短路径（使用 networkx） ====================
    def get_shortest_path(self, src, dst):
        """使用 networkx Dijkstra 算法计算最短路径"""
        if src == dst:
            return [src]
        if src not in self.graph or dst not in self.graph:
            return None
        try:
            path = nx.shortest_path(self.graph, src, dst)
            return path
        except nx.NetworkXNoPath:
            return None

    def get_path_out_port(self, src, dst):
        """获取最短路径上下一个交换机的出端口"""
        path = self.get_shortest_path(src, dst)
        if not path or len(path) <= 1:
            return None
        next_hop = path[1]
        # 从 src 到 next_hop 的边的 src_port
        if self.graph.has_edge(src, next_hop):
            return self.graph[src][next_hop].get('src_port')
        return None


# ===================== Web 服务 =====================
class TopologyController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(TopologyController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('topology', '/topology/data', methods=['GET'])
    def get_topo_data(self, req):
        body = json.dumps(self.switch_app.topology_data, ensure_ascii=False).encode('utf-8')
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