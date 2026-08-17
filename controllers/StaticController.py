import os

from ryu.app.wsgi import ControllerBase, route
from webob import Response

topology_api_name = 'topology_api'


class StaticController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(StaticController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('topology', '/', methods=['GET'])
    def index(self, req):
        """主页"""
        try:
            project_root = os.path.dirname(os.path.dirname(__file__))
            html_path = os.path.join(project_root, 'static', 'index.html')
            with open(html_path, 'r', encoding='utf-8') as f:
                html = f.read()
            return Response(content_type='text/html; charset=UTF-8', body=html.encode('utf-8'))
        except Exception as e:
            error_msg = f"<html><body><h1>Error loading topology page</h1><p>{str(e)}</p></body></html>"
            return Response(content_type='text/html; charset=UTF-8', body=error_msg.encode('utf-8'), status=500)

    @route('topology', '/static/{filepath:.*}', methods=['GET'])
    def static_files(self, req, filepath):
        """通用静态文件服务（含路径遍历防护）"""
        try:
            project_root = os.path.dirname(os.path.dirname(__file__))
            base = os.path.realpath(os.path.join(project_root, 'static'))
            target = os.path.realpath(os.path.join(base, filepath))
            if not target.startswith(base + os.sep) or not os.path.isfile(target):
                return Response(status=404, body=b'Not Found')
            ext = os.path.splitext(target)[1].lower()
            content_type_map = {
                '.html': 'text/html; charset=UTF-8',
                '.css': 'text/css; charset=UTF-8',
                '.js': 'application/javascript; charset=UTF-8',
                '.json': 'application/json; charset=UTF-8',
                '.png': 'image/png',
                '.svg': 'image/svg+xml',
            }
            content_type = content_type_map.get(ext, 'application/octet-stream')
            with open(target, 'rb') as f:
                content = f.read()
            return Response(content_type=content_type, body=content)
        except Exception as e:
            return Response(status=500, body=str(e).encode('utf-8'))