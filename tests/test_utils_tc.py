"""utils 的 tc 写入逻辑测试（不真的改网卡）。

钉住两件事：
1. 替换的是 netem **子队列**（parent 5:1 / handle 10:），不是 root——
   用 root 会把 TCLink 建的 htb 带宽层级冲掉。
2. 权限不足时自动尝试 `sudo -n`，仍失败则给出可操作的提示，而不是
   只抛一个 "exit 2"。

运行：cd QoSController && /usr/bin/python3.8 -m unittest tests.test_utils_tc -v
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_QC_DIR = os.path.dirname(_HERE)
if _QC_DIR not in sys.path:
    sys.path.insert(0, _QC_DIR)

import utils  # noqa: E402

QDISC_TREE = """qdisc htb 5: root refcnt 81 r2q 10 default 0x1 direct_packets_stat 0
qdisc netem 10: parent 5:1 limit 1000 delay 1ms
"""

HTB_ONLY = "qdisc htb 5: root refcnt 81 r2q 10 default 0x1\n"


class NetemTargetTests(unittest.TestCase):
    def setUp(self):
        self._orig = utils._run_tc

    def tearDown(self):
        utils._run_tc = self._orig

    def test_parses_existing_netem_child(self):
        utils._run_tc = lambda argv, timeout=3: (True, QDISC_TREE)
        self.assertEqual(utils._netem_qdisc_args('X-eth1'),
                         ['parent', '5:1', 'handle', '10:'])

    def test_falls_back_to_htb_class_when_no_netem(self):
        utils._run_tc = lambda argv, timeout=3: (True, HTB_ONLY)
        self.assertEqual(utils._netem_qdisc_args('X-eth1'),
                         ['parent', '5:1', 'handle', '10:'])

    def test_uses_root_only_when_nothing_else(self):
        utils._run_tc = lambda argv, timeout=3: (True, 'qdisc pfifo_fast 0: root\n')
        self.assertEqual(utils._netem_qdisc_args('X-eth1'),
                         ['root', 'handle', '10:'])


class SetNetemArgvTests(unittest.TestCase):
    def setUp(self):
        self._orig_run = utils._run_tc
        self._orig_args = utils._netem_qdisc_args
        self.calls = []
        utils._netem_qdisc_args = lambda dev: ['parent', '5:1', 'handle', '10:']
        utils._run_tc = lambda argv, timeout=3: (self.calls.append(list(argv)) or (True, ''))

    def tearDown(self):
        utils._run_tc = self._orig_run
        utils._netem_qdisc_args = self._orig_args

    def test_replaces_child_not_root(self):
        utils.set_netem('X-eth1', delay_ms=80.0, loss_pct=1.0)
        argv = self.calls[-1]
        self.assertNotIn('root', argv)
        self.assertEqual(argv[argv.index('parent') + 1], '5:1')
        self.assertIn('80.0ms', argv)
        self.assertIn('1.0%', argv)

    def test_clear_keeps_hierarchy(self):
        utils.clear_netem('X-eth1')
        argv = self.calls[-1]
        self.assertNotIn('root', argv)
        self.assertNotIn('delay', argv)
        self.assertIn('parent', argv)


class PermissionRetryTests(unittest.TestCase):
    def setUp(self):
        self._orig_once = utils._run_tc_once
        self.seen = []

    def tearDown(self):
        utils._run_tc_once = self._orig_once

    def test_retries_with_sudo_then_succeeds(self):
        def fake(argv, timeout=3):
            self.seen.append(list(argv))
            if argv[:1] == ['sudo']:
                return True, ''
            return False, 'RTNETLINK answers: Operation not permitted'
        utils._run_tc_once = fake
        ok, err = utils._run_tc(['tc', 'qdisc', 'replace'])
        self.assertTrue(ok)
        self.assertEqual(err, '')
        self.assertEqual(self.seen[0], ['tc', 'qdisc', 'replace'])
        self.assertEqual(self.seen[1], ['sudo', '-n', 'tc', 'qdisc', 'replace'])

    def test_both_fail_returns_actionable_hint(self):
        def fake(argv, timeout=3):
            if argv[:1] == ['sudo']:
                return False, 'sudo: a password is required'
            return False, 'RTNETLINK answers: Operation not permitted'
        utils._run_tc_once = fake
        ok, err = utils._run_tc(['tc', 'qdisc', 'replace'])
        self.assertFalse(ok)
        self.assertIn('root', err)
        self.assertIn('NOPASSWD', err)

    def test_non_permission_error_not_retried(self):
        calls = []
        utils._run_tc_once = lambda argv, timeout=3: (calls.append(argv) or (False, 'bad argument'))
        ok, err = utils._run_tc(['tc', 'qdisc', 'replace'])
        self.assertFalse(ok)
        self.assertEqual(err, 'bad argument')
        self.assertEqual(len(calls), 1)


class EventletStyleErrorTests(unittest.TestCase):
    """RYU 用 eventlet monkey-patch 过 subprocess，抛出的异常不是
    subprocess.CalledProcessError（isinstance 为 False），但带 output 属性。
    必须仍能提取出 stderr，否则认不出权限错误、也不会触发 sudo 重试。
    """

    def test_foreign_exception_with_output_attribute(self):
        import types
        orig = utils.subprocess

        class ForeignError(Exception):
            def __init__(self):
                self.output = 'RTNETLINK answers: Operation not permitted'
                self.returncode = 2

        def boom(*args, **kwargs):
            raise ForeignError()

        utils.subprocess = types.SimpleNamespace(
            check_output=boom, STDOUT=orig.STDOUT, CalledProcessError=orig.CalledProcessError)
        try:
            ok, err = utils._run_tc_once(['tc', 'qdisc'])
        finally:
            utils.subprocess = orig
        self.assertFalse(ok)
        self.assertIn('Operation not permitted', err)

    def test_foreign_exception_without_output_falls_back_to_returncode(self):
        import types
        orig = utils.subprocess

        class ForeignError(Exception):
            def __init__(self):
                self.output = None
                self.returncode = 7

        utils.subprocess = types.SimpleNamespace(
            check_output=lambda *a, **k: (_ for _ in ()).throw(ForeignError()),
            STDOUT=orig.STDOUT, CalledProcessError=orig.CalledProcessError)
        try:
            ok, err = utils._run_tc_once(['tc'])
        finally:
            utils.subprocess = orig
        self.assertFalse(ok)
        self.assertEqual(err, 'exit 7')


if __name__ == '__main__':
    unittest.main(verbosity=2)
