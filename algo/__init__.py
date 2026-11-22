"""算力网络联合优化算法包。

与 QoSController 解耦的纯函数模块，**不 import ryu / networkx 之外的东西**，
便于单独测试。QoSController 通过 sys.path 注入仓库根目录后导入。

- `routing`：多目标联合路由（时延 / 带宽 / 丢包 / 负载 / 算力余量）
- `predict`：链路负载的短时预测（EWMA + 线性趋势），供反馈式负载均衡使用
"""
