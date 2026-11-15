"""控制器主动探测：用 ICMP 探针测量每条链路的真实时延。

背景
----
链路的时延此前只有 netem 的**配置值**（`tc qdisc show` 里读出来的 delay 参数），
被动采集拿不到 RTT——交换机不上报时延，OF 端口统计里也没有这个字段。
要拿到真实时延只能主动发探测包。

测量原理
--------
Mininet 的 TCLink 在链路**两端**都配 netem（`TCIntf.config` 对 intf1/intf2
各调一次），所以一个包在一条链路上往返一次，延迟是 2×d。

直接测「A→B 的探针 RTT」会把主机侧链路（B→主机）的延迟也混进来，分不出
链路自身的时延。这里用**同目标、异注入点**的差分法：

    RTT_base = 在 B 上注入、发给 B 的主机 H   →  d(B,H) + d(B,H)
    RTT_meas = 在 A 上注入、同样发给主机 H     →  d(A,B) + d(B,H) + d(B,H)

两次探测的**回包路径完全相同**（H→B），处理开销也几乎一致，于是

    d(A,B) = RTT_meas − RTT_base

探针身份
--------
用一对控制器自造的身份，让主机内核照常回包：
- 源 IP：目标主机同网段内的保留地址（默认第四段 250）
- 源 MAC：locally-administered 的假 MAC（02:…）

主机内核对这个源 IP 发 ARP，控制器已有的 ARP 代理会替它回答（回的正是这个
假 MAC）；主机缓存之后，ICMP 回包直接以该 MAC 为目的地址。该 MAC 各交换机
都不认识，所以控制器在每台交换机上装一条「dst=假MAC → 送控制器」的流表，
回包因此在**最先到达的那台交换机**（目标交换机 B）就被交给控制器——正好是
差分法需要的采样点。

为什么不用「直接 ping 主机」测端到端 RTT：
端到端 RTT 是一条路径上多跳时延之和，无法归因到具体某条链路；上面的差分法
把链路自身的时延单独剥了出来。
"""

import socket
import time

from ryu.lib import hub
from ryu.lib.packet import ethernet, icmp, ipv4, packet
from ryu.ofproto import ether


# 探针源 MAC：locally-administered（02 开头），不会与真实主机 MAC 冲突
PROBE_MAC = '02:00:00:00:00:fe'
# IPv4 协议号：ICMP（ryu 的 ipv4 模块没有 ipro_* 常量，用 socket 的）
ICMP_PROTO = socket.IPPROTO_ICMP
# 探针源 IP 的第四段（前三段跟随目标主机所在网段）
PROBE_HOST_OCTET = 250
# 探测周期（秒）
PROBE_INTERVAL = 3.0
# 单次探测超时（秒）：超过则认为这次探测失败
PROBE_TIMEOUT = 2.0
# 滑动平均窗口（样本数）：抑制单次抖动
PROBE_SMOOTH = 5
# 「回包送控制器」流表的优先级（高于控制器下发的常规转发流）
COLLECTOR_FLOW_PRIORITY = 300


class LinkProbe:
    """链路时延主动探测器。

    由控制器实例持有并驱动（`hub.spawn(probe.loop)`），依赖控制器提供的：
    `graph`（拓扑）、`hosts`（主机表）、`datapaths`、`ip_to_mac`、`add_flow()`。
    """

    def __init__(self, app):
        self.app = app
        # 待回收的回包：{(icmp_id, seq): (发送时刻, 链路键, 种类, 轮次)}
        self._pending = {}
        # 已消费过的探针（同一回包的其它副本，用来安静地丢弃它们）
        self._consumed = {}
        # (链路键, 种类) → ICMP id
        self._probe_ids = {}
        # seq 计数器
        self._seq = 0
        # 轮次计数器
        self._round = 0
        # (链路键, 种类, 轮次) → RTT(ms)
        self._rtts = {}
        # 链路键 → {'delay_ms':…, 'samples':[…], 'updated':…}
        self._results = {}
        # 预热中的链路：首轮结果丢弃（首轮含 ARP 解析开销）
        self._warmup = set()
        # 已经探测过的链路（与 _warmup 分开：_warmup 只标记「当前这一轮是预热」）
        self._probed = set()
        # 已装「回包收集流」的交换机
        self._flows_installed = set()
        # 探针源 IP → 探针源 MAC（注册进控制器的 ARP 代理表）
        self._probe_ips = {}

    # ==================== 基础设施 ====================

    def _probe_ip_for(self, host_ip):
        """探针源 IP：跟随目标主机所在网段，只换第四段。

        必须与目标主机同网段——主机内核才会把回包当作「同网段直发」（直接
        以二层发给探针 MAC），而不是交给默认网关路由出去。
        """
        parts = (host_ip or '').split('.')
        if len(parts) != 4:
            return None
        return '.'.join(parts[:3] + [str(PROBE_HOST_OCTET)])

    def _register_probe_ip(self, host_ip):
        """把探针源 IP 注册进控制器的 ARP 代理表。

        主机查不到探针 IP 的 MAC 时会发 ARP 请求，控制器代理解答并告知探针
        MAC，主机随后把 ICMP 回包直接发给这个 MAC。
        """
        ip = self._probe_ip_for(host_ip)
        if ip is None:
            return None
        if ip not in self._probe_ips:
            self._probe_ips[ip] = PROBE_MAC
            self.app.ip_to_mac[ip] = PROBE_MAC
        return ip

    def on_switch_leave(self, dpid):
        """交换机离线：标记它的收集流需要重装。

        交换机重连时 OVS 会清空流表，之前下发的「回包收集流」随之消失，
        不清这个标记就再也不会重装，探针会退化成「回包被泛洪」的模式。
        """
        self._flows_installed.discard(dpid)

    def _ensure_collector_flows(self):
        """在所有交换机上装「dst MAC = 探针 MAC → 送控制器」的流表。

        没有它，回包会被泛洪（功能上仍能测，但会污染整个网络、放大噪声）；
        装上之后回包在最先到达的交换机（目标交换机）就交给控制器，采样点确定，
        差分法的两个 RTT 才来自同一位置。
        """
        for dpid, datapath in self.app.datapaths.items():
            if dpid in self._flows_installed:
                continue
            parser = datapath.ofproto_parser
            ofproto = datapath.ofproto
            match = parser.OFPMatch(eth_dst=PROBE_MAC)
            actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER)]
            try:
                self.app.add_flow(datapath, COLLECTOR_FLOW_PRIORITY, match, actions)
                self._flows_installed.add(dpid)
            except Exception as e:  # noqa: BLE001 - 装不上就退化成泛洪，功能仍可用
                self.app.logger.warning(f"[Probe] 收集流下发失败 SW:{self.app.sw_name(dpid)}: {e}")

    # ==================== 探测调度 ====================

    def loop(self):
        """周期探测循环（eventlet 绿线程）。"""
        while True:
            try:
                self.run_round()
            except Exception as e:  # noqa: BLE001 - 探测失败不应影响控制器主流程
                self.app.logger.warning(f"[Probe] 探测轮次失败: {e}")
            hub.sleep(PROBE_INTERVAL)

    def run_round(self):
        """一轮探测：对每条可测物理链路打两次探针（基线 + 测量）。"""
        self._ensure_collector_flows()
        self._round += 1
        self._expire_pending()
        self._expire_rtts()
        for link_key, (inject_dpid, inject_port, host) in self._collect_links().items():
            # 新链路的第一轮标记为预热：主机第一次给探针 IP 做 ARP 解析，
            # 回包因此偏慢，这一轮的结果要丢掉。
            # `_probed` 记录「这条链路探测过」，与 `_warmup`（当前这轮是预热）
            # 分开，否则每轮都会被当成预热、永远出不了结果。
            if link_key not in self._probed:
                self._probed.add(link_key)
                self._warmup.add(link_key)
            # 基线：在目标交换机上注入、发给它自己的主机 → 2×d(目标交换机,主机)
            self._send(host['dpid'], host['port'], host, link_key, 'base')
            # 测量：在对端交换机注入、发同一个主机 → 多出一整个 d(链路)
            self._send(inject_dpid, inject_port, host, link_key, 'meas')

    def _expire_rtts(self):
        """只保留最近两轮的 RTT，避免历史样本无限堆积。"""
        cutoff = self._round - 1
        for key in [k for k in self._rtts if k[2] < cutoff]:
            del self._rtts[key]

    def _expire_pending(self):
        """清理超时未回包的探测记录，避免表无限增长。"""
        deadline = time.monotonic() - PROBE_TIMEOUT
        if self._pending:
            for key in [k for k, v in self._pending.items() if v[0] < deadline]:
                del self._pending[key]
        # 已消费副本的记录也只留一小段时间
        if self._consumed:
            for key in [k for k, t in self._consumed.items() if t < deadline]:
                del self._consumed[key]

    def _collect_links(self):
        """物理链路 → (注入交换机 dpid, 注入端口, 目标主机)。

        目标交换机必须有主机（探针要有回包对象）。优先把「有主机的一端」当
        目标端，注入点选另一端——这样一条链路的两个方向都能测，且注入点
        不要求挂主机。
        """
        hosts_by_dpid = {}
        for mac, h in self.app.hosts.items():
            if h.get('ip') and h.get('dpid') is not None:
                hosts_by_dpid.setdefault(h['dpid'], []).append({
                    'mac': mac,
                    'ip': h['ip'],
                    'port': h.get('port'),
                    'dpid': h['dpid'],
                })

        # 物理链路 = 无向边。图里两个方向都有，用 (min,max) 归一
        phys = {}
        for a, b, attrs in self.app.graph.edges(data=True):
            key = (a, b) if a < b else (b, a)
            e = phys.get(key)
            if e is None:
                e = phys[key] = {'a': key[0], 'b': key[1]}
            # 有向边 (a,b) 的 src_port 是 a 侧端口，dst_port 是 b 侧端口
            if a < b:
                e['port_a'] = attrs.get('src_port')
                e['port_b'] = attrs.get('dst_port')
            else:
                e['port_b'] = attrs.get('src_port')
                e['port_a'] = attrs.get('dst_port')

        out = {}
        for key, e in phys.items():
            ha = hosts_by_dpid.get(e['a'])
            hb = hosts_by_dpid.get(e['b'])
            if hb:
                # 目标端 = b（有主机），注入点 = a
                out[key] = (e['a'], e['port_a'], hb[0])
            elif ha:
                # 反向：目标端 = a，注入点 = b
                out[key] = (e['b'], e['port_b'], ha[0])
        return out

    def _send(self, dpid, out_port, host, link_key, kind):
        """发一个 ICMP 探针。

        dpid/out_port 是注入点（报文从这个交换机的这个端口直接进入数据面，
        不会经过控制器到该交换机之间的任何链路——packet-out 是直接注入的）。
        """
        datapath = self.app.datapaths.get(dpid)
        if datapath is None or not out_port or not host.get('ip'):
            return

        probe_ip = self._register_probe_ip(host['ip'])
        if probe_ip is None:
            return

        parser = datapath.ofproto_parser
        ofproto = datapath.ofproto

        pkey = (link_key, kind)
        pid = self._probe_ids.get(pkey)
        if pid is None:
            pid = len(self._probe_ids) & 0xffff
            self._probe_ids[pkey] = pid
        self._seq = (self._seq + 1) & 0xffff
        seq = self._seq

        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(
            src=PROBE_MAC, dst=host['mac'], ethertype=ether.ETH_TYPE_IP))
        pkt.add_protocol(ipv4.ipv4(
            src=probe_ip, dst=host['ip'], proto=ICMP_PROTO))
        pkt.add_protocol(icmp.icmp(
            type_=icmp.ICMP_ECHO_REQUEST,
            data=icmp.echo(id_=pid, seq=seq, data=b'')))
        # ryu 的 Packet.serialize() 不返回值，序列化结果放在 pkt.data
        pkt.serialize()
        data = pkt.data

        self._pending[(pid, seq)] = (time.monotonic(), link_key, kind, self._round)

        actions = [parser.OFPActionOutput(out_port)]
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER, actions=actions, data=data)
        datapath.send_msg(out)

    # ==================== 回包处理 ====================

    def handle_packet(self, icmp_pkt):
        """识别探针报文（请求或回包）。

        返回 True 表示这是探针报文、已被消费，调用方不要再走常规转发逻辑。
        """
        echo = icmp_pkt.data
        if not isinstance(echo, icmp.echo):
            return False
        key = (echo.id, echo.seq)
        entry = self._pending.get(key)
        if entry is None:
            # 已经不是待回包状态：可能是同一回包的其它副本（收集流没装上时
            # 会被泛洪到各交换机）。这类副本必须安静地吃掉——若放行，会走进
            # 常规转发逻辑，触发一次无意义的 ARP 查询 + 全网泛洪。
            if key in self._consumed:
                return True
            # 也不是探针的流量（正常的 ICMP 业务），交回常规处理
            return False
        if icmp_pkt.type != icmp.ICMP_ECHO_REPLY:
            # 请求的副本（交换机不认识目标主机 MAC 时会泛洪回来），直接丢弃
            return True
        sent_at, link_key, kind, rnd = self._pending.pop(key)
        self._consumed[key] = time.monotonic()
        rtt_ms = (time.monotonic() - sent_at) * 1000.0
        if rtt_ms > PROBE_TIMEOUT * 1000:
            return True
        self._rtts[(link_key, kind, rnd)] = rtt_ms
        self._update_delay(link_key, rnd)
        return True

    def _update_delay(self, link_key, rnd):
        """基线与测量都到齐后算出链路时延，并做滑动平均。"""
        base = self._rtts.get((link_key, 'base', rnd))
        meas = self._rtts.get((link_key, 'meas', rnd))
        if base is None or meas is None:
            return
        delay = meas - base
        # 差值应为正且在合理范围内；负值说明两次探测的路径不一致（异常）
        if delay < 0 or delay > 5000:
            return
        if link_key in self._warmup:
            # 首轮含 ARP 解析开销（主机第一次要给探针 IP 做 ARP 解析，
            # 回包因此变慢），这一轮丢弃，下一轮才是干净值
            self._warmup.discard(link_key)
            return
        res = self._results.setdefault(link_key, {'samples': []})
        res['samples'].append(delay)
        if len(res['samples']) > PROBE_SMOOTH:
            res['samples'].pop(0)
        res['delay_ms'] = sum(res['samples']) / len(res['samples'])
        res['updated'] = time.time()

    # ==================== 对外接口 ====================

    def get_delay(self, src, dst):
        """取某条链路的实测时延（ms）；没有样本时返回 None。

        物理链路两个方向共用同一个测量值（netem 两端对称配置）。
        """
        key = (src, dst) if src < dst else (dst, src)
        res = self._results.get(key)
        if not res:
            return None
        return res.get('delay_ms')
