# -*- coding: utf-8 -*-
r"""商店页状态机台架包装（2026-10-13 需求①②）：转调 node 跑真实 store.js。

真身：tools/e2e/store-page-state-tests.js（最小 DOM + fetch 路由桩，直跑前端文件——
依赖页签按钮三态、「待重启」定格、卸载进度条与 progress_id 轮询）。

无 node 环境时**跳过**（退出 0，不误报失败）：前端状态机在真机验收里还要走一遍，
本台架是开发机上的快速回归面。

用法：python tools\e2e\store-page-state-tests.py   （退出码 0=全过/跳过）
"""
import os
import shutil
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, "store-page-state-tests.js")


def main():
    node = shutil.which("node")
    if not node:
        print("跳过：未找到 node（前端状态机台架仅开发机跑）")
        return 0
    r = subprocess.run([node, JS], cwd=os.path.dirname(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    sys.stdout.write(r.stdout or "")
    sys.stderr.write(r.stderr or "")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
