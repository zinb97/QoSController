"""算法层单元测试（不依赖 Mininet / RYU）。

运行：
    cd QoSController && /usr/bin/python3.8 -m unittest tests.test_algo -v
"""
import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import networkx as nx                    # noqa: E402
from algo import predict as P            # noqa: E402
from algo import routing as R            # noqa: E402


def diamond(used_short=800, used_long=20):
    """1-2-4：低时延但高负载；1-3-4：高时延但空闲"""
    g = nx.DiGraph()
    for u, v, d, l, used in ((1, 2, 5, 0.1, used_short), (2, 4, 5, 0.1, used_short),
                             (1, 3, 25, 0.5, used_long), (3, 4, 25, 0.5, used_long)):
        g.add_edge(u, v, delay=d, loss=l, loss_measured=l, bw=1000,
                   used=used, oneway_bps=used, total_bps=used)
    return g


class RoutingTests(unittest.TestCase):
    def test_delay_only_prefers_low_delay(self):
        path, _ = R.best_path(diamond(), 1, 4,
                              weights={'delay': 10, 'loss': 0, 'bandwidth': 0,
                                       'load': 0, 'compute': 0})
        self.assertEqual(path, [1, 2, 4])

    def test_load_weight_avoids_congested_link(self):
        path, _ = R.best_path(diamond(), 1, 4,
                              weights={'delay': 0, 'loss': 0, 'bandwidth': 0.5,
                                       'load': 5, 'compute': 0})
        self.assertEqual(path, [1, 3, 4])

    def test_compute_penalty_avoids_saturated_node(self):
        path, _ = R.best_path(diamond(), 1, 4,
                              weights={'delay': 1, 'loss': 0, 'bandwidth': 0,
                                       'load': 0, 'compute': 3},
                              node_penalty={2: 1.0})
        self.assertEqual(path, [1, 3, 4])

    def test_boundaries(self):
        g = diamond()
        self.assertEqual(R.best_path(g, 1, 1)[0], [1])
        self.assertEqual(R.best_path(g, 1, 99)[0], [])
        self.assertEqual(R.best_path(g, 4, 1)[0], [])   # 无反向边

    def test_sanitize_weights_rejects_bad_values(self):
        w = R.sanitize_weights({'delay': 'x', 'loss': -1, 'bogus': 9})
        self.assertEqual(w, R.DEFAULT_WEIGHTS)
        # 全零会退化，必须回退默认
        zero = {k: 0 for k in R.WEIGHT_KEYS}
        self.assertEqual(R.sanitize_weights(zero), R.DEFAULT_WEIGHTS)

    def test_explain_breakdown_sums_to_total(self):
        ex = R.explain_path(diamond(), [1, 3, 4])
        self.assertAlmostEqual(sum(ex['breakdown'].values()), ex['total'], places=4)
        self.assertAlmostEqual(sum(ex['shares'].values()), 1.0, places=3)
        self.assertEqual(len(ex['hops']), 2)


class PredictTests(unittest.TestCase):
    def test_not_ready_below_three_samples(self):
        p = P.TrendPredictor()
        p.push(100)
        p.push(110)
        self.assertFalse(p.ready)
        self.assertIsNone(p.predict())

    def test_rising_trend_extrapolates_upward(self):
        p = P.TrendPredictor()
        for v in (100, 110, 120, 130):
            p.push(v)
        self.assertTrue(p.ready)
        self.assertGreater(p.predict(), 130)
        self.assertAlmostEqual(p.slope(), 10.0, places=6)

    def test_flat_series_stays_flat(self):
        p = P.TrendPredictor()
        for v in (100, 100, 100):
            p.push(v)
        self.assertAlmostEqual(p.predict(), 100.0, places=6)

    def test_bad_samples_ignored(self):
        p = P.TrendPredictor()
        for v in (50, None, 60, float('nan'), 70):
            p.push(v)
        self.assertEqual(len(p), 3)

    def test_utilization_unknown_capacity(self):
        self.assertEqual(P.utilization(80, 100), 0.8)
        self.assertIsNone(P.utilization(80, 0))
        self.assertIsNone(P.utilization(None, 100))


if __name__ == '__main__':
    unittest.main(verbosity=2)
