import json

from ryu.app.wsgi import ControllerBase, route
from webob import Response

topology_api_name = 'topology_api'


class LinkEventController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(LinkEventController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('topology', '/link_event', methods=['GET', 'POST'])
    def set_link_event(self, req):
        """获取或设置链路事件处理开关"""
        if req.method == 'GET':
            body = json.dumps({'enabled': self.switch_app.link_event_enabled}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        try:
            data = json.loads(req.body.decode('utf-8'))
            enabled = data.get('enabled', True)
            self.switch_app.link_event_enabled = enabled
            response = json.dumps({'success': True, 'enabled': enabled}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=response)
        except Exception as e:
            response = json.dumps({'success': False, 'error': str(e)}, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=response)