"""
IGMP 处理、组播组管理、组播流规则安装/删除。

State ownership（读/写）：
- self.multicast_groups：{组播地址: {dpid: {mac: (src_ip, in_port)}}}
- self.multicast_flows：{组播地址: [(dpid, match_desc), ...]}
- self.multicast_flow_installed：set（已安装流规则的组播组）
- self.multicast_sources：{组播地址: src_ip}
- self.multicast_weights：{组播地址: {latency, bandwidth, loss}}
- self.multicast_active_flows：{组播地址: {source_ip, source_dpid, route_matrix, ...}}
- self.adj_matrix_nodes：拓扑节点顺序（_install_multicast_flow_rules 中赋值）

依赖：
- handlers.paths（get_path_out_port, add_flow, sw_name）
- client.RL_multicast_tree（gRPC 服务调用）
"""

from collections import deque
import time

import networkx as nx
import numpy as np

import handlers.paths as paths
from client import RL_multicast_tree


def handle_igmp(self, datapath, in_port, eth, ipv4_pkt, igmp_pkt):
    """
    处理 IGMP 报文（加入/离开组播组），如果某组播组已安装流表，同时有组播成员加入/离开，则删除该组播组安装的所有流表
    """
    dpid = datapath.id
    src_ip = ipv4_pkt.src
    src_mac = eth.src
    msgtype = igmp_pkt.msgtype

    # 跳过 IGMPv3 报文 (msgtype 0x22)
    if msgtype == 0x22:
        self.logger.info(f"[IGMP] IGMPv3 Report ignored, SW:{dpid}")
        return

    multicast_addr = igmp_pkt.address

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
                    _delete_multicast_flows(self, g)
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
                    _install_multicast_flow_rules(self, multicast_addr)
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
        _delete_multicast_flows(self, multicast_addr)

    # msgtype 0x12/0x16 = Join (加入组播组)
    if msgtype in [0x12, 0x16]:
        add_member_to_group(self, dpid, src_mac, src_ip, multicast_addr, in_port)
        if multicast_addr in self.multicast_sources:
            _install_multicast_flow_rules(self, multicast_addr)
    # msgtype 0x17 = Leave (离开组播组)
    elif msgtype == 0x17:
        remove_member_from_group(self, dpid, src_mac, multicast_addr)


def add_member_to_group(self, dpid, mac, src_ip, multicast_addr, in_port):
    """将主机加入组播组"""
    if multicast_addr not in self.multicast_groups:
        self.multicast_groups[multicast_addr] = {}

    if dpid not in self.multicast_groups[multicast_addr]:
        self.multicast_groups[multicast_addr][dpid] = {}

    self.multicast_groups[multicast_addr][dpid][mac] = (src_ip, in_port)
    self.logger.info(f"[Multicast Group] Host joined: Group={multicast_addr}, MAC={mac}, IP={src_ip}, SW={dpid}, Port={in_port}")
    print_multicast_groups(self)


def remove_member_from_group(self, dpid, mac, multicast_addr):
    """将主机从组播组删除"""
    if multicast_addr in self.multicast_groups:
        if dpid in self.multicast_groups[multicast_addr]:
            if mac in self.multicast_groups[multicast_addr][dpid]:
                del self.multicast_groups[multicast_addr][dpid][mac]
                self.logger.info(f"[Multicast Group] Host left: Group={multicast_addr}, MAC={mac}")

                # 如果该组播组在该交换机上没有成员了，删除该交换机的记录
                if not self.multicast_groups[multicast_addr][dpid]:
                    del self.multicast_groups[multicast_addr][dpid]

                # 如果该组播组没有组播成员了
                if not self.multicast_groups[multicast_addr]:
                    # 且该组播组没有对应的源IP，则删除该组播组
                    # 否则不做操作，保留该组播组（后续有新成员加入时可复用源IP）
                    if multicast_addr not in self.multicast_sources:
                        del self.multicast_groups[multicast_addr]
                else:
                    # 如果该组播组删除主机后还有组播成员，且有源IP，则重新安装流规则
                    if multicast_addr in self.multicast_sources:
                        _install_multicast_flow_rules(self, multicast_addr)
            else:
                self.logger.info(f"[Multicast Group] Host not in group: MAC={mac}")
        else:
            self.logger.info(f"[Multicast Group] Switch has no members in group")
    else:
        self.logger.info(f"[Multicast Group] Group does not exist")
    print_multicast_groups(self)


def print_multicast_groups(self):
    """打印当前所有组播组信息"""
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


def _install_multicast_flow_rules(self, multicast_addr):
    """
    为指定组播组安装组播流规则
    根据 multicast_sources 获取源 IP，根据 multicast_groups 获取组成员信息
    """
    if multicast_addr not in self.multicast_sources:
        self.logger.info(f"[Multicast Flow] Group {multicast_addr} has no source IP, skipping flow installation")
        return False

    if multicast_addr not in self.multicast_groups or not self.multicast_groups[multicast_addr]:
        self.logger.info(f"[Multicast Flow] Group {multicast_addr} has no members, skipping flow installation")
        return False

    self.logger.info(f"[Multicast Flow] multicast_weights content: {self.multicast_weights}")

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
        with self._lock:
            self.adj_matrix_nodes = sorted(self.graph.nodes())
            # 构建邻接矩阵节点索引和交换机 DPID 之间的的对应关系
            adj_matrix = nx.to_numpy_array(self.graph, nodelist=self.adj_matrix_nodes, dtype=np.float32)
            # 获取延迟、带宽、丢包矩阵
            latency_matrix = nx.attr_matrix(self.graph, edge_attr='delay', rc_order=self.adj_matrix_nodes, dtype=np.float32)
            bandwidth_matrix = nx.attr_matrix(self.graph, edge_attr='bw', rc_order=self.adj_matrix_nodes, dtype=np.float32)
            load_matrix = nx.attr_matrix(self.graph, edge_attr='total_bps', rc_order=self.adj_matrix_nodes, dtype=np.float32)
            available_bw_matrix = bandwidth_matrix - load_matrix
            loss_matrix = nx.attr_matrix(self.graph, edge_attr='loss', rc_order=self.adj_matrix_nodes, dtype=np.float32)

        src_switch_idx = self.adj_matrix_nodes.index(src_switch_dpid)
        src_switch_id = src_switch_idx + 1
        member_idx_set = [self.adj_matrix_nodes.index(dpid) for dpid in member_dpid_set]
        member_id_list = [idx + 1 for idx in member_idx_set]

        self.logger.info(f"[Multicast Route] Source: {src_ip} -> SW:{src_switch_id}")
        self.logger.info(f"[Multicast Route] Member Hosts in Destination Group: {member_ips}")
        self.logger.info(f"[Multicast Route] Member Switches: {member_id_list}")

        # 调用智能组播路由构建算法构建组播路由, 1-based:source_node,destination_group
        # 获取该组播组的权重配置，默认为 1.0
        weights = self.multicast_weights.get(multicast_addr,
                                             {'latency': 1.0, 'bandwidth': 1.0, 'loss': 1.0})
        self.logger.info(f"[Multicast Route] Using weights for {multicast_addr}: latency={weights['latency']}, bandwidth={weights['bandwidth']}, loss={weights['loss']}")
        is_success, route_matrix = RL_multicast_tree(adj_matrix, latency_matrix, available_bw_matrix, loss_matrix,
                                                     source_node=src_switch_id,
                                                     destination_group=member_id_list,
                                                     latency_weight=weights['latency'],
                                                     bandwidth_weight=weights['bandwidth'],
                                                     loss_weight=weights['loss'],
                                                     algorithm_type=self.routing_algorithm)
        if is_success:
            # 将邻接矩阵的双向性变成从源到目的集合的单向性矩阵（BFS）
            src_idx = self.adj_matrix_nodes.index(src_switch_dpid)
            visited = set()
            queue = deque([src_idx])
            visited.add(src_idx)
            net_size = len(self.adj_matrix_nodes)

            while queue:
                i = queue.popleft()
                for j in range(net_size):
                    if route_matrix[i, j] == 1:
                        route_matrix[j, i] = 0  # 删除反向边，确保单向性
                        if j not in visited:
                            queue.append(j)
                            visited.add(j)

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
                        self.logger.info(f"[Multicast Flow] SW:{paths.sw_name(self, src_node_dpid)} -> host port {member_port}")

                for j in range(net_size):
                    if route_matrix[i][j] == 1:
                        dst_node_dpid = self.adj_matrix_nodes[j]
                        out_port = paths.get_path_out_port(self, src_node_dpid, dst_node_dpid)
                        if out_port:
                            out_actions.append(src_parser.OFPActionOutput(out_port))
                            self.logger.info(f"[Multicast Flow] SW:{paths.sw_name(self, src_node_dpid)} -> port {out_port} (next hop SW:{paths.sw_name(self, dst_node_dpid)})")

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
                    paths.add_flow(self, src_datapath, 100, match, out_actions, idle_timeout=0, hard_timeout=0)
                    installed_flows.append((src_node_dpid, f"ipv4_src={src_ip}, ipv4_dst={multicast_addr} -> {len(out_actions)} port(s)"))

            # 记录已安装的流规则
            if multicast_addr not in self.multicast_flows:
                self.multicast_flows[multicast_addr] = []
            self.multicast_flows[multicast_addr].extend(installed_flows)
            self.logger.info(f"[Multicast Flow] Group {multicast_addr} installed {len(installed_flows)} flow rules")

            # 提取组播树路径列表
            path_list = []
            for i in range(net_size):
                for j in range(net_size):
                    if route_matrix[i, j] == 1:
                        path_list.append((self.adj_matrix_nodes[i], self.adj_matrix_nodes[j]))

            # 获取源主机与源交换机相连的端口号
            src_switch_in_port = None
            for mac, host_info in self.hosts.items():
                if host_info.get('ip') == src_ip and host_info.get('dpid') == src_switch_dpid:
                    src_switch_in_port = host_info.get('port')
                    break

            self.multicast_active_flows[multicast_addr] = {
                'source_ip': src_ip,
                'source_dpid': src_switch_dpid,
                'source_in_port': src_switch_in_port,
                'route_matrix': route_matrix,
                'member_ips': member_ips,
                'path': path_list,
                'installed_time': time.time()
            }
            self.logger.info(f"[Multicast Active Flow] Recorded active flow for group {multicast_addr}")

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
    """删除指定组播组的所有流表规则"""
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

    # 从组播活跃流中移除
    if multicast_addr in self.multicast_active_flows:
        del self.multicast_active_flows[multicast_addr]
        self.logger.info(f"[Multicast Active Flow] Removed active flow for group {multicast_addr}")