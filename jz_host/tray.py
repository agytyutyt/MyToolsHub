"""系统托盘与启动通知（仅打包运行）。

从 app.py 拆出（2026-10-10 P2-4 / 报告 §4.4「托盘」块）。
"""
import logging
import os

log = logging.getLogger("jztools.app")

# 托盘图标对象引用：notify_service_started 需跨线程经它弹系统气泡通知
_tray_icon = None


def _tray_icon_image():
    """生成托盘图标位图：64x64 主题蓝底 + 白色「JZ」。"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), "#4285F4")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((2, 2, 62, 62), radius=14, fill="#4285F4")
    draw.rectangle((0, 0, 63, 63), outline="#3367D6", width=2)
    draw.text((14, 12), "JZ", fill="white", font=None)
    return img


def run_tray_icon(host, port):
    """在后台线程运行系统托盘图标（仅打包运行，PyInstaller console=False）。

    - 左键双击：打开默认浏览器访问本站；
    - 右键菜单：打开浏览器 / 退出服务（退出即结束进程）。
    """
    import webbrowser
    import pystray

    def _open_browser():
        url = "http://127.0.0.1:%d" % port if host in ("0.0.0.0", "::") else "http://%s:%d" % (host, port)
        try:
            webbrowser.open(url)
        except Exception:
            log.debug("托盘「打开浏览器」失败（不阻断）", exc_info=True)

    def _quit():
        # 打包运行的 GUI 程序：直接结束进程即可（waitress 由 OS 回收）
        os._exit(0)

    try:
        menu = pystray.Menu(
            pystray.MenuItem("打开浏览器", lambda icon, item: _open_browser()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出服务", lambda icon, item: _quit()),
        )
        icon = pystray.Icon("JZToolsHub", _tray_icon_image(), "JZ 工具箱", menu)
        global _tray_icon
        _tray_icon = icon
        icon.run()
    except Exception:
        # 托盘启动失败不阻断服务（后台静默运行），但要留痕——否则真机上"没有托盘图标"
        # 这一现象完全无从排查（报告 §5.3 点名的"无日志"）。
        log.warning("系统托盘启动失败（服务仍正常，后台运行）", exc_info=True)


def notify_service_started(host, port):
    """服务真正开始监听后，经托盘图标弹系统级通知（气泡；Win10/11 渲染为 toast）。

    在后台线程运行：轮询本机端口直到可连接，再 notify()。服务每次启动（含
    插件升级「自动重启」拉起的新实例）都会触发，管理员可据此确认重启成功。
    仅打包运行有效——源码模式没有托盘图标，本函数也不会被调用。
    """
    import socket
    import time

    url = ("http://127.0.0.1:%d" % port) if host in ("0.0.0.0", "::") \
        else "http://%s:%d" % (host, port)
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.3)
    else:
        return  # 15 秒内端口未就绪（如启动失败），不发通知
    icon = _tray_icon
    if icon is None:
        return  # 托盘未起来（如 pystray 初始化失败），静默放弃
    for _ in range(5):
        try:
            icon.notify("服务已启动：%s" % url, "JZ 工具箱")
            return
        except Exception:
            time.sleep(1.0)  # 托盘窗口可能尚未就绪，短暂重试
