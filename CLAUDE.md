# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

使用 Mininet 和 RYU 控制器的 SDN（软件定义网络）仿真项目。模拟运营商级网络（ChinaNet）并测试 QoS 控制器。

## 常用命令

### RYU 控制器
```bash
# 单独运行 QoS 控制器
ryu-manager QoSController.py --wsapi-port 8080 --observe-links

# 同时运行 QoS 控制器 + OFCTL REST API（流表/端口管理）
ryu-manager QoSController.py ofctl_rest.py --wsapi-port 8080 --observe-links
```
REST API 地址：`http://localhost:8080`

### Mininet（需要 Linux + sudo）
```bash
# 内置拓扑
sudo mn --topo linear,2 --controller remote,ip=127.0.0.1,port=6653 --switch ovsk,protocols=OpenFlow13

# 自定义拓扑
sudo python netSim.py -t dual    # 双交换机测试拓扑
sudo python netSim.py -t ring    # 环形网络拓扑（默认6节点，可通过参数调整）
sudo python netSim.py -t net     # 骨干网拓扑（默认层级3）
sudo python netSim.py -t mcast   # 组播测试拓扑（层级化: 2-3-3-6）
sudo python netSim.py net -l 3 -f # 层级3 + 全量23省接入节点

# 组播专用拓扑（层级树状结构）
sudo python netSim_mcast.py          # 默认层级化拓扑（2-3-3-6）
sudo python netSim_mcast.py -l 4     # 自定义层级深度
sudo python netSim_mcast.py -w 3     # 自定义每层节点宽度
```

### REST API 端点

**QoSController (端口 8080):**
| 端点 | 说明 |
|------|------|
| `GET /` | Web 可视化界面 |
| `GET /topology/data` | 拓扑 JSON 数据（交换机列表、链路列表、主机列表） |
| `GET /flows/active` | 活跃流列表 |
| `GET /qos/rules` | QoS 规则列表 |
| `POST /qos/rules` | 创建 QoS 规则 |
| `DELETE /qos/rules/{rule_id}` | 删除 QoS 规则 |

**OFCTL REST API (需要同时加载 ofctl_rest.py):**
| 端点 | 说明 |
|------|------|
| `GET /stats/switches` | 所有交换机 DPID 列表 |
| `GET /stats/flow/{dpid}` | 交换机流表条目 |
| `GET /stats/port/{dpid}` | 交换机端口统计 |
| `POST /stats/flowentry/add` | 添加流表条目 |
| `DELETE /stats/flowentry/clear/{dpid}` | 清空流表 |

**Slice REST API (网络切片):**
| 端点 | 说明 |
|------|------|
| `GET /topology/slice` | 获取当前生效的切片类型 |
| `GET /topology/slice/config` | 获取切片配置 |
| `PUT /topology/slice/config` | 保存切片配置（节点、阈值） |
| `DELETE /topology/slice/config` | 清空切片配置和邻接矩阵 |
| `POST /topology/slice/basic` | 切换到基础切片（全局拓扑） |
| `POST /topology/slice/low-latency` | 切换到低时延切片 |
| `POST /topology/slice/low-loss` | 切换到低丢包切片 |
| `POST /topology/slice/low-latency-loss` | 切换到低时延-丢包切片 |

**Multicast REST API (组播):**
| 端点 | 说明 |
|------|------|
| `GET /multicast/active_flows` | 所有组播活跃流（含源 DPID、组成员、路径、route_matrix） |
| `GET /multicast/weights` | 所有组播组的权重配置（latency/bandwidth/loss） |
| `GET /multicast/weights/{addr}` | 指定组播组的权重（缺省返回全 1.0 默认值） |
| `PUT/POST /multicast/weights/{addr}` | 设置权重；值变更且已安装流规则时自动重装 |
| `DELETE /multicast/weights/{addr}` | 删除权重配置，恢复默认 |

**Routing REST API (路由算法):**
| 端点 | 说明 |
|------|------|
| `GET /routing/algorithm` | 获取当前组播路由算法（`dijkstra` 或 `smart`） |
| `PUT/POST /routing/algorithm` | 设置算法；变更后自动重装所有已安装的组播流 |

**Toggle REST API (运行时开关):**
| 端点 | 说明 |
|------|------|
| `GET /link_event` | 获取链路事件处理开关状态 |
| `POST /link_event` | 设置链路事件开关（body: `{"enabled": true/false}`） |
| `GET /multicast_reroute` | 获取组播组重路由开关状态 |
| `POST /multicast_reroute` | 设置组播重路由开关 |

**QoS 规则单条操作：**
| 端点 | 说明 |
|------|------|
| `GET /qos/rules/{rule_id}` | 获取单条 QoS 规则详情 |
| `PUT/POST /qos/rules/{rule_id}` | 更新单条规则（src_port/dst_port/enabled/priority 等） |

**静态文件：**
| 端点 | 说明 |
|------|------|
| `GET /` | 主页（`static/index.html`） |
| `GET /static/{filepath:.*}` | 静态资源（含路径遍历防护） |

**Prometheus Metrics:**
| 端点 | 说明 |
|------|------|
| `GET /metrics` | Prometheus 指标采集 |

可用指标：
- `qos_switches_count` - 交换机数量
- `qos_links_count` - 链路数量
- `qos_hosts_count` - 主机数量
- `qos_switch_packet_in_total` - 交换机 packet-in 事件总数
- `qos_switch_flows_installed` - 交换机已安装流表数
- `qos_link_bandwidth_usage_bits` - 链路带宽使用率(bits/s)
- `qos_link_delay_ms` - 链路延迟(ms)
- `qos_link_loss_percent` - 链路丢包率(%)
- `qos_rules_total` / `qos_rules_active` - QoS 规则总数/活跃数
- `qos_multicast_groups` / `qos_multicast_flows` - 组播组/流数量
- `qos_controller_uptime_seconds` - 控制器运行时间

## 架构

```
QoSController.py       # RYU SDN 控制器主类（单文件，~2340 行）
  └── QoSController      # 主应用类（OpenFlow 1.3）
      ├── MAC/IP 学习（packet-in 时学习源 MAC/IP）
      ├── packet-in 事件处理（二层转发+流表下发）
      ├── 拓扑发现（EventSwitchEnter, EventLinkAdd, EventHostAdd）
      ├── 最短路径计算（get_shortest_path，详见 guarantee 路由策略）
      ├── ARP 代理处理
      ├── 组播组管理（IGMP 加入/离开，UDP 组播流规则安装）
      └── 后台 hub 线程（每 3s 拓扑刷新 / 每 2s 端口统计 / 组播负载 / 链路负载 / 组播重路由）

controllers/             # REST API 控制器（模块化设计，独立 WSGI ControllerBase）
  ├── StaticController.py      # 主页 + /static/* 静态文件服务（含路径遍历防护）
  │                             # 注意：文件名虽叫 StaticController，但内部类名也叫 TopologyController（命名冲突）
  ├── TopologyController.py    # GET /topology/data（拓扑 JSON）
  ├── MetricsController.py     # GET /metrics（Prometheus 导出）
  ├── LinkEventController.py   # GET/POST /link_event（链路事件处理开关）
  ├── MulticastRerouteController.py # GET/POST /multicast_reroute（组播重路由开关）
  ├── MulticastWeightsController.py # GET/PUT/DELETE /multicast/weights[/{addr}]（组播权重配置）
  ├── ActiveFlowsController.py # GET /flows/active + GET /multicast/active_flows
  ├── RoutingController.py     # GET/PUT /routing/algorithm（dijkstra/smart）
  ├── QoSRuleController.py     # /qos/rules CRUD
  └── SliceController.py       # /topology/slice* 配置与切换

ofctl_rest.py           # RYU OFCTL REST API（支持 OF 1.0~1.5）
                        # 提供流表、端口、Meter、Group 等管理接口
                        # 使用方式：ryu-manager QoSController.py ofctl_rest.py ...

app/                    # RYU 应用示例集（含 ofctl/ 子模块）
                        # 主要用途：ofctl_rest.py 依赖 app/ofctl/ 提供底层 OpenFlow 操作

netSim.py               # Mininet 拓扑生成器（单播/组播基础拓扑）
netSim_mcast.py         # Mininet 组播拓扑生成器（层级化树状结构）

client.py               # 强化学习组播树构建客户端（调用 gRPC 服务构建组播树）

static/                 # Web UI 资源（vis-network，每 2 秒轮询）
  ├── index.html        # 主页面
  ├── style.css
  ├── vis-network.min.js
  └── js/
      ├── app.js        # 主应用逻辑
      ├── topology.js   # 拓扑渲染
      ├── slice.js      # 网络切片配置
      ├── modals.js     # 模态框组件
      ├── sidebar.js    # 侧边栏
      └── toggles.js    # 开关组件

utils.py                # tc 统计解析工具、OVS 交换机名称映射、DSCP WeightPref 枚举

testTool/               # 测试工具集
  ├── send_igmp.py      # IGMPv2 测试工具（发送 Join/Leave/Query 报文）
  ├── ditg_analyzer.py   # D-ITG 流量分析工具
  ├── mgen_analyzer.py  # MGEN 流量分析工具
  └── minibursty.py     # 突发流量测试工具

send_igmp.py            # IGMPv2 测试工具（项目根目录）

multicast_pb2.py        # gRPC protobuf stub
multicast_pb2_grpc.py  # gRPC RPC 存根
```

## 路由策略（QoS 规则 `guarantee` 字段）

QoS 规则创建时通过 `guarantee` 字段指定路由策略，映射到 `utils.WeightPref` 枚举：

| guarantee 值 | WeightPref | 策略 |
|--------------|------------|------|
| `default` | Default | hop count（默认，最短跳数） |
| `bandwidth` | Bandwidth | max-bw，选取带宽最大的路径 |
| `latency` | Latency | min-delay，选取总延迟最小的路径 |
| `loss_rate` | LossRate | min-loss，选取丢包率最低的路径 |
| `load_balance` | LoadBalance | min-load，选取剩余带宽最大的路径（后台每 2s 检测链路负载并自动重路由） |
| `smart_algorithm` | SmartAlgorithm | 调用 gRPC `RL_multicast_tree` 服务计算智能路径 |

> 注：原始文档中的 DSCP 值（0/1/2/4/8）已被 `guarantee` 字段取代。`dscp` 字段仍作为 OpenFlow 匹配条件之一保留（0-63），但不再决定路由选择。

**组播路由算法**（独立配置，通过 `/routing/algorithm` 切换）：
- `smart`（默认）：调用 gRPC 服务构建组播树
- `dijkstra`：本地 Dijkstra 算法

## DPID 规范（ChinaBackboneTopo）

DPID 为 16 进制 8 字节，格式：`00000000{L1}{L2:02x}0001`
- L1: 层级标识（1=核心, 2=汇聚, 3=骨干接入, 4+=省网/城域）
- L2: 区域/设备编号
- 末尾 0001: 固定标识

## ChinaBackboneTopo 仿真层级

| level | 说明 |
|-------|------|
| 1 | 仅核心层（北京、上海、广州全互联） |
| 2 | 核心 + 汇聚层（西安、成都、武汉、南京、沈阳） |
| 3 | + 骨干接入层（默认 6 节点，可通过 -f 全量 23 节点） |
| 4 | + 省网核心层 |
| 5 | + 省网接入 + 城域核心层 |

### 省网树状拓扑参数
省网结构：省网核心上联骨干接入层，省网接入层为树根直接挂载主机。

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--prov-depth` | 省网树深度（省网核心到主机层数） | 2 |
| `--prov-width` | 省网接入层节点数 | 2 |

示例：`sudo python netSim.py net -l 4 --prov-depth 2 --prov-width 3`

## 实现细节

- **OpenFlow 1.3**：RYU 与交换机通信使用 OF 1.3 协议
- **gRPC 组播树服务**：`client.RL_multicast_tree()` 连接外部服务（默认 `192.168.10.22:50051`，可在 `client.py:11` 改 `server_address`）。智能路由（`smart` 算法 + `smart_algorithm` guarantee）依赖此服务；**服务不可达将导致组播流安装失败**。
- **LLDP 拓扑发现**：需要 `--observe-links` 参数启用
- **MAC 学习**：packet-in 时学习源 MAC，生成流表实现线速转发
- **Web 轮询**：UI 每 2 秒轮询 `/topology/data` 更新拓扑
- **Mininet**：必须运行在 Linux 并具有 sudo 权限
- **后台线程**：`QoSController.__init__` 通过 `hub.spawn` 启动 5 个常驻循环：
  - `_topology_loop` (3s)：刷新 `topology_data` 并更新 Prometheus 指标
  - `_stats_loop` (2s)：请求端口/流统计；触发 `load_balance` 类型规则的自动重路由
  - `_multicast_load_loop`：组播组负载统计与高负载标记
  - `_link_load_thread`：链路负载与高负载计数
  - `_reroute_multicast_loop`：根据链路抖动自动重路由组播流（受 `multicast_reroute_enabled` 开关控制）
- **链路抖动去抖**：`_link_delete_delay = 0.01s` 延迟后删除链路，避免 LLDP 抖动导致频繁重路由
- **静默 WSGI 日志**：`hub.WSGIServer.serve_forever` 被 monkey-patch 为静默输出，避免 HTTP 访问日志淹没 topology 线程日志
- **类名陷阱**：`controllers/StaticController.py` 内定义的类实际叫 `TopologyController`（与 `controllers/TopologyController.py` 中的同名类冲突）。修改此类时需谨慎区分文件与类名。

### metrics可用指标:

  ┌───────────────────────────────┬─────────┬────────────────────────────────────┐
  │            指标名             │  类型   │                说明                │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_switches_count            │ Gauge   │ 交换机数量                         │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_links_count               │ Gauge   │ 链路数量                           │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_hosts_count               │ Gauge   │ 主机数量                           │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_switch_packet_in_total    │ Counter │ packet-in 事件总数（按 dpid 标签） │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_switch_flows_installed    │ Gauge   │ 已安装流表数（按 dpid 标签）       │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_link_bandwidth_usage_bits │ Gauge   │ 链路带宽使用率 bits/s              │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_link_delay_ms             │ Gauge   │ 链路延迟 ms                        │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_link_loss_percent         │ Gauge   │ 链路丢包率 %                       │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_network_total_bandwidth_bits │ Gauge │ 网络实时总带宽 bits/s              │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_rules_total               │ Gauge   │ QoS 规则总数                       │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_rules_active              │ Gauge   │ 活跃 QoS 规则数                    │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_multicast_groups          │ Gauge   │ 组播组数量                         │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_multicast_flows           │ Gauge   │ 组播流数量                         │
  ├───────────────────────────────┼─────────┼────────────────────────────────────┤
  │ qos_controller_uptime_seconds │ Gauge   │ 控制器运行时间                     │
  └───────────────────────────────┴─────────┴────────────────────────────────────┘
