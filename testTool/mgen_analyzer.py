import os.path
import re
import csv
from datetime import datetime
from collections import defaultdict

# ===================== 配置 =====================
TEST_PATH = '/home/u/test'
LOG_FILE = "recv.log"
OUTPUT_CSV = "mgen_result.csv"
SUMMARY_FILE = "mgen_summary.txt"
# =================================================

def parse_time(time_str):
    """解析时间字符串 13:29:21.066745 为秒级时间戳"""
    dt = datetime.strptime(time_str, "%H:%M:%S.%f")
    return dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1e6

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

            # 提取字段
            rx_time_str = match.group(1)
            flow = int(match.group(2))
            seq = int(match.group(3))
            tx_time_str = match.group(4)
            size = int(match.group(5))

            # 时间转秒
            rx_sec = parse_time(rx_time_str)
            tx_sec = parse_time(tx_time_str)
            delay_ms = (rx_sec - tx_sec) * 1000

            # 更新全局
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

            # 更新每秒
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

def write_summary(flow_summary):
    print("=" * 60 + "\n")
    print("            MGEN 测试结果汇总\n")
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

def write_detail_csv(flow_summary, sec_stats):
    """输出每秒详细CSV"""
    with open(OUTPUT_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow([
            "流ID", "时间(秒)", "带宽(Mbps)", "平均时延(ms)",
            "最小时延(ms)", "最大时延(ms)", "收包数", "丢包数", "丢包率(%)"
        ])

        for flow in sorted(sec_stats):
            s_sum = flow_summary[flow]
            _, expected_total, _, _ = compute_loss(s_sum)

            for slot in sorted(sec_stats[flow]):
                st = sec_stats[flow][slot]
                bytes = st["bytes"]
                delays = st["delays"]
                recv = len(delays)

                # 秒级丢包（近似）
                expected = expected_total // max(1, len(sec_stats[flow]))
                lost = max(0, expected - recv)
                loss_r = (lost/expected)*100 if expected>0 else 0

                mbps = (bytes * 8) / 1e6
                avg_d = sum(delays)/len(delays) if delays else 0
                min_d = min(delays) if delays else 0
                max_d = max(delays) if delays else 0

                w.writerow([
                    flow, slot, round(mbps,3), round(avg_d,2),
                    round(min_d,2), round(max_d,2), recv, lost, round(loss_r,2)
                ])

def main():
    print("[+] 解析日志...")
    flow_summary, sec_stats = parse_mgen_log(os.path.join(TEST_PATH, LOG_FILE))
    print("[+] 生成汇总报告...")
    write_summary(flow_summary)
    print("[+] 生成每秒CSV...")
    write_detail_csv(flow_summary, sec_stats)
    print("[✔] 完成！文件：mgen_result.csv + mgen_summary.txt")

if __name__ == "__main__":
    main()