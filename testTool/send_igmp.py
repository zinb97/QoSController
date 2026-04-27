#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发送 IGMPv2 报文的 Python 脚本
"""

import socket
import struct
import time
import random

def calc_checksum(data):
    """计算 IGMP 校验和"""
    if len(data) % 2 == 1:
        data += b'\x00'
    s = 0
    for i in range(0, len(data), 2):
        w = (data[i] << 8) + data[i+1]
        s += w
    s = (s >> 16) + (s & 0xffff)
    s += s >> 16
    return ~s & 0xffff

def send_igmpv2_report(multicast_addr='239.255.255.250', interface='0.0.0.0'):
    """发送 IGMPv2 Membership Report (Type=0x16)"""

    group = socket.inet_aton(multicast_addr)
    interface_ip = socket.inet_aton(interface) if interface != '0.0.0.0' else socket.inet_aton('0.0.0.0')

    # IGMPv2 Report: Type=0x16, Group Address=组播地址
    igmp_type = 0x16  # IGMPv2 Membership Report
    igmp_reserved = 0x00
    igmp_checksum = 0x0000

    # 构造 IGMPv2 Report 报文 (8字节)
    igmp_header = struct.pack('!BBH', igmp_type, igmp_reserved, igmp_checksum) + group

    # 计算校验和
    igmp_checksum = calc_checksum(igmp_header)
    igmp_header = struct.pack('!BBH', igmp_type, igmp_reserved, igmp_checksum) + group

    # 创建原始 IGMP socket
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)

    # 构造 IP 头 (20字节)
    version_ihl = (4 << 4) | 5
    ttl = 1
    proto = socket.IPPROTO_IGMP
    src_ip = interface_ip if interface != '0.0.0.0' else socket.inet_aton('0.0.0.0')
    dst_ip = socket.inet_aton('224.0.0.1')  # All hosts

    ip_header = struct.pack('!BBHHHBBH4s4s',
                            version_ihl, 0, 20 + len(igmp_header),
                            random.randint(0, 65535), 0x4000,
                            ttl, proto, 0,
                            src_ip, dst_ip)

    s.sendto(ip_header + igmp_header, (multicast_addr, 0))
    print(f'Sent IGMPv2 Report (Type=0x16) for group {multicast_addr}')
    s.close()

def send_igmpv2_leave(multicast_addr='239.255.255.250', interface='0.0.0.0'):
    """发送 IGMPv2 Leave Group (Type=0x17)"""

    group = socket.inet_aton(multicast_addr)
    interface_ip = socket.inet_aton(interface) if interface != '0.0.0.0' else socket.inet_aton('0.0.0.0')

    igmp_type = 0x17  # IGMPv2 Leave Group
    igmp_reserved = 0x00
    igmp_checksum = 0x0000

    # 构造 IGMPv2 Leave 报文 (8字节)
    igmp_header = struct.pack('!BBH', igmp_type, igmp_reserved, igmp_checksum) + group
    igmp_checksum = calc_checksum(igmp_header)
    igmp_header = struct.pack('!BBH', igmp_type, igmp_reserved, igmp_checksum) + group

    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)

    ttl = 1
    proto = socket.IPPROTO_IGMP
    src_ip = interface_ip
    dst_ip = socket.inet_aton('224.0.0.2')  # All routers

    ip_header = struct.pack('!BBHHHBBH4s4s',
                            (4 << 4) | 5, 0, 20 + len(igmp_header),
                            random.randint(0, 65535), 0x4000,
                            ttl, proto, 0,
                            src_ip, dst_ip)

    s.sendto(ip_header + igmp_header, (multicast_addr, 0))
    print(f'Sent IGMPv2 Leave (Type=0x17) for group {multicast_addr}')
    s.close()

def send_igmpv2_query(multicast_addr='0.0.0.0', interface='0.0.0.0'):
    """发送 IGMPv2 General Query (Type=0x11)

    Args:
        multicast_addr: 组播地址，0.0.0.0 表示 General Query，非0表示 Specific Query
        interface: 发送接口 IP
    """
    group = socket.inet_aton(multicast_addr)
    interface_ip = socket.inet_aton(interface) if interface != '0.0.0.0' else socket.inet_aton('0.0.0.0')

    # IGMPv2 Query: Type=0x11, Max Response Time=10 (自定义), Checksum=0, Group Address
    igmp_type = 0x11  # IGMPv2 Query
    igmp_max_resp = 10  # 最大响应时间（单位 0.1秒），这里设为 1 秒
    igmp_checksum = 0x0000

    # 构造 IGMPv2 Query 报文 (8字节)
    # Type(1) + Max Response Time(1) + Checksum(2) + Group Address(4)
    igmp_header = struct.pack('!BBH', igmp_type, igmp_max_resp, igmp_checksum) + group

    # 计算校验和
    igmp_checksum = calc_checksum(igmp_header)
    igmp_header = struct.pack('!BBH', igmp_type, igmp_max_resp, igmp_checksum) + group

    # 创建原始 IGMP socket
    s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_IGMP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)

    # 构造 IP 头 (20字节)
    version_ihl = (4 << 4) | 5
    ttl = 1
    proto = socket.IPPROTO_IGMP
    src_ip = interface_ip
    dst_ip = socket.inet_aton('224.0.0.1')  # All hosts

    ip_header = struct.pack('!BBHHHBBH4s4s',
                            version_ihl, 0, 20 + len(igmp_header),
                            random.randint(0, 65535), 0x4000,
                            ttl, proto, 0,
                            src_ip, dst_ip)

    s.sendto(ip_header + igmp_header, ('224.0.0.1', 0))
    if multicast_addr == '0.0.0.0':
        print(f'Sent IGMPv2 General Query (Type=0x11)')
    else:
        print(f'Sent IGMPv2 Specific Query (Type=0x11) for group {multicast_addr}')
    s.close()

if __name__ == '__main__':
    import sys

    multicast_addr = '239.255.255.250'
    action = 'join'

    if len(sys.argv) > 1:
        if sys.argv[1] == 'leave':
            action = 'leave'
        elif sys.argv[1] == 'query':
            action = 'query'
            if len(sys.argv) > 2:
                multicast_addr = sys.argv[2]
        elif sys.argv[1] == 'join':
            action = 'join'
            if len(sys.argv) > 2:
                multicast_addr = sys.argv[2]
        else:
            multicast_addr = sys.argv[1]
            if len(sys.argv) > 2:
                action = sys.argv[2]

    if action == 'leave':
        send_igmpv2_leave(multicast_addr)
    elif action == 'query':
        send_igmpv2_query(multicast_addr)
    else:
        send_igmpv2_report(multicast_addr)
