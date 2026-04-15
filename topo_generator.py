"""
Mininet Topology Generator
生成验证拓扑: 单的双交换机测试拓扑、Chinanet(中国电信)
"""

from functools import partial
from mininet.topo import Topo
from mininet.node import Node, OVSSwitch, Controller, RemoteController
from mininet.cli import CLI
from mininet.net import Mininet
from mininet.log import setLogLevel, info

CLI.prompt = 'netSim> '


class DualSwitchTopo(Topo):
    """简单的双交换机测试拓扑"""

    def __init__(self):
        Topo.__init__(self)

        # 添加两个主机
        h1 = self.addHost('h1', ip='10.0.0.1/24')
        h2 = self.addHost('h2', ip='10.0.0.2/24')

        # 添加交换机
        s1 = self.addSwitch('s1')
        s2 = self.addSwitch('s2')

        # 连接主机到交换机
        self.addLink(h1, s1)
        self.addLink(s1, s2)
        self.addLink(s2, h2)


class RingTopo(Topo):
    def build(self, n=10):
        # 创建 n 个交换机
        switches = []
        for i in range(1, n+1):
            sw = self.addSwitch('s%s' % i)
            switches.append(sw)

        # 创建 n 台主机，每台挂一个交换机
        for i in range(1, n+1):
            host = self.addHost('h%s' % i)
            self.addLink(switches[i-1], host)

        # 环形连接交换机：s1-s2, s2-s3... s10-s1
        for i in range(n):
            self.addLink(switches[i], switches[(i+1) % n])


class ChinaNetTopo(Topo):
    """
    Chinanet 中国电信骨干网络拓扑
    典型结构: 核心层-汇聚层-接入层
    """

    def __init__(self):
        Topo.__init__(self)

        # ========== 核心层 ==========
        c1 = self.addHost('c1', ip='10.10.0.1')
        c2 = self.addHost('c2', ip='10.10.0.2')
        c3 = self.addHost('c3', ip='10.10.0.3')
        c4 = self.addHost('c4', ip='10.10.0.4')

        # 核心层交换机
        core_sw1 = self.addSwitch('core_sw1')
        core_sw2 = self.addSwitch('core_sw2')

        # ========== 汇聚层 ==========
        agg1 = self.addSwitch('agg1')  # 华北
        agg2 = self.addSwitch('agg2')  # 华东
        agg3 = self.addSwitch('agg3')  # 华南
        agg4 = self.addSwitch('agg4')  # 西南

        # ========== 接入层 ==========
        acc1 = self.addSwitch('acc1')  # 北京
        acc2 = self.addSwitch('acc2')  # 上海
        acc3 = self.addSwitch('acc3')  # 广州
        acc4 = self.addSwitch('acc4')  # 成都
        acc5 = self.addSwitch('acc5')  # 武汉

        # 用户主机
        h1 = self.addHost('h1', ip='10.20.1.10/24')
        h2 = self.addHost('h2', ip='10.20.2.10/24')
        h3 = self.addHost('h3', ip='10.20.3.10/24')
        h4 = self.addHost('h4', ip='10.20.4.10/24')
        h5 = self.addHost('h5', ip='10.20.5.10/24')

        # ========== 核心层连接 ==========
        self.addLink(c1, core_sw1)
        self.addLink(c2, core_sw1)
        self.addLink(c3, core_sw2)
        self.addLink(c4, core_sw2)
        self.addLink(core_sw1, core_sw2)

        # ========== 汇聚层连接 ==========
        self.addLink(core_sw1, agg1)
        self.addLink(core_sw1, agg2)
        self.addLink(core_sw2, agg3)
        self.addLink(core_sw2, agg4)

        # ========== 接入层连接 ==========
        self.addLink(agg1, acc1)
        self.addLink(agg1, acc5)
        self.addLink(agg2, acc2)
        self.addLink(agg3, acc3)
        self.addLink(agg4, acc4)

        # ========== 主机连接 ==========
        self.addLink(acc1, h1)
        self.addLink(acc2, h2)
        self.addLink(acc3, h3)
        self.addLink(acc4, h4)
        self.addLink(acc5, h5)


# 拓扑注册
topos = {
    'dual': DualSwitchTopo,
    'chinanet': ChinaNetTopo,
    'ring': RingTopo
}


def run_topo(topo_name='dual'):
    """运行指定的拓扑"""
    topo = topos.get(topo_name, DualSwitchTopo)()
    # 默认使用 OpenFlow 1.3
    of13_switch = partial(OVSSwitch, protocols='OpenFlow13')
    net = Mininet(topo=topo, controller=lambda a: RemoteController(a, ip='127.0.0.1'), switch=of13_switch)
    net.start()

    info("*** 网络启动成功\n")
    info(f"*** 拓扑类型: {topo_name}\n")

    CLI(net)
    net.stop()


if __name__ == '__main__':
    import sys

    setLogLevel('info')

    if len(sys.argv) > 1:
        topo_name = sys.argv[1]
    else:
        topo_name = 'dual'

    if topo_name not in topos:
        print(f"可用拓扑: {', '.join(topos.keys())}")
        sys.exit(1)

    run_topo(topo_name)