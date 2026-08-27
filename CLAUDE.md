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

## 架构

```
QoSController.py       # RYU SDN 控制器（单文件）
  ├── QoSController      # 主应用类（OpenFlow 1.3）
  │   ├── MAC/IP 学习（packet-in 时学习源 MAC/IP）
  │   ├── packet-in 事件处理（二层转发+流表下发）
  │   ├── 拓扑发现（EventSwitchEnter, EventLinkAdd, EventHostAdd）
  │   ├── 最短路径计算（BFS/Dijkstra，详见 DSCP 路由策略）
  │   ├── ARP 代理处理
  │   └── 组播组管理（IGMP 加入/离开，UDP 组播流规则安装）
  ├── TopologyController # REST API 控制器（Web 可视化）
  └── QoSRuleController   # REST API 控制器（QoS 规则管理）

ofctl_rest.py           # RYU OFCTL REST API（支持 OF 1.0~1.5）
                        # 提供流表、端口、Meter、Group 等管理接口
                        # 使用方式：ryu-manager QoSController.py ofctl_rest.py ...

app/                    # RYU 应用示例集（含 ofctl/ 子模块）
                        # 主要用途：ofctl_rest.py 依赖 app/ofctl/ 提供底层 OpenFlow 操作

controllers/             # REST API 控制器（模块化设计）
  ├── TopologyController.py  # 拓扑 REST API
  ├── QoSRuleController.py    # QoS 规则管理
  ├── SliceController.py      # 网络切片配置与切换
  ├── RoutingController.py    # 路由策略管理
  ├── ActiveFlowsController.py # 活跃流查询
  ├── LinkEventController.py   # 链路事件监控
  ├── MulticastWeightsController.py  # 组播权重配置
  └── MulticastRerouteController.py   # 组播重路由

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

## DSCP 路由策略

| DSCP值 | 路由策略 | 说明 |
|--------|----------|------|
| 0/未设置 | 最短跳数 | hop count（默认） |
| 1 | 带宽优先 | max-bw，选取带宽最大的路径 |
| 2 | 丢包率优先 | min-loss，选取丢包率最低的路径 |
| 4 | 负载均衡 | min-load，选取剩余带宽最大的路径 |
| 8 | 延迟优先 | min-delay，选取总延迟最小的路径 |

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
- **gRPC 服务**：RL_multicast_tree 客户端连接 `192.168.10.21:50051`（可配置）构建组播树
- **LLDP 拓扑发现**：需要 `--observe-links` 参数启用
- **MAC 学习**：packet-in 时学习源 MAC，生成流表实现线速转发
- **Web 轮询**：UI 每 2 秒轮询 `/topology/data` 更新拓扑
- **Mininet**：必须运行在 Linux 并具有 sudo 权限