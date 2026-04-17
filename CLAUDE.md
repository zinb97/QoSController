# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 提供在代码仓库中工作的指导。

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
sudo python netSim.py dual
sudo python netSim.py chinanet
sudo python netSim.py ring
```

### REST API 端点
| 端点 | 说明 |
|------|------|
| `GET /` | Web 可视化界面 |
| `GET /topology/data` | 拓扑 JSON 数据（交换机列表、链路列表） |

## 架构

```
ryu_controller.py           # RYU SDN 控制器
  └── SimpleSwitchTopo      # 主应用类（OpenFlow 1.3）
      - MAC 地址学习
      - packet-in 事件处理
      - 拓扑发现（EventSwitchEnter, EventLinkAdd）
  └── TopologyController    # REST API 控制器
      - GET /topology/data  → JSON 拓扑数据
      - GET /              → Web 可视化页面

topo_generator.py           # Mininet 拓扑生成器
  └── DualSwitchTopo        # 双交换机测试拓扑（2主机+2交换机）
  └── ChinaNetTopo          # 三层运营商网络（核心/汇聚/接入）
  └── RingTopo              # 环形网络（n个交换机+主机）

topology_web.html            # 浏览器端拓扑可视化（vis-network）
```

## 实现细节

- **OpenFlow 1.3**：RYU 与交换机通信使用 OF 1.3 协议
- **LLDP 拓扑发现**：需要 `--observe-links` 参数启用
- **MAC 学习**：packet-in 时学习源 MAC，生成流表实现线速转发
- **Web 轮询**：UI 每 2 秒轮询 `/topology/data` 更新拓扑
- **Mininet**：必须运行在 Linux 并具有 sudo 权限