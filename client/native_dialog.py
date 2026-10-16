# -*- coding: utf-8 -*-
"""原生目录选择对话框（能力层 /local-api/fs/pick-dir 的支撑件）。

为什么不用 pywebview 的 window.create_file_dialog：vendored 实现把 ShowDialog
直接跑在调用线程上、无线程封送，从 Flask 工作线程调用会因非 STA 线程抛错并被
其内部 except 吞掉（表现=静默取消）。这里自起托管 STA 线程承载对话框，与壳
窗口线程解耦，宿主是否同进程都能工作。

台架可用环境变量 JZCLIENT_NO_DIALOG=1 让本模块直接报"对话框不可用"，
避免无头环境真的弹窗挂住测试。
"""
import os
import sys


class DialogUnavailable(Exception):
    """当前环境无法弹出原生对话框（非 Windows / pythonnet 缺失 / 台架禁用）。"""


def pick_folder(description, seed=None):
    """弹出目录选择对话框，返回所选绝对路径；用户取消返回 None。

    seed：初始定位目录（不存在则忽略）。阻塞至对话框关闭——本地端口是单用户
    场景，Flask 工作线程被占住是可接受的。
    """
    if os.environ.get("JZCLIENT_NO_DIALOG"):
        raise DialogUnavailable("对话框已按 JZCLIENT_NO_DIALOG 禁用")
    if sys.platform != "win32":
        raise DialogUnavailable("仅 Windows 支持原生对话框")
    try:
        import clr                                  # noqa: F401  pythonnet 初始化
        from System.Threading import ApartmentState, Thread, ThreadStart
        from System.Windows.Forms import DialogResult, FolderBrowserDialog
    except Exception as exc:
        raise DialogUnavailable("pythonnet 不可用：%s" % exc)

    result = {"path": None}

    def _run():
        dlg = FolderBrowserDialog()
        dlg.Description = description or "请选择目录"
        if seed and os.path.isdir(seed):
            dlg.SelectedPath = seed
        if dlg.ShowDialog() == DialogResult.OK:
            result["path"] = dlg.SelectedPath

    t = Thread(ThreadStart(_run))
    t.SetApartmentState(ApartmentState.STA)
    t.Start()
    t.Join()          # 保持托管线程引用到结束，防 GC 回收包装对象
    return result["path"]
