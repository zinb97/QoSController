#!/usr/bin/env bash
# 离线回归：不需要 Mininet、不需要 sudo、不需要真实交换机。
# 用法：cd QoSController && ./tests/run_all.sh
set -u

PY=${PY:-/usr/bin/python3.8}   # 控制器实际运行版本，不能用系统默认 3.10
cd "$(dirname "$0")/.." || exit 1

echo "== 语法检查（py3.8）=="
"$PY" -m py_compile QoSController.py utils.py controllers/*.py || exit 1

echo
echo "== 算法包单测 =="
"$PY" -m unittest tests.test_algo -v || exit 1

echo
echo "== tc 写入逻辑测试 =="
"$PY" -m unittest tests.test_utils_tc -v || exit 1

echo
echo "== 控制器行为测试（假 datapath）=="
"$PY" -m unittest tests.test_controller_offline -v || exit 1

echo
echo "全部通过。联调步骤见 tests/README.md"
