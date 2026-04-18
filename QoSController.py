from ryu.base import app_manager
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.topology import event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.lib import hub

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
        self.hosts = {}  # mac -> {ip, dpid, port, mac}

        # --- 拓扑相关 ---
        self.links = []  # Web 展示用
        self.graph = {}  # 邻接表: dpid -> {neighbor_dpid: out_port}
        self.topology_data = {"switches": [], "links": [], "hosts": []}

        # 注册 Web API
        wsgi = kwargs['wsgi']
        wsgi.register(TopologyController, {topology_api_name: self})

        # 启动定时线程，每3秒强制刷新一次拓扑（兜底机制）
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
        self.update_topology()

    @set_ev_cls(event.EventSwitchLeave)
    def switch_leave_handler(self, ev):
        dpid = ev.switch.dp.id
        self.logger.info(f"交换机离线: DPID={dpid}")
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
        self.graph = {}
        for s in switch_list:
            self.graph[s.dp.id] = {}

        # 更新链路信息 + 构建双向图
        links = []
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

        self.logger.debug(
            f"Topology updated: {len(switches_info)} switches, {len(self.links)} links, {len(topo_hosts)} hosts"
        )


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