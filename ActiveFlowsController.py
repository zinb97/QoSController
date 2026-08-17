import json

from ryu.app.wsgi import ControllerBase, route
from webob import Response

topology_api_name = 'topology_api'


class ActiveFlowsController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(ActiveFlowsController, self).__init__(req, link, data, **config)
        self.switch_app = data[topology_api_name]

    @route('topology', '/flows/active', methods=['GET'])
    def get_active_flows(self, req):
        """获取所有活跃流（QoS 规则流 + 普通 IPv4 流）"""
        flows = []
        for flow_key, flow_info in self.switch_app.active_flows.items():
            if flow_info.get('type') == 'qos':
                rule = flow_info.get('rule', {})
                path_names = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in flow_info.get('path', [])]
                flows.append({
                    'type': 'qos',
                    'rule_id': flow_info.get('rule_id'),
                    'name': rule.get('name'),
                    'src_ip': rule.get('src_ip'),
                    'dst_ip': rule.get('dst_ip'),
                    'protocol': rule.get('protocol'),
                    'dscp': rule.get('dscp'),
                    'guarantee': rule.get('guarantee'),
                    'path': path_names,
                    'path_dpids': [int(p) for p in flow_info.get('path', [])],
                    'enabled': rule.get('enabled')
                })
            else:
                src_ip, dst_ip = flow_key
                path_names = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in flow_info['path']]
                flows.append({
                    'type': 'flow',
                    'src_ip': src_ip,
                    'dst_ip': dst_ip,
                    'path': path_names,
                    'path_dpids': [int(p) for p in flow_info['path']],
                    'src_host': flow_info['src_host'],
                    'dst_host': flow_info['dst_host']
                })
        body = json.dumps(flows, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('multicast', '/multicast/active_flows', methods=['GET'])
    def get_multicast_active_flows(self, req):
        """获取所有组播活跃流"""
        flows = []
        for multicast_addr, flow_info in self.switch_app.multicast_active_flows.items():
            path_edges = [[u, v] for (u, v) in flow_info['path']]
            path_dpids = []
            if flow_info['path']:
                src_dpid = flow_info['source_dpid']
                adj = {}
                for (u, v) in flow_info['path']:
                    if u not in adj:
                        adj[u] = []
                    if v not in adj[u]:
                        adj[u].append(v)
                    if v not in adj:
                        adj[v] = []

                visited = set()
                queue = [src_dpid]
                visited.add(src_dpid)
                while queue:
                    curr = queue.pop(0)
                    path_dpids.append(curr)
                    for nxt in adj.get(curr, []):
                        if nxt not in visited:
                            visited.add(nxt)
                            queue.append(nxt)

            path_names = [self.switch_app.switch_name.get(f'{p:016x}', f'{p}') for p in path_dpids]
            route_matrix_list = flow_info['route_matrix'].tolist() if hasattr(flow_info['route_matrix'], 'tolist') else flow_info['route_matrix']
            flows.append({
                'multicast_addr': multicast_addr,
                'source_ip': flow_info['source_ip'],
                'source_dpid': f'{flow_info["source_dpid"]:x}',
                'source_switch': self.switch_app.switch_name.get(f'{flow_info["source_dpid"]:016x}', f'{flow_info["source_dpid"]:x}'),
                'member_ips': flow_info['member_ips'],
                'path': path_names,
                'path_dpids': path_dpids,
                'path_edges': path_edges,
                'route_matrix': route_matrix_list,
                'installed_time': flow_info['installed_time']
            })
        body = json.dumps(flows, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)