import json

from ryu.app.wsgi import ControllerBase, route
from webob import Response

topology_api_name = 'topology_api'


class SliceController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(SliceController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('topology', '/topology/slice', methods=['GET'])
    def get_current_slice(self, req):
        """获取当前生效的网络切片"""
        current_slice = getattr(self.switch_app, 'current_slice', 'all')
        body = json.dumps({
            'success': True,
            'current_slice': current_slice
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/topology/slice/basic', methods=['POST'])
    def activate_basic_slice(self, req):
        """切换到基础网络切片（占位实现，待后续接入子拓扑生成逻辑）"""
        self.switch_app.logger.info("[Slice] Request to activate basic slice (stub)")
        body = json.dumps({
            'success': False,
            'slice': 'basic',
            'error': '基础切片切换尚未实现'
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/topology/slice/low-latency', methods=['POST'])
    def activate_low_latency_slice(self, req):
        """切换到低时延网络切片（占位实现，待后续接入子拓扑生成逻辑）"""
        self.switch_app.logger.info("[Slice] Request to activate low-latency slice (stub)")
        body = json.dumps({
            'success': False,
            'slice': 'low-latency',
            'error': '低时延切片切换尚未实现'
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/topology/slice/low-loss', methods=['POST'])
    def activate_low_loss_slice(self, req):
        """切换到低丢包网络切片（占位实现，待后续接入子拓扑生成逻辑）"""
        self.switch_app.logger.info("[Slice] Request to activate low-loss slice (stub)")
        body = json.dumps({
            'success': False,
            'slice': 'low-loss',
            'error': '低丢包切片切换尚未实现'
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/topology/slice/low-latency-loss', methods=['POST'])
    def activate_low_latency_loss_slice(self, req):
        """切换到低时延-丢包网络切片（占位实现，待后续接入子拓扑生成逻辑）"""
        self.switch_app.logger.info("[Slice] Request to activate low-latency-loss slice (stub)")
        body = json.dumps({
            'success': False,
            'slice': 'low-latency-loss',
            'error': '低时延-丢包切片切换尚未实现'
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)
