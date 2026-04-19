from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER, CONFIG_DISPATCHER
from ryu.ofproto import ofproto_v1_3, ether
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, arp, ipv4, icmp, tcp, udp, ipv6

import json
import os
from webob import Response

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

        # 拓扑相关
        self.links = []
        self.graph = {}
        self.topology_data = {"switches": [], "links": [], "hosts": []}

        # 注册 Web API
        wsgi = kwargs['wsgi']
        wsgi.register(TopologyController, {topology_api_name: self})

        self._topo_thread = hub.spawn(self._topology_loop)

    def _topology_loop(self):
        while True:
            try:
                self.update_topology()
            except Exception as e:
                self.logger.warning(f"Topology loop update failed: {e}")
            hub.sleep(3)

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
            path = self.get_shortest_path(dpid, dst_dpid)
            if not path or len(path) <= 1:
                return
            next_hop = path[1]
            out_port = self.graph[dpid][next_hop]
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
            for sw in self.topology_data['switches']:
                if sw['dpid'] == dpid:
                    flood_ports = [p for p in sw['ports'] if p not in inter_ports]
                    break

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

        # 初始化拓扑结构
        self.graph = {s.dp.id: {} for s in switch_list}
        self.inter_switch_ports = {s.dp.id: set() for s in switch_list}
        links = []

        # 构建链路与级联端口
        for l in link_list:
            src_dpid = l.src.dpid
            src_port = l.src.port_no
            dst_dpid = l.dst.dpid
            dst_port = l.dst.port_no

            links.append({'src': {'dpid': src_dpid, 'port': src_port}, 'dst': {'dpid': dst_dpid, 'port': dst_port}})

            if src_dpid in self.graph:
                self.graph[src_dpid][dst_dpid] = src_port
            if dst_dpid in self.graph:
                self.graph[dst_dpid][src_dpid] = dst_port

            if src_dpid in self.inter_switch_ports:
                self.inter_switch_ports[src_dpid].add(src_port)
            if dst_dpid in self.inter_switch_ports:
                self.inter_switch_ports[dst_dpid].add(dst_port)

        self.links = links

        # 主机信息更新
        topo_hosts = []
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

            topo_hosts.append({'mac': mac, 'ip': self.hosts[mac].get('ip'), 'dpid': self.hosts[mac]['dpid'], 'port': self.hosts[mac]['port']})

        # 交换机信息
        switches_info = []
        for s in switch_list:
            dpid_hex = format(s.dp.id, '016x')
            level = int(dpid_hex[8:10], 16) if len(dpid_hex) >= 10 else 3
            switches_info.append({'dpid': s.dp.id, 'level': level, 'ports': [p.port_no for p in s.ports]})

        self.topology_data = {'switches': switches_info, 'links': self.links, 'hosts': topo_hosts}
        self.logger.debug(f"Topology updated: {len(switches_info)} switches, {len(self.links)} links, {len(topo_hosts)} hosts")

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

    # ==================== 最短路径 BFS ====================
    def get_shortest_path(self, src, dst):
        if src == dst:
            return [src]

        visited = {dpid: False for dpid in self.graph.keys()}
        parent = {dpid: None for dpid in self.graph.keys()}
        queue = [src]
        visited[src] = True

        while queue:
            current = queue.pop(0)
            if current == dst:
                path = []
                while current is not None:
                    path.insert(0, current)
                    current = parent[current]
                return path

            for neighbor in self.graph.get(current, {}):
                if not visited[neighbor]:
                    visited[neighbor] = True
                    parent[neighbor] = current
                    queue.append(neighbor)
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