# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 提供在代码仓库中工作的指导。

## 项目概述

这是一个使用 Mininet 和 RYU 控制器的 SDN（软件定义网络）仿真项目。用于模拟运营商级网络（ChinaNet）并测试 QoS 控制器。

## 常用命令

### RYU 控制器
```bash
ryu-manager ryu_controller.py --wsapi-port 8080 --observe-links
```
REST API 访问地址：`http://localhost:8080`

### Mininet（需要 Linux + sudo）
```bash
# 内置拓扑
sudo mn --topo linear,2 --controller remote,ip=127.0.0.1,port=6653 --switch ovsk,protocols=OpenFlow13
sudo mn --topo minimal --controller remote,ip=127.0.0.1,port=6653 --switch ovsk,protocols=OpenFlow13

# 自定义拓扑
sudo python topo_generator.py dual
sudo python topo_generator.py chinanet
```

### REST API 端点（RYU）
| 端点 | 说明 |
|------|------|
| `GET /topology` | 完整拓扑（交换机、链路、主机） |
| `GET /switches` | 所有交换机 |
| `GET /links` | 所有链路 |
| `GET /flows/{dpid}` | 指定交换机的流表 |

## 架构

```
ryu_controller.py          # RYU SDN 控制器应用
  └── SDNController        # 主控制器类（OpenFlow 1.3）
  └── TopologyController   # 拓扑查询 REST API
  └── WSGI                  # 内置 Web 服务器（端口 8080）

topo_generator.py          # Mininet 自定义拓扑定义
  └── DualSwitchTopo       # 双交换机测试拓扑（2主机+2交换机）
  └── ChinaNetTopo         # 三层运营商网络（核心/汇聚/接入）

topology_web.html           # 浏览器端拓扑可视化工具
```

## 注意事项

- Mininet 必须运行在 Linux 上并具有 sudo 权限
- RYU 控制器独立运行，通过 OpenFlow 1.3 与交换机通信
- 控制器通过 packet-in 事件自动学习 MAC/IP 表
- 拓扑发现使用 LLDP（--observe-links 参数）
- Web UI 每 3 秒轮询一次 `/topology` 获取更新