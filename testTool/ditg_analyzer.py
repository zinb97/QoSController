#!/usr/bin/env python3
"""
D-ITG 测试结果数据处理与可视化工具
支持绘制：吞吐量、延迟、抖动、丢包率随时间变化的折线图
支持多组数据对比
"""

import os

import matplotlib.pyplot as plt
import numpy as np

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['WenQuanYi Micro Hei', 'SimHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题

# D-ITG combined_stats.dat 列说明
# Col 1: Time (s)
# Col 2: Throughput (bps)
# Col 3: One-way Delay (s)
# Col 4: Jitter (s)
# Col 5: Loss Packet Count

# 测试发送速率 (pps)
PACKET_RATE = 2000  # packets per second


def calculate_statistics(data):
    """计算统计数据"""
    stats = {}
    for key in ['throughput', 'delay', 'jitter']:
        arr = data[key]
        stats[key] = {
            'mean': np.mean(arr),
            'std': np.std(arr),
            'min': np.min(arr),
            'max': np.max(arr),
            'median': np.median(arr)
        }
    return stats


def calculate_loss_rate(data):
    """计算丢包率 (百分比)"""
    time_arr = data['time']
    loss_arr = data['packets']

    # 丢包率 = 丢失包数 / 发送包数 * 100
    # 发送包数 = 速率 * 时间
    sent_packets = PACKET_RATE * time_arr

    # 安全除法，避免除零警告
    with np.errstate(divide='ignore', invalid='ignore'):
        loss_rate = np.where(sent_packets > 0, (loss_arr / sent_packets) * 100, 0)
        loss_rate = np.nan_to_num(loss_rate, nan=0.0, posinf=0.0, neginf=0.0)
    return loss_rate


def parse_ditg_file(filepath):
    """解析 D-ITG combined_stats.dat 文件"""
    times = []
    throughput = []
    delay = []
    jitter = []
    packets = []

    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip().replace('\r', '')
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 5:
                try:
                    times.append(float(parts[0]))
                    throughput.append(float(parts[1]))
                    delay.append(float(parts[2]) * 1000)  # 转换为毫秒
                    jitter.append(float(parts[3]) * 1000)  # 转换为毫秒
                    packets.append(float(parts[4]))
                except ValueError:
                    continue

    return {
        'time': np.array(times),
        'throughput': np.array(throughput),
        'delay': np.array(delay),
        'jitter': np.array(jitter),
        'packets': np.array(packets)
    }


def plot_comparison(data_list, labels, save_path=None):
    """绘制多组数据对比图"""
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b']

    fig, axes = plt.subplots(4, 1, figsize=(14, 16))

    # 确定时间范围
    max_time = max(d['time'][-1] for d in data_list)

    # 吞吐量对比
    for i, (data, label) in enumerate(zip(data_list, labels)):
        axes[0].plot(data['time'], data['throughput'] / 1000,
                     color=colors[i % len(colors)], linewidth=1.5, label=f'{label} (Avg: {np.mean(data["throughput"])/1000:.2f} Kbps)')
    axes[0].set_ylabel('Throughput (Kbps)', fontsize=11)
    axes[0].set_title('Throughput Comparison', fontsize=12, fontweight='bold')
    axes[0].grid(True, alpha=0.3)
    axes[0].legend(loc='upper right')
    axes[0].set_xlim([0, max_time])

    # 延迟对比
    for i, (data, label) in enumerate(zip(data_list, labels)):
        axes[1].plot(data['time'], data['delay'],
                     color=colors[i % len(colors)], linewidth=1.5, label=f'{label} (Avg: {np.mean(data["delay"]):.2f} ms)')
    axes[1].set_ylabel('Delay (ms)', fontsize=11)
    axes[1].set_title('One-way Delay Comparison', fontsize=12, fontweight='bold')
    axes[1].grid(True, alpha=0.3)
    axes[1].legend(loc='upper right')
    axes[1].set_xlim([0, max_time])

    # 抖动对比
    for i, (data, label) in enumerate(zip(data_list, labels)):
        axes[2].plot(data['time'], data['jitter'],
                     color=colors[i % len(colors)], linewidth=1.5, label=f'{label} (Avg: {np.mean(data["jitter"]):.4f} ms)')
    axes[2].set_ylabel('Jitter (ms)', fontsize=11)
    axes[2].set_title('Jitter Comparison', fontsize=12, fontweight='bold')
    axes[2].grid(True, alpha=0.3)
    axes[2].legend(loc='upper right')
    axes[2].set_xlim([0, max_time])

    # 丢包率对比
    for i, (data, label) in enumerate(zip(data_list, labels)):
        loss_rate = calculate_loss_rate(data)
        axes[3].plot(data['time'], loss_rate,
                     color=colors[i % len(colors)], linewidth=1.5, label=f'{label} (Avg: {np.mean(loss_rate):.4f} %)')
    axes[3].set_ylabel('Loss Rate (%)', fontsize=11)
    axes[3].set_title('Packet Loss Rate Comparison', fontsize=12, fontweight='bold')
    axes[3].grid(True, alpha=0.3)
    axes[3].legend(loc='upper right')
    axes[3].set_xlim([0, max_time])
    axes[3].set_xlabel('Time (s)', fontsize=11)

    plt.suptitle('D-ITG Test Results Comparison', fontsize=16, fontweight='bold', y=0.995)
    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图表已保存至: {save_path}")

    return fig


def plot_single(data, save_path=None):
    """绘制单个数据文件的图表"""
    stats = calculate_statistics(data)
    loss_rate = calculate_loss_rate(data)
    fig, axes = plt.subplots(4, 1, figsize=(14, 16))

    # 吞吐量
    axes[0].plot(data['time'], data['throughput'] / 1000, 'b-', linewidth=1.5)
    axes[0].axhline(y=stats['throughput']['mean'] / 1000, color='r', linestyle='--', alpha=0.7)
    axes[0].set_ylabel('Throughput (Kbps)', fontsize=11)
    axes[0].set_title('Throughput', fontsize=12, fontweight='bold')
    axes[0].grid(True, alpha=0.3)
    axes[0].set_xlim([0, data['time'][-1]])

    # 延迟
    axes[1].plot(data['time'], data['delay'], 'g-', linewidth=1.5)
    axes[1].axhline(y=stats['delay']['mean'], color='r', linestyle='--', alpha=0.7)
    axes[1].set_ylabel('Delay (ms)', fontsize=11)
    axes[1].set_title('One-way Delay', fontsize=12, fontweight='bold')
    axes[1].grid(True, alpha=0.3)
    axes[1].set_xlim([0, data['time'][-1]])

    # 抖动
    axes[2].plot(data['time'], data['jitter'], 'm-', linewidth=1.5)
    axes[2].axhline(y=stats['jitter']['mean'], color='r', linestyle='--', alpha=0.7)
    axes[2].set_ylabel('Jitter (ms)', fontsize=11)
    axes[2].set_title('Jitter', fontsize=12, fontweight='bold')
    axes[2].grid(True, alpha=0.3)
    axes[2].set_xlim([0, data['time'][-1]])

    # 丢包率
    axes[3].plot(data['time'], loss_rate, 'r-', linewidth=1.5)
    axes[3].axhline(y=np.mean(loss_rate), color='r', linestyle='--', alpha=0.7)
    axes[3].set_ylabel('Loss Rate (%)', fontsize=11)
    axes[3].set_title('Packet Loss Rate', fontsize=12, fontweight='bold')
    axes[3].grid(True, alpha=0.3)
    axes[3].set_xlim([0, data['time'][-1]])
    axes[3].set_xlabel('Time (s)', fontsize=11)

    plt.suptitle('D-ITG Test Results Analysis', fontsize=16, fontweight='bold', y=0.995)
    plt.tight_layout()

    if save_path:
        plt.savefig(save_path, dpi=150, bbox_inches='tight')
        print(f"图表已保存至: {save_path}")

    return fig


def print_statistics(stats):
    """打印统计数据"""
    print("\n" + "=" * 60)
    print("D-ITG 测试结果统计")
    print("=" * 60)

    print("\n【吞吐量 Throughput】")
    print(f"  平均值: {stats['throughput']['mean']/1000:.2f} Kbps")
    print(f"  标准差: {stats['throughput']['std']/1000:.2f} Kbps")
    print(f"  最小值: {stats['throughput']['min']/1000:.2f} Kbps")
    print(f"  最大值: {stats['throughput']['max']/1000:.2f} Kbps")
    print(f"  中位数: {stats['throughput']['median']/1000:.2f} Kbps")

    print("\n【延迟 Delay】")
    print(f"  平均值: {stats['delay']['mean']:.4f} ms")
    print(f"  标准差: {stats['delay']['std']:.4f} ms")
    print(f"  最小值: {stats['delay']['min']:.4f} ms")
    print(f"  最大值: {stats['delay']['max']:.4f} ms")
    print(f"  中位数: {stats['delay']['median']:.4f} ms")

    print("\n【抖动 Jitter】")
    print(f"  平均值: {stats['jitter']['mean']:.6f} ms")
    print(f"  标准差: {stats['jitter']['std']:.6f} ms")
    print(f"  最小值: {stats['jitter']['min']:.6f} ms")
    print(f"  最大值: {stats['jitter']['max']:.6f} ms")
    print(f"  中位数: {stats['jitter']['median']:.6f} ms")

    print("\n" + "=" * 60)


def main():
    """默认运行：对比 1.txt 和 2.txt"""
    filepath = '/home/u/test'
    filename = ['1.txt', '2.txt']
    labels = [f'flow {i + 1}' for i in range(len(filename))]
    data_list = []
    output = 'comparison.png'

    for f in filename:
        print(f"正在解析文件: {f}")
        data = parse_ditg_file(os.path.join(filepath, f))
        print(f"  成功读取 {len(data['time'])} 条数据记录")
        data_list.append(data)

    # 如果只有一个文件，绘制单文件图表；否则绘制对比图
    if len(data_list) == 1:
        plot_single(data_list[0], output)
    else:
        plot_comparison(data_list, labels, output)

    plt.show()


if __name__ == '__main__':
    main()
