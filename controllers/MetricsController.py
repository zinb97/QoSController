from ryu.app.wsgi import ControllerBase, route
from webob import Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

topology_api_name = 'topology_api'


class MetricsController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(MetricsController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('metrics', '/metrics', methods=['GET'])
    def get_metrics(self, req):
        """获取 Prometheus 指标"""
        # 指标已在拓扑循环中自动更新
        metrics_output = generate_latest()
        return Response(content_type=CONTENT_TYPE_LATEST, body=metrics_output)
