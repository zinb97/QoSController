"""链路负载短时预测（研究内容 2.4.3 / 测试用例 6.12）。

**为什么需要它**：已有的负载均衡是「当前负载高 → 立刻重算路径」，属于
事后反应。一条 80% 的链路在把业务切过去之后往往几秒内就过载，然后来回
切换（抖振）。预测的意义是把「当前」换成「下一步」，并在超过阈值**之前**
动作。

**算法选择**：EWMA（平滑当前水平）+ 最小二乘斜率（方向），外推 horizon
步。不引入 LSTM/ARIMA，理由有两条：
1. 采样周期 2–3s、窗口 10 个点（20–30s），这么短的历史拟合复杂模型只会
   过拟合噪声，参数还没热身数据就滑出窗口了；
2. 控制器跑在 Python 3.8 + 纯标准库环境，引入 numpy/sklearn 依赖会让
   部署面变大，而线性外推在这个时间尺度上的效果与 ARIMA 相差不大。

预测失败（样本不足、计数回退、全零）时显式返回 None，由调用方退回
「当前值」——**绝不拿预测值冒充实测值**。
"""
from __future__ import annotations

import math
from collections import deque


class TrendPredictor:
    """单条链路负载的预测器。

    用法：
        p = TrendPredictor(maxlen=10, alpha=0.5)
        p.push(120.0); p.push(150.0)
        nxt = p.predict()      # 样本不足时返回 None
    """

    def __init__(self, maxlen=10, alpha=0.5):
        self.maxlen = max(2, int(maxlen))
        self.alpha = min(max(float(alpha), 0.05), 1.0)
        self._history = deque(maxlen=self.maxlen)
        self._ewma = None

    def push(self, value):
        """记录一个新样本（负值按 0 处理：计数器回退时不要产生负负载）"""
        if value is None:
            return
        try:
            v = float(value)
        except (TypeError, ValueError):
            return
        if math.isnan(v) or math.isinf(v):
            return
        v = max(0.0, v)
        self._history.append(v)
        if self._ewma is None:
            self._ewma = v
        else:
            self._ewma = self.alpha * v + (1 - self.alpha) * self._ewma

    def __len__(self):
        return len(self._history)

    @property
    def ready(self):
        """至少 3 个样本才预测（少于 3 个点算不出有意义的方向）"""
        return len(self._history) >= 3

    def level(self):
        """当前平滑水平（样本不足返回最后一个样本或 None）"""
        if self._ewma is not None:
            return self._ewma
        return self._history[-1] if self._history else None

    def slope(self):
        """最小二乘斜率（每步变化量）→ float 或 None"""
        n = len(self._history)
        if n < 3:
            return None
        ys = list(self._history)
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom <= 0:
            return None
        return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom

    def predict(self, horizon=1):
        """外推 horizon 步 → float 或 None

        以 EWMA 水平为基准（比最后一个点稳），加斜率 × 步数，下限 0。
        斜率对噪声敏感，所以外部还应该在调用侧做「连续 N 次超阈值」的去抖。
        """
        if not self.ready:
            return None
        base = self.level()
        if base is None:
            return None
        s = self.slope() or 0.0
        return max(0.0, base + s * max(1, int(horizon)))

    def reset(self):
        self._history.clear()
        self._ewma = None


def utilization(load_mbps, capacity_mbps):
    """负载 / 额定 → 0~1（额定未知返回 None，调用方按「不可判断」处理）"""
    try:
        cap = float(capacity_mbps)
    except (TypeError, ValueError):
        return None
    if cap <= 0:
        return None
    try:
        load = float(load_mbps)
    except (TypeError, ValueError):
        return None
    return max(0.0, load) / cap
