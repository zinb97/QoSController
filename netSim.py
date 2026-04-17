"""
Mininet Topology Generator - 重构优化版
功能：双交换机拓扑、环形拓扑、中国电信骨干网拓扑
优化点：DPID规范、设备名简化、层级仿真控制、命令行参数增强
"""
from enum import Enum
from functools import partial
from mininet.link import TCLink
from mininet.topo import Topo
from mininet.node import OVSSwitch
from mininet.cli import CLI
from mininet.net import Mininet
from mininet.log import setLogLevel, info
from mininet.node import RemoteController

# 自定义CLI提示符
CLI.prompt = 'netSim> '

class NetworkLevel(Enum):
    CORE             = 1  # 骨干核心层
    AGG              = 2  # 骨干汇聚层
    BACKBONE_ACCESS  = 3  # 骨干接入层
    PROVINCE_CORE    = 4  # 省网核心层
    PROVINCE_ACCESS  = 5  # 省网接入层
    METRO_ACCESS     = 6  # 城域网接入层


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
    def build(self, n=10):
        switches = [self.addSwitch(f's{i}') for i in range(1, n+1)]
        # 每台交换机挂一台主机
        for i in range(n):
            host = self.addHost(f'h{i+1}', ip=f'10.0.0.{i+1}/24')
            self.addLink(switches[i], host)
        # 环形连接
        for i in range(n):
            self.addLink(switches[i], switches[(i+1) % n])


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
    def __init__(self, level=3, is_full=False, **params):
        """
        :param level: 仿真层级 1=仅核心 2=核心+汇聚 3=核心+汇聚+接入 4=+省网 5=+城域
        :param is_full: True=全量23省接入 False=简化5省
        """
        self.sim_level = level
        self.is_full = is_full
        super().__init__(**params)
        self._init_link_config()
        self.build_topo()

    def _init_link_config(self):
        """初始化各层级链路带宽/延迟/丢包配置"""
        self.link_cfg = {
            'core_core':   {'bw': 1000, 'delay': '1ms', 'loss': 0.1, 'cls': TCLink},
            'core_agg':    {'bw': 400,  'delay': '2ms', 'loss': 0.1, 'cls': TCLink},
            'agg_acc':     {'bw': 100,  'delay': '3ms', 'loss': 0.2, 'cls': TCLink},
            'acc_prov':    {'bw': 100,  'delay': '3ms', 'loss': 0.2, 'cls': TCLink},
            'prov_core':   {'bw': 100,  'delay': '1ms', 'loss': 0.1, 'cls': TCLink},
            'prov_metro':  {'bw': 100,  'delay': '2ms', 'loss': 0.2, 'cls': TCLink},
            'metro_acc':   {'bw': 50,   'delay': '3ms', 'loss': 0.3, 'cls': TCLink},
            'acc_inner':   {'bw': 50,   'delay': '1ms', 'loss': 0.1, 'cls': TCLink},
        }

    def build_topo(self):
        """拓扑构建主入口"""
        self.core = {}  # 核心节点
        self.agg = {}   # 汇聚节点
        self.acc = []   # 骨干接入节点

        info("\n*** [1/6] 构建骨干核心层\n")
        self._build_core()
        if self.sim_level < NetworkLevel.AGG.value: return

        info("*** [2/6] 构建骨干汇聚层\n")
        self._build_agg()
        if self.sim_level < NetworkLevel.BACKBONE_ACCESS.value: return

        info("*** [3/6] 构建骨干接入层\n")
        self._build_access()
        if self.sim_level < NetworkLevel.PROVINCE_CORE.value: return

        info("*** [4/6] 构建省网核心层\n")
        self._build_all_provinces()
        if self.sim_level < NetworkLevel.PROVINCE_ACCESS.value: return

        info("*** [5/6] 构建省网接入层\n")
        if self.sim_level < NetworkLevel.METRO_ACCESS.value: return

        info("*** [6/6] 构建城域网接入层\n")

    def _build_core(self):
        """核心层：北京、上海、广州 全互联"""
        cities = {'bj': 1, 'sh': 2, 'gz': 3}
        for city, idx in cities.items():
            dpid = f'00000000{NetworkLevel.CORE.value:02x}{idx:02x}0001'
            self.core[city] = self.addSwitch(f'C{idx}', dpid=dpid, protocols='OpenFlow13')
        # 全互联
        self.addLink(self.core['bj'], self.core['sh'], **self.link_cfg['core_core'])
        self.addLink(self.core['bj'], self.core['gz'], **self.link_cfg['core_core'])
        self.addLink(self.core['sh'], self.core['gz'], **self.link_cfg['core_core'])

    def _build_agg(self):
        """汇聚层：西安、成都、武汉、南京、沈阳"""
        cities = {'xa': 4, 'cd': 5, 'wh': 6, 'nj': 7, 'sy': 8}
        for city, idx in cities.items():
            dpid = f'00000000{NetworkLevel.AGG.value:02x}{idx:02x}0001'
            self.agg[city] = self.addSwitch(f'A{idx}', dpid=dpid, protocols='OpenFlow13')
        # 上联核心
        self.addLink(self.agg['xa'], self.core['bj'], **self.link_cfg['core_agg'])
        self.addLink(self.agg['xa'], self.core['sh'], **self.link_cfg['core_agg'])
        self.addLink(self.agg['cd'], self.core['bj'], **self.link_cfg['core_agg'])
        self.addLink(self.agg['wh'], self.core['bj'], **self.link_cfg['core_agg'])
        self.addLink(self.agg['nj'], self.core['sh'], **self.link_cfg['core_agg'])
        self.addLink(self.agg['sy'], self.core['gz'], **self.link_cfg['core_agg'])

    def _build_access(self):
        """骨干接入层：简化5节点 / 全量23节点"""
        node_cnt = 23 if self.is_full else 5
        agg_list = list(self.agg.values())
        for i in range(1, node_cnt + 1):
            dpid = f'00000000{NetworkLevel.BACKBONE_ACCESS.value:02x}{i:02x}0001'
            sw = self.addSwitch(f'N{i}', dpid=dpid, protocols='OpenFlow13')
            self.acc.append(sw)
            # 负载均衡上联汇聚层
            self.addLink(sw, agg_list[(i-1) % len(agg_list)], **self.link_cfg['agg_acc'])

    def _build_all_provinces(self):
        for idx, uplink in enumerate(self.acc):
            self._build_province(pid=idx+1, uplink=uplink)

    def _build_province(self, pid, uplink):
        info(f'    → 构建省份 {pid:02d}\n')
        p_core = []
        # 省网核心
        for i in [1,2]:
            dpid = f'00000000{NetworkLevel.PROVINCE_CORE.value:02x}{pid:02x}{i:02x}01'
            sw = self.addSwitch(f'P{pid}C{i}', dpid=dpid, protocols='OpenFlow13')
            p_core.append(sw)
            self.addLink(sw, uplink, **self.link_cfg['acc_prov'])
        self.addLink(p_core[0], p_core[1], **self.link_cfg['prov_core'])

        # 城域核心
        m_core = []
        for i in range(1,7):
            dpid = f'00000000{NetworkLevel.PROVINCE_ACCESS.value:02x}{pid:02x}{i:02x}01'
            sw = self.addSwitch(f'P{pid}M{i}', dpid=dpid, protocols='OpenFlow13')
            m_core.append(sw)
            self.addLink(sw, p_core[0], **self.link_cfg['prov_metro'])
            self.addLink(sw, p_core[1], **self.link_cfg['prov_metro'])

        # 测试主机
        h1 = self.addHost(f'PH{pid}_1', ip=f'10.{pid}.0.1/24')
        h2 = self.addHost(f'PH{pid}_2', ip=f'10.{pid}.0.2/24')
        self.addLink(h1, m_core[0])
        self.addLink(h2, m_core[1])


# ==============================================================================
# 拓扑注册 & 启动入口
# ==============================================================================
topos = {
    'dual': DualSwitchTopo,
    'ring': RingTopo,
    'chinanet': ChinaBackboneTopo
}


def run_chinanet(level=3, is_full=False):
    """启动骨干网拓扑"""
    topo = ChinaBackboneTopo(level=level, is_full=is_full)
    switch = partial(OVSSwitch, protocols='OpenFlow13')
    net = Mininet(topo=topo, controller=lambda n: RemoteController(n, ip='127.0.0.1'), switch=switch)
    net.start()
    info(f"\n✅ 骨干网拓扑启动完成 | 仿真层级：{level} | 全量节点：{is_full}\n")
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
    parser.add_argument('-t', '--type', default='dual', choices=['dual', 'ring', 'net'], help='拓扑类型')
    parser.add_argument('-l', '--level', type=int, default=3, help='net仿真层级 1~5')
    parser.add_argument('-f', '--full', action='store_true', help='启用全量23省接入节点')
    args = parser.parse_args()

    # 启动对应拓扑
    if args.type == 'net':
        run_chinanet(level=args.level, is_full=args.full)
    else:
        run_base(args.type)