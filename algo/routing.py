"""多目标联合路由（研究内容 2.4.2 / 测试用例 6.11）。

**为什么需要它**：控制器原来的 `get_shortest_path` 是单目标 Dijkstra——
一次只看一个权重（时延、或带宽、或丢包），`SmartAlgorithm` 分支甚至还是
`# todo`，直接退化成跳数最短。单目标的问题是「优化 A 就把 B 打坏」：
最小时延的路径可能绕远、丢包高；最大带宽的路径可能时延抖。算力路由要的
是**存力/算力/运力协调**，本质是多目标折中。

**做法**：把每条边的每个指标先归一化到「越大越差」的 0~1 量纲，再按权重
加权求和成单一边权，最后跑一次 Dijkstra。加权和而非 Pareto 前沿，是因为
Dijkstra 要求边权可加且非负；加权和满足这两点，代价是只能得到凸包上的解
——对「实时选路」这个场景够用，且**结果可解释**（能逐项报出这条路径在
每个指标上贡献了多少分，评审时问得出来）。

归一化的参照值（REF_*）是经验值，不是标准，写在常量里方便按需调。
"""
from __future__ import annotations

import math

# 各指标的归一化参照值：达到参照值记 1.0 分（满分惩罚）
REF_DELAY_MS = 50.0      # 50ms 时延记满分惩罚
REF_LOSS_PCT = 2.0       # 2% 丢包记满分惩罚
REF_FREE_BW_MBPS = 100.0  # 只剩 100Mbps 余量记满分惩罚
BW_PENALTY_CAP = 5.0     # 带宽惩罚上限，避免近零余量把整条路径权重拉到无穷

# 默认权重。时延权重最高（业务最敏感），带宽次之，算力余量权重最低但非零
# ——它是「算力路由」区别于纯网络路由的那一项，设成 0 就等于没做算力感知。
DEFAULT_WEIGHTS = {
    'delay': 0.35,
    'loss': 0.15,
    'bandwidth': 0.25,
    'load': 0.20,
    'compute': 0.05,
}

# 权重键的合法集合，供 REST 接口校验
WEIGHT_KEYS = tuple(DEFAULT_WEIGHTS.keys())


def sanitize_weights(weights=None):
    """把外部传入的权重整理成合法 dict → dict

    规则：未知键丢弃；非数值/负数/NaN 回退到默认值；总权重为 0 时
    回退整组默认值（全零权重会让所有路径代价相同，等于随机选路）。
    """
    out = dict(DEFAULT_WEIGHTS)
    if not isinstance(weights, dict):
        return out
    for key in WEIGHT_KEYS:
        if key not in weights:
            continue
        raw = weights[key]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            continue
        value = float(raw)
        if math.isnan(value) or math.isinf(value) or value < 0:
            continue
        out[key] = value
    if sum(out.values()) <= 0:
        return dict(DEFAULT_WEIGHTS)
    return out


def edge_metrics(attrs):
    """从图边属性提取五项原始指标 → dict

    单位口径与 QoSController._rebuild_graph 写入的完全一致：
    - delay        ms（netem 配置值；被动采集拿不到 RTT）
    - loss         %（**实测**，tc -s 计数差分；缺失时退回配置值）
    - bw           Mbps（htb rate，额定带宽）
    - used         Mbps（**实测**单向吞吐 oneway_bps，src→dst）
    - capacity     bit/s（OVS link_speed，仅作旁证，不参与打分）
    """
    bw = attrs.get('bw') or 0.0
    used = attrs.get('oneway_bps')
    if used is None:
        used = attrs.get('total_bps') or 0.0
    loss = attrs.get('loss_measured')
    if loss is None:
        loss = attrs.get('loss') or 0.0
    return {
        'delay': max(0.0, float(attrs.get('delay') or 0.0)),
        'loss': max(0.0, float(loss or 0.0)),
        'bw': max(0.0, float(bw or 0.0)),
        'used': max(0.0, float(used or 0.0)),
    }


def edge_penalties(attrs):
    """五项指标归一化成 0~1（带宽项可超 1）的惩罚分 → dict

    「惩罚」= 越大越差，与权重加权和的方向一致。额定带宽未知（bw=0）
    时带宽与负载两项都记 0 —— 宁可当它空闲，也不要凭猜测把一条可能是
    唯一连通的路径判死。
    """
    m = edge_metrics(attrs)
    bw, used = m['bw'], m['used']
    if bw > 0:
        free = max(bw - used, 0.0)
        bw_penalty = min(REF_FREE_BW_MBPS / max(free, 1.0), BW_PENALTY_CAP)
        load_penalty = min(used / bw, 1.0)
    else:
        bw_penalty = 0.0
        load_penalty = 0.0
    return {
        'delay': min(m['delay'] / REF_DELAY_MS, 1.0),
        'loss': min(m['loss'] / REF_LOSS_PCT, 1.0),
        'bandwidth': bw_penalty,
        'load': load_penalty,
    }


def edge_cost(attrs, weights=None, node_penalty=0.0):
    """单条有向边的综合代价 → float

    node_penalty 是**进入该边终点**的算力代价（0~1，1=算力耗尽）。
    把它加在边上而不是节点上，是因为 Dijkstra 只认边权；等价于「进入一个
    繁忙算力节点要多付一段代价」，这正是算力感知路由最简的落法。
    """
    w = sanitize_weights(weights)
    p = edge_penalties(attrs)
    cost = sum(w[k] * p[k] for k in ('delay', 'loss', 'bandwidth', 'load'))
    cost += w['compute'] * max(0.0, min(float(node_penalty or 0.0), 1.0))
    # Dijkstra 要求非负；全零权重的链路也要能走，给一个极小正数
    return max(cost, 1e-9)


def _node_penalty(node_penalty, node):
    if not node_penalty:
        return 0.0
    return node_penalty.get(node, 0.0) or 0.0


def best_path(graph, src, dst, weights=None, node_penalty=None):
    """多目标加权最短路 → (path, cost)

    path 为空列表表示不可达。cost 是该路径所有边代价之和，可用于
    「新旧路径谁更优」的比较（负载均衡的决策依据）。
    """
    import networkx as nx

    if src == dst:
        return [src], 0.0
    if src not in graph or dst not in graph:
        return [], float('inf')

    def _weight(u, v, attrs):
        return edge_cost(attrs, weights, _node_penalty(node_penalty, v))

    try:
        path = nx.dijkstra_path(graph, src, dst, weight=_weight)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return [], float('inf')
    return path, path_cost(graph, path, weights, node_penalty)


def path_cost(graph, path, weights=None, node_penalty=None):
    """按同一套代价函数计算一条已知路径的总代价 → float"""
    if not path or len(path) < 2:
        return 0.0 if path else float('inf')
    total = 0.0
    for u, v in zip(path, path[1:]):
        if not graph.has_edge(u, v):
            return float('inf')
        total += edge_cost(graph[u][v], weights,
                           _node_penalty(node_penalty, v))
    return total


def explain_path(graph, path, weights=None, node_penalty=None):
    """逐项拆解一条路径的代价构成 → dict

    给「可解释」用：返回每个指标贡献的总分与占比，以及每跳明细。
    评审问「为什么选这条不选那条」时，这张表就是答案。
    """
    w = sanitize_weights(weights)
    if not path or len(path) < 2:
        return {'path': list(path), 'total': float('inf'), 'breakdown': {}, 'hops': []}

    breakdown = {k: 0.0 for k in WEIGHT_KEYS}
    hops = []
    for u, v in zip(path, path[1:]):
        if not graph.has_edge(u, v):
            continue
        attrs = graph[u][v]
        p = edge_penalties(attrs)
        p['compute'] = max(0.0, min(float(_node_penalty(node_penalty, v)), 1.0))
        hop_cost = 0.0
        for k in WEIGHT_KEYS:
            contribution = w[k] * p[k]
            breakdown[k] += contribution
            hop_cost += contribution
        hops.append({'from': u, 'to': v, 'cost': round(hop_cost, 6),
                     'penalties': {k: round(p[k], 4) for k in WEIGHT_KEYS}})

    total = sum(breakdown.values())
    # 占比：看是哪一项主导了这次选路
    shares = ({k: (v / total if total > 0 else 0.0) for k, v in breakdown.items()})
    return {
        'path': list(path),
        'total': round(total, 6),
        'breakdown': {k: round(v, 6) for k, v in breakdown.items()},
        'shares': {k: round(v, 4) for k, v in shares.items()},
        'hops': hops,
    }
