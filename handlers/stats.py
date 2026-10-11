"""
端口/流表统计 handlers、stats 请求、链路负载检测、组播重路由。

State ownership（读/写）：
- self.port_stats, self.port_stats_prev
- self.flow_stats, self.flow_stats_prev
- self.link_high_load_count
- self.multicast_load, self.multicast_high_load
- self.multicast_reroute_enabled（由 MulticastRerouteController 切换）

依赖：
- handlers.paths（add_flow, sw_name, get_path_out_port）
- handlers.multicast（_delete_multicast_flows, _install_multicast_flow_rules）
- client（RL_multicast_tree）
"""

from collections import deque
import time

import networkx as nx
import numpy as np
from ryu.controller import ofp_event
from ryu.controller.handler import set_ev_cls, MAIN_DISPATCHER
from ryu.ofproto import ofproto_v1_3

import handlers.multicast as multicast
import handlers.paths as paths
from client import RL_multicast_tree


def port_stats_reply_handler(self, ev):
    """处理端口统计应答，计算链路负载"""
    msg = ev.msg
    dpid = msg.datapath.id
    now = time.time()

    self.port_stats[dpid] = {}

    for stat in msg.body:
        port_no = stat.port_no
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
                rx_bps = (rx_bytes_delta * 8) / time_delta / 1_000_000
                tx_bps = (tx_bytes_delta * 8) / time_delta / 1_000_000
                self.port_stats[dpid][port_no]['rx_bps'] = round(rx_bps, 2)
                self.port_stats[dpid][port_no]['tx_bps'] = round(tx_bps, 2)
                self.port_stats[dpid][port_no]['total_bps'] = round(rx_bps + tx_bps, 2)

    self.port_stats_prev[dpid] = self.port_stats[dpid]


def flow_stats_reply_handler(self, ev):
    """处理流表统计应答，计算组播组负载"""
    msg = ev.msg
    dpid = msg.datapath.id
    now = time.time()

    self.flow_stats[dpid] = {}

    for stat in msg.body:
        # 生成 match 的唯一标识
        match = stat.match
        ipv4_src = match.get('ipv4_src')
        ipv4_dst = match.get('ipv4_dst')
        if not ipv4_src or not ipv4_dst:
            continue
        flow_key = f"{ipv4_src}:{ipv4_dst}"

        self.flow_stats[dpid][flow_key] = {
            'byte_count': stat.byte_count,
            'packet_count': stat.packet_count,
            'timestamp': now
        }

        # 计算与上次的差值和负载
        if dpid in self.flow_stats_prev and flow_key in self.flow_stats_prev[dpid]:
            prev = self.flow_stats_prev[dpid][flow_key]
            time_delta = now - prev['timestamp']
            if time_delta > 0:
                byte_delta = stat.byte_count - prev['byte_count']
                load_mbps = (byte_delta * 8) / time_delta / 1_000_000

                # 查找对应的组播组并记录负载
                for multicast_addr, flow_info in self.multicast_active_flows.items():
                    if flow_info.get('source_ip') == ipv4_src:
                        self.multicast_load[multicast_addr] = {
                            'load': round(load_mbps, 2),
                            'timestamp': now
                        }
                        self.logger.debug(f"[Multicast Load] Group {multicast_addr}: {round(load_mbps, 2)} Mbps (flow {flow_key})")

    self.flow_stats_prev[dpid] = self.flow_stats[dpid]


def flow_removed_handler(self, ev):
    """处理交换机流表因 idle_timeout 过期而被删除的事件"""
    try:
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        if msg.reason not in (ofproto.OFPRR_IDLE_TIMEOUT, ofproto.OFPRR_HARD_TIMEOUT):
            return

        dpid = datapath.id
        match = msg.match
        ipv4_src = match.get('ipv4_src')
        ipv4_dst = match.get('ipv4_dst')
        if not ipv4_src or not ipv4_dst:
            return

        flow_key = (ipv4_src, ipv4_dst)
        if flow_key in self.active_flows:
            del self.active_flows[flow_key]
            self.logger.info(f"[FlowRemoved] Flow expired on SW:{paths.sw_name(self, dpid)}: {ipv4_src} -> {ipv4_dst}, removed from active_flows")
    except Exception as e:
        self.logger.info(f'Event OFPFlowRemoved {e}')


def request_port_stats(self):
    """向所有交换机发送端口统计请求"""
    for dpid, datapath in self.datapaths.items():
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        req = parser.OFPPortStatsRequest(datapath, port_no=ofproto.OFPP_ANY)
        datapath.send_msg(req)


def request_flow_stats(self, dpid, multicast_addr=None, src_ip=None):
    """向指定交换机发送流表统计请求"""
    if dpid not in self.datapaths:
        return
    datapath = self.datapaths[dpid]
    parser = datapath.ofproto_parser

    # 如果提供了源IP和组播地址，构建精确的匹配条件
    if src_ip and multicast_addr:
        match = parser.OFPMatch(
            eth_type=0x0800,
            ipv4_src=src_ip,
            ipv4_dst=multicast_addr
        )
    else:
        match = parser.OFPMatch(eth_type=0x0800)

    ofproto = datapath.ofproto
    req = parser.OFPFlowStatsRequest(
        datapath,
        table_id=ofproto.OFPTT_ALL,
        out_port=ofproto.OFPP_ANY,
        out_group=ofproto.OFPG_ANY,
        cookie=0,
        cookie_mask=0,
        match=match
    )
    datapath.send_msg(req)


def _check_link_load(self):
    """检查所有链路负载，更新高负载计数和组播组标记"""
    high_threshold = 0.8  # 80% 阈值

    for src_dpid, dst_dpid, attrs in self.graph.edges(data=True):
        bw = attrs.get('bw', 0)
        tx_bps = attrs.get('tx_bps', 0)
        load_ratio = tx_bps / bw if bw > 0 else 0

        link_key = (src_dpid, dst_dpid)

        if load_ratio > high_threshold:
            # 增加连续高负载计数
            self.link_high_load_count[link_key] = self.link_high_load_count.get(link_key, 0) + 1
            self.logger.debug(f"[Link Load] {paths.sw_name(self, src_dpid)}->{paths.sw_name(self, dst_dpid)} load={load_ratio:.2%}, count={self.link_high_load_count[link_key]}")
        else:
            # 负载恢复正常，重置计数
            self.link_high_load_count[link_key] = 0

        # 如果连续三次超过80%且该链路在组播路径中，标记组播组为高负载
        if self.link_high_load_count.get(link_key, 0) >= 3:
            _mark_multicast_high_load(self, src_dpid, dst_dpid)


def _mark_multicast_high_load(self, src_dpid, dst_dpid):
    """
    如果链路在组播路径中，标记对应组播组为高负载
    但如果组播组的负载占链路带宽的比例超过阈值（0.8），则不标记为高负载
    如果链路任意一端节点是不可替代节点（度=1），代表这条链路也为不可替代链路，则组播组也不标记为高负载
    """
    high_threshold = 0.8

    # 检查链路两端是否都是非临界节点
    if src_dpid in self.critical_nodes or dst_dpid in self.critical_nodes:
        self.logger.debug(f"[Multicast High Load] Skipping link {paths.sw_name(self, src_dpid)}->{paths.sw_name(self, dst_dpid)}: "
                         f"one or both nodes are critical (degree=1)")
        return

    for multicast_addr, flow_info in self.multicast_active_flows.items():
        path = flow_info.get('path', [])
        # 检查 (src_dpid, dst_dpid) 或 (dst_dpid, src_dpid) 是否在路径中
        if (src_dpid, dst_dpid) in path or (dst_dpid, src_dpid) in path:
            # 获取组播组的负载和链路带宽
            mcast_load = self.multicast_load.get(multicast_addr, {}).get('load', 0)
            # 获取链路带宽
            if self.graph.has_edge(src_dpid, dst_dpid):
                link_bw = self.graph[src_dpid][dst_dpid].get('bw', 0)
            elif self.graph.has_edge(dst_dpid, src_dpid):
                link_bw = self.graph[dst_dpid][src_dpid].get('bw', 0)
            else:
                link_bw = 0

            if link_bw > 0:
                load_ratio = mcast_load / link_bw
                if load_ratio > high_threshold:
                    self.logger.debug(f"[Multicast High Load] Group {multicast_addr} NOT marked as high load "
                                     f"(mcast_load={mcast_load:.2f}Mbps, link_bw={link_bw:.2f}Mbps, ratio={load_ratio:.2%} > {high_threshold:.2%})")
                    continue

            self.multicast_high_load[multicast_addr] = True
            self.logger.info(f"[Multicast High Load] Group {multicast_addr} marked as high load "
                             f"(link {paths.sw_name(self, src_dpid)}->{paths.sw_name(self, dst_dpid)}, "
                             f"mcast_load={mcast_load:.2f}Mbps, link_bw={link_bw:.2f}Mbps)")


def _reroute_high_load_multicast(self):
    """为高负载组播组重新计算路由
    1. 获取带宽矩阵、可用带宽矩阵、当前组播负载、当前路由矩阵
    2. 更新可用带宽矩阵：释放当前组播占用的带宽
    3. 剔除高负载链路后检查连通性
    4. 如果连通，重新计算路由并安装
    """
    if not self.multicast_reroute_enabled:
        return

    if not self.multicast_high_load:
        return

    for multicast_addr, is_high_load in list(self.multicast_high_load.items()):
        if not is_high_load:
            continue

        self.logger.info(f"[Reroute] Reinstalling multicast flow for high-load group {multicast_addr}")

        # 获取组播组当前负载
        current_load = self.multicast_load.get(multicast_addr, {}).get('load', 0)
        if current_load <= 0:
            self.logger.warning(f"[Reroute] No valid load for group {multicast_addr}, skipping reroute")
            continue

        # 获取当前路由矩阵
        if multicast_addr not in self.multicast_active_flows:
            self.logger.warning(f"[Reroute] No active flow found for group {multicast_addr}, skipping reroute")
            continue

        current_flow = self.multicast_active_flows[multicast_addr]
        route_matrix = current_flow.get('route_matrix')
        if route_matrix is None:
            self.logger.warning(f"[Reroute] No route matrix for group {multicast_addr}, skipping reroute")
            continue

        src_ip = current_flow.get('source_ip')
        src_switch_dpid = current_flow.get('source_dpid')
        if not src_ip or not src_switch_dpid:
            self.logger.warning(f"[Reroute] Invalid source info for group {multicast_addr}, skipping reroute")
            continue

        # 收集组成员信息
        member_ips = []
        member_dpid_set = set()
        if multicast_addr in self.multicast_groups:
            for group_dpid, mac_dict in self.multicast_groups[multicast_addr].items():
                member_dpid_set.add(group_dpid)
                for mac, (member_ip, member_port) in mac_dict.items():
                    member_ips.append(member_ip)

        # 构建矩阵
        adj_matrix_nodes = sorted(self.graph.nodes())
        net_size = len(adj_matrix_nodes)

        # 构建邻接矩阵、带宽矩阵、延迟矩阵、丢包矩阵
        adj_matrix = nx.to_numpy_array(self.graph, nodelist=adj_matrix_nodes, dtype=np.float32)
        bandwidth_matrix = nx.attr_matrix(self.graph, edge_attr='bw', rc_order=adj_matrix_nodes, dtype=np.float32)
        latency_matrix = nx.attr_matrix(self.graph, edge_attr='delay', rc_order=adj_matrix_nodes, dtype=np.float32)
        loss_matrix = nx.attr_matrix(self.graph, edge_attr='loss', rc_order=adj_matrix_nodes, dtype=np.float32)
        load_matrix = nx.attr_matrix(self.graph, edge_attr='tx_bps', rc_order=adj_matrix_nodes, dtype=np.float32)
        available_bw_matrix = bandwidth_matrix - load_matrix

        # 释放当前组播路由占用的带宽：遍历路由矩阵，将对应链路的可用带宽加上当前负载
        src_idx = adj_matrix_nodes.index(src_switch_dpid)
        visited = set()
        queue = deque([src_idx])
        visited.add(src_idx)

        while queue:
            i = queue.popleft()
            for j in range(net_size):
                if route_matrix[i, j] == 1:
                    # 释放这条链路的带宽（但不超过链路带宽）
                    if available_bw_matrix[i, j] + current_load <= bandwidth_matrix[i, j]:
                        available_bw_matrix[i, j] += current_load
                    else:
                        available_bw_matrix[i, j] = bandwidth_matrix[i, j]
                    if j not in visited:
                        queue.append(j)
                        visited.add(j)

        self.logger.info(f"[Reroute] Released {current_load} Mbps from current route")

        # 剔除高负载链路：如果链路 (可用带宽-当前组播负载) / 带宽 <= 1 - 阈值(0.8)，则剔除
        high_threshold = 0.8
        filtered_adj_matrix = adj_matrix.copy()
        for i in range(net_size):
            for j in range(net_size):
                if adj_matrix[i, j] > 0:  # 链路存在
                    potential_load = available_bw_matrix[i, j] - current_load
                    if bandwidth_matrix[i, j] > 0 and potential_load / bandwidth_matrix[i, j] <= 1 - high_threshold:
                        filtered_adj_matrix[i, j] = 0
                        self.logger.debug(f"[Reroute] Filtered out link {adj_matrix_nodes[i]}->{adj_matrix_nodes[j]} due to high load")

        # 检查源和所有目的地是否连通
        src_idx = adj_matrix_nodes.index(src_switch_dpid)
        member_idx_set = [adj_matrix_nodes.index(dpid) for dpid in member_dpid_set]

        # 使用 BFS 检查从源能否到达所有成员
        reachable = set()
        queue = deque([src_idx])
        reachable.add(src_idx)
        while queue:
            i = queue.popleft()
            for j in range(net_size):
                if filtered_adj_matrix[i, j] > 0 and j not in reachable:
                    queue.append(j)
                    reachable.add(j)

        all_reachable = all(idx in reachable for idx in member_idx_set)
        if not all_reachable:
            self.logger.warning(f"[Reroute] Source cannot reach all members after filtering, skipping reroute for {multicast_addr}")
            continue

        self.logger.info(f"[Reroute] Source can reach all members, proceeding with reroute for {multicast_addr}")

        # 删除旧的流表规则
        multicast._delete_multicast_flows(self, multicast_addr)

        # 删除旧的 multicast_active_flows 记录
        if multicast_addr in self.multicast_active_flows:
            del self.multicast_active_flows[multicast_addr]

        # 清除高负载标记
        self.multicast_high_load[multicast_addr] = False

        # 使用过滤后的邻接矩阵重新计算路由
        # 重新构建延迟、可用带宽、丢包矩阵（基于过滤后的邻接矩阵）
        # 将filtered_adj_matrix中为0的位置，带宽也设为0
        filtered_available_bw = available_bw_matrix * (filtered_adj_matrix > 0).astype(np.float32)

        # 索引转换（1-based）
        src_switch_id = src_idx + 1
        member_id_list = [idx + 1 for idx in member_idx_set]

        # 获取权重配置
        weights = self.multicast_weights.get(multicast_addr, {'latency': 1.0, 'bandwidth': 1.0, 'loss': 1.0})
        self.logger.info(f"[Reroute] Using weights: latency={weights['latency']}, bandwidth={weights['bandwidth']}, loss={weights['loss']}")

        # 调用路由算法
        is_success, new_route_matrix = RL_multicast_tree(
            filtered_adj_matrix, latency_matrix, filtered_available_bw, loss_matrix,
            source_node=src_switch_id,
            destination_group=member_id_list,
            latency_weight=weights['latency'],
            bandwidth_weight=weights['bandwidth'],
            loss_weight=weights['loss'],
            algorithm_type=self.routing_algorithm
        )

        if not is_success:
            self.logger.warning(f"[Reroute] Failed to find new route for group {multicast_addr}")
            continue

        # 安装新的流表规则
        self.multicast_flow_installed.add(multicast_addr)

        # 将邻接矩阵的双向性变成单向性矩阵（BFS）
        visited = set()
        queue = deque([src_idx])
        visited.add(src_idx)

        while queue:
            i = queue.popleft()
            for j in range(net_size):
                if new_route_matrix[i, j] == 1:
                    new_route_matrix[j, i] = 0
                    if j not in visited:
                        queue.append(j)
                        visited.add(j)

        # 安装流规则
        installed_flows = []
        for i in range(net_size):
            src_node_dpid = adj_matrix_nodes[i]
            if src_node_dpid not in self.datapaths:
                continue

            src_datapath = self.datapaths[src_node_dpid]
            src_parser = src_datapath.ofproto_parser

            out_actions = []

            # 如果该交换机上有组成员主机，转发到主机端口
            if src_node_dpid in member_dpid_set:
                for mac, (member_ip, member_port) in self.multicast_groups[multicast_addr].get(src_node_dpid, {}).items():
                    out_actions.append(src_parser.OFPActionOutput(member_port))
                    self.logger.info(f"[Reroute] SW:{paths.sw_name(self, src_node_dpid)} -> host port {member_port}")

            for j in range(net_size):
                if new_route_matrix[i][j] == 1:
                    dst_node_dpid = adj_matrix_nodes[j]
                    out_port = paths.get_path_out_port(self, src_node_dpid, dst_node_dpid)
                    if out_port:
                        out_actions.append(src_parser.OFPActionOutput(out_port))
                        self.logger.info(f"[Reroute] SW:{paths.sw_name(self, src_node_dpid)} -> port {out_port} (next hop SW:{paths.sw_name(self, dst_node_dpid)})")

            if out_actions:
                match = src_parser.OFPMatch(eth_type=0x0800, ipv4_src=src_ip, ipv4_dst=multicast_addr)
                paths.add_flow(self, src_datapath, 100, match, out_actions, idle_timeout=0, hard_timeout=0)
                installed_flows.append((src_node_dpid, f"ipv4_src={src_ip}, ipv4_dst={multicast_addr} -> {len(out_actions)} port(s)"))

        # 记录已安装的流规则
        if multicast_addr not in self.multicast_flows:
            self.multicast_flows[multicast_addr] = []
        self.multicast_flows[multicast_addr].extend(installed_flows)

        # 提取路径列表
        path_list = []
        for i in range(net_size):
            for j in range(net_size):
                if new_route_matrix[i, j] == 1:
                    path_list.append((adj_matrix_nodes[i], adj_matrix_nodes[j]))

        # 获取源主机与源交换机相连的端口号
        src_switch_in_port = None
        for mac, host_info in self.hosts.items():
            if host_info.get('ip') == src_ip and host_info.get('dpid') == src_switch_dpid:
                src_switch_in_port = host_info.get('port')
                break

        # 更新 multicast_active_flows
        self.multicast_active_flows[multicast_addr] = {
            'source_ip': src_ip,
            'source_dpid': src_switch_dpid,
            'source_in_port': src_switch_in_port,
            'route_matrix': new_route_matrix,
            'member_ips': member_ips,
            'path': path_list,
            'installed_time': time.time()
        }

        self.logger.info(f"[Reroute] Successfully reinstalled multicast flow for group {multicast_addr}, installed {len(installed_flows)} flow rules")

        # 重路由成功则直接退出 for 循环，下一个组播组重路由在下一轮任务中再执行，不同组播组高负载重路由之间形成间隔
        break