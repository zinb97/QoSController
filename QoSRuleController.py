import json

from ryu.app.wsgi import ControllerBase, route
from ryu.controller.ofp_event import EventOFPFlowRemoved
from webob import Response

from utils import WeightPref

topology_api_name = 'topology_api'


class QoSRuleController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(QoSRuleController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('qos', '/qos/rules', methods=['GET'])
    def get_all_qos_rules(self, req):
        """获取所有 QoS 规则"""
        rules = []
        for rule_id, rule in self.switch_app.qos_rules.items():
            rule_data = rule.copy()
            rule_data['id'] = rule_id
            # 将 path DPIDs 转换为名称
            if 'path' in rule_data:
                rule_data['path_names'] = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in rule_data['path']]
            rules.append(rule_data)
        body = json.dumps(rules, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('qos', '/qos/rules', methods=['POST'])
    def create_qos_rule(self, req):
        """创建新的 QoS 规则"""
        try:
            data = json.loads(req.body.decode('utf-8'))

            # 验证必填字段
            required_fields = ['name', 'src_ip', 'dst_ip']
            for field in required_fields:
                if not data.get(field):
                    error_msg = json.dumps({'success': False, 'error': f'Missing required field: {field}'}, ensure_ascii=False).encode('utf-8')
                    return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 验证 protocol
            protocol = data.get('protocol', 'any')
            if protocol not in ('any', 'tcp', 'udp', 'icmp'):
                error_msg = json.dumps({'success': False, 'error': 'Invalid protocol. Must be any, tcp, udp, or icmp'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 验证 guarantee
            guarantee = data.get('guarantee', 'default')
            valid_guarantees = ('default', 'bandwidth', 'latency', 'lossrate', 'loadbalance', 'samplealgorithm')
            if guarantee not in valid_guarantees:
                error_msg = json.dumps({'success': False, 'error': f'Invalid guarantee. Must be one of: {valid_guarantees}'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 验证 DSCP (0-63)
            dscp = int(data.get('dscp', 0))
            if not (0 <= dscp <= 63):
                error_msg = json.dumps({'success': False, 'error': 'DSCP must be between 0 and 63'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 验证 priority (1-1000)
            priority = int(data.get('priority', 100))
            if not (1 <= priority <= 1000):
                error_msg = json.dumps({'success': False, 'error': 'Priority must be between 1 and 1000'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 查找源和目的主机的 DPID
            src_ip = data['src_ip']
            dst_ip = data['dst_ip']
            src_dpid = self.switch_app.host_ip_to_dpid.get(src_ip)
            dst_dpid = self.switch_app.host_ip_to_dpid.get(dst_ip)

            if not src_dpid or not dst_dpid:
                error_msg = json.dumps({'success': False, 'error': 'Source or destination host not found in topology'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 根据 guarantee 转换为 WeightPref 枚举用于路由计算
            pref_map = {
                'default': None,
                'bandwidth': WeightPref.Bandwidth,
                'latency': WeightPref.Latency,
                'lossrate': WeightPref.LossRate,
                'loadbalance': WeightPref.LoadBalance,
                'samplealgorithm': WeightPref.SmartAlgorithm
            }
            preference = pref_map.get(guarantee)

            # 计算路径
            path = self.switch_app.get_shortest_path(src_dpid, dst_dpid, preference)
            if not path or len(path) < 2:
                error_msg = json.dumps({'success': False, 'error': 'No path found between source and destination'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 获取路径上的交换机信息
            src_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(src_ip), {})
            dst_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(dst_ip), {})

            path_switches = self.switch_app.get_path_switches(
                path,
                src_host.get('dpid', path[0]),
                src_host.get('port', 1),
                dst_host.get('dpid', path[-1]),
                dst_host.get('port', 1)
            )

            # 生成规则 ID
            rule_id = self.switch_app.qos_rule_counter + 1
            self.switch_app.qos_rule_counter = rule_id

            # 创建规则
            rule = {
                'name': data['name'],
                'src_ip': src_ip,
                'dst_ip': dst_ip,
                'protocol': protocol,
                'src_port': data.get('src_port'),
                'dst_port': data.get('dst_port'),
                'dscp': dscp,
                'priority': priority,
                'guarantee': guarantee,
                'enabled': data.get('enabled', True),
                'src_dpid': src_dpid,
                'dst_dpid': dst_dpid,
                'path': path,
                'path_switches': path_switches
            }

            self.switch_app.qos_rules[rule_id] = rule

            # 安装流表
            if rule['enabled']:
                self.switch_app._install_qos_flow(rule)

            # 将 QoS 规则添加到 active_flows
            self.switch_app.active_flows[f'qos_{rule_id}'] = {
                'type': 'qos',
                'path': path,
                'path_switches': path_switches,
                'rule_id': rule_id,
                'rule': rule
            }

            self.switch_app.logger.info(f"[QoS] Created rule: {rule['name']} ({src_ip} -> {dst_ip})")

            # 返回创建的规则（包含 ID）
            rule_data = rule.copy()
            rule_data['id'] = rule_id
            rule_data['path_names'] = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in path]
            body = json.dumps({'success': True, 'rule': rule_data}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)

        except Exception as e:
            error_msg = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

    @route('qos', '/qos/rules/{rule_id}', methods=['GET'])
    def get_qos_rule(self, req, rule_id):
        """获取指定 QoS 规则"""
        try:
            rule_id = int(rule_id)
            if rule_id not in self.switch_app.qos_rules:
                error_msg = json.dumps({'success': False, 'error': 'Rule not found'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=404)

            rule = self.switch_app.qos_rules[rule_id].copy()
            rule['id'] = rule_id
            if 'path' in rule:
                rule['path_names'] = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in rule['path']]
            body = json.dumps(rule, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        except ValueError:
            error_msg = json.dumps({'success': False, 'error': 'Invalid rule ID'}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body, status=400)

    @route('qos', '/qos/rules/{rule_id}', methods=['PUT', 'POST'])
    def update_qos_rule(self, req, rule_id):
        """更新指定 QoS 规则"""
        try:
            rule_id = int(rule_id)
            if rule_id not in self.switch_app.qos_rules:
                error_msg = json.dumps({'success': False, 'error': 'Rule not found'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=404)

            data = json.loads(req.body.decode('utf-8'))
            rule = self.switch_app.qos_rules[rule_id]

            # 删除旧流表
            self.switch_app._delete_qos_flow(rule)

            # 更新字段
            if 'name' in data:
                rule['name'] = data['name']
            if 'src_ip' in data:
                rule['src_ip'] = data['src_ip']
            if 'dst_ip' in data:
                rule['dst_ip'] = data['dst_ip']
            if 'protocol' in data:
                rule['protocol'] = data['protocol']
            if 'src_port' in data:
                rule['src_port'] = data['src_port']
            if 'dst_port' in data:
                rule['dst_port'] = data['dst_port']
            if 'dscp' in data:
                rule['dscp'] = int(data['dscp'])
            if 'priority' in data:
                rule['priority'] = int(data['priority'])
            if 'guarantee' in data:
                rule['guarantee'] = data['guarantee']
            if 'enabled' in data:
                rule['enabled'] = data['enabled']

            # 重新计算路径（如果 IP 或 guarantee 改变）
            src_ip = rule['src_ip']
            dst_ip = rule['dst_ip']
            guarantee = rule['guarantee']
            src_dpid = self.switch_app.host_ip_to_dpid.get(src_ip)
            dst_dpid = self.switch_app.host_ip_to_dpid.get(dst_ip)

            if src_dpid and dst_dpid:
                pref_map = {
                    'default': None,
                    'bandwidth': WeightPref.Bandwidth,
                    'latency': WeightPref.Latency,
                    'lossrate': WeightPref.LossRate,
                    'loadbalance': WeightPref.LoadBalance,
                    'samplealgorithm': WeightPref.SmartAlgorithm
                }
                preference = pref_map.get(guarantee)
                path = self.switch_app.get_shortest_path(src_dpid, dst_dpid, preference)
                if path and len(path) >= 2:
                    rule['path'] = path
                    rule['src_dpid'] = src_dpid
                    rule['dst_dpid'] = dst_dpid
                    src_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(src_ip), {})
                    dst_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(dst_ip), {})
                    path_switches = self.switch_app.get_path_switches(
                        path,
                        src_host.get('dpid', path[0]),
                        src_host.get('port', 1),
                        dst_host.get('dpid', path[-1]),
                        dst_host.get('port', 1)
                    )
                    rule['path_switches'] = path_switches

                    # 更新 active_flows 中的记录
                    flow_key = f'qos_{rule_id}'
                    if flow_key in self.switch_app.active_flows:
                        self.switch_app.active_flows[flow_key] = {
                            'type': 'qos',
                            'path': path,
                            'path_switches': path_switches,
                            'rule_id': rule_id,
                            'rule': rule
                        }

            # 安装新流表
            if rule['enabled']:
                self.switch_app._install_qos_flow(rule)

            self.switch_app.logger.info(f"[QoS] Updated rule: {rule['name']} (id={rule_id})")

            rule_data = rule.copy()
            rule_data['id'] = rule_id
            if 'path' in rule_data:
                rule_data['path_names'] = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in rule_data['path']]
            body = json.dumps({'success': True, 'rule': rule_data}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)

        except ValueError:
            error_msg = json.dumps({'success': False, 'error': 'Invalid rule ID'}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)
        except Exception as e:
            error_msg = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

    @route('qos', '/qos/rules/{rule_id}', methods=['DELETE'])
    def delete_qos_rule(self, req, rule_id):
        """删除指定 QoS 规则"""
        try:
            rule_id = int(rule_id)
            if rule_id not in self.switch_app.qos_rules:
                error_msg = json.dumps({'success': False, 'error': 'Rule not found'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=404)

            rule = self.switch_app.qos_rules[rule_id]

            # 删除流表
            self.switch_app._delete_qos_flow(rule)

            # 从 active_flows 中删除
            flow_key = f'qos_{rule_id}'
            if flow_key in self.switch_app.active_flows:
                del self.switch_app.active_flows[flow_key]

            # 删除规则
            del self.switch_app.qos_rules[rule_id]

            self.switch_app.logger.info(f"[QoS] Deleted rule: {rule['name']} (id={rule_id})")

            body = json.dumps({'success': True}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)

        except ValueError:
            error_msg = json.dumps({'success': False, 'error': 'Invalid rule ID'}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)
        except Exception as e:
            error_msg = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)