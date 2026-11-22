"""确定性时延分析（研究内容 2.4.1 / 测试用例 12）

简化版网络演算：
- 到达曲线：令牌桶 (r, b)
- 服务曲线：速率延迟 (R, T)
- 时延上界：b/R + T + 传播时延

这是算力网络 QoS 保障的理论基础：不只给统计平均值，而是给出
**数学可证的最坏情况时延上界**，用于关键业务的确定性服务保障。
"""

def compute_hop_delay_bound(arrival_rate_bps, burst_bytes, capacity_bps, prop_delay_ms):
    """单跳时延上界（ms）

    参数：
    - arrival_rate_bps: 到达速率（bit/s）
    - burst_bytes: 突发大小（字节）
    - capacity_bps: 链路容量（bit/s）
    - prop_delay_ms: 传播时延（ms）

    返回：时延上界（ms）

    公式：
    - 传播时延：固定，由物理距离决定
    - 排队时延上界：burst / capacity（最坏情况下整个突发在队列中）
    - 总时延 = 传播 + 排队
    """
    if capacity_bps <= 0:
        return float('inf')

    # 排队时延上界 = burst / capacity
    burst_bits = burst_bytes * 8
    queue_delay_s = burst_bits / capacity_bps

    # 总时延 = 传播 + 排队
    total_delay_ms = prop_delay_ms + queue_delay_s * 1000

    return total_delay_ms


def end_to_end_delay_bound(path, graph, traffic_spec):
    """端到端时延上界（ms）

    参数：
    - path: 路径（DPID 列表）
    - graph: NetworkX 图
    - traffic_spec: {'rate_mbps': 10, 'burst_kb': 100}

    返回：时延上界（ms）

    网络演算的串联定理：
    多跳链路的端到端时延 = 各跳时延之和（最坏情况）
    流整形效应：经过一跳后，突发会被限制在该跳容量以内
    """
    if not path or len(path) < 2:
        return 0.0

    arrival_rate_bps = traffic_spec['rate_mbps'] * 1e6
    burst_bytes = traffic_spec['burst_kb'] * 1024

    total_delay = 0.0

    for u, v in zip(path, path[1:]):
        if not graph.has_edge(u, v):
            return float('inf')

        edge = graph[u][v]
        capacity_mbps = edge.get('bw', 0) or 1000  # 默认 1Gbps
        capacity_bps = capacity_mbps * 1e6
        prop_delay_ms = edge.get('delay', 0)

        hop_delay = compute_hop_delay_bound(
            arrival_rate_bps, burst_bytes, capacity_bps, prop_delay_ms
        )

        total_delay += hop_delay

        # 流整形效应：后续跳的到达速率不超过前一跳容量
        # （保守估计，实际可能更优）
        arrival_rate_bps = min(arrival_rate_bps, capacity_bps)

    return total_delay


def verify_deterministic_sla(path, graph, sla_ms, traffic_spec):
    """验证路径的确定性 SLA

    参数：
    - path: 路径（DPID 列表）
    - graph: NetworkX 图
    - sla_ms: SLA 要求（ms）
    - traffic_spec: {'rate_mbps': 10, 'burst_kb': 100}

    返回：
    {
      'path': [...],
      'delay_bound_ms': 42.3,
      'sla_ms': 50,
      'satisfied': True,
      'margin_ms': 7.7,
      'hops': 2
    }

    用途：
    - 业务准入控制：新业务接入前验证网络是否能满足 SLA
    - 路径选择依据：在多条可达路径中选择满足 SLA 的那条
    - 性能证明：向用户提供数学保证（不是统计值）
    """
    delay_bound = end_to_end_delay_bound(path, graph, traffic_spec)

    return {
        'path': list(path),
        'delay_bound_ms': round(delay_bound, 2),
        'sla_ms': sla_ms,
        'satisfied': delay_bound <= sla_ms,
        'margin_ms': round(sla_ms - delay_bound, 2),
        'hops': len(path) - 1 if path else 0
    }


def verify_bandwidth_guarantee(path, graph, required_mbps):
    """验证路径的带宽保障能力

    参数：
    - path: 路径（DPID 列表）
    - graph: NetworkX 图
    - required_mbps: 需求带宽（Mbps）

    返回：
    {
      'satisfied': True/False,
      'bottleneck_capacity_mbps': 100,
      'bottleneck_available_mbps': 80,
      'bottleneck_link': ('dpid1', 'dpid2')
    }
    """
    if not path or len(path) < 2:
        return {
            'satisfied': False,
            'bottleneck_capacity_mbps': 0,
            'bottleneck_available_mbps': 0,
            'bottleneck_link': None
        }

    bottleneck_avail = float('inf')
    bottleneck_cap = float('inf')
    bottleneck_link = None

    for u, v in zip(path, path[1:]):
        if not graph.has_edge(u, v):
            return {
                'satisfied': False,
                'bottleneck_capacity_mbps': 0,
                'bottleneck_available_mbps': 0,
                'bottleneck_link': (u, v)
            }

        edge = graph[u][v]
        capacity_mbps = edge.get('bw', 0)
        used_mbps = edge.get('oneway_bps', 0)
        available_mbps = capacity_mbps - used_mbps

        if available_mbps < bottleneck_avail:
            bottleneck_avail = available_mbps
            bottleneck_cap = capacity_mbps
            bottleneck_link = (u, v)

    return {
        'satisfied': bottleneck_avail >= required_mbps,
        'bottleneck_capacity_mbps': round(bottleneck_cap, 2),
        'bottleneck_available_mbps': round(bottleneck_avail, 2),
        'bottleneck_link': bottleneck_link
    }
