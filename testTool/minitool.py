#!/usr/bin/env python3
import subprocess


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
