import os
import re
from datetime import datetime
from collections import defaultdict

import matplotlib.pyplot as plt


plt.rcParams['font.sans-serif'] = ['Noto Sans CJK JP', 'Noto Sans CJK TC', 'Noto Sans CJK SC']
plt.rcParams['axes.unicode_minus'] = False

# ===================== 配置 =====================
TEST_PATH = '/home/hello/jcyz_ceshi/QoSController/testTool'
LOG_PATTERN = "/home/hello/jcyz_ceshi/QoSController/mcast_recv_h*.log"
OUTPUT_CSV = "mgen_mcast_result.csv"
SUMMARY_FILE = "mgen_mcast_summary.txt"
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

def summary(flow_summary):
    print("=" * 60 + "\n")
    print("            组播MGEN 测试结果汇总\n")
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
        print()

def write_detail_csv(flow_summary, sec_stats):
    """绘制每秒详细折线图"""
    flows = sorted(sec_stats.keys())
    if not flows:
        print("[!] 无数据可绘图")
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
    ax_bw.set_title('Multicast MGEN Test - BW/Delay/Loss')
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
    plt.savefig(OUTPUT_CSV.replace('.csv', '.png'), dpi=150)
    print(f"[+] Chart saved: {OUTPUT_CSV.replace('.csv', '.png')}")

def find_log_files(path, pattern):
    """查找匹配模式的日志文件"""
    import glob
    search_pattern = os.path.join(path, pattern)
    return sorted(glob.glob(search_pattern))

def main():
    print("[+] 查找日志文件...")
    log_files = find_log_files(TEST_PATH, LOG_PATTERN)

    if not log_files:
        print(f"[!] 未找到匹配文件: {LOG_PATTERN}")
        return

    print(f"[+] 找到 {len(log_files)} 个日志文件")

    # 合并所有日志文件的统计
    combined_flow_summary = defaultdict(lambda: {
        "start": None,
        "end": None,
        "total_bytes": 0,
        "recv_seqs": set(),
        "min_seq": None,
        "max_seq": None,
        "delays": [],
    })
    combined_sec_stats = defaultdict(lambda: defaultdict(lambda: {
        "bytes": 0,
        "delays": [],
        "seqs": set(),
    }))

    for log_file in log_files:
        print(f"[+] 解析日志: {log_file}")
        flow_summary, sec_stats = parse_mcast_log(log_file)

        for flow, s in flow_summary.items():
            combined = combined_flow_summary[flow]
            combined["total_bytes"] += s["total_bytes"]
            combined["recv_seqs"].update(s["recv_seqs"])
            combined["delays"].extend(s["delays"])

            if combined["min_seq"] is None or s["min_seq"] < combined["min_seq"]:
                combined["min_seq"] = s["min_seq"]
            if combined["max_seq"] is None or s["max_seq"] > combined["max_seq"]:
                combined["max_seq"] = s["max_seq"]
            if combined["start"] is None or (s["start"] is not None and s["start"] < combined["start"]):
                combined["start"] = s["start"]
            if combined["end"] is None or (s["end"] is not None and s["end"] > combined["end"]):
                combined["end"] = s["end"]

        for flow, slots in sec_stats.items():
            for slot, st in slots.items():
                sec_slot = combined_sec_stats[flow][slot]
                sec_slot["bytes"] += st["bytes"]
                sec_slot["delays"].extend(st["delays"])
                sec_slot["seqs"].update(st["seqs"])

    print("[+] 生成汇总报告...")
    summary(combined_flow_summary)
    print("[+] 生成每秒图表...")
    write_detail_csv(combined_flow_summary, combined_sec_stats)
    print("[+] 完成!")

if __name__ == "__main__":
    main()
    plt.show()