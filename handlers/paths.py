"""
路径计算、流表下发、拓扑数据构建。

State ownership:
- sw_name: pure helper，无状态
- add_flow: 纯发送 OFPFlowMod
- get_shortest_path / get_path_out_port / get_path_switches: 只读 self.graph
- update_topology: 重建 self.graph、self.inter_switch_ports、self.host_ip_to_dpid、self.hosts、self.critical_nodes、self.topology_data
- _build_topology_data: 只读 self.graph、self.hosts

依赖：handlers.metrics（间接通过 topology_loop 调用 update_prometheus_metrics）
"""

import time

import networkx as nx
from ryu.lib.packet import ethernet
from ryu.topology.api import get_switch, get_link, get_host

import utils
from utils import WeightPref


def sw_name(self, dpid):
    return f'{self.switch_name.get(f"{dpid:016x}", f"{dpid}")}'


def add_flow(self, datapath, priority, match, actions, buffer_id=None, idle_timeout=0, hard_timeout=0):
    ofproto = datapath.ofproto
    parser = datapath.ofproto_parser
    inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]

    if buffer_id:
        mod = parser.OFPFlowMod(datapath=datapath, buffer_id=buffer_id, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                                flags=ofproto.OFPFF_SEND_FLOW_REM)
    else:
        mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
                                match=match, instructions=inst,
                                idle_timeout=idle_timeout, hard_timeout=hard_timeout,
                                flags=ofproto.OFPFF_SEND_FLOW_REM)
    datapath.send_msg(mod)


def get_shortest_path(self, src, dst, preference=None):
    """使用 networkx Dijkstra 算法计算最短路径
    preference: WeightPref 枚举或 None（None = 跳数）
    """
    if src == dst:
        return [src]
    if src not in self.graph or dst not in self.graph:
        return []
    try:
        if preference == WeightPref.Bandwidth:
            def weight_bw(u, v, d):
                bw = d.get("bw", 0)
                return 1.0 / (bw + 0.001)
            path = nx.shortest_path(self.graph, src, dst, weight=weight_bw)
        elif preference == WeightPref.LossRate:
            path = nx.shortest_path(self.graph, src, dst, weight='loss')
        elif preference == WeightPref.LoadBalance:
            def weight_load(u, v, d):
                bw = d.get("bw", 0.1)
                load= d.get("total_bps", 0)
                return 1 / (bw - load) if bw > load else 999
            path = nx.shortest_path(self.graph, src, dst, weight=weight_load)
        elif preference == WeightPref.Latency:
            path = nx.shortest_path(self.graph, src, dst, weight='delay')
        elif preference == WeightPref.SmartAlgorithm:
            # todo 调用智能算法
            path = nx.shortest_path(self.graph, src, dst)
        else:
            path = nx.shortest_path(self.graph, src, dst)
        return path
    except nx.NetworkXNoPath:
        return []


def get_path_out_port(self, src, dst, dscp=None):
    """获取最短路径上下一个交换机的出端口"""
    path = get_shortest_path(self, src, dst, dscp)
    if not path or len(path) <= 1:
        return None
    next_hop = path[1]
    # 从 src 到 next_hop 的边的 src_port
    if self.graph.has_edge(src, next_hop):
        return self.graph[src][next_hop].get('src_port')
    return None


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


def update_topology(self):
    """重建拓扑 DiGraph，更新主机表、级联端口、关键节点"""
    if not self._topology_stable:
        if time.time() - self._start_time > 5:
            self.logger.info('Controller Init')
            self._topology_stable = True
        return

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
    for l in list(link_list):
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
    self.topology_data = _build_topology_data(self)

    # 找到出入度和为 1 或 2 的节点（不可替代节点）
    self.critical_nodes = {node for node, degree in self.graph.degree() if (degree == 1 or degree == 2)}
    if self.critical_nodes:
        self.logger.debug(f"Critical nodes (degree=1): {[sw_name(self, n) for n in self.critical_nodes]}")

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