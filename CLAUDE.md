    # CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

使用 Mininet 和 RYU 控制器的 SDN（软件定义网络）仿真项目。模拟运营商级网络（ChinaNet）并测试 QoS 控制器。

## 常用命令

### RYU 控制器
```bash
ryu-manager QoSController.py --wsapi-port 8080 --observe-links
```
REST API 地址：`http://localhost:8080`

### Mininet（需要 Linux + sudo）
```bash
# 内置拓扑
sudo mn --topo linear,2 --controller remote,ip=127.0.0.1,port=6653 --switch ovsk,protocols=OpenFlow13

# 自定义拓扑
sudo python netSim.py -t dual
sudo python netSim.py -t chinanet
sudo python netSim.py -t ring
```

### REST API 端点
| 端点 | 说明 |
|------|------|
| `GET /` | Web 可视化界面 |
| `GET /topology/data` | 拓扑 JSON 数据（交换机列表、链路列表、主机列表） |

## 架构

```
QoSController.py            # RYU SDN 控制器（单文件）
  ├── SimpleSwitchTopo        # 主应用类（OpenFlow 1.3）
  │   ├── MAC 地址学习（packet-in 时学习源 MAC/IP）
  │   ├── packet-in 事件处理（二层转发+流表下发）
  │   └── 拓扑发现（EventSwitchEnter, EventLinkAdd, EventHostAdd）
  └── TopologyController      # REST API 控制器
      ├── GET /topology/data  → JSON 拓扑数据
      └── GET /               → Web 可视化页面

netSim.py                    # Mininet 拓扑生成器
  ├── DualSwitchTopo          # 双交换机测试拓扑（2主机+2交换机）
  ├── RingTopo                # 环形网络（n个交换机+主机）
  └── ChinaBackboneTopo      # 骨干网拓扑（分层级：核心/汇聚/接入/省网/城域）

topology_web.html            # 浏览器端拓扑可视化（vis-network，每 2 秒轮询，按层级显示不同颜色）
```

## DPID 规范（ChinaBackboneTopo）

DPID 为 16 进制 8 字节，格式：`00000000{L1}{L2}{ID}`
- L1: 层级标识（1=核心, 2=汇聚, 3=骨干接入, 4=省网核心, 5=省网接入, 6=城域接入）
- L2+ID: 区域/设备编号

## ChinaBackboneTopo 仿真层级

| level | 说明 |
|-------|------|
| 1 | 仅核心层（北京、上海、广州全互联） |
| 2 | 核心 + 汇聚层（西安、成都、武汉、南京、沈阳） |
| 3 | + 骨干接入层（默认 6 节点，可通过 -f 全量 23 节点） |
| 4 | + 省网核心层 |
| 5 | + 省网接入 + 城域核心层 |

启动示例：`sudo python netSim.py net -l 3 -f`

### 省网树状拓扑参数
省网结构：省网核心上联骨干接入层，省网接入层为树根直接挂载主机。

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--prov-depth` | 省网树深度（省网核心到主机层数） | 2 |
| `--prov-width` | 省网接入层节点数 | 2 |

示例：`sudo python netSim.py net -l 4 --prov-depth 2 --prov-width 3`

## 实现细节

- **OpenFlow 1.3**：RYU 与交换机通信使用 OF 1.3 协议
- **LLDP 拓扑发现**：需要 `--observe-links` 参数启用
- **MAC 学习**：packet-in 时学习源 MAC，生成流表实现线速转发
- **Web 轮询**：UI 每 2 秒轮询 `/topology/data` 更新拓扑
- **Mininet**：必须运行在 Linux 并具有 sudo 权限