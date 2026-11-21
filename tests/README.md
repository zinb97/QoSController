# 测试指南（本次改造：DSCP 打标 / SLA 保障 / 联合路由 / 反馈式负载均衡）

分三层。**前两层现在就能跑**，第三层需要 sudo 起 Mininet。

## 第 1 层：离线回归（不用 sudo，秒级）

```bash
cd QoSController && ./tests/run_all.sh        # 或 PY=python3.8 ./tests/run_all.sh
```

包含：
- `tests/test_algo.py`（11 个）：多目标选路、权重清洗、可解释拆解、预测器边界
- `tests/test_controller_offline.py`（18 个）：DSCP 打标位置、meter 指令顺序与速率、
  SLA 剪枝与退回、删除覆盖两类表项、负载均衡去抖/冷却/增益门槛

单跑某一组：

```bash
/usr/bin/python3.8 -m unittest tests.test_controller_offline.MeterTests -v
```

> 为什么用 `/usr/bin/python3.8`：控制器实际跑在 3.8（ryu/eventlet 在 3.10 装不起来），
> 用系统默认 3.10 验证会和线上不一致。

## 第 2 层：对着跑着的控制器做只读检查

```bash
curl -s localhost:8080/routing/weights | python3 -m json.tool
curl -s localhost:8080/metrics | grep -E 'qos_rule_sla|qos_routing|qos_lb_'
```

`/routing/weights` 里 `"available": false` 说明这个进程还是**改动前的旧代码**（或
`algo` 包没找到），必须重启：

```bash
# 找出并重启（PID 换成本机实际值）
kill <ryu PID>
cd QoSController && nohup /usr/bin/python3.8 /usr/local/bin/ryu-manager \
  QoSController.py ofctl_rest.py --wsapi-port 8080 --observe-links > /tmp/ryu.log 2>&1 &
sleep 3 && curl -s localhost:8080/routing/weights | python3 -m json.tool
# 期望 "available": true，defaults 里五项权重齐全
```

## 第 3 层：端到端（Mininet，需 sudo）

### 3.1 起拓扑

```bash
# 终端 A：控制器（同上）
# 终端 B：
cd netSim && sudo python3 cmnet.py --scale small --prov-depth 1 --prov-width 1
```

`small` 只有 5 个省，起得快；`--prov-depth 1` 减少城域节点，方便定位主机对。

### 3.2 验证 DSCP 打标真的生效

```bash
# 建一条带标记的规则（把 IP 换成 mininet 里 h1/h2 的地址，mn 里 `h1 ip addr` 可查）
curl -X POST localhost:8080/qos/rules -H 'Content-Type: application/json' -d '{
  "name":"t-dscp","src_ip":"10.0.1.1","dst_ip":"10.0.2.1",
  "protocol":"tcp","dst_port":5201,"dscp":46,"target_bps":50
}'

# 看首跳流表：应当出现 actions=set_field:0x2e->ip_dscp（46 的十六进制是 2e）
curl -s localhost:8080/stats/flow/<首跳 dpid> | python3 -m json.tool | grep -A12 ip_dscp
# 中间跳：match 里带 ip_dscp，actions 里**没有** set_field
```

判据：源侧交换机有 `set_field`，其余交换机没有；`n_packets` 随流量增长。
改造前这里永远是 0，因为规则只匹配「已带标记」的包。

### 3.3 验证 meter 限速

```bash
# 规则已带 target_bps=50
curl -s localhost:8080/stats/meter/<dpid> | python3 -m json.tool
# 期望 meter_id=规则id，bands 里 rate=50000(kbps)

# h1 打 100M 的 UDP 流，目标限 50M
mininet> h1 iperf3 -s -p 5201 &
mininet> h2 iperf3 -c 10.0.1.1 -u -b 100M -t 20
# 期望接收端吞吐被压在 ~50Mbps，meter 的 flow_count / packet_in_count 增长
```

### 3.4 验证 SLA 选路与净空

**字段语义（务必分清）**：

| 字段 | 作用 | 会不会改链路 |
|---|---|---|
| `target_delay_ms` / `target_loss_pct` | 选路约束：跳过已违反该值的链路 | **不会** |
| `inject_delay_ms` / `inject_loss_pct` | 损伤注入：把指定损伤加到本规则路径首跳 | 会 |
| `target_bps` | 带宽约束 + 首跳 meter 限速 | 不追加损伤 |

```bash
# ① 制造劣化场景：手工给某条链路加 80ms（等 ≥6s 让图刷新）
sudo tc qdisc replace dev S2-eth1 root netem delay 80ms
sleep 7 && curl -s localhost:8080/metrics | grep 'qos_link_delay_ms{dst_dpid="S1",src_dpid="S2"}'

# ② 带约束建规则：路径应绕开该链路
curl -X POST localhost:8080/qos/rules -H 'Content-Type: application/json' -d '{
  "name":"t34-sla","src_ip":"10.1.3.1","dst_ip":"10.0.1.101","target_delay_ms":20
}' | python3 -m json.tool | grep -E 'path_names|sla_fallback'

# ③ 或者用规则自带的注入能力造场景（无需 sudo）
curl -X POST localhost:8080/qos/rules -H 'Content-Type: application/json' -d '{
  "name":"t34-inject","src_ip":"10.1.3.1","dst_ip":"10.0.1.101","inject_delay_ms":80
}'
# 日志应出现 [Inject] netem applied on M311-eth1: delay=80.0ms
# tc qdisc show dev M311-eth1 可见 80ms

# ④ 约束不可满足 → 退回尽力而为，响应带 sla_fallback
curl -X POST localhost:8080/qos/rules -H 'Content-Type: application/json' -d '{
  "name":"t34-fallback","src_ip":"10.1.3.1","dst_ip":"10.0.1.101","target_delay_ms":1
}' | python3 -m json.tool | grep sla_fallback
```

> 约束是**逐跳**判断，不是端到端求和：路径总时延 31ms 仍可通过 `target_delay_ms=20`，
> 只要每一跳都不超过 20ms。

### 3.5 验证算力感知路由

```bash
curl -X PUT localhost:8080/routing/compute -H 'Content-Type: application/json' \
  -d '{"compute_load":{"<dpid>":1.0}}'
# 再建 smart_algorithm 规则，路径应绕开该节点
curl -X POST localhost:8080/qos/rules -H 'Content-Type: application/json' -d '{
  "name":"t-compute","src_ip":"10.0.1.1","dst_ip":"10.0.2.1","guarantee":"smart_algorithm"
}'
```

### 3.6 验证反馈式负载均衡

```bash
# 建一条 load_balance 规则，然后用 iperf 把当前路径打到 80% 以上
# 观察控制器日志：
#   [LoadBalance] ...: reroute (predicted util 0.87 at swX->swY, cost a -> b)
# 以及指标：
curl -s localhost:8080/metrics | grep -E 'qos_lb_(evaluated|reroutes|skipped)_total'
# 期望：evaluated 持续增长，reroutes 只在越阈值时增加（这就是「降低反馈开销」的证据）
```

### 3.7 阈值可调（不改代码，便于复现）

```bash
LB_HIGH_WATERMARK=0.6 LB_HOT_CYCLES=2 LB_COOLDOWN_S=5 \
  ryu-manager QoSController.py ofctl_rest.py --wsapi-port 8080 --observe-links
```

## 常见误判

| 现象 | 原因 |
|---|---|
| `/routing/weights` 里 available=false | 进程是旧代码，或 `algo/` 没随部署上去 |
| 流表里有 set_field 但 n_packets 不涨 | 规则五元组和实际流量对不上（端口/协议写错） |
| 改了 `LB_*` 环境变量没反应 | 控制器只在启动时读取，需重启 |
| meter 建了但吞吐没被压 | 交换机不支持 meter 或 rule 未命中（同样看 n_packets） |
