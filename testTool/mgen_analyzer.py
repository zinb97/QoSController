import os.path
import re
import argparse
from datetime import datetime
from collections import defaultdict

import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei', 'SimHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

# ===================== 配置 =====================
LOG_PATH = '/home/u/test/multi_recv.log'

flow_name = {
    1: '普通业务流',
    2: '时延优先流',
    3: '丢包优先流',
    4: '负载优先流',
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


def write_detail_csv(flow_summary, sec_stats, plot_type='all'):
    """绘制每秒详细折线图

    Args:
        flow_summary: 全局流统计
        sec_stats: 每秒统计
        plot_type: 绘制类型，可选 'bw' (带宽), 'delay' (时延), 'loss' (丢包), 'all' (全部), 或 ['bw', 'delay'] 等列表组合
    """
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

    # 确定要绘制的子图
    plot_list = []
    if isinstance(plot_type, list):
        # 接受列表类型，如 ['bw', 'delay']
        plot_list = [p for p in plot_type if p in ('bw', 'delay', 'loss')]
        if not plot_list:
            plot_list = ['bw', 'delay', 'loss']
    elif plot_type == 'all':
        plot_list = ['bw', 'delay', 'loss']
    elif plot_type == 'bw':
        plot_list = ['bw']
    elif plot_type == 'delay':
        plot_list = ['delay']
    elif plot_type == 'loss':
        plot_list = ['loss']
    else:
        print(f"[!] 未知绘图类型: {plot_type}，将绘制全部")
        plot_list = ['bw', 'delay', 'loss']

    if not plot_list:
        return

    # 创建子图
    n_plots = len(plot_list)
    fig, axes = plt.subplots(n_plots, 1, figsize=(12, 4 * n_plots), sharex=True)
    if n_plots == 1:
        axes = [axes]

    title_map = {'bw': '带宽 (Mbps)', 'delay': '时延 (ms)', 'loss': '丢包率 (%)'}
    data_map = {'bw': bw_data, 'delay': delay_data, 'loss': loss_data}
    stat_key = {'bw': 'bw', 'delay': 'delay', 'loss': 'loss'}
    stat_fmt = {'bw': '{:.2f} Mbps', 'delay': '{:.2f} ms', 'loss': '{:.3f}%'}

    for idx, ptype in enumerate(plot_list):
        ax = axes[idx]
        for flow in flows:
            stat = flow_stats[flow]
            label = f'{flow_name[flow]} ({stat_fmt[ptype].format(stat[stat_key[ptype]])})'
            ax.plot(time_data[flow], data_map[ptype][flow], marker='.', label=label, linewidth=1.5)
        ax.set_ylabel(title_map[ptype])
        if idx == 0:
            ax.set_title('MGEN 测试 - ' + '/'.join([title_map[p] for p in plot_list]))
        ax.legend(loc='upper right')
        ax.grid(True, alpha=0.3)
        if idx == n_plots - 1:
            ax.set_xlabel('时间 (秒)')

    plt.tight_layout()


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description='MGEN 日志分析工具 - 解析 MGEN 接收日志并生成带宽/时延/丢包率统计图',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  %(prog)s -i /home/u/test/multi_recv.log              # 分析指定日志文件
  %(prog)s -i /home/u/test/multi_recv.log -p bw        # 仅绘制带宽图
  %(prog)s -i /home/u/test/multi_recv.log -p delay      # 仅绘制时延图
  %(prog)s -i /home/u/test/multi_recv.log -p loss      # 仅绘制丢包率图
  %(prog)s -i /home/u/test/multi_recv.log -p all       # 绘制全部三个图 (默认)
  %(prog)s -i /home/u/test/multi_recv.log -p bw,delay  # 绘制带宽和时延图
  %(prog)s -i /home/u/test/multi_recv.log --no-plot     # 仅输出统计，不绘图
        '''
    )
    parser.add_argument('-i', '--input', dest='log_file', default=LOG_PATH ,
                        help='MGEN 接收日志文件路径')
    parser.add_argument('-p', '--plot', dest='plot_type', default='all',
                        help='指定绘图的类型，可选值: bw (带宽), delay (时延), loss (丢包率), all (全部，默认) 或用逗号分隔组合如 "bw,delay"')
    parser.add_argument('--no-plot', dest='no_plot', action='store_true',
                        help='仅输出统计信息，不绘制图表')

    args = parser.parse_args()

    # 处理逗号分隔的绘图类型
    if args.plot_type and args.plot_type != 'all' and ',' in args.plot_type:
        plot_types = args.plot_type.split(',')
        # 去重并保持顺序
        seen = set()
        unique_types = []
        for t in plot_types:
            t = t.strip().lower()
            if t not in seen and t in ('bw', 'delay', 'loss'):
                seen.add(t)
                unique_types.append(t)
        if len(unique_types) == 1:
            args.plot_type = unique_types[0]
        elif len(unique_types) > 1:
            # 多类型组合，使用特殊标记或直接传列表
            args.plot_type = unique_types
        else:
            args.plot_type = 'all'
    elif args.plot_type:
        args.plot_type = args.plot_type.lower()
        if args.plot_type not in ('bw', 'delay', 'loss', 'all'):
            parser.error(f"无效的绘图类型: {args.plot_type}，可选值: bw, delay, loss, all")

    return args


def main():
    args = parse_args()

    log_path = args.log_file
    if not os.path.exists(log_path):
        print(f"[!] 日志文件不存在: {log_path}")
        return

    print(f"[+] 解析日志: {log_path}")
    flow_summary, sec_stats = parse_mgen_log(log_path)
    print("[+] 生成汇总报告...")
    summary(flow_summary)

    if not args.no_plot:
        print("[+] 生成图表...")
        write_detail_csv(flow_summary, sec_stats, args.plot_type)
        print("[✔] 完成！")
        plt.show()
    else:
        print("[✔] 完成！(已禁用绘图)")


if __name__ == "__main__":
    main()