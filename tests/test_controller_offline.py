"""控制器行为测试（假 datapath，不需要 Mininet / 真实交换机）。

覆盖本次改造的四块：
1. DSCP 打标：首跳打标、中间跳认标、删除能覆盖两类表项
2. meter 限速：仅首跳、指令顺序 meter→apply、rate 换算正确
3. SLA 约束选路：剪枝、不可满足时退回
4. 反馈式负载均衡：去抖、冷却、增益门槛、低负载不触发

运行：
    cd QoSController && /usr/bin/python3.8 -m unittest tests.test_controller_offline -v
"""
import importlib.util
import logging
import os
import types
import unittest

import networkx as nx
from ryu.ofproto import ofproto_v1_3 as ofp
from ryu.ofproto import ofproto_v1_3_parser as parser

logging.disable(logging.CRITICAL)

_HERE = os.path.dirname(os.path.abspath(__file__))
_QC_DIR = os.path.dirname(_HERE)

_spec = importlib.util.spec_from_file_location(
    'qcmod_under_test', os.path.join(_QC_DIR, 'QoSController.py'))
qc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qc)


class FakeDP(object):
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofp
        self.ofproto_parser = parser
        self.sent = []

    def send_msg(self, msg):
        self.sent.append(msg)


METHODS = (
    'add_flow', '_add_flow_with_meter', 'install_qos_flow', 'delete_qos_flow',
    '_rule_sla', '_rule_injection', '_positive_num',
    '_rule_meter_id', '_install_meter', '_delete_meter',
    '_install_netem_for_rule', '_remove_netem_for_rule', '_sla_block_reason',
    'get_sla_path', 'get_shortest_path', '_compute_path',
    '_check_and_update_load_balance_rules', '_predict_path_utilization',
    '_joint_route',
)


def bind(obj, names=METHODS):
    for name in names:
        func = qc.QoSController.__dict__[name]
        if isinstance(func, staticmethod):
            # Python 3.8 的 staticmethod 对象不可直接调用，取 __func__ 后
            # 作为普通函数挂到实例上（实例调用不会再传 self）
            setattr(obj, name, func.__func__)
        else:
            setattr(obj, name, types.MethodType(func, obj))


def make_ctrl(dps=(1, 2, 3), graph=None):
    """最小可用的控制器替身：只提供被测试方法依赖的属性。

    默认绑定**真实的** install_qos_flow / delete_qos_flow，便于检查下发的
    流表与 meter 消息；负载均衡用例用 make_lb_ctrl（把装表换成计数器）。
    """
    c = types.SimpleNamespace()
    c.datapaths = {d: FakeDP(d) for d in dps}
    c.graph = graph if graph is not None else nx.DiGraph()
    c.logger = logging.getLogger('test')
    c.routing_weights = dict(qc.algo_routing.DEFAULT_WEIGHTS)
    c.compute_load = {}
    c.host_ip_to_dpid = {}
    c.ip_to_mac = {}
    c.hosts = {}
    c.active_flows = {}
    c.qos_rules = {}
    c._link_metrics_prev = {}
    c._link_load_predictors = {}
    c._lb_rule_state = {}
    c._lb_high_watermark = 0.75
    c._lb_hot_cycles = 3
    c._lb_cooldown_s = 8
    c._lb_min_gain = 0.05
    c._lb_last_cycle = {'evaluated': 0, 'triggered': 0, 'skipped': 0,
                        'ts': 0, 'duration': 0}
    c.installs = 0
    c.deletes = 0
    c.sw_name = lambda d: 'sw%d' % d
    bind(c)
    return c


def make_lb_ctrl(dps=(1, 2, 3), graph=None):
    """负载均衡专用：装/删表只计次数，不真的拼流表消息"""
    c = make_ctrl(dps=dps, graph=graph)
    c.install_qos_flow = lambda rule: (setattr(c, 'installs', c.installs + 1) or True)
    c.delete_qos_flow = lambda rule: setattr(c, 'deletes', c.deletes + 1)
    c.get_path_switches = lambda path, sd, sp, dd, dp: [
        {'dpid': n, 'in_port': n, 'out_port': n + 1} for n in path]
    return c


def base_rule(**over):
    rule = {
        'id': 7, 'name': 'r', 'src_ip': '10.0.0.1', 'dst_ip': '10.0.0.2',
        'protocol': 'tcp', 'dst_port': 5201, 'dscp': 8, 'priority': 200,
        'path': [1, 2, 3],
        'path_switches': [
            {'dpid': 1, 'in_port': 1, 'out_port': 2},
            {'dpid': 2, 'in_port': 2, 'out_port': 3},
            {'dpid': 3, 'in_port': 3, 'out_port': 4},
        ],
    }
    rule.update(over)
    return rule


def flow_msgs(dp):
    return [m for m in dp.sent if isinstance(m, parser.OFPFlowMod)]


def meter_msgs(dp):
    return [m for m in dp.sent if isinstance(m, parser.OFPMeterMod)]


def has_set_dscp(msg, dscp):
    for inst in msg.instructions:
        if inst.type == ofp.OFPIT_APPLY_ACTIONS:
            for act in inst.actions:
                if act.type == ofp.OFPAT_SET_FIELD and act.key == 'ip_dscp' \
                        and act.value == dscp:
                    return True
    return False


class DscpMarkingTests(unittest.TestCase):
    def test_first_hop_marks_and_middle_hop_only_matches(self):
        c = make_ctrl()
        c.install_qos_flow(base_rule())
        fwd_first = flow_msgs(c.datapaths[1])[0]
        fwd_mid = flow_msgs(c.datapaths[2])[0]

        self.assertNotIn('ip_dscp', dict(fwd_first.match.items()))
        self.assertTrue(has_set_dscp(fwd_first, 8))
        # 中间跳认标：match 带 ip_dscp，动作里没有 set_field
        self.assertEqual(dict(fwd_mid.match.items())['ip_dscp'], 8)
        self.assertFalse(has_set_dscp(fwd_mid, 8))

    def test_reverse_marking_at_last_hop(self):
        c = make_ctrl()
        c.install_qos_flow(base_rule())
        reverse_last = flow_msgs(c.datapaths[3])[1]
        self.assertTrue(has_set_dscp(reverse_last, 8))

    def test_dscp_zero_keeps_legacy_behaviour(self):
        c = make_ctrl()
        c.install_qos_flow(base_rule(dscp=0))
        for dp in c.datapaths.values():
            for msg in flow_msgs(dp):
                self.assertNotIn('ip_dscp', dict(msg.match.items()))
                self.assertFalse(has_set_dscp(msg, 0))

    def test_single_switch_path_marks_both_directions(self):
        c = make_ctrl(dps=(1,))
        c.install_qos_flow(base_rule(path=[1],
                                     path_switches=[{'dpid': 1, 'in_port': 1,
                                                     'out_port': 2}]))
        msgs = flow_msgs(c.datapaths[1])
        self.assertEqual(len(msgs), 2)
        self.assertTrue(all(has_set_dscp(m, 8) for m in msgs))


class MeterTests(unittest.TestCase):
    def test_meter_installed_only_on_first_hop(self):
        c = make_ctrl()
        c.install_qos_flow(base_rule(target_bps=100.0, target_delay_ms=None,
                                     target_loss_pct=None))
        self.assertEqual(len(meter_msgs(c.datapaths[1])), 2)  # delete + add
        self.assertEqual(len(meter_msgs(c.datapaths[2])), 0)

    def test_meter_rate_converted_to_kbps(self):
        c = make_ctrl()
        c.install_qos_flow(base_rule(target_bps=50.0))
        add = [m for m in meter_msgs(c.datapaths[1])
               if m.command == ofp.OFPMC_ADD][0]
        self.assertEqual(add.bands[0].rate, 50000)

    def test_meter_instruction_precedes_actions(self):
        c = make_ctrl()
        c.install_qos_flow(base_rule(target_bps=50.0))
        first = flow_msgs(c.datapaths[1])[0]
        self.assertEqual(first.instructions[0].type, ofp.OFPIT_METER)
        self.assertEqual(first.instructions[1].type, ofp.OFPIT_APPLY_ACTIONS)

    def test_no_sla_no_meter(self):
        c = make_ctrl()
        c.install_qos_flow(base_rule())
        for dp in c.datapaths.values():
            self.assertEqual(meter_msgs(dp), [])


class SlaRoutingTests(unittest.TestCase):
    def _diamond(self, short_delay=5, short_loss=0.1, short_used=800):
        g = nx.DiGraph()
        for u, v, d, l, used in ((1, 2, short_delay, short_loss, short_used),
                                 (2, 4, short_delay, short_loss, short_used),
                                 (1, 3, 25, 0.5, 20), (3, 4, 25, 0.5, 20)):
            g.add_edge(u, v, delay=d, loss=l, loss_measured=l, bw=1000,
                       oneway_bps=used, total_bps=used, src_port=v)
        return g

    def test_constraint_filters_bad_path(self):
        c = make_ctrl(graph=self._diamond())
        path, reason = c.get_sla_path(1, 4, {'target_bps': None,
                                             'target_delay_ms': 20,
                                             'target_loss_pct': None})
        self.assertEqual(path, [1, 2, 4])
        self.assertIsNone(reason)

    def test_impossible_constraint_falls_back(self):
        c = make_ctrl(graph=self._diamond())
        path, reason = c.get_sla_path(1, 4, {'target_bps': None,
                                             'target_delay_ms': 1,
                                             'target_loss_pct': None})
        self.assertEqual(path, [1, 2, 4])
        self.assertIn('fell back', reason)

    def test_bandwidth_unknown_is_not_blocked(self):
        g = nx.DiGraph()
        g.add_edge(1, 2, bw=0, delay=0, loss_measured=None, oneway_bps=0)
        g.add_edge(2, 3, bw=0, delay=0, loss_measured=None, oneway_bps=0)
        c = make_ctrl(graph=g)
        path, reason = c.get_sla_path(1, 3, {'target_bps': 500,
                                             'target_delay_ms': None,
                                             'target_loss_pct': None})
        self.assertEqual(path, [1, 2, 3])
        self.assertIsNone(reason)


class DeleteTests(unittest.TestCase):
    def test_delete_match_omits_dscp_and_clears_meter(self):
        c = make_ctrl()
        rule = base_rule(target_bps=100.0)
        c.install_qos_flow(rule)
        for dp in c.datapaths.values():
            dp.sent.clear()
        c.delete_qos_flow(rule)

        for dp in c.datapaths.values():
            for msg in flow_msgs(dp):
                self.assertNotIn('ip_dscp', dict(msg.match.items()))
        self.assertEqual(rule.get('meter_id'), None)
        self.assertEqual(rule.get('netem_devs'), [])


class LoadBalanceTests(unittest.TestCase):
    def _graph(self, used_short, used_long):
        g = nx.DiGraph()
        for u, v, used in ((1, 2, used_short), (2, 4, used_short),
                           (4, 2, 0), (2, 1, 0),
                           (1, 3, used_long), (3, 4, used_long),
                           (4, 3, 0), (3, 1, 0)):
            g.add_edge(u, v, delay=10, loss=0.2, loss_measured=0.2, bw=1000,
                       oneway_bps=used, total_bps=used, src_port=v)
        return g

    def _ctrl(self, used_short=850, used_long=50, min_gain=0.05):
        c = make_lb_ctrl(graph=self._graph(used_short, used_long))
        c.host_ip_to_dpid = {'s': 1, 'd': 4}
        c.qos_rules = {1: {'id': 1, 'name': 'lb', 'guarantee': 'load_balance',
                           'src_ip': 's', 'dst_ip': 'd', 'path': [1, 2, 4],
                           'path_switches': []}}
        c._lb_min_gain = min_gain
        return c

    def _feed(self, c, value):
        for edge in ((1, 2), (2, 4), (1, 3), (3, 4)):
            pred = c._link_load_predictors.setdefault(
                edge, qc.algo_predict.TrendPredictor())
            pred.push(value)

    def test_reroute_after_consecutive_hot_cycles(self):
        c = self._ctrl()
        for _ in range(5):
            self._feed(c, 850)
        c._check_and_update_load_balance_rules()
        c._check_and_update_load_balance_rules()
        self.assertEqual(c.installs, 0)          # 去抖：未满 3 次
        c._check_and_update_load_balance_rules()
        self.assertEqual(c.installs, 1)
        self.assertEqual(c.qos_rules[1]['path'], [1, 3, 4])

    def test_cooldown_blocks_immediate_reroute(self):
        c = self._ctrl()
        for _ in range(5):
            self._feed(c, 850)
        for _ in range(3):
            c._check_and_update_load_balance_rules()
        before = c.installs
        c._lb_rule_state[1]['hot'] = 5
        c._check_and_update_load_balance_rules()
        self.assertEqual(c.installs, before)

    def test_low_load_never_triggers(self):
        c = self._ctrl(used_short=100, used_long=100)
        for _ in range(5):
            self._feed(c, 100)
        for _ in range(4):
            c._check_and_update_load_balance_rules()
        self.assertEqual(c.installs, 0)

    def test_gain_threshold_prevents_flapping(self):
        c = self._ctrl(used_short=900, used_long=880, min_gain=0.5)
        for _ in range(5):
            self._feed(c, 900)
        for _ in range(4):
            c._check_and_update_load_balance_rules()
        self.assertEqual(c.installs, 0)

    def test_non_load_balance_rule_ignored(self):
        c = self._ctrl()
        c.qos_rules[1]['guarantee'] = 'latency'
        for _ in range(4):
            c._check_and_update_load_balance_rules()
        self.assertEqual(c._lb_last_cycle['evaluated'], 0)

    def test_unknown_capacity_does_not_crash(self):
        c = self._ctrl()
        for u, v in ((1, 2), (2, 4), (1, 3), (3, 4)):
            c.graph[u][v]['bw'] = 0
            c.graph[u][v]['oneway_bps'] = 0
        c._check_and_update_load_balance_rules()
        self.assertEqual(c.installs, 0)
        self.assertEqual(c._lb_last_cycle['skipped'], 1)


class InjectTests(unittest.TestCase):
    """约束（target_*）与损伤注入（inject_*）必须完全分开。

    曾经的实现把 target_delay_ms 同时施加成 netem 延迟，等于「承诺时延
    不超过 20ms，然后主动加 20ms」。这组用例钉住拆分后的语义。
    """

    def setUp(self):
        self.calls = []
        self._orig_set = qc.utils.set_netem
        self._orig_find = qc.utils.find_injector_device
        qc.utils.set_netem = lambda dev, delay_ms=None, loss_pct=None, jitter_ms=None, limit=1000: (
            self.calls.append((dev, delay_ms, loss_pct)) or (True, ''))
        qc.utils.find_injector_device = lambda dev: dev

    def tearDown(self):
        qc.utils.set_netem = self._orig_set
        qc.utils.find_injector_device = self._orig_find

    def test_constraint_only_does_not_touch_tc(self):
        c = make_ctrl()
        rule = base_rule(target_delay_ms=20, target_loss_pct=0.5, target_bps=100)
        c.install_qos_flow(rule)
        self.assertEqual(self.calls, [])
        self.assertEqual(rule['netem_devs'], [])

    def test_injection_applies_to_first_hop(self):
        c = make_ctrl()
        rule = base_rule(inject_delay_ms=80, inject_loss_pct=1.0)
        c.install_qos_flow(rule)
        self.assertEqual(len(self.calls), 1)
        dev, delay, loss = self.calls[0]
        self.assertEqual(dev, 'sw1-eth2')     # 首跳出接口
        self.assertEqual(delay, 80)
        self.assertEqual(loss, 1.0)
        self.assertEqual(rule['netem_devs'], ['sw1-eth2'])

    def test_injection_without_constraint_is_allowed(self):
        c = make_ctrl()
        rule = base_rule(inject_delay_ms=50)
        c.install_qos_flow(rule)
        self.assertEqual(len(self.calls), 1)

    def test_parse_injection_rejects_bad_values(self):
        spec = importlib.util.spec_from_file_location(
            'qrc_under_test', os.path.join(_QC_DIR, 'controllers', 'QoSRuleController.py'))
        qrc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qrc)
        C = qrc.QoSRuleController

        ok, err = C._parse_injection({'inject_delay_ms': 30, 'inject_loss_pct': 2})
        self.assertIsNone(err)
        self.assertEqual(ok['inject_delay_ms'], 30.0)

        for bad in ({'inject_delay_ms': 0}, {'inject_delay_ms': -1},
                    {'inject_loss_pct': 101}, {'inject_delay_ms': 'x'}):
            _, err = C._parse_injection(bad)
            self.assertIsNotNone(err, bad)

    def test_sla_and_injection_dont_leak_into_each_other(self):
        spec = importlib.util.spec_from_file_location(
            'qrc_under_test2', os.path.join(_QC_DIR, 'controllers', 'QoSRuleController.py'))
        qrc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(qrc)
        C = qrc.QoSRuleController
        sla, _ = C._parse_sla({'inject_delay_ms': 30})
        inj, _ = C._parse_injection({'target_delay_ms': 30})
        self.assertEqual(set(sla), {'target_bps', 'target_delay_ms', 'target_loss_pct'})
        self.assertEqual(set(inj), {'inject_delay_ms', 'inject_loss_pct'})
        self.assertTrue(all(v is None for v in sla.values()))
        self.assertTrue(all(v is None for v in inj.values()))


class RoutingRestTests(unittest.TestCase):
    """REST 层契约：available 必须读实例属性 algo_available。

    曾经写成 getattr(app, 'ALGO_AVAILABLE', False) —— 那是模块级全局名，
    实例上没有，导入成功也会报 available=false，表现为「算法包明明在，
    界面却说不可用」。这里把它钉住。
    """

    def _controller(self, available):
        spec = importlib.util.spec_from_file_location(
            'rc_under_test', os.path.join(_QC_DIR, 'controllers', 'RoutingController.py'))
        rc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rc)

        class App(object):
            algo_available = available
            routing_weights = {'delay': 1.0}
            compute_load = {}
            logger = logging.getLogger('test')
            datapaths = {1: object()}
            switch_name = {'0000000000000001': 'sw1'}

        class Req(object):
            def __init__(self, method, body=None):
                import json as _json
                self.method = method
                self.body = _json.dumps(body).encode() if body is not None else b''

        ctrl = rc.RoutingController.__new__(rc.RoutingController)
        ctrl.switch_app = App()
        return rc, ctrl, Req

    def test_available_true_reports_defaults(self):
        rc, ctrl, Req = self._controller(True)
        payload = __import__('json').loads(ctrl.routing_weights(Req('GET')).body)
        self.assertTrue(payload['available'])
        self.assertEqual(payload['defaults'], rc.algo_routing.DEFAULT_WEIGHTS)

    def test_available_false_reports_empty_defaults(self):
        rc, ctrl, Req = self._controller(False)
        payload = __import__('json').loads(ctrl.routing_weights(Req('GET')).body)
        self.assertFalse(payload['available'])
        self.assertEqual(payload['defaults'], {})

    def test_put_rejected_when_unavailable(self):
        rc, ctrl, Req = self._controller(False)
        r = ctrl.routing_weights(Req('PUT', {'weights': {'delay': 1}}))
        self.assertIn('unavailable', r.body.decode())

    def test_put_sanitizes_weights(self):
        rc, ctrl, Req = self._controller(True)
        r = ctrl.routing_weights(Req('PUT', {'weights': {'delay': 9, 'bogus': 1}}))
        payload = __import__('json').loads(r.body)
        self.assertTrue(payload['success'])
        self.assertEqual(payload['weights']['delay'], 9.0)
        self.assertNotIn('bogus', payload['weights'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
