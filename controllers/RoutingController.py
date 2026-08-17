import json
import time

from ryu.app.wsgi import ControllerBase, route
from webob import Response

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