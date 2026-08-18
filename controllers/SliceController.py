import json
import numpy as np
import networkx as nx

from ryu.app.wsgi import ControllerBase, route
from webob import Response

from client import RL_multicast_tree

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
        """切换到低时延网络切片"""
        self.switch_app.logger.info("[Slice] Request to activate low-latency slice")
        slice_topology = self._build_slice_topology('low-latency')
        if slice_topology is None:
            body = json.dumps({
                'success': False,
                'slice': 'low-latency',
                'error': '低时延切片拓扑不存在'
            }, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        body = json.dumps({
            'success': True,
            'slice': 'low-latency',
            'topology': slice_topology
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/topology/slice/low-loss', methods=['POST'])
    def activate_low_loss_slice(self, req):
        """切换到低丢包网络切片"""
        self.switch_app.logger.info("[Slice] Request to activate low-loss slice")
        slice_topology = self._build_slice_topology('low-loss')
        if slice_topology is None:
            body = json.dumps({
                'success': False,
                'slice': 'low-loss',
                'error': '低丢包切片拓扑不存在'
            }, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        body = json.dumps({
            'success': True,
            'slice': 'low-loss',
            'topology': slice_topology
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    @route('topology', '/topology/slice/low-latency-loss', methods=['POST'])
    def activate_low_latency_loss_slice(self, req):
        """切换到低时延-丢包网络切片"""
        self.switch_app.logger.info("[Slice] Request to activate low-latency-loss slice")
        slice_topology = self._build_slice_topology('low-latency-loss')
        if slice_topology is None:
            body = json.dumps({
                'success': False,
                'slice': 'low-latency-loss',
                'error': '低时延-丢包切片拓扑不存在'
            }, ensure_ascii=False).encode('utf-8')
            return Response(content_type='application/json; charset=UTF-8', body=body)
        body = json.dumps({
            'success': True,
            'slice': 'low-latency-loss',
            'topology': slice_topology
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)

    def _build_slice_topology(self, slice_type):
        """根据切片类型构建拓扑数据"""
        slice_config = getattr(self.switch_app, 'slice_config', None)
        slice_adj_matrix = getattr(self.switch_app, 'slice_adj_matrix', None)
        graph = getattr(self.switch_app, 'graph', None)
        adj_matrix_nodes = sorted(graph.nodes())

        if slice_config is None or slice_adj_matrix is None or graph is None:
            return None

        config_key_map = {
            'low-latency': 'low_latency',
            'low-loss': 'low_loss',
            'low-latency-loss': 'low_latency_loss'
        }
        matrix_key_map = {
            'low-latency': 'low_latency',
            'low-loss': 'low_loss',
            'low-latency-loss': 'low_latency_loss'
        }

        config_key = config_key_map.get(slice_type)
        matrix_key = matrix_key_map.get(slice_type)

        if config_key is None or matrix_key is None:
            return None

        slice_nodes = set(slice_config.get(config_key, {}).get('nodes', []))
        adj_matrix = slice_adj_matrix.get(matrix_key, [])

        if not adj_matrix:
            return None

        net_size = len(adj_matrix_nodes)
        if net_size == 0:
            return None

        # 从邻接矩阵中提取切片网络涉及的节点
        slice_network_nodes = set()
        adj_matrix_np = np.array(adj_matrix)
        for i in range(net_size):
            for j in range(net_size):
                if adj_matrix_np[i, j] > 0:
                    slice_network_nodes.add(adj_matrix_nodes[i])
                    slice_network_nodes.add(adj_matrix_nodes[j])

        # 补充：切片所属节点也必须包含在网络中
        slice_network_nodes.update(slice_nodes)

        # 构建交换机列表（邻接矩阵涉及的所有节点 + 切片所属节点）
        switches = []
        for dpid in slice_network_nodes:
            if dpid not in adj_matrix_nodes:
                continue
            attrs = graph.nodes.get(dpid, {})
            switches.append({
                'dpid': dpid,
                'name': attrs.get('name', self.switch_app.sw_name(dpid)),
                'level': attrs.get('level', 3),
                'ports': attrs.get('ports', [])
            })

        # 构建链路列表（邻接矩阵中所有 > 0 的边）
        links = []
        for i in range(net_size):
            for j in range(net_size):
                if adj_matrix_np[i, j] > 0:
                    src_dpid = adj_matrix_nodes[i]
                    dst_dpid = adj_matrix_nodes[j]
                    edge_attrs = graph.edges.get((src_dpid, dst_dpid), {})
                    if not edge_attrs:
                        edge_attrs = graph.edges.get((dst_dpid, src_dpid), {})
                    links.append({
                        'src': {'dpid': src_dpid, 'port': edge_attrs.get('src_port', 0)},
                        'dst': {'dpid': dst_dpid, 'port': edge_attrs.get('dst_port', 0)},
                        'bw': edge_attrs.get('bw', 0),
                        'delay': edge_attrs.get('delay', 0),
                        'loss': edge_attrs.get('loss', 0),
                        'load': {
                            'rx_bps': edge_attrs.get('rx_bps', 0),
                            'tx_bps': edge_attrs.get('tx_bps', 0),
                            'total_bps': edge_attrs.get('total_bps', 0)
                        }
                    })

        return {'switches': switches, 'links': links, 'hosts': []}

    def _generate_slice_adj_matrix(self, graph, adj_matrix_nodes, slice_nodes, latency_weight, bandwidth_weight, loss_weight):
        """生成切片邻接矩阵：以每个节点为源，其余为目标，调用组播路由算法"""
        net_size = len(adj_matrix_nodes)
        if net_size == 0 or len(slice_nodes) == 0:
            return np.zeros((net_size, net_size), dtype=np.float32).tolist()

        # 构建完整拓扑的矩阵
        adj_matrix = nx.to_numpy_array(graph, nodelist=adj_matrix_nodes, dtype=np.float32)
        latency_matrix = nx.attr_matrix(graph, edge_attr='delay', rc_order=adj_matrix_nodes, dtype=np.float32)
        bandwidth_matrix = nx.attr_matrix(graph, edge_attr='bw', rc_order=adj_matrix_nodes, dtype=np.float32)
        loss_matrix = nx.attr_matrix(graph, edge_attr='loss', rc_order=adj_matrix_nodes, dtype=np.float32)

        # 初始化切片邻接矩阵
        slice_adj_matrix = np.zeros((net_size, net_size), dtype=np.float32)

        # 将 slice_nodes 中的 dpid 转换为索引
        node_indices = [adj_matrix_nodes.index(dpid) for dpid in slice_nodes if dpid in adj_matrix_nodes]

        # 对每个节点作为源，其余为目标，调用组播路由算法
        for src_idx in node_indices:
            # 其余节点作为目标
            dst_indices = [idx for idx in node_indices if idx != src_idx]
            if not dst_indices:
                continue

            # 调用组播路由算法（source_node 和 destination_group 都是 1-based）
            try:
                success, route_matrix = RL_multicast_tree(
                    adj_matrix, latency_matrix, bandwidth_matrix, loss_matrix,
                    source_node=src_idx + 1,
                    destination_group=[idx + 1 for idx in dst_indices],
                    latency_weight=latency_weight,
                    bandwidth_weight=bandwidth_weight,
                    loss_weight=loss_weight
                )
                if success and route_matrix is not None:
                    # 将路由矩阵的边添加到切片邻接矩阵
                    slice_adj_matrix += route_matrix
            except Exception as e:
                self.switch_app.logger.warning(f"[Slice] Failed to generate route for source {src_idx}: {e}")

        return slice_adj_matrix.tolist()

    @route('topology', '/topology/slice/config', methods=['PUT'])
    def save_slice_config(self, req):
        """保存切片配置（节点列表、阈值等），并生成各切片邻接矩阵"""
        data = json.loads(req.body.decode('utf-8'))
        self.switch_app.logger.info(f"[Slice] Request to save slice config: {data}")

        # 获取完整拓扑
        graph = getattr(self.switch_app, 'graph', None)
        adj_matrix_nodes = sorted(graph.nodes())
        net_size = len(adj_matrix_nodes)
        self.switch_app.logger.info(f"[Slice] \: {graph}")
        self.switch_app.logger.info(f"[Slice] \: {adj_matrix_nodes}")
        self.switch_app.logger.info(f"[Slice] \: {net_size}")

        # 解析并格式化保存切片配置
        self.switch_app.slice_config = {
            'low_latency': {
                'nodes': data.get('low_latency', {}).get('nodes', []),
                'threshold': data.get('low_latency', {}).get('threshold', 0.0)
            },
            'low_loss': {
                'nodes': data.get('low_loss', {}).get('nodes', []),
                'threshold': data.get('low_loss', {}).get('threshold', 0.0)
            },
            'low_latency_loss': {
                'nodes': data.get('low_latency_loss', {}).get('nodes', []),
                'latency_threshold': data.get('low_latency_loss', {}).get('latency_threshold', 0.0),
                'loss_threshold': data.get('low_latency_loss', {}).get('loss_threshold', 0.0)
            }
        }

        # 初始化四种切片的邻接矩阵
        self.switch_app.slice_adj_matrix = {
            'basic': np.zeros((net_size, net_size), dtype=np.float32).tolist() if net_size > 0 else [],
            'low_latency': np.zeros((net_size, net_size), dtype=np.float32).tolist() if net_size > 0 else [],
            'low_loss': np.zeros((net_size, net_size), dtype=np.float32).tolist() if net_size > 0 else [],
            'low_latency_loss': np.zeros((net_size, net_size), dtype=np.float32).tolist() if net_size > 0 else [],
        }

        # 如果拓扑存在，生成各切片邻接矩阵
        if graph is not None and net_size > 0:
            self.switch_app.logger.info(f"[Slice] 1111111111111111111111")
            # 低时延切片：latency_weight=1, bandwidth_weight=0, loss_weight=0
            self.switch_app.slice_adj_matrix['low_latency'] = self._generate_slice_adj_matrix(
                graph, adj_matrix_nodes, self.switch_app.slice_config['low_latency']['nodes'],
                latency_weight=1.0, bandwidth_weight=0.0, loss_weight=0.0
            )

            # 低丢包切片：latency_weight=0, bandwidth_weight=0, loss_weight=1
            self.switch_app.slice_adj_matrix['low_loss'] = self._generate_slice_adj_matrix(
                graph, adj_matrix_nodes, self.switch_app.slice_config['low_loss']['nodes'],
                latency_weight=0.0, bandwidth_weight=0.0, loss_weight=1.0
            )

            # 低时延-丢包切片：latency_weight=1, bandwidth_weight=0, loss_weight=1
            self.switch_app.slice_adj_matrix['low_latency_loss'] = self._generate_slice_adj_matrix(
                graph, adj_matrix_nodes, self.switch_app.slice_config['low_latency_loss']['nodes'],
                latency_weight=1.0, bandwidth_weight=0.0, loss_weight=1.0
            )

        self.switch_app.logger.info(f"[Slice] Parsed slice config: {self.switch_app.slice_config}")
        self.switch_app.logger.info(f"[Slice] Generated slice adjacency matrices: {self.switch_app.slice_adj_matrix}")

        body = json.dumps({
            'success': True
        }, ensure_ascii=False).encode('utf-8')
        return Response(content_type='application/json; charset=UTF-8', body=body)
