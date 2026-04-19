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

        # --- 核心数据存储 ---
        self.hosts = {}                 # mac -> {ip, dpid, port, mac}
        self.ip_to_mac = {}             # ip -> mac
        self.inter_switch_ports = {}    # { dpid: set(port_no, ...) }
        self.datapaths = {}             # 新增：保存所有交换机

        # --- 拓扑相关 ---
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
        self.datapaths[ev.switch.dp.id] = ev.switch.dp  # 新增
        self.update_topology()

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self.logger.info(f"交换机离线: DPID={dpid}")
        self.datapaths.pop(dpid, None)  # 新增
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

        mod = parser.OFPFlowMod(datapath=datapath, command=ofproto.OFPFC_DELETE,
                                out_port=ofproto.OFPP_ANY, out_group=ofproto.OFPG_ANY)
        datapath.send_msg(mod)
        self.logger.info(f"[Switch Init] Clear all flow entries on Switch {datapath.id}")

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

        # 忽略 LLDP 数据包
        if eth.ethertype == ether.ETH_TYPE_LLDP:
            return
        # 忽略 IPv6 数据包
        if eth.ethertype == ether.ETH_TYPE_IPV6:
            return

        dst_eth, src_eth = eth.dst, eth.src
        dpid = datapath.id

        arp_pkt = pkt.get_protocol(arp.arp)
        ipv4_pkt = pkt.get_protocol(ipv4.ipv4)

        if arp_pkt:
            src_ip_arp, dst_ip_arp = arp_pkt.src_ip, arp_pkt.dst_ip
            src_mac, dst_mac = arp_pkt.src_mac, arp_pkt.dst_mac
            arp_opcode = arp_pkt.opcode
            arp_htype = arp_pkt.hwtype
            arp_ptype = arp_pkt.proto

            # 2. 映射操作码为可读名称（兼容 RARP 等扩展）
            arp_op_name = {
                1: "ARP Request",
                2: "ARP Reply",
                3: "RARP Request",
                4: "RARP Reply"
            }.get(arp_opcode, f"ARP Opcode {arp_opcode}")

            # 3. 硬件/协议类型快速校验（可选，过滤非以太网+IPv4的ARP）
            if arp_htype != 1 or arp_ptype != 0x0800:
                self.logger.debug(f"[PacketIn] 非以太网IPv4 ARP，忽略: HTYPE={arp_htype}, PTYPE={hex(arp_ptype)}")
                return

            # 4. 结构化日志输出（参照 IPv4 格式，突出 MAC-IP 绑定）
            self.logger.info(
                f"[PacketIn] SW:{dpid} {arp_op_name} "
                f"ETH: {src_eth}-->{dst_eth} | "
                f"ARP: {src_mac}({src_ip_arp})-->{dst_mac}({dst_ip_arp})"
            )

            is_inter_switch_port = dpid in self.inter_switch_ports and in_port in self.inter_switch_ports[dpid]
            if not is_inter_switch_port:
                if src_eth not in self.hosts:
                    self.hosts[src_eth] = {'mac': src_mac, 'ip': src_ip_arp, 'dpid': dpid, 'port': in_port}
                    self.logger.info(f"[Host Learn] MAC:{src_mac} IP:{src_ip_arp} located at Switch {dpid} Port {in_port}")
                elif self.hosts[src_eth]['dpid'] != dpid or self.hosts[src_eth]['port'] != in_port:
                    self.hosts[src_eth] = {'mac': src_mac, 'ip': src_ip_arp, 'dpid': dpid, 'port': in_port}
                    self.logger.info(f"[Host Move] MAC:{src_mac} IP:{src_ip_arp} located at Switch {dpid} Port {in_port}")
                else:
                    if src_ip_arp and not self.hosts[src_eth]['ip']:
                        self.hosts[src_eth]['ip'] = src_ip_arp

                if src_ip_arp:
                    self.ip_to_mac[src_ip_arp] = src_eth

            # # ARP 包使用二层匹配 (Match dst MAC)
            # flow_match = parser.OFPMatch(in_port=in_port, eth_dst=dst_eth)
            flow_match = None

        elif ipv4_pkt:
            src_ip = ipv4_pkt.src
            dst_ip = ipv4_pkt.dst
            proto = ipv4_pkt.proto

            tcp_pkt = pkt.get_protocol(tcp.tcp)
            udp_pkt = pkt.get_protocol(udp.udp)
            icmp_pkt = pkt.get_protocol(icmp.icmp)

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

            # IPv4 包使用三层匹配 (Match dst IP)
            flow_match = parser.OFPMatch(in_port=in_port, eth_type=ether.ETH_TYPE_IP,ipv4_dst=dst_ip)
            dst_eth = self.ip_to_mac[dst_ip] if dst_ip in self.ip_to_mac else dst_eth

        else:
            return

        # ---------------- 路由决策逻辑 ----------------
        if dst_eth in self.hosts:
            # 已知单播：正常转发
            dst_dpid = self.hosts[dst_eth]['dpid']
            dst_port = self.hosts[dst_eth]['port']

            if dpid == dst_dpid:
                out_port = dst_port
                actions = [parser.OFPActionOutput(out_port)]
            else:
                path = self.get_shortest_path(dpid, dst_dpid)
                if path and len(path) > 1:
                    next_hop = path[1]
                    out_port = self.graph[dpid][next_hop]
                    actions = [parser.OFPActionOutput(out_port)]
                else:
                    return
        else:
            if arp_pkt:
                # ==================== 关键修改：未知ARP由控制器全网广播 ====================
                self.logger.info(f"[ARP Flood] 未知IP {arp_pkt.dst_ip}，控制器向全网泛洪")
                self.flood_arp_to_all_switches(msg.data)
                return  # 已由控制器处理，当前包不再转发
            else:
                return  # 其他未知包直接丢弃

        # ---------------- 下发流表 & 发送包 ----------------
        if out_port != ofproto.OFPP_FLOOD and flow_match:
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, flow_match, actions, msg.buffer_id)
                return
            else:
                self.add_flow(datapath, 1, flow_match, actions)

        data = msg.data if msg.buffer_id == ofproto.OFP_NO_BUFFER else None
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data
        )
        datapath.send_msg(out)

    # ==================== 【核心新增】向所有交换机广播ARP ====================
    def flood_arp_to_all_switches(self, data):
        """控制器将ARP包发给所有交换机，每个交换机只向主机端口泛洪"""
        for dpid, dp in self.datapaths.items():
            ofproto = dp.ofproto
            parser = dp.ofproto_parser

            # 获取该交换机的 非级联端口（主机端口）
            inter_ports = self.inter_switch_ports.get(dpid, set())
            flood_ports = []
            for sw in self.topology_data['switches']:
                if sw['dpid'] == dpid:
                    for p in sw['ports']:
                        if p not in inter_ports:
                            flood_ports.append(p)
                    break

            if not flood_ports:
                continue

            # 批量输出到主机端口
            actions = [parser.OFPActionOutput(p) for p in flood_ports]
            out = parser.OFPPacketOut(
                datapath=dp, buffer_id=ofproto.OFP_NO_BUFFER,
                in_port=ofproto.OFPP_CONTROLLER,
                actions=actions, data=data
            )
            dp.send_msg(out)

    # ==================== 拓扑数据更新 ====================
    def update_topology(self):
        """调用 Ryu API 获取最新拓扑并更新 graph / links / topology_data /（适度更新 hosts）"""
        try:
            switch_list = get_switch(self, None)
            link_list = get_link(self, None)
            host_list = get_host(self, None)
        except TypeError:
            switch_list = get_switch(self)
            link_list = get_link(self)
            host_list = get_host(self)

        # 初始化邻接图
        self.graph = {s.dp.id: {} for s in switch_list}
        self.inter_switch_ports = {s.dp.id: set() for s in switch_list}
        links = []

        # 更新链路信息 + 构建双向图
        for l in link_list:
            src_dpid = l.src.dpid
            src_port = l.src.port_no
            dst_dpid = l.dst.dpid
            dst_port = l.dst.port_no

            links.append({
                'src': {'dpid': src_dpid, 'port': src_port},
                'dst': {'dpid': dst_dpid, 'port': dst_port}
            })

            if src_dpid in self.graph:
                self.graph[src_dpid][dst_dpid] = src_port
            if dst_dpid in self.graph:
                self.graph[dst_dpid][src_dpid] = dst_port

            # --- 新增：标记这些端口为级联端口 ---
            if src_dpid in self.inter_switch_ports:
                self.inter_switch_ports[src_dpid].add(src_port)
            if dst_dpid in self.inter_switch_ports:
                self.inter_switch_ports[dst_dpid].add(dst_port)

        self.links = links

        # 更新主机信息（拓扑发现作为补充：不用 None 覆盖已有 IP）
        topo_hosts = []
        for h in host_list:
            if not h.port:
                continue

            mac = h.mac
            ip = None
            if getattr(h, "ipv4", None):
                try:
                    ip = next(iter(h.ipv4), None)
                except Exception:
                    ip = None

            old = self.hosts.get(mac)
            if old:
                # 位置可更新，但 IP 不用 None 覆盖
                old['dpid'] = h.port.dpid
                old['port'] = h.port.port_no
                if ip:
                    old['ip'] = ip
            else:
                self.hosts[mac] = {'mac': mac, 'ip': ip, 'dpid': h.port.dpid, 'port': h.port.port_no}

            topo_hosts.append({'mac': mac, 'ip': self.hosts[mac].get('ip'), 'dpid': self.hosts[mac]['dpid'], 'port': self.hosts[mac]['port']})

        # Web UI switches 信息
        switches_info = []
        for s in switch_list:
            dpid_hex = format(s.dp.id, '016x')
            level = int(dpid_hex[8:10], 16) if len(dpid_hex) >= 10 else 3
            switches_info.append({
                'dpid': s.dp.id,
                'level': level,
                'ports': [p.port_no for p in s.ports]
            })

        self.topology_data = {'switches': switches_info, 'links': self.links, 'hosts': topo_hosts}
        self.logger.debug(f"Topology updated: {len(switches_info)} switches, {len(self.links)} links, {len(topo_hosts)} hosts")

    # ==================== 辅助方法：流表操作 ====================
    def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=30, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        if buffer_id:
            mod = parser.OFPFlowMod(
                datapath=datapath, buffer_id=buffer_id,
                priority=priority, match=match,
                instructions=inst,
                idle_timeout=idle_timeout,
                hard_timeout=hard_timeout
            )
        else:
            mod = parser.OFPFlowMod(
                datapath=datapath, priority=priority,
                match=match, instructions=inst,
                idle_timeout=idle_timeout, hard_timeout=hard_timeout
            )
        datapath.send_msg(mod)

    # ==================== 辅助方法：最短路径计算 (BFS) ====================
    def get_shortest_path(self, src, dst):
        """计算 src dpid 到 dst dpid 的最短路径"""
        if src == dst:
            return [src]

        # 简单的 BFS 实现
        visited = {}
        queue = []
        parent = {}

        for dpid in self.graph.keys():
            visited[dpid] = False
            parent[dpid] = None

        visited[src] = True
        queue.append(src)

        while queue:
            current = queue.pop(0)
            if current == dst:
                path = []
                while current is not None:
                    path.insert(0, current)
                    current = parent[current]
                return path

            if current not in self.graph:
                continue

            for neighbor in self.graph[current]:
                if not visited.get(neighbor, False):
                    visited[neighbor] = True
                    parent[neighbor] = current
                    queue.append(neighbor)
        return None


# ===================== Web 服务控制器 =====================
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