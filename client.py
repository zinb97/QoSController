import grpc
import multicast_pb2
import multicast_pb2_grpc
import numpy as np


def RL_multicast_tree(adjacency_matrix, latency_matrix, bandwidth_matrix, loss_matrix,
                     source_node, destination_group,
                     latency_weight=1.0, bandwidth_weight=1.0, loss_weight=1.0,
                     server_address='192.168.10.21:50051'):
    """调用 gRPC 服务构建组播树"""

    def unflatten_matrix(flat, size):
        return [flat[i * size: (i + 1) * size] for i in range(size)]

    # 连接服务器
    channel = grpc.insecure_channel(server_address)
    stub = multicast_pb2_grpc.MulticastServiceStub(channel)

    def make_matrix(matrix):
        return multicast_pb2.AdjacencyMatrix(
            row=matrix.flatten().tolist(),
            size=matrix.shape[0]
        )

    request = multicast_pb2.MulticastRequest(
        adjacency_matrix=make_matrix(adjacency_matrix),
        source_node=source_node,
        destination_group=destination_group,
        latency_matrix=make_matrix(latency_matrix),
        bandwidth_matrix=make_matrix(bandwidth_matrix),
        loss_matrix=make_matrix(loss_matrix),
        latency_weight=latency_weight,
        bandwidth_weight=bandwidth_weight,
        loss_weight=loss_weight
    )

    # 调用 RPC
    response = stub.BuildMulticastTree(request)

    if response.success:
        return response.success, np.array(unflatten_matrix(response.multicast_tree.row, response.multicast_tree.size))
    else:
        return response.success


if __name__ == '__main__':
    size = 14

    # 14节点网络邻接矩阵
    adj_matrix = np.array([
        [0., 0., 1., 0., 1., 0., 0., 0., 0., 0., 1., 0., 0., 0.],
        [0., 0., 0., 1., 0., 0., 0., 1., 0., 0., 0., 1., 0., 0.],
        [1., 0., 0., 1., 1., 0., 0., 0., 0., 0., 0., 0., 1., 0.],
        [0., 1., 1., 0., 1., 0., 1., 0., 0., 0., 0., 0., 0., 1.],
        [1., 0., 1., 1., 0., 0., 1., 1., 0., 0., 0., 0., 0., 0.],
        [0., 0., 0., 0., 0., 0., 0., 1., 0., 1., 1., 0., 1., 0.],
        [0., 0., 0., 1., 1., 0., 0., 1., 1., 0., 0., 0., 0., 1.],
        [0., 1., 0., 0., 1., 1., 1., 0., 0., 1., 0., 1., 0., 1.],
        [0., 0., 0., 0., 0., 0., 1., 0., 0., 1., 1., 0., 1., 0.],
        [0., 0., 0., 0., 0., 1., 0., 1., 1., 0., 1., 1., 0., 1.],
        [1., 0., 0., 0., 0., 1., 0., 0., 1., 1., 0., 1., 0., 1.],
        [0., 1., 0., 0., 0., 0., 0., 1., 0., 1., 1., 0., 1., 1.],
        [0., 0., 1., 0., 0., 1., 0., 0., 1., 0., 0., 1., 0., 1.],
        [0., 0., 0., 1., 0., 0., 1., 1., 0., 1., 1., 1., 1., 0.]
    ], dtype=np.float32)

    # 生成随机时延矩阵 (对称矩阵，只看上三角)
    np.random.seed(42)
    latency_base = np.random.uniform(1, 100, (size, size))
    latency_matrix = (latency_base + latency_base.T) / 2 * adj_matrix

    # 生成随机带宽矩阵 (对称矩阵，只看上三角)
    bandwidth_base = np.random.uniform(10, 1000, (size, size))
    bandwidth_matrix = (bandwidth_base + bandwidth_base.T) / 2 * adj_matrix

    # 生成随机丢包矩阵 (对称矩阵，只看上三角)
    loss_base = np.random.uniform(0.001, 0.1, (size, size))
    loss_matrix = (loss_base + loss_base.T) / 2 * adj_matrix

    print(RL_multicast_tree(adj_matrix, latency_matrix, bandwidth_matrix, loss_matrix,
                           source_node=1, destination_group=[5, 6, 7, 8, 9, 10, 11, 12, 13]))