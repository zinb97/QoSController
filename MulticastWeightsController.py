import json

from ryu.app.wsgi import ControllerBase, route
from webob import Response

topology_api_name = 'topology_api'


class MulticastWeightsController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(MulticastWeightsController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('multicast', '/multicast/weights', methods=['GET'])
    def get_all_multicast_weights(self, req):
        """获取所有组播组的权重配置"""
        body = json.dumps(self.switch_app.multicast_weights, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('multicast', '/multicast/weights/{multicast_addr}', methods=['GET'])
    def get_multicast_weight(self, req, multicast_addr):
        """获取指定组播组的权重配置"""
        if multicast_addr in self.switch_app.multicast_weights:
            weight = self.switch_app.multicast_weights[multicast_addr]
        else:
            weight = {'latency': 1.0, 'bandwidth': 1.0, 'loss': 1.0}
        body = json.dumps(weight, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('multicast', '/multicast/weights/{multicast_addr}', methods=['PUT', 'POST'])
    def set_multicast_weight(self, req, multicast_addr):
        """设置指定组播组的权重配置

        如果设置的权重与原有权重不同，且该组播组满足以下条件：
        1. 存在源 IP（multicast_sources 中有记录）
        2. 已安装过流规则（multicast_flow_installed 中有记录）

        则会删除已有的流规则，并使用新权重重新安装流规则。
        """
        try:
            data = json.loads(req.body.decode('utf-8'))
            latency = float(data.get('latency_weight', 1.0))
            bandwidth = float(data.get('bandwidth_weight', 1.0))
            loss = float(data.get('loss_weight', 1.0))

            if not (0 <= latency <= 1 and 0 <= bandwidth <= 1 and 0 <= loss <= 1):
                error_msg = json.dumps({'success': False, 'error': 'Weight values must be in range [0, 1]'}, ensure_ascii=False).encode('utf-8')
                return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

            new_weight = {
                'latency': latency,
                'bandwidth': bandwidth,
                'loss': loss
            }

            old_weight = self.switch_app.multicast_weights.get(multicast_addr)

            weights_changed = (old_weight is None) or (
                old_weight['latency'] != latency or
                old_weight['bandwidth'] != bandwidth or
                old_weight['loss'] != loss
            )

            if weights_changed and multicast_addr in self.switch_app.multicast_flow_installed:
                self.switch_app.multicast_weights[multicast_addr] = new_weight
                self.switch_app.logger.info(f"[Multicast Weights] Set weights for {multicast_addr}: {new_weight}")
                self.switch_app.logger.info(f"[Multicast Weights] Weight changed for {multicast_addr}, reinstalling flow rules with new weights")
                self.switch_app._delete_multicast_flows(multicast_addr)
                self.switch_app.multicast_flow_installed.discard(multicast_addr)
                self.switch_app._install_multicast_flow_rules(multicast_addr)

            body = json.dumps({'success': True, 'weights': new_weight}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        except Exception as e:
            error_msg = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=error_msg, status=400)

    @route('multicast', '/multicast/weights/{multicast_addr}', methods=['DELETE'])
    def delete_multicast_weight(self, req, multicast_addr):
        """删除指定组播组的权重配置，恢复默认值"""
        if multicast_addr in self.switch_app.multicast_weights:
            del self.switch_app.multicast_weights[multicast_addr]
            self.switch_app.logger.info(f"[Multicast Weights] Deleted weights for {multicast_addr}")
            body = json.dumps({'success': True}, ensure_ascii=False).encode('utf-8')
        else:
            body = json.dumps({'success': False, 'error': 'Group not found'}, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)