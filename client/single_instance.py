# -*- coding: utf-8 -*-
"""单实例互斥（批次 1 T08）：命名 mutex；第二实例直接退出（窗口激活在批次 2 T10 接入）。"""
import ctypes
import sys


def acquire_or_exit(mutex_name):
    """成功返回 True；已有实例在跑则返回 False（调用方应退出）。"""
    if not sys.platform.startswith("win"):
        return True
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\" + mutex_name)
    # ERROR_ALREADY_EXISTS = 183
    if ctypes.windll.kernel32.GetLastError() == 183:
        return False
    # 句柄常驻进程生命周期即可，无需关闭
    return True
