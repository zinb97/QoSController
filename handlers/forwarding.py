"""
Packet-in 分发、ARP、IPv4 处理、主机学习、洪泛辅助函数。

State ownership（读/写）：
- self.hosts, self.ip_to_mac（_learn_host）
- self.inter_switch_ports（_learn_host 用于判断是否级联端口）
- self.active_flows（handle_ipv4 跨交换机转发时记录）
- self.datapaths（send_packet_out 等）

依赖：
- handlers.paths（add_flow, get_shortest_path, get_path_switches, sw_name）
- handlers.qos_rules（_add_path_flow）
- handlers.metrics（间接通过 packet_in 触发 qos_switch_packet_in.inc）
"""

from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp, tcp, udp
from ryu.ofproto import ether

import handlers.metrics as metrics
import handlers.paths as paths
import handlers.qos_rules as qos_rules


def _is_multicast_ip(self, ip):
    """检查是否为组播 IP 地址 (224.0.0.0 - 239.255.255.255)

    为避免循环依赖（forwarding → multicast），本函数保留在 forwarding.py，
    但 multicast.py 内部需要时也会 import 它。
    """
    ip_parts = [int(x) for x in ip.split('.')]
    return ip_parts[0] >= 224 and ip_parts[0] <= 239


def packet_in_handler(self, ev):
    """PacketIn 分发：IGMP → ARP → IPv4"""
    msg = ev.msg
    datapath = msg.datapath
    in_port = msg.match['in_port']

    pkt = packet.Packet(msg.data)
    eth = pkt.get_protocol(ethernet.ethernet)

    # 忽略 LLDP / IPv6
    if eth.ethertype in (ether.ETH_TYPE_LLDP, ether.ETH_TYPE_IPV6):
        return

    # 更新 Prometheus packet-in 指标
    metrics.qos_switch_packet_in.labels(dpid=paths.sw_name(self, datapath.id)).inc()

    arp_pkt = pkt.get_protocol(arp.arp)
    ipv4_pkt = pkt.get_protocol(ipv4.ipv4)

    # IGMP 处理（在 multicast 模块中实现）
    if ipv4_pkt and ipv4_pkt.proto == 2:  # IGMP protocol number
        from ryu.lib.packet import igmp as igmp_mod
        from handlers import multicast  # 延迟导入避免循环
        igmp_pkt = pkt.get_protocol(igmp_mod.igmp)
        if igmp_pkt:
            multicast.handle_igmp(self, datapath, in_port, eth, ipv4_pkt, igmp_pkt)
            return

    # ARP 处理
    if arp_pkt:
        handle_arp(self, datapath, in_port, msg, eth, arp_pkt)
        return

    # IPv4 处理
    if ipv4_pkt:
        handle_ipv4(self, datapath, in_port, msg, eth, ipv4_pkt, pkt)
        return


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
    self.logger.info(f"[PacketIn] {paths.sw_name(self, dpid)} {arp_op_name} ETH:{src_eth}-->{dst_eth} "
                     f"| ARP:{src_mac}({src_ip_arp})-->{dst_mac}({dst_ip_arp})")

    _learn_host(self, dpid, in_port, src_eth, src_ip_arp, proto_tag="ARP")

    # ARP 请求
    if arp_opcode == arp.ARP_REQUEST:
        if dst_ip_arp in self.ip_to_mac:
            target_mac = self.ip_to_mac[dst_ip_arp]
            send_arp_reply(self, datapath, eth_pkt, src_mac, src_ip_arp, target_mac, dst_ip_arp, in_port)
            self.logger.info(f"[ARP Reply] {paths.sw_name(self, dpid)} Reply {dst_ip_arp} is {target_mac}")
        else:
            flood_arp_to_all_switches(self, msg.data)
            self.logger.info(f"[ARP Flood] unknown IP {dst_ip_arp}")

    # ARP 响应
    elif arp_opcode == arp.ARP_REPLY:
        if dst_mac in self.hosts:
            target_dpid = self.hosts[dst_mac]['dpid']
            target_port = self.hosts[dst_mac]['port']
            if target_dpid in self.datapaths:
                target_dp = self.datapaths[target_dpid]
                send_packet_out(self, target_dp, target_port, msg.data)
                self.logger.info(f"[ARP Forward] ARP Reply → Host {dst_mac} (SW:{target_dpid}, Port:{target_port})")
            else:
                self.logger.warning(f"[ARP Forward] 目标交换机不存在: {target_dpid}")
        else:
            self.logger.warning(f"[ARP Forward] 未知目标主机: {dst_mac}，丢弃")


def handle_ipv4(self, datapath, in_port, msg, eth, ipv4_pkt, pkt):
    """统一处理 IPv4 报文（TCP/UDP/ICMP）"""
    ofproto, parser = datapath.ofproto, datapath.ofproto_parser
    dpid = datapath.id
    sw_name = paths.sw_name(self, dpid)
    dst_eth, src_eth = eth.dst, eth.src
    src_ip, dst_ip = ipv4_pkt.src, ipv4_pkt.dst
    proto = ipv4_pkt.proto

    # 解析四层协议
    tcp_pkt = pkt.get_protocol(tcp.tcp)
    udp_pkt = pkt.get_protocol(udp.udp)
    icmp_pkt = pkt.get_protocol(icmp.icmp)

    _learn_host(self, dpid, in_port, src_eth, src_ip, proto_tag="IPv4")

    # 日志输出
    if tcp_pkt:
        self.logger.info(f"[PacketIn] SW:{sw_name} TCP {src_ip}:{tcp_pkt.src_port}-->{dst_ip}:{tcp_pkt.dst_port}")
    elif udp_pkt:
        self.logger.info(f"[PacketIn] SW:{sw_name} UDP {src_ip}:{udp_pkt.src_port}-->{dst_ip}:{udp_pkt.dst_port}")
    elif icmp_pkt:
        icmp_type = icmp_pkt.type
        if icmp_type == icmp.ICMP_ECHO_REQUEST:
            self.logger.info(f"[PacketIn] SW:{sw_name} ICMP Echo Request {src_ip}-->{dst_ip}")
        elif icmp_type == icmp.ICMP_ECHO_REPLY:
            self.logger.info(f"[PacketIn] SW:{sw_name} ICMP Echo Reply {src_ip}-->{dst_ip}")
        else:
            self.logger.info(f"[PacketIn] SW:{sw_name} ICMP Type:{icmp_type} {src_ip}-->{dst_ip}")
    else:
        self.logger.info(f"[PacketIn] SW:{sw_name} proto:{str(proto)} {src_ip}-->{dst_ip}")

    # UDP 组播报文直接丢弃
    if udp_pkt and _is_multicast_ip(self, dst_ip):
        return

    if dst_ip.endswith("255"):
        return

    if dst_ip in self.ip_to_mac:
        dst_eth = self.ip_to_mac[dst_ip]
    else:
        self.logger.info(f"[PacketIn] SW:{sw_name} {dst_ip} not in ip_to_mac")

    # 路由与转发决策
    if dst_eth not in self.hosts:
        send_arp_request_for_ip(self, datapath, src_eth, src_ip, dst_ip)
        self.logger.info(f"[ARP Flood] 模拟 {src_ip}({src_eth}) 发送 ARP 请求查询 {dst_ip}")
        flood_packet_to_all_host_ports(self, msg.data)
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
            paths.add_flow(self, datapath, 101, flow_match, actions)
        # 发送当前报文
        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data
        )
        datapath.send_msg(out)
        return

    # 跨交换机：计算完整路径并下发双向流表
    path = paths.get_shortest_path(self, src_dpid, dst_dpid)
    if not path or len(path) < 2:
        self.logger.warning(f"[Route] No path found from {paths.sw_name(self, src_dpid)} to {paths.sw_name(self, dst_dpid)}")
        return

    path_switches = paths.get_path_switches(self, path, src_dpid, src_port, dst_dpid, dst_port)
    self.logger.info(f"[Route] {src_ip}->{dst_ip} Path: " +
                     ' -> '.join(f'{p["in_port"]}:{paths.sw_name(self, p["dpid"])}:{p["out_port"]}'
                                 for p in path_switches))

    qos_rules._add_path_flow(self, src_ip, dst_ip, path_switches)

    # 保存活跃流信息
    self.active_flows[(src_ip, dst_ip)] = {
        'type': 'flow',
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
    send_packet_out(self, datapath, in_port, pkt.data)


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
    flood_packet_to_all_host_ports(self, pkt.data)


def flood_arp_to_all_switches(self, data):
    flood_packet_to_all_host_ports(self, data)


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
    flood_arp_to_all_switches(self, pkt.data)
    self.logger.info(f"[ARP Flood Trigger] 已泛洪针对 IP {target_ip} 的ARP请求")