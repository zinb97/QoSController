#!/usr/bin/env python3
"""
Mininet 主机管理工具（修复版）
功能：
1. 获取 Mininet 主机的 PID、IP 信息
2. D-ITG 背景流量生成
3. 每个发送线程使用一个主机向随机目标发送流量
"""

import argparse
import concurrent.futures
import json
import random
import subprocess
import time
from typing import Dict, List, Optional

# ==================== 配置常量 ====================
LOG_DIR = '/tmp/ditg_logs'
DEFAULT_DURATION = 60  # 秒
DEFAULT_BANDWIDTH = 1000  # Kbps（修复：降低默认值避免过载）
DEFAULT_SENDER_THREADS = 2  # 发送线程总数（修复：减少默认值便于测试）
DEFAULT_PACKET_SIZE = 512  # 字节
DITG_RECV_PORT = 9000  # D-ITG 接收端口


# ==================== 主机信息获取 ====================
def extract_mininet_hosts():
    """从 ps 命令输出中提取 mininet 主机进程"""
    result = subprocess.run(['ps', '-eo', 'pid,cmd'], capture_output=True, text=True, check=True)
    host_pid_map = {}
    for line in result.stdout.splitlines():
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


# ==================== 主机信息类 ====================
class HostInfo:
    """主机信息"""

    def __init__(self, hostname: str, pid: str, ip: str):
        self.hostname = hostname
        self.pid = pid
        self.ip = ip

    def __repr__(self):
        return f"HostInfo({self.hostname}, pid={self.pid}, ip={self.ip})"


# ==================== 工具函数 ====================
def bandwidth_to_packet_rate(bandwidth_kbps: int, packet_size: int) -> int:
    """
    将带宽（Kbps）转换为 D-ITG 所需的每秒包数 (-C 参数)
    公式: 包数/秒 = (带宽 bps) / (包长字节 * 8)
    """
    bandwidth_bps = bandwidth_kbps * 1024  # 转换为 bps
    packet_rate = int(bandwidth_bps / (packet_size * 8))
    return max(packet_rate, 1)  # 确保至少 1 包/秒


# ==================== D-ITG 流量生成 ====================
class DITGManager:
    """D-ITG 流量管理器"""

    def __init__(self, hosts: List[HostInfo], duration: int = DEFAULT_DURATION,
                 bandwidth: int = DEFAULT_BANDWIDTH, sender_threads: int = DEFAULT_SENDER_THREADS):
        self.hosts = hosts
        self.duration = duration
        self.bandwidth = bandwidth
        self.sender_threads = sender_threads
        self.running = False

        # 创建日志目录
        subprocess.run(["mkdir", "-p", LOG_DIR], check=False)

    def _run_in_namespace(self, host: HostInfo, cmd_list: List[str],
                          background: bool = False, capture_output: bool = False):
        """
        在指定主机的网络命名空间执行命令（修复版）
        :param host: HostInfo 对象
        :param cmd_list: 命令列表（如 ["ITGRecv", "-D", "-p", "9000"]）
        :param background: 是否后台运行（Popen）
        :param capture_output: 是否捕获输出
        :return: Popen 对象（background=True）或 CompletedProcess 对象
        """
        # 核心修复：添加 --net 参数进入网络命名空间
        nsenter_cmd = ["nsenter", "--target", host.pid, "--net", "--pid", "--mount", "--"]
        full_cmd = nsenter_cmd + cmd_list

        print(' '.join(full_cmd))
        try:
            if background:
                return subprocess.Popen(
                    full_cmd,
                    stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                    stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                    text=True
                )
            else:
                return subprocess.run(
                    full_cmd,
                    check=True,
                    capture_output=capture_output,
                    text=True
                )
        except Exception as e:
            print(f"[{host.hostname}] 命令执行失败: {' '.join(full_cmd)}")
            print(f"[错误详情] {e}")
            raise

    def start_receiver(self, host: HostInfo):
        """启动 D-ITG 接收端（后台运行）"""
        # 使用 -Sp 端口参数，后台运行
        cmd = ["IGTRecv", "-Sp", str(DITG_RECV_PORT)]
        proc = self._run_in_namespace(host, cmd, background=True)
        pid = getattr(proc, 'pid', None) if proc else None
        print(f"[{host.hostname}] 接收端已启动 (PID: {pid}, 端口: {DITG_RECV_PORT})")

    def stop_receiver(self, host: HostInfo):
        """停止 D-ITG 接收端"""
        try:
            self._run_in_namespace(host, ["pkill", "-f", "ITGRecv"], background=True)
            print(f"[{host.hostname}] 接收端已停止")
        except subprocess.CalledProcessError:
            pass  # 进程不存在时忽略

    def start_all_receivers(self):
        """启动所有接收端"""
        print("\n" + "=" * 50)
        print("启动 D-ITG 接收端...")
        print("=" * 50)

        for host in self.hosts:
            self.start_receiver(host)

        time.sleep(2)  # 等待接收端就绪
        print(f"已启动 {len(self.hosts)} 个接收端")

    def stop_all_receivers(self):
        """停止所有接收端"""
        print("\n停止所有接收端...")
        for host in self.hosts:
            self.stop_receiver(host)

    def sender_worker(self, sender_host: HostInfo, dest_host: HostInfo, thread_id: int):
        """发送线程工作函数（修复版）"""
        log_file = f"{LOG_DIR}/t{thread_id}_{sender_host.hostname}_to_{dest_host.ip.replace('.', '_')}.log"
        packet_rate = bandwidth_to_packet_rate(self.bandwidth, DEFAULT_PACKET_SIZE)
        duration_ms = self.duration * 1000

        # 修复：使用正确的 ITGSend 参数
        # -a: 目标IP, -T: 协议, -t: 时长(ms), -l: 日志文件
        # -C: 每秒包数, -c: 包长(字节), -p: 目标端口
        cmd = [
            "ITGSend",
            "-a", dest_host.ip,
            "-T", "UDP",
            "-t", str(duration_ms),
            "-l", log_file,
            "-C", str(packet_rate),
            "-c", str(DEFAULT_PACKET_SIZE),
            "-p", str(DITG_RECV_PORT)
        ]

        print(f"[T{thread_id}] {sender_host.hostname} -> {dest_host.ip} "
              f"(带宽: {self.bandwidth} Kbps, 包速: {packet_rate} pps)")

        try:
            # 前台运行，等待发送完成
            self._run_in_namespace(sender_host, cmd, background=False, capture_output=False)
            print(f"[T{thread_id}] 发送完成")
        except Exception as e:
            print(f"[T{thread_id}] 发送失败: {e}")

    def start_senders(self):
        """启动所有发送线程"""
        print("\n" + "=" * 50)
        print("启动 D-ITG 发送端...")
        print("=" * 50)

        # 为每个发送线程分配主机
        tasks = []
        for i in range(self.sender_threads):
            sender = random.choice(self.hosts)
            other_hosts = [h for h in self.hosts if h.hostname != sender.hostname]
            if other_hosts:
                dest = random.choice(other_hosts)
                tasks.append((i, sender, dest))

        print(f"共 {len(tasks)} 个发送任务")

        # 使用线程池执行
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(self.sender_worker, s, d, tid) for tid, s, d in tasks]
            concurrent.futures.wait(futures)

        print("所有发送任务已完成")

    def run(self):
        """运行流量生成"""
        print("=" * 50)
        print("D-ITG 背景流量生成器（修复版）")
        print("=" * 50)
        print(f"主机数量: {len(self.hosts)}")
        print(f"发送线程: {self.sender_threads}")
        print(f"持续时间: {self.duration} 秒")
        print(f"每条带宽: {self.bandwidth} Kbps")
        print(f"包长: {DEFAULT_PACKET_SIZE} 字节")
        print("=" * 50)

        try:
            self.start_all_receivers()
            self.start_senders()
        except KeyboardInterrupt:
            print("\n\n收到中断信号，正在停止...")
        finally:
            self.stop_all_receivers()
            print("\n背景流量生成结束")


# ==================== 主函数 ====================
def main():
    parser = argparse.ArgumentParser(description='Mininet 主机管理与 D-ITG 流量生成')
    parser.add_argument('--list', action='store_true', help='列出所有主机信息')
    parser.add_argument('--hosts-file', type=str, help='主机信息 JSON 文件路径')
    parser.add_argument('-d', '--duration', type=int, default=DEFAULT_DURATION,
                        help=f'发送持续时间（秒），默认 {DEFAULT_DURATION}')
    parser.add_argument('-b', '--bandwidth', type=int, default=DEFAULT_BANDWIDTH,
                        help=f'发送带宽（Kbps），默认 {DEFAULT_BANDWIDTH}')
    parser.add_argument('-t', '--threads', type=int, default=DEFAULT_SENDER_THREADS,
                        help=f'发送线程总数，默认 {DEFAULT_SENDER_THREADS}')
    parser.add_argument('--host-list', type=str,
                        help='指定主机列表，逗号分隔，如 h1,h2,h3')
    parser.add_argument('--traffic', action='store_true',
                        help='启动 D-ITG 背景流量生成')

    args = parser.parse_args()

    # 获取主机信息
    if args.hosts_file:
        with open(args.hosts_file, 'r') as f:
            data = json.load(f)
        host_dict = {k: v for k, v in data.items()}
    else:
        host_dict = extract_host_info()

    # 转换为 HostInfo 列表
    hosts = []
    for hostname, info in sorted(host_dict.items()):
        if hostname.startswith('h'):
            hosts.append(HostInfo(hostname, info['pid'], info['ip']))

    # 过滤主机列表
    if args.host_list:
        hostnames = set(args.host_list.split(','))
        hosts = [h for h in hosts if h.hostname in hostnames]

    if not hosts:
        print("错误: 没有可用主机，请先启动 Mininet")
        return

    # 列出主机
    if args.list:
        print("\n主机列表:")
        print("-" * 40)
        for h in hosts:
            print(f"  {h.hostname}: PID={h.pid}, IP={h.ip}")
        print("-" * 40)

    # 启动流量生成
    if args.traffic:
        manager = DITGManager(
            hosts=hosts,
            duration=args.duration,
            bandwidth=args.bandwidth,
            sender_threads=args.threads
        )
        manager.run()
    elif not args.list:
        # 默认显示主机列表
        for h in hosts:
            print(f"{h.hostname} {h.pid} {h.ip}")


if __name__ == '__main__':
    main()