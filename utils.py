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


def _run_tc_once(argv, timeout=3):
    """执行一条 tc 命令 → (ok, 输出或错误)

    注意**不能只 catch subprocess.CalledProcessError**：RYU 启动时会
    `eventlet.monkey_patch()`，subprocess 被替换成 eventlet 的版本，
    抛出的异常 repr 虽然仍是 "CalledProcessError"，但**不是同一个类对象**，
    isinstance 判定为 False。只 catch 标准类会让失败落到兜底分支，
    拿不到 stderr（于是认不出 "Operation not permitted"、也不会重试 sudo）。
    因此这里统一按属性提取 output / returncode。
    """
    try:
        out = subprocess.check_output(argv, text=True, timeout=timeout,
                                      stderr=subprocess.STDOUT)
        return True, (out or '').strip()
    except Exception as e:                     # noqa: BLE001 - 见上：异常类不可靠
        if isinstance(e, FileNotFoundError):
            return False, 'tc not found'
        output = getattr(e, 'output', None)
        returncode = getattr(e, 'returncode', None)
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        text = output.strip() if isinstance(output, str) else ''
        if text:
            return False, text
        if returncode is None:
            return False, f'{type(e).__name__}: {e}'
        return False, f'exit {returncode}'


# 写 qdisc 需要 CAP_NET_ADMIN；读（tc qdisc show）不需要。
# 控制器通常以普通用户运行（Mininet 才是 root），所以写操作会拿到
# "Operation not permitted"。这里自动尝试 sudo -n（无密码 sudo）一次。
_PERM_HINTS = ('Operation not permitted', 'Permission denied', 'not permitted')


def _run_tc(argv, timeout=3):
    """执行 tc 命令，写操作遇权限不足时自动尝试 `sudo -n` → (ok, 输出/错误)。

    **成功时第二个返回值是命令输出**，调用方（如 `_netem_qdisc_args`）
    要靠它解析现有 qdisc 层级；失败时是错误信息。
    只返回 '' 会让解析方误判「没有 netem」，进而回退到替换 root——
    那会把 htb 带宽层级冲掉。
    """
    ok, out = _run_tc_once(argv, timeout)
    if ok:
        return True, out
    if any(hint in out for hint in _PERM_HINTS):
        ok2, out2 = _run_tc_once(['sudo', '-n'] + list(argv), timeout)
        if ok2:
            return True, out2
        return False, (f'{out2}；写 qdisc 需要 root 权限——'
                       f'用 sudo 启动 ryu-manager，或为 /usr/sbin/tc 配置 NOPASSWD（sudoers）')
    return False, out


# 已有 qdisc 的识别：TCLink 建出来的层级是
#   qdisc htb 5: root ...
#   qdisc netem 10: parent 5:1 limit 1000 delay 1ms
# 必须替换**netem 子队列**，用 `root` 替换会把 htb 一起冲掉，
# 链路的带宽限制随之消失（bw 指标会掉到 0）。
_NETEM_QDISC_RE = re.compile(r'qdisc netem ([0-9a-fA-F]+):\s+parent ([0-9a-fA-F:]+)')
_HTB_ROOT_RE = re.compile(r'qdisc htb ([0-9a-fA-F]+):\s+root')


def _netem_qdisc_args(dev):
    """定位应替换的 netem qdisc → tc 的 [parent P handle H] 参数

    - 已有 netem：替换它（保留父 htb）
    - 只有 htb 根：挂到默认类 `<handle>:1` 下（TCLink 的约定），不动根
    - 都没有：在 root 上新建一个句柄 10: 的 netem
    """
    ok, out = _run_tc(['tc', 'qdisc', 'show', 'dev', dev], timeout=2)
    if ok:
        match = _NETEM_QDISC_RE.search(out)
        if match:
            return ['parent', match.group(2), 'handle', match.group(1) + ':']
        match = _HTB_ROOT_RE.search(out)
        if match:
            return ['parent', match.group(1) + ':1', 'handle', '10:']
    return ['root', 'handle', '10:']


def find_injector_device(dev):
    """找到施加 netem 的目标设备，返回 tc 可用的设备名。

    Mininet 里交换机侧的 `<sw>-eth<N>` 是 OVS 端口，直接在它上面挂 netem
    往往失败（OVS veth 的主干被 bridge 接管）。真正的注入点是**该 OVS
    端口对应的 veth pair 对端**，即 `ip link` 里同名但挂在主机 netns 的
    那一半。这里不做猜测：依次尝试 `dev` 本身、`dev` 的对端（按常见
    Mininet 命名 `<sw>-eth<N>` → `<sw>-eth<N>` 已是最外层，对端在
    veth 的 iflink 上），任一个能被 `tc qdisc show` 读到就采用。

    返回 None 表示没有可用设备——调用方应把该链路记为「无法施加损伤」，
    而不是假装成功。
    """
    candidates = [dev]
    # veth 对端：读 /sys/class/net/<dev>/iflink 拿到对端 ifindex，
    # 再反查 /sys/class/net/*/ifindex 找到名字。读不到就算了。
    try:
        with open(f'/sys/class/net/{dev}/iflink') as f:
            peer_idx = f.read().strip()
        import os
        for name in os.listdir('/sys/class/net'):
            try:
                with open(f'/sys/class/net/{name}/ifindex') as f:
                    if f.read().strip() == peer_idx:
                        candidates.append(name)
                        break
            except OSError:
                continue
    except OSError:
        pass

    for cand in candidates:
        ok, _ = _run_tc(['tc', 'qdisc', 'show', 'dev', cand], timeout=2)
        if ok:
            return cand
    return None


def set_netem(dev, delay_ms=None, loss_pct=None, jitter_ms=None, limit=1000):
    """在 `dev` 上施加/更新 netem 时延与丢包 → (ok, 错误信息)。

    参数为 None 表示该项**不动**；两者都为 None 等价于清空损伤。
    delay 带 `jitter_ms` 时写成 `Nms Mms`（netem 原生语法）。

    **替换的是已有 netem 子队列，不是 root**：Mininet 链路上是
    `htb 5: root` + `netem 10: parent 5:1`，用 `root` 替换会把 htb
    带宽限制一起冲掉（bw 指标随即变 0）。定位逻辑见 `_netem_qdisc_args`。

    写 qdisc 需要 CAP_NET_ADMIN，普通用户运行控制器时会自动尝试
    `sudo -n`；仍失败则返回带操作指引的错误。
    """
    if delay_ms is None and loss_pct is None:
        return clear_netem(dev)

    args = (['tc', 'qdisc', 'replace', 'dev', dev]
            + _netem_qdisc_args(dev)
            + ['netem', 'limit', str(limit)])
    if delay_ms is not None:
        if jitter_ms:
            args += ['delay', f'{delay_ms}ms', f'{jitter_ms}ms']
        else:
            args += ['delay', f'{delay_ms}ms']
    if loss_pct is not None:
        args += ['loss', f'{loss_pct}%']
    return _run_tc(args)


def clear_netem(dev):
    """清空设备上的 netem 损伤（保留 htb 带宽层级）→ (ok, 错误信息)。

    做法是用一条不带 delay/loss 的 netem 覆盖原 netem 子队列，而不是
    `tc qdisc del root` —— 后者会把 TCLink 建的 htb 一起删掉。
    """
    args = (['tc', 'qdisc', 'replace', 'dev', dev]
            + _netem_qdisc_args(dev)
            + ['netem', 'limit', '1000'])
    return _run_tc(args)


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