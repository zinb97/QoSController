#!/usr/bin/env python3
"""
流量发送工具 - 高效批量 UDP 报文发送（带宽+时长模式）
"""

import socket
import time
import random
from concurrent.futures import ThreadPoolExecutor
import threading


def send_burst(dst_ips, dst_port, duration, bandwidth_mbps, threads=5, payload_size=1024):

    print(f'[流量发送] 启动')
    print(f'目标IP: {dst_ips}')
    print(f'目标端口: {dst_port}')
    print(f'发送时长: {duration}s')
    print(f'目标带宽: {bandwidth_mbps} Mbps')
    print(f'线程数: {threads}')
    print(f'报文大小: {payload_size} 字节\n')

    # 计算理论每秒可发送包数（总带宽）
    # 1 Mbps = 1,000,000 bits/s → ÷8 = bytes/s
    bytes_per_second = (bandwidth_mbps * 1000000) // 8
    packets_per_second = bytes_per_second // payload_size
    if packets_per_second <= 0:
        print("错误：带宽太小或报文太大，无法计算发包速率！")
        return

    # 每个线程分摊的速率
    pps_per_thread = packets_per_second // threads
    if pps_per_thread == 0:
        pps_per_thread = 1

    # 停止标志
    stop_event = threading.Event()
    # 全局统计
    total_sent = [0]
    stat_lock = threading.Lock()

    # 创建socket
    socks = []
    for _ in range(threads):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8 * 1024 * 1024)
            socks.append(sock)
        except Exception as e:
            print(f"创建socket失败: {e}")
            return

    def worker(wid):
        nonlocal total_sent
        sock = socks[wid]
        payload = bytes(random.randint(0, 255) for _ in range(payload_size))
        ip_count = len(dst_ips)
        thread_sent = 0
        last_print = time.time()

        # 精准限速：固定间隔发送
        interval = 1.0 / pps_per_thread
        next_send_time = time.time()

        while not stop_event.is_set():
            now = time.time()
            if now < next_send_time:
                time.sleep(next_send_time - now)
                continue

            # 发送
            try:
                dst_ip = dst_ips[thread_sent % ip_count]
                sock.sendto(payload, (dst_ip, dst_port))
                thread_sent += 1
                next_send_time += interval
            except Exception:
                continue

        # 累计总数
        with stat_lock:
            total_sent[0] += thread_sent

    # 开始时间
    start_time = time.time()
    print(f"开始发送 -> 持续 {duration} 秒...\n")

    # 启动线程池
    with ThreadPoolExecutor(max_workers=threads) as executor:
        for i in range(threads):
            executor.submit(worker, i)

        # 运行指定时间
        time.sleep(duration)
        # 停止所有线程
        stop_event.set()

    # 关闭socket
    for s in socks:
        s.close()

    # 统计结果
    elapsed = time.time() - start_time
    total_bytes = total_sent[0] * payload_size
    real_bandwidth = (total_bytes * 8) / elapsed / 1000000  # Mbps

    print("=" * 60)
    print(f"发送完成！")
    print(f"总时长: {elapsed:.2f}s")
    print(f"总报文: {total_sent[0]} 个")
    print(f"总流量: {total_bytes / 1024 / 1024:.2f} MB")
    print(f"实际带宽: {real_bandwidth:.2f} Mbps")
    print(f"平均速率: {total_sent[0] / elapsed:.2f} pps")
    print("=" * 60)


if __name__ == '__main__':
    # 目标IP列表
    dst_ips = [f'10.0.0.{i + 1}' for i in range(6) if i != 3]

    send_burst(
        dst_ips=dst_ips,
        dst_port=9999,
        duration=60,      # 发送时长：秒
        bandwidth_mbps=100, # 带宽：Mbps
    )