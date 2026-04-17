from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet
from ryu.lib.packet import ether_types
from ryu.lib.packet import ipv4

from ryu.topology import event, switches
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
import json
import os
from webob import Response

# Web 界面路由配置
topology_api_name = 'topology_api'

class SimpleSwitchTopo(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    # 加载 WSGI Web 服务 + 拓扑服务
    _CONTEXTS = {
        'wsgi': WSGIApplication,
        'switches': switches.Switches
    }

    def __init__(self, *args, **kwargs):
        super(SimpleSwitchTopo, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.hosts = {}  # MAC -> {ip, dpid, port}
        self.topology_data = {}  # 存储拓扑数据

        # 注册 Web API
        wsgi = kwargs['wsgi']
        wsgi.register(TopologyController, {topology_api_name: self})

    # 交换机初始化：下发默认流表
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER, ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)

    def add_flow(self, datapath, priority, match, actions):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority, match=match, instructions=inst)
        datapath.send_msg(mod)

    # 包处理：二层转发 + 主机学习
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match['in_port']

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocols(ethernet.ethernet)[0]

        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        dpid = datapath.id

        # 学习主机 MAC/IP
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        if ip_pkt:
            self.hosts[src] = {
                'mac': src,
                'ip': ip_pkt.src,
                'dpid': dpid,
                'port': in_port
            }
            self.logger.debug(f"Host learned: {src} -> IP={ip_pkt.src}, Switch={dpid}, Port={in_port}")
        else:
            self.hosts[src] = {
                'mac': src,
                'ip': None,
                'dpid': dpid,
                'port': in_port
            }
            self.logger.debug(f"Host learned: {src} -> Switch={dpid}, Port={in_port} (no IP)")

        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        if dst in self.mac_to_port[dpid]:
            out_port = self.mac_to_port[dpid][dst]
        else:
            out_port = ofproto.OFPP_FLOOD

        actions = [parser.OFPActionOutput(out_port)]

        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            if msg.buffer_id != ofproto.OFP_NO_BUFFER:
                self.add_flow(datapath, 1, match, actions)
                return

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data

        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id, in_port=in_port,
            actions=actions, data=data)
        datapath.send_msg(out)

    # ===================== 拓扑发现事件 =====================
    @set_ev_cls(event.EventSwitchEnter)
    def switch_enter_handler(self, ev):
        self.logger.info(f"交换机上线: DPID={ev.switch.dp.id}")
        self.update_topology()

    @set_ev_cls(event.EventLinkAdd)
    def link_add_handler(self, ev):
        self.logger.info(f"新增链路: {ev.link.src} <-> {ev.link.dst}")
        self.update_topology()

    @set_ev_cls(event.EventHostAdd)
    def host_add_handler(self, ev):
        self.logger.info(f"主机加入: MAC={ev.host.mac} {ev.host}")
        self.update_topology()

    # 更新拓扑数据
    def update_topology(self):
        switch_list = get_switch(self)
        link_list = get_link(self)

        switches = []
        for s in switch_list:
            switches.append({
                'dpid': s.dp.id,
                'ports': [p.port_no for p in s.ports]
            })

        links = []
        for l in link_list:
            links.append({
                'src': {'dpid': l.src.dpid, 'port': l.src.port_no},
                'dst': {'dpid': l.dst.dpid, 'port': l.dst.port_no}
            })

        # 转换主机数据 - 使用 RYU 拓扑 API 获取的主机信息
        hosts = []
        host_list = get_host(self)
        for h in host_list:
            # h.mac 是 MAC 地址字符串
            # h.port 是 SwitchPort 对象 (dpid, port_no)
            # h.ipv4 是该主机拥有的 IP 地址集合
            if h.port:
                hosts.append({
                    'mac': h.mac,
                    'ip': list(h.ipv4)[0] if h.ipv4 else None,
                    'dpid': h.port.dpid,
                    'port': h.port.port_no
                })
                self.logger.debug(f"Host from API: {h.mac} @ Switch={h.port.dpid}:{h.port.port_no} IPs={h.ipv4}")

        self.topology_data = {
            'switches': switches,
            'links': links,
            'hosts': hosts
        }

# ===================== Web 服务（已修复所有报错） =====================
class TopologyController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(TopologyController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    # 提供拓扑 JSON 接口
    @route('topology', '/topology/data', methods=['GET'])
    def get_topo_data(self, req):
        return Response(
            content_type='application/json; charset=UTF-8',
            body=json.dumps(self.switch_app.topology_data)
        )

    # Web 主页（可视化页面）
    @route('topology', '/', methods=['GET'])
    def index(self, req):
        html_path = os.path.join(os.path.dirname(__file__), 'topology_web.html')
        with open(html_path, 'r', encoding='utf-8') as f:
            html = f.read()
        return Response(content_type='text/html; charset=UTF-8', body=html)