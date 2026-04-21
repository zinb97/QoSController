import subprocess
import re

import json

# 预编译正则表达式（避免每次调用函数都重新编译）
_BW_PATTERN = re.compile(r'rate\s+(\d+)([KMG]?bit)')
_DELAY_PATTERN = re.compile(r'delay\s+(\d+)([mus]*)')
_LOSS_PATTERN = re.compile(r'loss\s+([\d.]+)%')

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


def get_tc_stats_optimized(dev):
    bandwidth, delay, loss = None, 0.0, 0.0
    try:
        class_output = subprocess.check_output(
            ["tc", "class", "show", "dev", dev],
            text=True,
            timeout=2  # ✅ 超时保护：2秒没返回就放弃，防止卡死
        )
        # 第二条 tc qdisc
        qdisc_output = subprocess.check_output(
            ["tc", "qdisc", "show", "dev", dev],
            text=True,
            timeout=2
        )
        # 合并输出
        output = class_output + "\n" + qdisc_output
        lines = output.splitlines()
        lines = output.splitlines()
    except subprocess.CalledProcessError:
        return None, 0.0, 0.0
    except Exception:
        print(f'{Exception}\nrun error {dev}')

    # 2. 单次遍历同时解析所有指标
    for line in lines:
        # 解析带宽（仅在未找到时继续匹配）
        if bandwidth is None:
            bw_match = _BW_PATTERN.search(line)
            if bw_match:
                val, unit = bw_match.groups()
                bandwidth = int(val) * _UNIT_MULTIPLIERS.get(unit, 0)

        # 解析延迟
        delay_match = _DELAY_PATTERN.search(line)
        if delay_match:
            val, unit = delay_match.groups()
            delay = float(val) * _DELAY_UNITS.get(unit, 1)

        # 解析丢包率
        loss_match = _LOSS_PATTERN.search(line)
        if loss_match:
            loss = float(loss_match.group(1))

    return bandwidth, delay, loss


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
    print(get_tc_stats_optimized('N1-eth1'))
    print(get_switch_name())