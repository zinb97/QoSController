import subprocess
import re

import json
from enum import Enum
from typing import NamedTuple


# 预编译正则表达式（避免每次调用函数都重新编译）
_BW_PATTERN = re.compile(r'rate\s+(\d+)([KMG]?bit)')
_DELAY_PATTERN = re.compile(r'delay\s+([\d.]+)([mus]*)')
_LOSS_PATTERN = re.compile(r'loss\s+([\d.]+)%')
# `tc -s` 的计数行：Sent 42767574 bytes 709574 pkt (dropped 2145, overlimits 0 requeues 0)
_SENT_PATTERN = re.compile(r'Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt\s*\(dropped\s+(\d+)')
_SENT_PATTERN_NO_DROP = re.compile(r'Sent\s+(\d+)\s+bytes\s+(\d+)\s+pkt')

# 单位映射表（复用常量，避免重复创建）
_UNIT_MULTIPLIERS = {
    'bit': 1e-6,
    'Kbit': 1e-3,
    'Mbit': 1,
    'Gbit': 1e3,
}
_DELAY_UNITS = {
    'us': 1e-3,  # 微秒 -> 毫秒
    'ms': 1,
    's': 1e3,  # 秒 -> 毫秒
    '': 1  # 默认单位为毫秒
}


class WeightPref(Enum):
    Default = 0
    Latency = 1
    LoadBalance = 2
    LossRate = 3
    SmartAlgorithm = 4
    Bandwidth = 5


class LinkTcStats(NamedTuple):
    """单个网络接口的一次 tc 采样结果。

    前三项是**配置值**（htb/netem 的配置参数），后三项是**实测计数**
    （`tc -s` 读出的 qdisc 累计计数，是差分算实测丢包率的唯一数据源）。

    - bw_mbps: htb class 的 rate（配置额定带宽）
    - delay_ms: netem 的 delay（配置时延）
    - loss_configured_pct: netem 的 loss（配置丢包率）
    - bytes_sent / packets_sent: qdisc 累计**成功转发**的字节/包数
    - packets_dropped: qdisc 累计**丢弃**的包数

    实测丢包率 = Δdropped / (Δsent + Δdropped)：
    内核在 qdisc 丢包时只累加 drops，**不计入 Sent**，所以分母要加上 dropped。
    """
    bw_mbps: float = 0.0
    delay_ms: float = 0.0
    loss_configured_pct: float = 0.0
    bytes_sent: int = 0
    packets_sent: int = 0
    packets_dropped: int = 0


def _parse_qdisc_blocks(text):
    """把 `tc -s qdisc show` 输出拆成 qdisc 块。

    每块 = {'params': qdisc 头行文本, 'bytes':…, 'packets':…, 'dropped':…}。
    头行可能自带参数（qdisc netem 10: parent 5:1 limit 1000 delay 22ms loss 0.3%），
    累计计数在随后的 "Sent N bytes M pkt (dropped K, …)" 行里。
    """
    blocks = []
    cur = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith('qdisc '):
            if cur is not None:
                blocks.append(cur)
            cur = {'params': line, 'bytes': 0, 'packets': 0, 'dropped': 0}
            continue
        if cur is None:
            continue
        m = _SENT_PATTERN.search(line)
        if m:
            cur['bytes'] = int(m.group(1))
            cur['packets'] = int(m.group(2))
            cur['dropped'] = int(m.group(3))
            continue
        m = _SENT_PATTERN_NO_DROP.search(line)
        if m:
            cur['bytes'] = int(m.group(1))
            cur['packets'] = int(m.group(2))
    if cur is not None:
        blocks.append(cur)
    return blocks


def get_tc_stats_optimized(dev):
    """采样单个接口的 tc 状态 → LinkTcStats。

    一次 `tc class show`（取 htb 的 rate）+ 一次 `tc -s qdisc show`
    （取 netem 的 delay/loss 配置值 **以及** qdisc 的实测计数）。

    为什么必须带 `-s`：`tc qdisc show` 只打印配置（delay/loss），
    `tc -s qdisc show` 才带 "Sent N bytes M pkt (dropped K, …)" 实测计数。
    实测丢包率要靠这两个累计计数做差分才能得到。

    选块规则：优先取带 delay/loss 参数的那个块（netem，即链路上真正施加
    损伤的 qdisc）；没有 netem 时退回根 qdisc（htb）的计数。
    """
    bandwidth = 0.0
    try:
        class_output = subprocess.check_output(
            ["tc", "class", "show", "dev", dev],
            text=True,
            timeout=2  # ✅ 超时保护：2秒没返回就放弃，防止卡死
        )
        # 第二条 tc qdisc：带 -s 才有实测计数
        qdisc_output = subprocess.check_output(
            ["tc", "-s", "qdisc", "show", "dev", dev],
            text=True,
            timeout=2
        )
    except subprocess.CalledProcessError:
        return LinkTcStats()
    except Exception:
        return LinkTcStats()

    # 带宽：htb class 的 rate（取第一个匹配）
    bw_match = _BW_PATTERN.search(class_output)
    if bw_match:
        val, unit = bw_match.groups()
        bandwidth = int(val) * _UNIT_MULTIPLIERS.get(unit, 0)

    blocks = _parse_qdisc_blocks(qdisc_output)
    impaired = None
    for b in blocks:
        if 'delay' in b['params'] or 'loss' in b['params']:
            impaired = b
    target = impaired or (blocks[0] if blocks else None)
    if target is None:
        return LinkTcStats(bw_mbps=bandwidth)

    delay = 0.0
    loss = 0.0
    delay_match = _DELAY_PATTERN.search(target['params'])
    if delay_match:
        val, unit = delay_match.groups()
        delay = float(val) * _DELAY_UNITS.get(unit, 1)
    loss_match = _LOSS_PATTERN.search(target['params'])
    if loss_match:
        loss = float(loss_match.group(1))

    return LinkTcStats(
        bw_mbps=bandwidth,
        delay_ms=delay,
        loss_configured_pct=loss,
        bytes_sent=target['bytes'],
        packets_sent=target['packets'],
        packets_dropped=target['dropped'],
    )


def get_ovs_interface_speeds():
    """OVS 接口额定速率 → {接口名: bit/s}。

    OVS 的 Interface 表有 `link_speed` 列（网卡驱动上报的协商速率），
    这就是链路的**额定速率**——比「全网都按 1000 Mbps 算」的假设准得多。
    Mininet 拓扑里交换机侧的接口是 veth，`link_speed` 报 10 Gbps。

    一次 `ovs-vsctl --columns=name,link_speed --format=json list Interface`
    取全部接口。桥本身的 internal 端口没有 link_speed（空集合），跳过。

    读不到（无权限/OVS 没起）返回空 dict——调用方按「未知」处理，绝不编造。
    """
    try:
        output = subprocess.check_output(
            ['sudo', '-n', 'ovs-vsctl', '--columns=name,link_speed',
             '--format=json', 'list', 'Interface'],
            text=True,
            timeout=5,
        )
    except Exception:
        return {}

    try:
        data = json.loads(output)
    except ValueError:
        return {}

    headings = data.get('headings') or []
    if 'name' not in headings or 'link_speed' not in headings:
        return {}
    i_name = headings.index('name')
    i_speed = headings.index('link_speed')

    out = {}
    for row in data.get('data') or []:
        if len(row) <= max(i_name, i_speed):
            continue
        speed = row[i_speed]
        # internal 端口是 ["set", []]（空集合），不是速率
        if isinstance(speed, bool) or not isinstance(speed, int) or speed <= 0:
            continue
        out[str(row[i_name])] = speed
    return out


def get_switch_name():
    """
    从 OVS 获取交换机的 datapath_id <-> 交换机名(dp-desc) 映射
    返回：{datapath_id: dp_desc}
    """
    switch_map = {}

    try:
        output = subprocess.check_output(
            ["sudo", "ovs-vsctl", "--format=json", "list", "bridge"],
            universal_newlines=True,
            stderr=subprocess.DEVNULL
        )
        ovs_data = json.loads(output)

        headings = ovs_data["headings"]
        other_config_idx = headings.index("other_config")

        for bridge in ovs_data["data"]:
            other_config = bridge[other_config_idx]

            if not (isinstance(other_config, list) and
                    len(other_config) == 2 and
                    other_config[0] == "map"):
                continue

            config_dict = dict(other_config[1])

            dpid = config_dict.get("datapath-id")
            name = config_dict.get("dp-desc")

            if dpid and name:
                switch_map[dpid] = name

    except Exception as e:
        print(f"[错误] 获取交换机信息失败: {str(e)}")

    return switch_map


if __name__ == '__main__':
    print(get_tc_stats_optimized('C1-eth1'))
    print(get_switch_name())