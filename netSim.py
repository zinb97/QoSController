"""
Mininet Topology Generator - 重构优化版
功能：双交换机拓扑、环形拓扑、中国电信骨干网拓扑
优化点：DPID规范、设备名简化、层级仿真控制、命令行参数增强
"""
import random
from enum import Enum
from functools import partial

from mininet.cli import CLI
from mininet.link import TCLink, Intf
from mininet.log import setLogLevel, info
from mininet.net import Mininet
from mininet.node import OVSSwitch
from mininet.node import RemoteController
from mininet.topo import Topo

# 自定义CLI提示符
CLI.prompt = 'netSim> '

class NetworkLevel(Enum):
    CORE             = 1  # 骨干核心层
    AGG              = 2  # 骨干汇聚层
    BACKBONE_ACCESS  = 3  # 骨干接入层
    PROVINCE         = 4

# ==============================================================================
# 1. 基础拓扑：双交换机拓扑
# ==============================================================================
class DualSwitchTopo(Topo):
    """简单双交换机测试拓扑"""
    def build(self):
        # 主机
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')
        # 交换机
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')
        # 链路
        self.addLink(h1, s1)
        self.addLink(s1, s2)
        self.addLink(s2, h2)


# ==============================================================================
# 2. 基础拓扑：环形拓扑
# ==============================================================================
class RingTopo(Topo):
    """可自定义节点数的环形拓扑"""
    def build(self, n=6):
        switches = [self.addSwitch(f's{i}') for i in range(1, n+1)]
        # 每台交换机挂一台主机
        for i in range(n):
            host = self.addHost(f'h{i+1}', ip=f'10.0.0.{i+1}/24')
            self.addLink(switches[i], host)
        # 环形连接
        for i in range(n):
            self.addLink(switches[i], switches[(i+1) % n])


class McastTestTopo (Topo):
    def build(self):


        layer_node_counts = [2, 3, 3, 6]
        level = len(layer_node_counts)
        switches = [[] for _ in range(level)]

        for layer, count in enumerate(layer_node_counts):
            for idx in range(count):
                dpid = f'00000000{layer + 1:02x}{idx + 1:02x}0001'
                sw = self.addSwitch(f'L{layer + 1}s{idx + 1}', dpid=dpid, protocols='OpenFlow13')
                switches[layer].append(sw)

        for i in range(level - 2):
            for src in switches[i]:
                for dst in switches[i + 1]:
                    self.addLink(src, dst, bw=400, delay=f'{round(random.uniform(0, 20), 2)}ms',
                                 loss=round(random.uniform(0, 0.1), 3), cls=TCLink)

        for i, sw in enumerate(switches[-2]):
            self.addLink(sw, switches[-1][2 * i], bw=400, delay=f'{round(random.uniform(0, 20), 2)}ms',
                         loss=round(random.uniform(0, 0.1), 3), cls=TCLink)
            self.addLink(sw, switches[-1][2 * i + 1], bw=400, delay=f'{round(random.uniform(0, 20), 2)}ms',
                         loss=round(random.uniform(0, 0.1), 3), cls=TCLink)

        for i in range(layer_node_counts[-1]):
            host = self.addHost(f'h{i + 1}', ip=f'10.0.0.{i}/24')
            self.addLink(host, switches[-1][i])


# ==============================================================================
# 3. 核心拓扑：骨干网拓扑
# 功能：层级化仿真、DPID规范、名称简化、支持层级仿真控制
# ==============================================================================
class ChinaBackboneTopo(Topo):
    """
    骨干网层级拓扑（重构版）
    层级：核心层 → 汇聚层 → 接入层 → 省网核心 → 城域核心 → 城域接入
    DPID规则（16进制，8字节）：
    - 第1位：层级标识 1=核心 2=汇聚 3=骨干接入 4=省网核心 5=城域核心 6=城域接入
    - 第2~3位：区域/省份编号
    - 第4~5位：设备编号
    命名规则：极简命名，杜绝超长网卡名
    """
    def __init__(self, level=3, is_full=False, prov_depth=2, prov_width=2, **params):
        """
        :param level: 仿真层级 1=仅核心 2=核心+汇聚 3=核心+汇聚+接入 4=+省网 5=+城域
        :param is_full: True=全量23省接入 False=简化5省
        :param prov_depth: 省网树状拓扑深度（省网核心到主机层数，默认2）
        :param prov_width: 省网树状拓扑每层宽度（省网接入节点数，默认2）
        """
        self.sim_level = level
        self.is_full = is_full
        self.prov_depth = prov_depth
        self.prov_width = prov_width
        super().__init__(**params)
        self.build_topo()

    def build_topo(self):
        """拓扑构建主入口"""
        self.core = {}  # 核心节点
        self.agg = {}   # 汇聚节点
        self.acc = []   # 骨干接入节点

        info(f"\n*** [1/{self.sim_level}] 构建骨干核心层\n")
        self._build_core()
        if self.sim_level < NetworkLevel.AGG.value: return

        info(f"*** [2/{self.sim_level}] 构建骨干汇聚层\n")
        self._build_agg()
        if self.sim_level < NetworkLevel.BACKBONE_ACCESS.value: return

        info(f"*** [3/{self.sim_level}] 构建骨干接入层\n")
        self._build_access()
        if self.sim_level < NetworkLevel.PROVINCE.value: return

    def _build_core(self):
        """核心层：北京、上海、广州 全互联"""
        cities = {'bj': 1, 'sh': 2, 'gz': 3}
        for city, idx in cities.items():
            dpid = f'00000000{NetworkLevel.CORE.value:02x}{idx:02x}0001'
            self.core[city] = self.addSwitch(f'C{idx}', dpid=dpid, protocols='OpenFlow13')
        # 全互联
        self.addLink(self.core['bj'], self.core['sh'], bw=400, delay='22ms', loss=0.3, cls=TCLink)
        self.addLink(self.core['bj'], self.core['gz'], bw=400, delay='8ms', loss=0.05, cls=TCLink)
        self.addLink(self.core['sh'], self.core['gz'], bw=400, delay='5ms', loss=0.05, cls=TCLink)

        test_hosts = [self.addHost(f't{i + 1}', ip=f'10.0.0.10{i + 1}/24') for i in range(3)]
        self.addLink(self.core['bj'], test_hosts[0])
        self.addLink(self.core['sh'], test_hosts[1])
        self.addLink(self.core['gz'], test_hosts[2])

    def _build_agg(self):
        """汇聚层：西安、成都、武汉、南京、沈阳"""
        cities = {'xa': 4, 'cd': 5, 'wh': 6, 'nj': 7, 'sy': 8}
        for city, idx in cities.items():
            dpid = f'00000000{NetworkLevel.AGG.value:02x}{idx:02x}0001'
            self.agg[city] = self.addSwitch(f'A{idx}', dpid=dpid, protocols='OpenFlow13')
        # 上联核心
        self.addLink(self.agg['xa'], self.core['bj'], bw=400, delay='3ms', loss=0.1, cls=TCLink)
        self.addLink(self.agg['xa'], self.core['sh'], bw=400, delay='4ms', loss=0.1, cls=TCLink)
        self.addLink(self.agg['cd'], self.core['bj'], bw=400, delay='3ms', loss=0.1, cls=TCLink)
        self.addLink(self.agg['wh'], self.core['bj'], bw=400, delay='3ms', loss=0.1, cls=TCLink)
        self.addLink(self.agg['nj'], self.core['sh'], bw=400, delay='3ms', loss=0.1, cls=TCLink)
        self.addLink(self.agg['sy'], self.core['gz'], bw=400, delay='3ms', loss=0.1, cls=TCLink)

        t4 = self.addHost('t4', ip='10.0.0.104/24')
        self.addLink(self.agg['xa'], t4)

    def _build_access(self):
        """骨干接入层：简化5节点 / 全量23节点"""
        node_cnt = 23 if self.is_full else 6
        agg_list = list(self.agg.values())
        for i in range(1, node_cnt + 1):
            dpid = f'00000000{NetworkLevel.BACKBONE_ACCESS.value:02x}{i:02x}0001'
            sw = self.addSwitch(f'N{i}', dpid=dpid, protocols='OpenFlow13')
            self.acc.append(sw)
            # 负载均衡上联汇聚层
            self.addLink(sw, agg_list[(i-1) % len(agg_list)], bw=400, delay='3ms', loss=0.1, cls=TCLink)
            # 末端交换机挂载主机
            host = self.addHost(f'h{i}', ip=f'10.0.0.{i}/24')
            self.addLink(sw, host)


# ==============================================================================
# 拓扑注册 & 启动入口
# ==============================================================================
topos = {
    'dual': DualSwitchTopo,
    'ring': RingTopo,
    'chinanet': ChinaBackboneTopo,
    'mcast': McastTestTopo
}


def run_chinanet(level=3, is_full=False, prov_depth=3, prov_width=3):
    """启动骨干网拓扑"""
    topo = ChinaBackboneTopo(level=level, is_full=is_full, prov_depth=prov_depth, prov_width=prov_width)
    switch = partial(OVSSwitch, protocols='OpenFlow13')
    net = Mininet(topo=topo, controller=lambda n: RemoteController(n, ip='127.0.0.1'), switch=switch)

    print('bind interface')
    # Intf('enp71s0f0', node=net.switches[10])
    # Intf('enp71s0f1', node=net.switches[10])
    # Intf('enp71s0f2', node=net.switches[11])
    # Intf('enp71s0f0', node=net.acc[-1])
    # Intf('enp71s0f1', node=net.acc[-1])
    # Intf('enp71s0f2', node=net.acc[-1])

    net.start()
    info(f"\n✅ 骨干网拓扑启动完成 | 仿真层级：{level} | 全量节点：{is_full} | 省网树(深{prov_depth}x宽{prov_width})\n")
    CLI(net)
    net.stop()


def run_base(topo_name='dual'):
    """启动基础拓扑"""
    topo = topos[topo_name]()
    switch = partial(OVSSwitch, protocols='OpenFlow13')
    net = Mininet(topo=topo, controller=lambda n: RemoteController(n, ip='127.0.0.1'), switch=switch)
    net.start()
    info(f"\n✅ 拓扑 {topo_name} 启动完成\n")
    CLI(net)
    net.stop()


if __name__ == '__main__':
    import argparse
    setLogLevel('info')

    # 命令行参数解析（增强版）
    parser = argparse.ArgumentParser(description='Mininet 拓扑仿真工具')
    parser.add_argument('-t', '--type', default='net', choices=['dual', 'ring', 'net', 'mcast'], help='拓扑类型')
    parser.add_argument('-l', '--level', type=int, default=3, help='net仿真层级 1~5')
    parser.add_argument('-f', '--full', action='store_true', help='启用全量23省接入节点')
    parser.add_argument('--prov-depth', type=int, default=2, help='省网树状拓扑深度')
    parser.add_argument('--prov-width', type=int, default=2, help='省网树状拓扑每层宽度')
    args = parser.parse_args()

    # 启动对应拓扑
    if args.type == 'net':
        run_chinanet(level=args.level, is_full=args.full, prov_depth=args.prov_depth, prov_width=args.prov_width)
    else:
        run_base(args.type)