import os.path
import re
from datetime import datetime
from collections import defaultdict

import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei', 'SimHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

# ===================== 配置 =====================
TEST_PATH = '/home/u/test'
LOG_FILE = "multi_recv.log"
OUTPUT_CSV = "mgen_result.csv"
SUMMARY_FILE = "mgen_summary.txt"

flow_name = {
    1: '时延优先流',
    2: '丢包优先流',
    3: '带宽优先流',
    4: '普通业务流',
}
# =================================================

def parse_time(time_str):
    """解析时间字符串 13:29:21.066745 为秒级时间戳"""
    dt = datetime.strptime(time_str, "%H:%M:%S.%f")
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6


def parse_time_window(time_window_str):
    """解析时间窗口字符串 13:29:21 为秒级时间戳 (用于相对时间计算)"""
    dt = datetime.strptime(time_window_str, "%H:%M:%S")
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def parse_mgen_log(log_path):
    """解析MGEN接收日志，返回全局统计 + 每秒统计"""
    pattern = re.compile(
        r"(\d+\:\d+\:\d+\.\d+) RECV proto\>UDP flow\>(\d+) seq\>(\d+) .* sent\>(\d+\:\d+\:\d+\.\d+) size\>(\d+)"
    )

    # 全局统计
    flow_summary = defaultdict(lambda: {
        "start": None,
        "end": None,
        "total_bytes": 0,
        "recv_seqs": set(),
        "min_seq": None,
        "max_seq": None,
        "delays": [],
    })

    # 每秒统计 (Key 改为 HH:MM:SS 字符串格式，与第二个程序一致)
    sec_stats = defaultdict(lambda: defaultdict(lambda: {
        "bytes": 0,
        "delays": [],
        "seqs": set(),
    }))

    with open(log_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if "RECV proto>" not in line:
                continue

            match = pattern.search(line)
            if not match:
                continue

            # 提取字段
            rx_time_str = match.group(1)
            flow = int(match.group(2))
            seq = int(match.group(3))
            tx_time_str = match.group(4)
            size = int(match.group(5))

            # 时间转秒 (用于全局统计和时延计算)
            rx_sec = parse_time(rx_time_str)
            tx_sec = parse_time(tx_time_str)
            delay_ms = (rx_sec - tx_sec) * 1000

            # 提取时间窗口 (HH:MM:SS，基于发送时间，与第二个程序一致)
            sent_time_window = tx_time_str.split(".")[0]

            # 更新全局统计
            s = flow_summary[flow]
            s["total_bytes"] += size
            s["recv_seqs"].add(seq)
            s["delays"].append(delay_ms)

            if s["min_seq"] is None or seq < s["min_seq"]:
                s["min_seq"] = seq
            if s["max_seq"] is None or seq > s["max_seq"]:
                s["max_seq"] = seq
            if s["start"] is None or tx_sec < s["start"]:
                s["start"] = tx_sec
            if s["end"] is None or tx_sec > s["end"]:
                s["end"] = tx_sec

            # 更新每秒统计 (Key 为 HH:MM:SS)
            sec_slot = sec_stats[flow][sent_time_window]
            sec_slot["bytes"] += size
            sec_slot["delays"].append(delay_ms)
            sec_slot["seqs"].add(seq)

    return flow_summary, sec_stats


def compute_loss(summary):
    """计算丢包数和丢包率 (全局统计方法保持不变)"""
    min_seq = summary["min_seq"]
    max_seq = summary["max_seq"]
    recv = len(summary["recv_seqs"])
    expected = max_seq - min_seq + 1 if (min_seq is not None and max_seq is not None) else 0
    lost = expected - recv
    loss_rate = (lost / expected) * 100 if expected > 0 else 0.0
    return recv, expected, lost, loss_rate


def summary(flow_summary):
    print("=" * 60 + "\n")
    print("            MGEN 测试结果汇总\n")
    print("=" * 60 + "\n\n")
    for flow in sorted(flow_summary):
        s = flow_summary[flow]
        duration = s["end"] - s["start"] if s["end"] and s["start"] else 1
        recv, expected, lost, loss_rate = compute_loss(s)
        bw_mbps = (s["total_bytes"] * 8) / duration / 1e6 if duration > 0 else 0
        delays = s["delays"]
        avg_d = sum(delays) / len(delays) if delays else 0
        min_d = min(delays) if delays else 0
        max_d = max(delays) if delays else 0

        print(f"【流 {flow}】")
        print(f"  测试时长    : {duration:.2f} s")
        print(f"  期望收包    : {expected}")
        print(f"  实际收包    : {recv}")
        print(f"  丢包数      : {lost}")
        print(f"  丢包率      : {loss_rate:.2f} %")
        print(f"  平均带宽    : {bw_mbps:.3f} Mbps")
        print(f"  平均时延    : {avg_d:.2f} ms")
        print(f"  最小时延    : {min_d:.2f} ms")
        print(f"  最大时延    : {max_d:.2f} ms")


def write_detail_csv(flow_summary, sec_stats):
    """绘制每秒详细折线图"""
    flows = sorted(sec_stats.keys())
    if not flows:
        print("[!] 无数据可绘图")
        return

    # 找所有流中最小的起始时间 (用于计算相对时间 X 轴)
    all_time_windows = set()
    for flow in flows:
        all_time_windows.update(sec_stats[flow].keys())

    if not all_time_windows:
        print("[!] 无时间窗口数据可绘图")
        return

    min_start_sec = min(parse_time_window(tw) for tw in all_time_windows)

    # 准备数据
    time_data = {}  # flow -> [相对时间(秒)]
    bw_data = {}  # flow -> [带宽(Mbps)]
    delay_data = {}  # flow -> [平均时延(ms)]
    loss_data = {}  # flow -> [丢包率(%)]

    for flow in flows:
        s_sum = flow_summary[flow]

        # 使用第二个程序的丢包率统计方法：基于时间窗口内的 seq 范围计算
        times, bws, delays, losses = [], [], [], []

        # 按时间顺序遍历 (HH:MM:SS 字符串字典序即时间序)
        for time_window in sorted(sec_stats[flow]):
            st = sec_stats[flow][time_window]
            pkt_bytes = st["bytes"]
            dlys = st["delays"]
            seq_set = st["seqs"]
            recv_count = len(seq_set)

            # --- 这里完全复用第二个程序的逻辑 ---
            if recv_count > 0:
                min_seq = min(seq_set)
                max_seq = max(seq_set)
                expected = max_seq - min_seq + 1
                lost = max(0, expected - recv_count)
                loss_r = (lost / expected) * 100 if expected > 0 else 0
            else:
                loss_r = 0
            # ----------------------------------

            # 计算相对时间
            current_sec = parse_time_window(time_window)
            times.append(current_sec - min_start_sec)

            bws.append(pkt_bytes * 8 / 1e6)
            delays.append(sum(dlys) / len(dlys) if dlys else 0)
            losses.append(loss_r)

        time_data[flow] = times
        bw_data[flow] = bws
        delay_data[flow] = delays
        loss_data[flow] = losses

    # 计算每条流的统计信息用于图例标签
    flow_stats = {}
    for flow in flows:
        s = flow_summary[flow]
        duration = s["end"] - s["start"] if s["end"] and s["start"] else 1
        recv, expected, lost, loss_rate = compute_loss(s)
        bw_mbps = (s["total_bytes"] * 8) / duration / 1e6 if duration > 0 else 0
        delays = s["delays"]
        avg_delay = sum(delays) / len(delays) if delays else 0
        avg_loss = loss_rate
        flow_stats[flow] = {
            'bw': bw_mbps,
            'delay': avg_delay,
            'loss': avg_loss
        }

    # 创建子图：带宽、时延、丢包率
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

    # 带宽子图
    ax_bw = axes[0]
    for flow in flows:
        stat = flow_stats[flow]
        label = f'{flow_name[flow]} (Bandwidth: {stat["bw"]:.2f} Mbps)'
        ax_bw.plot(time_data[flow], bw_data[flow], marker='.', label=label, linewidth=1.5)
    ax_bw.set_ylabel('带宽 (Mbps)')
    ax_bw.set_title('MGEN 测试 - 带宽/时延/丢包率')
    ax_bw.legend(loc='upper right')
    ax_bw.grid(True, alpha=0.3)

    # 时延子图
    ax_delay = axes[1]
    for flow in flows:
        stat = flow_stats[flow]
        label = f'{flow_name[flow]} (Delay: {stat["delay"]:.2f} ms)'
        ax_delay.plot(time_data[flow], delay_data[flow], marker='.', label=label, linewidth=1.5)
    ax_delay.set_ylabel('时延 (ms)')
    ax_delay.legend(loc='upper right')
    ax_delay.grid(True, alpha=0.3)

    # 丢包率子图（正常坐标轴）
    ax_loss = axes[2]
    for flow in flows:
        ax_loss.plot(time_data[flow], loss_data[flow], marker='.', label=f'{flow_name[flow]} (Loss: {flow_stats[flow]["loss"]:.3f}%)', linewidth=1.5)
    ax_loss.set_ylabel('丢包率 (%)')
    ax_loss.set_xlabel('时间 (秒)')
    ax_loss.legend(loc='upper right')
    ax_loss.grid(True, alpha=0.3)

    plt.tight_layout()


def main():
    print("[+] 解析日志...")
    flow_summary, sec_stats = parse_mgen_log(os.path.join(TEST_PATH, LOG_FILE))
    print("[+] 生成汇总报告...")
    summary(flow_summary)
    print("[+] 生成每秒图表...")
    write_detail_csv(flow_summary, sec_stats)
    print("[✔] 完成！")
    plt.show()


if __name__ == "__main__":
    main()