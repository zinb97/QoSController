import os
import re
from datetime import datetime
from collections import defaultdict
import glob

import matplotlib.pyplot as plt


plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'Noto Sans CJK TC', 'Noto Sans CJK SC']
plt.rcParams['axes.unicode_minus'] = False

# ===================== 配置 =====================
TEST_PATH = '/home/hello/jcyz_ceshi/QoSController/testTool'
LOG_PATTERN = "/home/hello/jcyz_ceshi/QoSController/mcast_recv_h*.log"
# =================================================

def parse_time(time_str):
    """解析时间字符串 13:29:21.066745 为秒级时间戳"""
    dt = datetime.strptime(time_str, "%H:%M:%S.%f")
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6

def parse_mcast_log(log_path):
    """解析组播接收日志，返回全局统计 + 每秒统计"""
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

    # 每秒统计
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

            rx_time_str = match.group(1)
            flow = int(match.group(2))
            seq = int(match.group(3))
            tx_time_str = match.group(4)
            size = int(match.group(5))

            rx_sec = parse_time(rx_time_str)
            tx_sec = parse_time(tx_time_str)
            delay_ms = (rx_sec - tx_sec) * 1000

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

            slot = int(tx_sec)
            sec_slot = sec_stats[flow][slot]
            sec_slot["bytes"] += size
            sec_slot["delays"].append(delay_ms)
            sec_slot["seqs"].add(seq)

    return flow_summary, sec_stats

def compute_loss(summary):
    """计算丢包数和丢包率"""
    min_seq = summary["min_seq"]
    max_seq = summary["max_seq"]
    recv = len(summary["recv_seqs"])
    expected = max_seq - min_seq + 1 if (min_seq is not None and max_seq is not None) else 0
    lost = expected - recv
    loss_rate = (lost / expected) * 100 if expected > 0 else 0.0
    return recv, expected, lost, loss_rate

def print_summary(flow_summary):
    print("=" * 60 + "\n")
    print("            Multicast MGEN Test Summary\n")
    print("=" * 60 + "\n\n")
    for flow in sorted(flow_summary):
        s = flow_summary[flow]
        duration = s["end"] - s["start"] if s["end"] and s["start"] else 1
        recv, expected, lost, loss_rate = compute_loss(s)
        bw_mbps = (s["total_bytes"] * 8) / duration / 1e6 if duration > 0 else 0
        delays = s["delays"]
        avg_d = sum(delays)/len(delays) if delays else 0
        min_d = min(delays) if delays else 0
        max_d = max(delays) if delays else 0

        print(f"[Flow {flow}]")
        print(f"  Duration     : {duration:.2f} s")
        print(f"  Expected     : {expected}")
        print(f"  Received     : {recv}")
        print(f"  Lost         : {lost}")
        print(f"  Loss Rate    : {loss_rate:.2f} %")
        print(f"  Avg Bandwidth: {bw_mbps:.3f} Mbps")
        print(f"  Avg Delay    : {avg_d:.2f} ms")
        print(f"  Min Delay    : {min_d:.2f} ms")
        print(f"  Max Delay    : {max_d:.2f} ms")
        print()

def plot_stats(flow_summary, sec_stats, output_prefix):
    """绘制每秒详细折线图"""
    flows = sorted(sec_stats.keys())
    if not flows:
        print("[!] No data to plot")
        return

    min_start = min(
        sorted(sec_stats[flow])[0]
        for flow in flows
    )

    time_data = {}
    bw_data = {}
    delay_data = {}
    loss_data = {}

    for flow in flows:
        s_sum = flow_summary[flow]
        _, expected_total, _, _ = compute_loss(s_sum)

        times, bws, delays, losses = [], [], [], []
        for slot in sorted(sec_stats[flow]):
            st = sec_stats[flow][slot]
            pkt_bytes = st["bytes"]
            dlys = st["delays"]
            recv = len(dlys)

            expected = expected_total // max(1, len(sec_stats[flow]))
            lost = max(0, expected - recv)
            loss_r = (lost / expected) * 100 if expected > 0 else 0

            times.append(slot - min_start)
            bws.append(pkt_bytes * 8 / 1e6)
            delays.append(sum(dlys) / len(dlys) if dlys else 0)
            losses.append(loss_r)

        time_data[flow] = times
        bw_data[flow] = bws
        delay_data[flow] = delays
        loss_data[flow] = losses

    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

    ax_bw = axes[0]
    for flow in flows:
        ax_bw.plot(time_data[flow], bw_data[flow], marker='.', label=f'Flow {flow}', linewidth=1.5)
    ax_bw.set_ylabel('Bandwidth (Mbps)')
    ax_bw.set_title(f'Multicast MGEN Test - {output_prefix}')
    ax_bw.legend(loc='upper right')
    ax_bw.grid(True, alpha=0.3)

    ax_delay = axes[1]
    for flow in flows:
        ax_delay.plot(time_data[flow], delay_data[flow], marker='.', label=f'Flow {flow}', linewidth=1.5)
    ax_delay.set_ylabel('Delay (ms)')
    ax_delay.legend(loc='upper right')
    ax_delay.grid(True, alpha=0.3)

    ax_loss = axes[2]
    for flow in flows:
        ax_loss.plot(time_data[flow], loss_data[flow], marker='.', label=f'Flow {flow}', linewidth=1.5)
    ax_loss.set_ylabel('Loss Rate (%)')
    ax_loss.set_xlabel('Time (sec)')
    ax_loss.legend(loc='upper right')
    ax_loss.grid(True, alpha=0.3)

    plt.tight_layout()
    output_png = f"{output_prefix}.png"
    plt.savefig(output_png, dpi=150)
    print(f"[+] Chart saved: {output_png}")
    plt.close()

def process_single_log(log_file):
    """处理单个日志文件"""
    print(f"\n[+] Processing: {log_file}")

    flow_summary, sec_stats = parse_mcast_log(log_file)

    if not flow_summary:
        print(f"[!] No data found in {log_file}")
        return

    # 提取文件名作为前缀
    basename = os.path.basename(log_file)
    prefix = basename.replace('.log', '')

    print_summary(flow_summary)
    plot_stats(flow_summary, sec_stats, prefix)

def main():
    print("[+] Searching log files...")
    log_files = sorted(glob.glob(LOG_PATTERN))

    if not log_files:
        print(f"[!] No files match: {LOG_PATTERN}")
        return

    print(f"[+] Found {len(log_files)} log files")

    # 收集所有日志数据
    all_data = []  # [(label, flow_summary, sec_stats), ...]

    for log_file in log_files:
        print(f"[+] Parsing: {log_file}")
        flow_summary, sec_stats = parse_mcast_log(log_file)
        if flow_summary:
            label = os.path.basename(log_file).replace('.log', '')
            all_data.append((label, flow_summary, sec_stats))

    if not all_data:
        print("[!] No data found")
        return

    # 绘制所有数据在一张图上
    fig, axes = plt.subplots(3, 1, figsize=(14, 12), sharex=True)

    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']

    for idx, (label, flow_summary, sec_stats) in enumerate(all_data):
        print(f"\n[{label}]")
        print_summary(flow_summary)

        flows = sorted(sec_stats.keys())
        min_start = min(sorted(sec_stats[flow])[0] for flow in flows)

        s_sum = flow_summary[flows[0]]
        _, expected_total, _, _ = compute_loss(s_sum)

        times, bws, delays, losses = [], [], [], []
        for slot in sorted(sec_stats[flows[0]]):
            st = sec_stats[flows[0]][slot]
            pkt_bytes = st["bytes"]
            dlys = st["delays"]
            recv = len(dlys)

            expected = expected_total // max(1, len(sec_stats[flows[0]]))
            lost = max(0, expected - recv)
            loss_r = (lost / expected) * 100 if expected > 0 else 0

            times.append(slot - min_start)
            bws.append(pkt_bytes * 8 / 1e6)
            delays.append(sum(dlys) / len(dlys) if dlys else 0)
            losses.append(loss_r)

        color = colors[idx % len(colors)]

        axes[0].plot(times, bws, marker='.', label=label, linewidth=1.5, color=color)
        axes[1].plot(times, delays, marker='.', label=label, linewidth=1.5, color=color)
        axes[2].plot(times, losses, marker='.', label=label, linewidth=1.5, color=color)

    axes[0].set_ylabel('Bandwidth (Mbps)')
    axes[0].set_title('Multicast MGEN Test - BW/Delay/Loss')
    axes[0].legend(loc='upper right')
    axes[0].grid(True, alpha=0.3)

    axes[1].set_ylabel('Delay (ms)')
    axes[1].legend(loc='upper right')
    axes[1].grid(True, alpha=0.3)

    axes[2].set_ylabel('Loss Rate (%)')
    axes[2].set_xlabel('Time (sec)')
    axes[2].legend(loc='upper right')
    axes[2].grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig('mgen_mcast_result.png', dpi=150)
    print(f"\n[+] Chart saved: mgen_mcast_result.png")
    plt.close()
    print("\n[+] All done!")

if __name__ == "__main__":
    main()