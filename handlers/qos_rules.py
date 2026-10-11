"""
QoS 规则生命周期：install/delete、负载均衡自动重路由、拓扑变化触发的流重算。

State ownership（读/写）：
- self.qos_rules, self.qos_rule_counter
- self.active_flows（含 'type': 'qos' 和 'type': 'flow' 两种）

依赖：
- handlers.paths（add_flow, get_shortest_path, get_path_switches, sw_name）
- handlers.multicast（_delete_multicast_flows, _install_multicast_flow_rules）
- utils（WeightPref）
"""

from ryu.ofproto import ether
from utils import WeightPref

import handlers.multicast as multicast
import handlers.paths as paths


def install_qos_flow(self, rule):
    """为 QoS 规则安装流表
    rule: {name, src_ip, dst_ip, protocol, src_port, dst_port, dscp, priority,
           guarantee, src_dpid, dst_dpid, path, path_switches}
    """
    src_ip = rule['src_ip']
    dst_ip = rule['dst_ip']
    protocol = rule.get('protocol')
    src_port = rule.get('src_port')
    dst_port = rule.get('dst_port')
    dscp = rule.get('dscp', 0)
    priority = rule.get('priority', 100)
    path = rule.get('path')
    path_switches = rule.get('path_switches')

    if not path_switches:
        self.logger.warning(f"[QoS] No path_switches for rule {rule.get('name')}")
        return False

    for ps in path_switches:
        sw_dp = self.datapaths.get(ps['dpid'])
        if not sw_dp:
            continue

        parser = sw_dp.ofproto_parser
        # 构建匹配字段
        match_kwargs = {
            'in_port': ps['in_port'],
            'eth_type': ether.ETH_TYPE_IP,
            'ipv4_src': src_ip,
            'ipv4_dst': dst_ip,
        }

        # 添加协议和端口匹配
        if protocol == 'tcp':
            match_kwargs['ip_proto'] = 6
            if dst_port:
                match_kwargs['tcp_dst'] = dst_port
            if src_port:
                match_kwargs['tcp_src'] = src_port
        elif protocol == 'udp':
            match_kwargs['ip_proto'] = 17
            if dst_port:
                match_kwargs['udp_dst'] = dst_port
            if src_port:
                match_kwargs['udp_src'] = src_port
        elif protocol == 'icmp':
            match_kwargs['ip_proto'] = 1

        # 添加 DSCP 标记
        if dscp:
            match_kwargs['ip_dscp'] = dscp

        match = parser.OFPMatch(**match_kwargs)
        actions = [parser.OFPActionOutput(ps['out_port'])]
        paths.add_flow(self, sw_dp, priority, match, actions)

        # 反向流表（目的到源）
        reverse_match_kwargs = {
            'in_port': ps['out_port'],
            'eth_type': ether.ETH_TYPE_IP,
            'ipv4_src': dst_ip,
            'ipv4_dst': src_ip,
        }
        if protocol == 'tcp':
            reverse_match_kwargs['ip_proto'] = 6
            if dst_port:
                reverse_match_kwargs['tcp_src'] = dst_port
            if src_port:
                reverse_match_kwargs['tcp_dst'] = src_port
        elif protocol == 'udp':
            reverse_match_kwargs['ip_proto'] = 17
            if dst_port:
                reverse_match_kwargs['udp_src'] = dst_port
            if src_port:
                reverse_match_kwargs['udp_dst'] = src_port
        elif protocol == 'icmp':
            reverse_match_kwargs['ip_proto'] = 1

        if dscp:
            reverse_match_kwargs['ip_dscp'] = dscp

        reverse_match = parser.OFPMatch(**reverse_match_kwargs)
        reverse_actions = [parser.OFPActionOutput(ps['in_port'])]
        paths.add_flow(self, sw_dp, priority, reverse_match, reverse_actions)

    self.logger.info(f"[QoS] Installed flow for rule: {rule.get('name')} ({src_ip} -> {dst_ip}, {protocol}, dscp={dscp}, priority={priority})")
    self.logger.info(f"[QoS] path: {' -> '.join(paths.sw_name(self, p) for p in path)}")
    return True


def delete_qos_flow(self, rule):
    """删除 QoS 规则的流表"""
    src_ip = rule['src_ip']
    dst_ip = rule['dst_ip']
    protocol = rule.get('protocol', 'any')
    src_port = rule.get('src_port')
    dst_port = rule.get('dst_port')
    dscp = rule.get('dscp', 0)
    path_switches = rule.get('path_switches', [])

    for ps in path_switches:
        sw_dp = self.datapaths.get(ps['dpid'])
        if not sw_dp:
            continue

        parser = sw_dp.ofproto_parser
        ofproto = sw_dp.ofproto

        # 构建匹配字段
        match_kwargs = {
            'eth_type': ether.ETH_TYPE_IP,
            'ipv4_src': src_ip,
            'ipv4_dst': dst_ip,
        }

        if protocol == 'tcp':
            match_kwargs['ip_proto'] = 6
            if dst_port:
                match_kwargs['tcp_dst'] = dst_port
            if src_port:
                match_kwargs['tcp_src'] = src_port
        elif protocol == 'udp':
            match_kwargs['ip_proto'] = 17
            if dst_port:
                match_kwargs['udp_dst'] = dst_port
            if src_port:
                match_kwargs['udp_src'] = src_port
        elif protocol == 'icmp':
            match_kwargs['ip_proto'] = 1

        if dscp:
            match_kwargs['ip_dscp'] = dscp

        match = parser.OFPMatch(**match_kwargs)
        del_flow = parser.OFPFlowMod(
            datapath=sw_dp,
            command=ofproto.OFPFC_DELETE,
            table_id=0,
            priority=0,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=match,
        )
        sw_dp.send_msg(del_flow)

        # 删除反向流
        reverse_match_kwargs = {
            'eth_type': ether.ETH_TYPE_IP,
            'ipv4_src': dst_ip,
            'ipv4_dst': src_ip,
        }
        if protocol == 'tcp':
            reverse_match_kwargs['ip_proto'] = 6
            if dst_port:
                reverse_match_kwargs['tcp_src'] = dst_port
            if src_port:
                reverse_match_kwargs['tcp_dst'] = src_port
        elif protocol == 'udp':
            reverse_match_kwargs['ip_proto'] = 17
            if dst_port:
                reverse_match_kwargs['udp_src'] = dst_port
            if src_port:
                reverse_match_kwargs['udp_dst'] = src_port
        elif protocol == 'icmp':
            reverse_match_kwargs['ip_proto'] = 1

        if dscp:
            reverse_match_kwargs['ip_dscp'] = dscp

        reverse_match = parser.OFPMatch(**reverse_match_kwargs)
        del_flow = parser.OFPFlowMod(
            datapath=sw_dp,
            command=ofproto.OFPFC_DELETE,
            table_id=0,
            priority=0,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=reverse_match,
        )
        sw_dp.send_msg(del_flow)

    self.logger.info(f"[QoS] Deleted flow for rule: {rule.get('name')} ({src_ip} -> {dst_ip})")


def _add_path_flow(self, src_ip, dst_ip, path_switches):
    # 路径流表
    priority = 10
    idle_timeout = 300
    for ps in path_switches:
        sw_dp = self.datapaths.get(ps['dpid'])
        if sw_dp:
            parser = sw_dp.ofproto_parser
            match = parser.OFPMatch(in_port=ps['in_port'], eth_type=ether.ETH_TYPE_IP,
                                    ipv4_src=src_ip, ipv4_dst=dst_ip)
            actions = [parser.OFPActionOutput(ps['out_port'])]
            paths.add_flow(self, sw_dp, priority, match, actions, idle_timeout=idle_timeout)

            # 反向流表
            match = parser.OFPMatch(in_port=ps['out_port'], eth_type=ether.ETH_TYPE_IP,
                                    ipv4_src=dst_ip, ipv4_dst=src_ip)
            actions = [parser.OFPActionOutput(ps['in_port'])]
            paths.add_flow(self, sw_dp, priority, match, actions, idle_timeout=idle_timeout)


def _delete_flow(self, src_ip, dst_ip):
    """删除指定流的流表"""
    flow_info = self.active_flows.get((src_ip, dst_ip))
    if not flow_info:
        return

    for ps in flow_info['path_switches']:
        sw_dp = self.datapaths.get(ps['dpid'])
        if sw_dp:
            parser = sw_dp.ofproto_parser
            ofproto = sw_dp.ofproto
            # 删除正向流
            match = parser.OFPMatch(eth_type=ether.ETH_TYPE_IP, ipv4_src=src_ip, ipv4_dst=dst_ip, ip_dscp=0)
            del_flow = parser.OFPFlowMod(
                datapath=sw_dp,
                command=ofproto.OFPFC_DELETE,
                table_id=0,
                priority=0,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=match,
            )
            sw_dp.send_msg(del_flow)
            # 删除反向流
            match = parser.OFPMatch(eth_type=ether.ETH_TYPE_IP, ipv4_src=dst_ip, ipv4_dst=src_ip, ip_dscp=0)
            del_flow = parser.OFPFlowMod(
                datapath=sw_dp,
                command=ofproto.OFPFC_DELETE,
                table_id=0,
                priority=0,
                out_port=ofproto.OFPP_ANY,
                out_group=ofproto.OFPG_ANY,
                match=match,
            )
            sw_dp.send_msg(del_flow)


def _check_and_update_load_balance_rules(self):
    """检查所有 load_balance QoS 规则的链路负载，必要时重新计算路径"""
    for rule_id, rule in list(self.qos_rules.items()):
        if rule.get('guarantee') != 'load_balance':
            continue

        src_ip = rule.get('src_ip')
        dst_ip = rule.get('dst_ip')
        src_dpid = self.host_ip_to_dpid.get(src_ip)
        dst_dpid = self.host_ip_to_dpid.get(dst_ip)
        if not src_dpid or not dst_dpid:
            continue

        current_path = rule.get('path', [])
        new_path = paths.get_shortest_path(self, src_dpid, dst_dpid, WeightPref.LoadBalance)
        if not new_path or len(new_path) < 2:
            continue

        if new_path == current_path:
            continue

        self.logger.info(f"[LoadBalance] {rule.get('name')}: path changed, re-routing")
        self.logger.info(f"[LoadBalance] old path: {' -> '.join(paths.sw_name(self, p) for p in current_path)}")
        self.logger.info(f"[LoadBalance] new path: {' -> '.join(paths.sw_name(self, p) for p in new_path)}")

        # 更新 rule 中的路径信息
        src_host = self.hosts.get(self.ip_to_mac.get(src_ip), {})
        dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip), {})
        path_switches = paths.get_path_switches(self,
            new_path,
            src_host.get('dpid', new_path[0]),
            src_host.get('port', 1),
            dst_host.get('dpid', new_path[-1]),
            dst_host.get('port', 1)
        )

        delete_qos_flow(self, rule)
        rule['path'] = new_path
        rule['path_switches'] = path_switches

        # 更新 active_flows
        self.active_flows[rule_id] = {
            'type': 'qos',
            'path': new_path,
            'path_switches': path_switches,
            'rule': rule,
            'src_host': src_host,
            'dst_host': dst_host,
        }

        # 删除旧流表，安装新流表
        install_qos_flow(self, rule)
        self.logger.info(f"[LoadBalance] {rule.get('name')}: flow table updated")


def _update_affected_flows(self, flow_key):
    flow_info = self.active_flows[flow_key]

    if not flow_info:
        self.logger.info(f"[ERROR] {flow_key} 为空")
        return

    if flow_info.get('type') == 'flow':
        src_ip, dst_ip = flow_key
        self.logger.info(f"[Route] update flow: {src_ip}->{dst_ip}")
        src_host, dst_host = flow_info['src_host'], flow_info['dst_host']
        _delete_flow(self, src_ip, dst_ip)

        # 重新计算路径
        new_path = paths.get_shortest_path(self, src_host['dpid'], dst_host['dpid'])
        if not new_path or len(new_path) < 2:
            self.logger.warning(f"[ERROR] 重新计算路径失败，删除流 {src_ip} -> {dst_ip}")
            del self.active_flows[flow_key]
            return

        self.logger.info(f"[Route] new path: {' -> '.join(self.switch_name.get(f'{p:016x}', f'{p}') for p in new_path)}")
        path_switches = paths.get_path_switches(self, new_path, src_host['dpid'], src_host['port'], dst_host['dpid'], dst_host['port'])
        _add_path_flow(self, src_ip, dst_ip, path_switches)
        # 更新活跃流信息
        self.active_flows[flow_key] = {
            'type': 'flow',
            'path': new_path,
            'path_switches': path_switches,
            'src_host': src_host,
            'dst_host': dst_host
        }
        self.logger.info(f"[Route] 流表更新完成: {src_ip} -> {dst_ip}")

    if flow_info.get('type') == 'qos':
        # QoS 规则的重新计算
        rule_id = flow_key
        rule = flow_info.get('rule')
        self.logger.info(f"[QoS] update qos: {rule['name']}")

        if not rule or rule_id is None:
            self.logger.info(f"[ERROR] {rule_id} 为空")

        delete_qos_flow(self, rule)

        src_ip, dst_ip = rule['src_ip'], rule['dst_ip']
        guarantee = rule['guarantee']
        src_dpid = self.host_ip_to_dpid.get(src_ip)
        dst_dpid = self.host_ip_to_dpid.get(dst_ip)

        if not src_dpid or not dst_dpid:
            self.logger.warning(f"[ERROR] IP {src_ip}/{src_ip} not find")
            return

        pref_map = {
            'default': None,
            'bandwidth': WeightPref.Bandwidth,
            'latency': WeightPref.Latency,
            'loss_rate': WeightPref.LossRate,
            'load_balance': WeightPref.LoadBalance,
            'smart_algorithm': WeightPref.SmartAlgorithm
        }
        preference = pref_map.get(guarantee)
        new_path = paths.get_shortest_path(self, src_dpid, dst_dpid, preference)

        if not new_path or len(new_path) < 2:
            self.logger.warning(f"[QoS ERROR] {rule.get('name')}重新计算路径失败")
            return

        rule['path'] = new_path
        src_host = self.hosts.get(self.ip_to_mac.get(src_ip), {})
        dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip), {})
        path_switches = paths.get_path_switches(self,
            new_path,
            src_host.get('dpid', new_path[0]),
            src_host.get('port', 1),
            dst_host.get('dpid', new_path[-1]),
            dst_host.get('port', 1)
        )
        rule['path_switches'] = path_switches

        # 更新 qos_rules active_flows 中的记录
        self.qos_rules[rule_id] = rule
        self.active_flows[rule_id] = {
            'type': 'qos',
            'path': new_path,
            'path_switches': path_switches,
            'rule': rule,
            'src_host': src_host,
            'dst_host': dst_host,
        }
        install_qos_flow(self, rule)


def _recalculate_affected_flows(self):
    """重新计算受拓扑变化影响的流"""
    for flow_key, flow_info in list(self.active_flows.items()):
        path = flow_info['path']
        need_recalc = False

        # 检查路径上的交换机是否都还在
        for dpid in path:
            if dpid not in self.datapaths:
                need_recalc = True
                break

        # 检查路径上的链路是否都还在
        if not need_recalc:
            for i in range(len(path) - 1):
                src_sw = path[i]
                dst_sw = path[i + 1]
                if not self.graph.has_edge(src_sw, dst_sw):
                    need_recalc = True
                    break

        if need_recalc:
            _update_affected_flows(self, flow_key)


def check_better_path_for_active_flows(self):
    """检查所有活跃流是否有更优路径可走"""
    for flow_key, flow_info in list(self.active_flows.items()):
        is_qos_flow = flow_info.get('type') == 'qos'
        if is_qos_flow:
            rule = flow_info.get('rule')
            rule_id = flow_key
            src_ip, dst_ip = rule.get('src_ip'), rule.get('dst_ip')
            src_host = self.hosts.get(self.ip_to_mac.get(src_ip), {})
            dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip), {})
            pref_map = {
                'default': None,
                'bandwidth': WeightPref.Bandwidth,
                'latency': WeightPref.Latency,
                'loss_rate': WeightPref.LossRate,
                'load_balance': WeightPref.LoadBalance,
                'smart_algorithm': WeightPref.SmartAlgorithm
            }
            guarantee = rule.get('guarantee')
            preference = pref_map.get(guarantee)

        else:
            src_ip, dst_ip = flow_key
            src_host = flow_info['src_host']
            dst_host = flow_info['dst_host']
            preference = None

        current_path = flow_info['path']

        # 检查源和目的主机是否可达
        if src_host['dpid'] not in self.datapaths or dst_host['dpid'] not in self.datapaths:
            continue

        # 计算新最优路径
        new_path = paths.get_shortest_path(self, src_host['dpid'], dst_host['dpid'], preference)

        if not new_path or len(new_path) < 2:
            continue
        # 如果路径相同，跳过
        if new_path == current_path:
            continue

        if is_qos_flow:
            self.logger.info(f"[QoS] {rule['name']} 发现更优路径: {src_ip} -> {dst_ip}")
            delete_qos_flow(self, rule)

            src_host = self.hosts.get(self.ip_to_mac.get(src_ip))
            dst_host = self.hosts.get(self.ip_to_mac.get(dst_ip))
            path_switches = paths.get_path_switches(self, new_path, src_host['dpid'], src_host['port'],  dst_host['dpid'], dst_host['port'])
            rule['path'] = new_path
            rule['path_switches'] = path_switches

            self.qos_rules[rule_id] = rule
            self.active_flows[rule_id] = {
                'type': 'qos',
                'path': new_path,
                'path_switches': path_switches,
                'rule': rule,
                'src_host': src_host,
                'dst_host': dst_host,
            }
            install_qos_flow(self, rule)
        else:
            self.logger.info(f"[Route] 发现更优路径: {src_ip} -> {dst_ip}")
            self.logger.info(f"[Route] 新路径: {' -> '.join(paths.sw_name(self, p) for p in new_path)}")

            # 删除旧流表
            _delete_flow(self, src_ip, dst_ip)
            # 构建新路径的 path_switches
            path_switches = paths.get_path_switches(self, new_path, src_host['dpid'], src_host['port'], dst_host['dpid'], dst_host['port'])

            # 下发新流表
            _add_path_flow(self, src_ip, dst_ip, path_switches)

            # 更新活跃流信息
            self.active_flows[flow_key] = {
                'type': 'flow',
                'path': new_path,
                'path_switches': path_switches,
                'src_host': src_host,
                'dst_host': dst_host
            }
            self.logger.info(f"[Route] 流表更新完成: {src_ip} -> {dst_ip}")


def _recalculate_affected_multicast_flows(self, deleted_link=None):
    """重新计算受拓扑变化影响的组播流"""
    affected_groups = []

    for multicast_addr, flow_info in list(self.multicast_active_flows.items()):
        path_edges = flow_info.get('path', [])
        need_recalc = False

        # 检查路径上的链路是否都还在
        if deleted_link:
            # 如果指定了删除的链路，检查该链路是否在组播树路径中
            src_dpid, dst_dpid = deleted_link
            for (u, v) in path_edges:
                if (u == src_dpid and v == dst_dpid) or (u == dst_dpid and v == src_dpid):
                    need_recalc = True
                    break
        else:
            # 如果没有指定链路，检查所有边是否都在图中
            for (u, v) in path_edges:
                if not self.graph.has_edge(u, v):
                    need_recalc = True
                    break

        if need_recalc:
            affected_groups.append(multicast_addr)

    if affected_groups:
        self.logger.info(f"[Multicast Route] 需要重新计算的组播流: {len(affected_groups)}")

    # 重新计算每条受影响的组播流
    for multicast_addr in affected_groups:
        # 检查源和成员是否还可达
        if multicast_addr not in self.multicast_sources:
            self.logger.warning(f"[Multicast Route] 组播组 {multicast_addr} 源 IP 不可达，删除")
            multicast._delete_multicast_flows(self, multicast_addr)
            if multicast_addr in self.multicast_active_flows:
                del self.multicast_active_flows[multicast_addr]
            continue

        if multicast_addr not in self.multicast_groups or not self.multicast_groups[multicast_addr]:
            self.logger.warning(f"[Multicast Route] 组播组 {multicast_addr} 无成员，删除")
            multicast._delete_multicast_flows(self, multicast_addr)
            if multicast_addr in self.multicast_active_flows:
                del self.multicast_active_flows[multicast_addr]
            continue

        # 删除旧流表
        multicast._delete_multicast_flows(self, multicast_addr)
        self.multicast_flow_installed.discard(multicast_addr)

        # 重新安装流规则
        success = multicast._install_multicast_flow_rules(self, multicast_addr)
        if success:
            self.logger.info(f"[Multicast Route] 组播流重新下发完成: {multicast_addr}")
            # 组播组之间间隔5秒
            # time.sleep(5)
        else:
            self.logger.warning(f"[Multicast Route] 组播流重新下发失败: {multicast_addr}")
            if multicast_addr in self.multicast_active_flows:
                del self.multicast_active_flows[multicast_addr]