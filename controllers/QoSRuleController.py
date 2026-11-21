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

    # SLA 数值字段 → (字段名, 单位, 上限)。上限是**物理合理性**检查，
    # 不是业务约束：超过这些值基本可以断定单位填错了（比如把 bps 当
    # Mbps 传），早失败比事后查「为什么限速没生效」便宜得多。
    _SLA_FIELDS = (
        ('target_bps', 'Mbps', 100000.0),
        ('target_delay_ms', 'ms', 60000.0),
        ('target_loss_pct', '%', 100.0),
    )

    # 损伤注入字段（测试/演示用）：把指定时延/丢包施加到本规则路径首跳。
    # 与 _SLA_FIELDS 严格分开——约束只决定「选哪条路」，注入才改链路。
    _INJECT_FIELDS = (
        ('inject_delay_ms', 'ms', 60000.0),
        ('inject_loss_pct', '%', 100.0),
    )

    @classmethod
    def _parse_fields(cls, data, fields):
        """按 (字段名, 单位, 上限) 表解析数值字段 → (dict, error)

        全部可选；给了就必须是 > 0 的有限数。缺省写 None，
        让控制器侧按「没有该项」处理。
        """
        out = {}
        for key, unit, upper in fields:
            raw = data.get(key)
            if raw is None or raw == '':
                out[key] = None
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return None, f'{key} must be a number (unit: {unit})'
            if not (0 < value <= upper):
                return None, f'{key} must be in (0, {upper:g}] {unit}'
            out[key] = value
        return out, None

    @classmethod
    def _parse_sla(cls, data):
        """解析 SLA 约束字段（target_bps / target_delay_ms / target_loss_pct）"""
        return cls._parse_fields(data, cls._SLA_FIELDS)

    @classmethod
    def _parse_injection(cls, data):
        """解析损伤注入字段（inject_delay_ms / inject_loss_pct）"""
        return cls._parse_fields(data, cls._INJECT_FIELDS)

    @route('qos', '/qos/rules', methods=['GET'])
    def get_all_qos_rules(self, req):
        """获取所有 QoS 规则"""
        rules = []
        for rule_id, rule in self.switch_app.qos_rules.items():
            rule_data = rule.copy()
            rule_data['id'] = rule_id
            # 将 path DPIDs 转换为名称
            if 'path' in rule_data:
                rule_data['path_names'] = [self.switch_app.sw_name(p) for p in rule_data['path']]
            rules.append(rule_data)
        body = json.dumps(rules, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('qos', '/qos/rules', methods=['POST'])
    def create_qos_rule(self, req):
        """创建新的 QoS 规则"""
        try:
            data = json.loads(req.body.decode('utf-8'))

            # 验证必填字段
            required_fields = ['src_ip', 'dst_ip']
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
            valid_guarantees = ('default', 'bandwidth', 'latency', 'loss_rate', 'load_balance', 'smart_algorithm')
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

            # 验证 SLA 约束字段（全部可选，给了就必须是正数）
            #   target_bps       承诺速率，单位 Mbps（落到 meter 限速）
            #   target_delay_ms  时延上限，单位 ms（**只做选路约束**）
            #   target_loss_pct  丢包上限，单位 %（**只做选路约束**）
            sla_fields, sla_error = self._parse_sla(data)
            if sla_error:
                error_msg = json.dumps({'success': False, 'error': sla_error}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            # 验证损伤注入字段（可选，与上面的约束无关）
            #   inject_delay_ms / inject_loss_pct：把指定损伤施加到本规则路径首跳，
            #   用于构造「某条路径不满足 SLA」的场景，供其它规则避让
            inject_fields, inject_error = self._parse_injection(data)
            if inject_error:
                error_msg = json.dumps({'success': False, 'error': inject_error}, ensure_ascii=False).encode('utf-8')
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
                'default': WeightPref.Default,
                'bandwidth': WeightPref.Bandwidth,
                'latency': WeightPref.Latency,
                'loss_rate': WeightPref.LossRate,
                'load_balance': WeightPref.LoadBalance,
                'smart_algorithm': WeightPref.SmartAlgorithm
            }
            preference = pref_map.get(guarantee)

            # 计算路径
            # 有 SLA 目标时改用带约束的选路：先剪掉已知会违反约束的边，
            # 剪完不可达则退回尽力而为（reason 会说明），保证规则仍能建成
            path, sla_reason = self.switch_app.get_sla_path(src_dpid, dst_dpid, sla_fields)
            if not path or len(path) < 2:
                error_msg = json.dumps({'success': False, 'error': 'No path found between source and destination'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)
            if sla_reason:
                self.switch_app.logger.warning(f"[SLA] rule {data.get('name')}: {sla_reason}")

            # 获取路径上的交换机信息
            src_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(src_ip))
            dst_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(dst_ip))

            path_switches = self.switch_app.get_path_switches(path, src_host.get('dpid'), src_host.get('port'), dst_host.get('dpid'), dst_host.get('port'))

            # 生成规则 ID
            rule_id = self.switch_app.qos_rule_counter + 1
            self.switch_app.qos_rule_counter += 1

            # 创建规则
            rule = {
                'name': data.get('name') if data.get('name') else f'qos {rule_id}',
                'id': rule_id,
                'src_ip': src_ip,
                'dst_ip': dst_ip,
                'protocol': protocol,
                'src_port': data.get('src_port'),
                'dst_port': data.get('dst_port'),
                'dscp': dscp,
                'priority': priority,
                'guarantee': guarantee,
                'path': path,
                'path_switches': path_switches
            }
            # 写入 SLA 约束与损伤注入字段（含 None，字段始终存在便于接口消费）
            rule.update(sla_fields)
            rule.update(inject_fields)
            if sla_reason:
                rule['sla_fallback'] = sla_reason

            self.switch_app.qos_rules[rule_id] = rule
            self.switch_app.install_qos_flow(rule)

            # 将 QoS 规则添加到 active_flows
            self.switch_app.active_flows[rule_id] = {
                'type': 'qos',
                'path': path,
                'path_switches': path_switches,
                'rule': rule,
                'src_host': src_host,
                'dst_host': dst_host,
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
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

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
            self.switch_app.delete_qos_flow(rule)

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

            # SLA 约束字段：只要请求里出现任一字段就整体重解析，
            # 未出现的字段保持原值（部分更新语义）
            if any(k in data for k in ('target_bps', 'target_delay_ms', 'target_loss_pct')):
                merged = {k: data.get(k, rule.get(k)) for k, _u, _m in self._SLA_FIELDS}
                new_sla, sla_error = self._parse_sla(merged)
                if sla_error:
                    error_msg = json.dumps({'success': False, 'error': sla_error}, ensure_ascii=False).encode('utf-8')
                    return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)
                rule.update(new_sla)

            # 损伤注入字段：同样按部分更新处理
            if any(k in data for k in ('inject_delay_ms', 'inject_loss_pct')):
                merged = {k: data.get(k, rule.get(k)) for k, _u, _m in self._INJECT_FIELDS}
                new_inject, inject_error = self._parse_injection(merged)
                if inject_error:
                    error_msg = json.dumps({'success': False, 'error': inject_error}, ensure_ascii=False).encode('utf-8')
                    return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)
                rule.update(new_inject)

            # 重新计算路径（如果 IP 或 guarantee 改变）
            src_ip = rule['src_ip']
            dst_ip = rule['dst_ip']
            guarantee = rule['guarantee']
            src_dpid = self.switch_app.host_ip_to_dpid.get(src_ip)
            dst_dpid = self.switch_app.host_ip_to_dpid.get(dst_ip)

            if not src_dpid or not dst_dpid:
                error_msg = json.dumps({'success': False, 'error': 'Source or destination host not found in topology'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            pref_map = {
                'default': WeightPref.Default,
                'bandwidth': WeightPref.Bandwidth,
                'latency': WeightPref.Latency,
                'loss_rate': WeightPref.LossRate,
                'load_balance': WeightPref.LoadBalance,
                'smart_algorithm': WeightPref.SmartAlgorithm
            }
            preference = pref_map.get(guarantee)
            path = self.switch_app.get_shortest_path(src_dpid, dst_dpid, preference)
            if not path or len(path) < 2:
                error_msg = json.dumps({'success': False, 'error': 'No path found between source and destination'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            src_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(src_ip))
            dst_host = self.switch_app.hosts.get(self.switch_app.ip_to_mac.get(dst_ip))
            path_switches = self.switch_app.get_path_switches(path, src_host.get('dpid'), src_host.get('port'), dst_host.get('dpid'), dst_host.get('port'))

            rule['path'] = path
            rule['path_switches'] = path_switches

            # 更新 active_flows 中的记录
            if rule_id in self.switch_app.active_flows:
                self.switch_app.active_flows[rule_id] = {
                    'type': 'qos',
                    'path': path,
                    'path_switches': path_switches,
                    'rule': rule,
                    'src_host': src_host,
                    'dst_host': dst_host,
                }
            self.switch_app.install_qos_flow(rule)
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
            self.switch_app.delete_qos_flow(rule)

            # 从 active_flows 中删除
            flow_key = rule['id']
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