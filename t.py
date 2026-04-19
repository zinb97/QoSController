from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_0
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import arp
from ryu.lib.packet import ipv4
from ryu.lib.packet import icmp
from ryu.lib import mac


class IPForwarder(app_manager.RyuApp):
    # 指定使用 OpenFlow 1.0 协议
    OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super(IPForwarder, self).__init__(*args, **kwargs)
        # 核心数据结构：ARP表（IP -> MAC映射） + 端口表（MAC -> 交换机端口映射）
        # 格式：{dpid: {ip: mac}}
        self.arp_table = {}
        # 格式：{dpid: {mac: port}}
        self.mac_to_port = {}

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        """
        交换机首次连接时的初始化：下发默认流表（未匹配的报文上送控制器）
        """
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 下发 Table-miss 流表：优先级最低，所有未匹配报文上送控制器
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions):
        """
        辅助函数：向交换机下发流表
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 构造 FlowMod 消息
        mod = parser.OFPFlowMod(
            datapath=datapath,
            match=match,
            cookie=0,
            command=ofproto.OFPFC_ADD,
            idle_timeout=0,  # 0表示永久有效
            hard_timeout=0,
            priority=priority,
            flags=ofproto.OFPFF_SEND_FLOW_REM,
            actions=actions
        )
        datapath.send_msg(mod)

    def send_packet_out(self, datapath, port, pkt_data):
        """
        辅助函数：向交换机发送 PacketOut 消息（直接转发报文）
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        actions = [parser.OFPActionOutput(port)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER,
            actions=actions,
            data=pkt_data
        )
        datapath.send_msg(out)

    def handle_arp(self, datapath, in_port, eth_pkt, arp_pkt):
        """
        处理 ARP 报文：学习 ARP 表 + 响应 ARP 请求
        """
        dpid = datapath.id
        src_mac = arp_pkt.src_mac
        src_ip = arp_pkt.src_ip
        dst_ip = arp_pkt.dst_ip

        # 1. 初始化交换机的 ARP 表和 MAC 端口表
        if dpid not in self.arp_table:
            self.arp_table[dpid] = {}
        if dpid not in self.mac_to_port:
            self.mac_to_port[dpid] = {}

        # 2. 学习：记录源IP -> 源MAC，源MAC -> 入端口
        self.arp_table[dpid][src_ip] = src_mac
        self.mac_to_port[dpid][src_mac] = in_port
        self.logger.info(f"[ARP Learn] SW:{dpid} IP:{src_ip} -> MAC:{src_mac} Port:{in_port}")

        # 3. 处理 ARP 请求（opcode=1）
        if arp_pkt.opcode == arp.ARP_REQUEST:
            # 检查是否知道目标IP的MAC
            if dst_ip in self.arp_table[dpid]:
                # 知道目标MAC：构造 ARP 响应并发送
                dst_mac = self.arp_table[dpid][dst_ip]
                self.send_arp_reply(datapath, eth_pkt, arp_pkt, src_mac, src_ip, dst_mac, dst_ip, in_port)
                self.logger.info(f"[ARP Reply] SW:{dpid} Reply {dst_ip} is {dst_mac}")
            else:
                # 不知道目标MAC：泛洪 ARP 请求（除入端口外的所有端口）
                self.flood_packet(datapath, in_port, eth_pkt)
                self.logger.info(f"[ARP Flood] SW:{dpid} Flood ARP Request for {dst_ip}")

        # 4. 处理 ARP 响应（opcode=2）：直接转发到请求方
        elif arp_pkt.opcode == arp.ARP_REPLY:
            if dst_mac in self.mac_to_port[dpid]:
                out_port = self.mac_to_port[dpid][dst_mac]
                self.send_packet_out(datapath, out_port, eth_pkt.data)
                self.logger.info(f"[ARP Forward] SW:{dpid} Forward ARP Reply to Port:{out_port}")

    def send_arp_reply(self, datapath, eth_pkt, arp_pkt, src_mac, src_ip, dst_mac, dst_ip, in_port):
        """
        构造并发送 ARP 响应报文
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 构造 ARP 响应包
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=eth_pkt.ethertype,
            src=dst_mac,
            dst=src_mac
        ))
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=dst_mac,
            src_ip=dst_ip,
            dst_mac=src_mac,
            dst_ip=src_ip
        ))
        pkt.serialize()

        # 发送 PacketOut
        self.send_packet_out(datapath, in_port, pkt.data)

    def flood_packet(self, datapath, in_port, pkt_data):
        """
        泛洪报文（除入端口外的所有端口）
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        actions = [parser.OFPActionOutput(ofproto.OFPP_FLOOD)]
        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=in_port,
            actions=actions,
            data=pkt_data
        )
        datapath.send_msg(out)

    def handle_ipv4(self, datapath, in_port, eth_pkt, ipv4_pkt):
        """
        处理 IPv4 报文：查 ARP 表 + 下发流表 + 转发
        """
        dpid = datapath.id
        src_ip = ipv4_pkt.src
        dst_ip = ipv4_pkt.dst
        src_mac = eth_pkt.src
        dst_mac = eth_pkt.dst

        # 1. 初始化表（防止首次运行报错）
        if dpid not in self.arp_table:
            self.arp_table[dpid] = {}
        if dpid not in self.mac_to_port:
            self.mac_to_port[dpid] = {}

        # 2. 记录源 MAC -> 端口（二层学习）
        self.mac_to_port[dpid][src_mac] = in_port

        # 3. 检查是否知道目标 IP 的 MAC
        if dst_ip not in self.arp_table[dpid]:
            # 不知道目标 MAC：先泛洪 ARP 请求，暂不转发 IP 报文
            self.logger.info(f"[IP Forward] SW:{dpid} Unknown MAC for {dst_ip}, Flood ARP")
            self.send_arp_request(datapath, src_ip, src_mac, dst_ip, in_port)
            return

        # 4. 知道目标 MAC：获取下一跳信息
        target_mac = self.arp_table[dpid][dst_ip]
        if target_mac not in self.mac_to_port[dpid]:
            # 目标 MAC 对应的端口未知，泛洪
            self.flood_packet(datapath, in_port, eth_pkt.data)
            return

        out_port = self.mac_to_port[dpid][target_mac]
        self.logger.info(f"[IP Forward] SW:{dpid} {src_ip}-->{dst_ip} Port:{out_port}")

        # 5. 下发流表（后续同流报文直接由交换机转发，无需上送控制器）
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 匹配字段：IPv4 + 目标 IP
        match = parser.OFPMatch(
            dl_type=ethernet.ether.ETH_TYPE_IP,
            nw_dst=ipv4_pkt.dst
        )
        # 动作：修改目的 MAC（可选，若为直连主机则无需修改） + 转发到端口
        actions = [
            parser.OFPActionSetDlDst(target_mac),
            parser.OFPActionOutput(out_port)
        ]
        # 下发流表，优先级设为 10（高于默认流表）
        self.add_flow(datapath, 10, match, actions)

        # 6. 发送当前报文（PacketOut）
        self.send_packet_out(datapath, out_port, eth_pkt.data)

    def send_arp_request(self, datapath, src_ip, src_mac, dst_ip, in_port):
        """
        构造并发送 ARP 请求报文（用于查询目标 IP 的 MAC）
        """
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # 构造 ARP 请求包
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            ethertype=ethernet.ether.ETH_TYPE_ARP,
            src=src_mac,
            dst=mac.BROADCAST_STR
        ))
        pkt.add_protocol(arp.arp(
            opcode=arp.ARP_REQUEST,
            src_mac=src_mac,
            src_ip=src_ip,
            dst_mac=mac.DONTCARE_STR,
            dst_ip=dst_ip
        ))
        pkt.serialize()

        # 泛洪 ARP 请求
        self.flood_packet(datapath, in_port, pkt.data)

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        """
        核心入口：处理所有 PacketIn 消息
        """
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        in_port = msg.in_port

        # 解析报文
        pkt = packet.Packet(msg.data)
        eth_pkt = pkt.get_protocol(ethernet.ethernet)
        if not eth_pkt:
            return

        # 分协议处理
        if eth_pkt.ethertype == ethernet.ether.ETH_TYPE_ARP:
            # ARP 报文
            arp_pkt = pkt.get_protocol(arp.arp)
            if arp_pkt:
                self.handle_arp(datapath, in_port, eth_pkt, arp_pkt)
        elif eth_pkt.ethertype == ethernet.ether.ETH_TYPE_IP:
            # IPv4 报文
            ipv4_pkt = pkt.get_protocol(ipv4.ipv4)
            if ipv4_pkt:
                self.handle_ipv4(datapath, in_port, eth_pkt, ipv4_pkt)