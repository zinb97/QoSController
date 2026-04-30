# -*- coding: utf-8 -*-
import argparse
import os
import random
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import netifaces

# 全局打印锁，保证多线程打印不混乱
print_lock = threading.Lock()


def log(msg):
    """带时间戳的日志输出"""
    prefix = time.strftime('%H:%M:%S')
    with print_lock:
        print(f"\r[{prefix}] {msg}")


# ==================== 主机信息获取 ====================
def extract_mininet_hosts():
    """从 ps 命令输出中提取 mininet 主机进程"""
    result = subprocess.run(['ps', '-eo', 'pid,cmd'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    host_pid_map = {}
    for line in result.stdout.decode('utf-8').splitlines():
        if 'mininet:' in line and 'grep' not in line:
            parts = line.split()
            pid = parts[0]
            # 更稳健的 hostname 提取
            for part in parts:
                if part.startswith('mininet:'):
                    hostname = part.split(':')[-1]
                    host_pid_map[hostname] = pid
                    break
    return host_pid_map


def extract_host_info():
    """获取所有 mininet 主机的详细信息"""
    host_pid_map = extract_mininet_hosts()
    host_info = {}
    for hostname, pid in sorted(host_pid_map.items()):
        if hostname.startswith('h'):
            host_info[hostname] = {'pid': pid}
            host_info[hostname]['ip'] = f'10.0.0.{hostname[1:]}'
    return host_info


def get_local_ips():
    ip_list = []
    for iface in netifaces.interfaces():
        addrs = netifaces.ifaddresses(iface)
        if netifaces.AF_INET in addrs:
            for addr_info in addrs[netifaces.AF_INET]:
                ip = addr_info["addr"]
                if ip != "127.0.0.1":
                    ip_list.append(ip)
    return ip_list


def send_burst(dst_ips, dst_port, duration, bandwidth_mbps, threads=5, payload_size=1024):
    log(f"[发送端] 启动 | 目标: {dst_ips} | 端口: {dst_port} | 时长: {duration}s | 带宽: {bandwidth_mbps}Mbps "
        f"| 线程: {threads} | 报长: {payload_size}B")

    # 计算理论每秒可发送包数（总带宽）
    # 1 Mbps = 1,000,000 bits/s → ÷8 = bytes/s
    bytes_per_second = (bandwidth_mbps * 1000000) // 8
    packets_per_second = bytes_per_second // payload_size
    if packets_per_second <= 0:
        log(f"[错误] 带宽太小或报文太大，无法计算发包速率！")
        return

    # 每个线程分摊的速率
    pps_per_thread = packets_per_second // threads
    if pps_per_thread == 0:
        pps_per_thread = 1

    log(f"[发送端] 限速: {pps_per_thread} pps/线程 | 总计: {packets_per_second} pps")

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
            log(f"[错误] 创建socket失败: {e}")
            return

    def worker(wid):
        nonlocal total_sent
        sock = socks[wid]
        payload = bytes(random.randint(0, 255) for _ in range(payload_size))
        ip_count = len(dst_ips)
        thread_sent = 0

        # 精准限速：固定间隔发送
        interval = 1.0 / pps_per_thread
        next_send_time = time.time()

        while not stop_event.is_set():
            now = time.time()
            if now < next_send_time:
                time.sleep(next_send_time - now)
                continue

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
    log(f"[发送端] 开始发送...")

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
    total_mb = total_bytes / 1024 / 1024
    real_bandwidth = (total_bytes * 8) / elapsed / 1000000  # Mbps
    log(f"[完成] 耗时: {elapsed:.2f}s | 速率: {real_bandwidth:.2f} Mbps | 总量: {total_mb:.2f} MB | 报文: {total_sent[0]}")


# =================================================
# 批量并行执行函数（多线程）
# =================================================
def run_batch_in_namespaces(pid_list, port, duration, bandwidth, threads, size):
    """
    批量并行在多个 PID 网络命名空间运行脚本
    :param pid_list: PID 列表 [111, 222, 333]
    :param port: 目标端口
    :param duration: 发送时长（秒）
    :param bandwidth: 目标带宽（Mbps）
    :param threads: 发送线程数
    :param size: 报文大小（字节）
    """
    def run_in_namespace(target_pid):
        cmd = [
            "sudo", "nsenter", "-t", str(target_pid), "-n", "python3", SCRIPT_PATH,
            "-p", str(port),
            "-d", str(duration),
            "-b", str(bandwidth),
            "-t", str(threads),
            "-s", str(size),
        ]
        log(f"[Batch] 启动 PID={target_pid}")
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log(f"[Batch] 完成 PID={target_pid}")
        except Exception as e:
            log(f"[Batch] 失败 PID={target_pid}: {e}")

    run_threads = []
    for target_pid in pid_list:
        t = threading.Thread(target=run_in_namespace, args=(target_pid,))
        run_threads.append(t)
        t.start()
    for t in run_threads:
        t.join()

    log("\r✅ 所有 PID 任务执行完毕！\r")


def main():

    parser = argparse.ArgumentParser(description='Mininet 流量发生器')
    parser.add_argument('-p', '--port', type=int, default=9999, help='目标端口 (默认: 9999)')
    parser.add_argument('-d', '--duration', type=int, default=60, help='发送时长，秒 (默认: 60)')
    parser.add_argument('-b', '--bandwidth', type=int, default=100, help='目标带宽，Mbps (默认: 100)')
    parser.add_argument('-t', '--threads', type=int, default=5, help='发送线程数 (默认: 5)')
    parser.add_argument('-s', '--size', type=int, default=1024, help='报文大小，字节 (默认: 1024)')
    parser.add_argument('-l', '--list', action='store_true', help='仅列出检测到的主机并退出')
    parser.add_argument('-m', '--batch', action='store_true', help='多线程批量模式：在所有 mininet 主机上并行执行')
    args = parser.parse_args()

    host_info = extract_host_info()
    local_ips = get_local_ips()
    dst_ips = [host_info[host]['ip'] for host in host_info if host_info[host]['ip'] not in local_ips]

    if args.list:
        log(f"[列表] 检测到的主机: {list(host_info.keys())}")
        log(f"[列表] 本机IP: {local_ips[0] if len(local_ips) == 1 else local_ips}")
        log(f"[列表] 可用目标IP: {dst_ips}")
        exit(0)

    if args.batch:
        # 多线程批量模式：在每台主机的网络命名空间中并行执行
        pid_list = [int(host_info[host]['pid']) for host in host_info]
        if not pid_list:
            log("[错误] 未检测到 mininet 主机")
            exit(1)
        log(f"[Batch] 批量模式启动 | 主机数: {len(pid_list)} | "
            f"端口: {args.port} | 时长: {args.duration}s | 带宽: {args.bandwidth}Mbps | "
            f"线程: {args.threads} | 报长: {args.size}B")
        run_batch_in_namespaces(
            pid_list,
            port=args.port,
            duration=args.duration,
            bandwidth=args.bandwidth,
            threads=args.threads,
            size=args.size,
        )
    else:
        # 单机直接模式
        if not dst_ips:
            log("[错误] 未检测到可用的 mininet 主机")
            exit(1)
        send_burst(
            dst_ips=dst_ips,
            dst_port=args.port,
            duration=args.duration,
            bandwidth_mbps=args.bandwidth,
            threads=args.threads,
            payload_size=args.size,
        )


if __name__ == "__main__":
    SCRIPT_PATH = os.path.abspath(__file__)
    main()