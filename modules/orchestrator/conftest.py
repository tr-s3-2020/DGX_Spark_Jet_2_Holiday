"""让 tests/ 无论在哪个目录被 pytest 调用都能 import orchestrator。

pytest 默认只把 tests/ 自己插进 sys.path，而 orchestrator.py 在上一级。
放在这里的 conftest.py 会在收集测试模块之前被加载，所以能提前补好路径——
否则只有 cwd 恰好是本目录时才能跑通。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
