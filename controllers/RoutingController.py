import json
import os
import sys
import time

from ryu.app.wsgi import ControllerBase, route
from webob import Response

# 算法包在仓库根目录（与 QoSController.py 里的注入保持一致）。
# 导入失败不致命：只是 /routing/weights 的 GET 无法给出 defaults，
# 控制器主体仍能跑单目标路由。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    import algo.routing as algo_routing
except Exception:      # noqa: BLE001
    algo_routing = None

topology_api_name = 'topology_api'


class RoutingController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(RoutingController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('routing', '/routing/algorithm', methods=['GET'])
    def get_routing_algorithm(self, req):
        """获取当前路由算法配置"""
        body = json.dumps({'algorithm': self.switch_app.routing_algorithm}, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('routing', '/routing/algorithm', methods=['PUT', 'POST'])
    def set_routing_algorithm(self, req):
        """设置路由算法: 'dijkstra' 或 'smart'"""
        try:
            data = json.loads(req.body.decode('utf-8'))
            algorithm = data.get('algorithm', 'smart')
            if algorithm not in ['dijkstra', 'smart']:
                error_msg = json.dumps({'success': False, 'error': 'Invalid algorithm. Must be dijkstra or smart'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            if self.switch_app.routing_algorithm == algorithm:
                self.switch_app.logger.info(f"[Routing] Algorithm unchanged ({algorithm}), skipping")
                body = json.dumps({'success': True, 'algorithm': algorithm, 'reinstalled': [], 'skipped': [], 'unchanged': True}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=body)

            self.switch_app.routing_algorithm = algorithm
            self.switch_app.logger.info(f"[Routing] Algorithm changed to: {algorithm}")

            reinstalled = []
            skipped = []
            for multicast_addr in list(self.switch_app.multicast_groups.keys()):
                if multicast_addr not in self.switch_app.multicast_sources:
                    skipped.append({'group': multicast_addr, 'reason': 'no source IP'})
                    continue
                if multicast_addr not in self.switch_app.multicast_groups or not self.switch_app.multicast_groups[multicast_addr]:
                    skipped.append({'group': multicast_addr, 'reason': 'no members'})
                    continue
                self.switch_app._delete_multicast_flows(multicast_addr)
                success = self.switch_app._install_multicast_flow_rules(multicast_addr)
                if success:
                    reinstalled.append(multicast_addr)
                else:
                    skipped.append({'group': multicast_addr, 'reason': 'installation failed'})

                time.sleep(5)

            self.switch_app.logger.info(f"[Routing] Reinstalled {len(reinstalled)} multicast flows, skipped {len(skipped)}")
            body = json.dumps({'success': True, 'algorithm': algorithm, 'reinstalled': reinstalled, 'skipped': skipped}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        except Exception as e:
            error_msg = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

    @route('routing', '/routing/weights', methods=['GET', 'PUT', 'POST'])
    def routing_weights(self, req):
        """查看/调整多目标联合路由的权重（2.4.2）。

        GET  → {'weights': {...}, 'defaults': {...}, 'available': bool}
        PUT  → 请求体 {"weights": {"delay":0.4, ...}}，返回生效后的权重组。

        未知键忽略、非法值回退默认（见 algo.routing.sanitize_weights），
        所以调参不会因为一个手误的字段把路由整体打坏。
        """
        app = self.switch_app
        # 读实例属性 app.algo_available（QoSController.__init__ 里挂的），
        # 不要读 app.ALGO_AVAILABLE —— 那是模块级名字，实例上没有，
        # getattr 会一律回退成 False，导致导入成功却报「算法包不可用」。
        available = bool(getattr(app, 'algo_available',
                                 algo_routing is not None))
        if req.method == 'GET':
            body = json.dumps({
                'weights': getattr(app, 'routing_weights', {}),
                'defaults': (dict(algo_routing.DEFAULT_WEIGHTS)
                             if available and algo_routing else {}),
                'available': available,
            }, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)

        try:
            data = json.loads(req.body.decode('utf-8'))
            raw = data.get('weights', data)
            if not available:
                raise RuntimeError('algo package unavailable; cannot set weights')
            app.routing_weights = algo_routing.sanitize_weights(raw)
            app.logger.info(f"[Routing] weights updated: {app.routing_weights}")
            body = json.dumps({'success': True, 'weights': app.routing_weights},
                              ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        except Exception as e:
            error_msg = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

    @route('routing', '/routing/compute', methods=['GET', 'PUT', 'POST', 'DELETE'])
    def routing_compute(self, req):
        """注入/查看各交换机关联的算力占用（0~1），供算力感知路由使用。

        GET    → {'compute_load': {'<dpid>': 0.7, ...}}
        PUT    → {"compute_load": {"<dpid 或交换机名>": 0.7}}
        DELETE → 清空（回到「不影响选路」）

        这是「算力路由」的数据入口：节点算力余量由外部调度器（或后续接
        Prometheus 的采集脚本）在此写入，路由据此把繁忙算力节点算作更贵。
        dpid 接受十进制或 '0000000000010001' 形式的 16 位十六进制。
        """
        app = self.switch_app
        if req.method == 'GET':
            body = json.dumps({
                'compute_load': {str(k): v for k, v in getattr(app, 'compute_load', {}).items()},
            }, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)

        if req.method == 'DELETE':
            app.compute_load = {}
            body = json.dumps({'success': True, 'compute_load': {}},
                              ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)

        try:
            data = json.loads(req.body.decode('utf-8'))
            raw = data.get('compute_load', data)
            if not isinstance(raw, dict):
                raise ValueError('compute_load must be an object: {dpid_or_name: 0..1}')

            merged = dict(getattr(app, 'compute_load', {}))
            for key, value in raw.items():
                dpid = self._resolve_dpid(key)
                if dpid is None:
                    raise ValueError(f'unknown switch: {key}')
                try:
                    load = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f'compute load for {key} must be a number')
                if not (0.0 <= load <= 1.0):
                    raise ValueError(f'compute load for {key} must be within [0, 1]')
                merged[dpid] = load
            app.compute_load = merged
            app.logger.info(f"[Routing] compute load updated: {merged}")
            body = json.dumps({'success': True,
                               'compute_load': {str(k): v for k, v in merged.items()}},
                              ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        except Exception as e:
            error_msg = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

    def _resolve_dpid(self, key):
        """把 '12' / '0000000000010001' / 交换机名 解析成 dpid → int 或 None"""
        app = self.switch_app
        if isinstance(key, int):
            return key if key in app.datapaths else None
        text = str(key).strip()
        try:
            value = int(text, 16) if len(text) > 8 else int(text)
        except ValueError:
            value = None
        if value is not None and value in app.datapaths:
            return value
        # 名称反查：switch_name 是 {16 位 dpid: 名称}
        for dpid_hex, name in getattr(app, 'switch_name', {}).items():
            if name == text:
                try:
                    return int(dpid_hex, 16)
                except ValueError:
                    continue
        return None