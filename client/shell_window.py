# -*- coding: utf-8 -*-
"""UI 层（批次 2 T10–T12）：pywebview 窗口 + 托盘 + 几何记忆 + 关窗隐藏 + 导航锁定。

pywebview 6.2.1 源码侦察结论（2026-10-03，实现依据）：
- closing 事件可取消：handler 返回 False → winforms args.Cancel=True（关窗=隐藏的落点）；
- confirm_close 默认 False，关窗无确认框；
- OPEN_EXTERNAL_LINKS_IN_BROWSER 默认 True：target=_blank/NewWindow 已转系统浏览器；
- WEBVIEW2_RUNTIME_PATH 设置项原生支持 fixed 内核目录（存在才启用，否则系统常青）；
- storage_path + private_mode=False：WebView2 用户数据目录落客户端数据目录，会话跨重启持久。

窗口尺寸契约：最小 720×560（方案 §4.0.1）；几何记忆防抖落盘到**数据目录**（不落程序目录，T11）。
"""
import json
import os
import sys
import threading
import traceback
import webbrowser

import pystray
import webview
from PIL import Image, ImageDraw

MIN_SIZE = (720, 560)
INIT_SIZE = (1024, 700)


def webview_escape_param(value):
    """URL 查询参数转义（仅 run token 场景；标准库 urllib.parse，零新依赖）。"""
    from urllib.parse import quote
    return quote(str(value), safe="")


class GeometryStore:
    """resize/move 300ms 防抖落盘（T01 spike 已验证钳制与恢复口径）。"""

    def __init__(self, path, log):
        self.path, self._log = path, log
        self._timer = None
        self._state = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def update(self, **kv):
        self._state.update(kv)
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(0.3, self._flush)
        self._timer.daemon = True
        self._timer.start()

    def _flush(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
            self._log("geometry saved: %s" % json.dumps(self._state))
        except Exception:
            self._log("geometry save failed:\n" + traceback.format_exc())

    @property
    def start_geom(self):
        s = self._state
        g = {k: s[k] for k in ("width", "height", "x", "y") if k in s}
        g.setdefault("width", INIT_SIZE[0])
        g.setdefault("height", INIT_SIZE[1])
        return _clamp_to_screen(g, self._log)


def _virtual_screen():
    """主屏+扩展屏的虚拟桌面范围（ctypes GetSystemMetrics，零依赖）；取不到返回 None。"""
    try:
        import ctypes
        u = ctypes.windll.user32
        x, y = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        w, h = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if w > 0 and h > 0:
            return int(x), int(y), int(w), int(h)
    except Exception:
        pass
    return None


def _clamp_to_screen(g, log):
    """几何恢复的屏幕内钳制：显示器拓扑变化或保存过最小化坐标（Win32 离屏值）会把
    窗口还原到屏幕外——任务栏可见而窗口不可见（2026-10-07 真机报告，实测 x/y=-25600）。
    策略：宽高钳到虚拟桌面；x/y 钳回边界内，保证标题栏至少 120×40 可见。"""
    vs = _virtual_screen()
    if not vs:
        return g
    vx, vy, vw, vh = vs
    try:
        g["width"] = max(480, min(int(g.get("width", INIT_SIZE[0])), vw))
        g["height"] = max(360, min(int(g.get("height", INIT_SIZE[1])), vh))
        if "x" in g and "y" in g:
            g["x"] = min(max(int(g["x"]), vx), vx + vw - 120)
            g["y"] = min(max(int(g["y"]), vy), vy + vh - 40)
    except (TypeError, ValueError):
        g.pop("x", None); g.pop("y", None)
    log("start geometry（屏幕内钳制后）: %s" % json.dumps(g))
    return g


class ShellBridge:
    """window.jzShell 后端：至多三项 version/engine/openExternal（方案 §7.3）。"""

    def __init__(self, build_cfg, log):
        self._cfg, self._log = build_cfg, log

    def version(self):
        return (self._cfg.get("product") or {}).get("version", "")

    def engine(self):
        p = self._cfg.get("product") or {}
        return "%s %s (pywebview/WebView2)" % (p.get("name", "JZClient"), p.get("version", ""))

    def openExternal(self, url):
        webbrowser.open(url)
        self._log("openExternal: %s" % url)
        return True


def _tray_image():
    img = Image.new("RGB", (64, 64), "#1a73e8")
    d = ImageDraw.Draw(img)
    d.ellipse((12, 12, 52, 52), fill="#ffffff")
    d.ellipse((24, 24, 40, 40), fill="#1a73e8")
    return img


def run_shell(build_cfg, client_cfg, port, data_dir, log, shell_token=""):
    """开窗并阻塞主线程（webview.start 占主线程；能力层 waitress 已在后台线程）。

    shell_token：本地端口壳自证 run token（client_main 同值传能力层，G2-6）——
    首导航经 /shell-boot?_shell=<token> 种 Cookie 后再 302 进主页面。
    """
    from client_config import webview2_runtime_dir, geometry_path, webview2_profile_dir

    product = build_cfg.get("product") or {}
    display = product.get("display_name", "JZ 工具箱")
    origin = "http://127.0.0.1:%d" % port
    # 启动引导 URL（G2-6）：先种壳自证 Cookie 再进主页；Cookie 持久化后刷新/iframe 均自带
    boot_url = origin + "/shell-boot?_shell=" + webview_escape_param(shell_token) + "&next=/"
    geo = GeometryStore(geometry_path(build_cfg), log)
    bridge = ShellBridge(build_cfg, log)

    # T11：fixed 内核（目录存在才启用，否则回落系统常青）+ 用户数据目录进客户端数据目录。
    # profile 按内核形态分离（2026-10-11 白屏修复）：Chromium 禁止低版本内核复用高版本
    # profile（数据格式降级保护）——常青 154 写过的 webview2-profile 若被 fixed 100 复用，
    # WebView2 环境创建失败 → 壳白屏（无 msedgewebview2 进程、日志无报错）。fixed 用独立
    # profile 与常青互不污染；profile 目录里混入常青数据的既有机器，首次换回常青即自愈。
    rt = webview2_runtime_dir(build_cfg)
    webview.settings["WEBVIEW2_RUNTIME_PATH"] = rt or ""
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    profile_dir = webview2_profile_dir(build_cfg) + ("-fixed" if rt else "")
    os.makedirs(profile_dir, exist_ok=True)
    log("webview2 runtime: %s" % (rt or "系统常青"))

    first_close = {"shown": bool(client_cfg.get("tray_hint_shown"))}
    window = webview.create_window(
        display, boot_url,
        js_api=bridge,
        min_size=MIN_SIZE,
        confirm_close=False,
        **geo.start_geom,
    )

    def _hide_to_tray(icon=None):
        window.hide()
        log("window hidden to tray")
        if not first_close["shown"]:
            first_close["shown"] = True
            try:
                from client_config import load_client_config, save_client_config
                cfg = dict(load_client_config(build_cfg))
                cfg["tray_hint_shown"] = True
                save_client_config(build_cfg, cfg)
                if icon:
                    icon.notify("已最小化到托盘，退出请用托盘菜单", display)
            except Exception:
                log("气泡提示失败:\n" + traceback.format_exc())

    def on_closing():
        _hide_to_tray(tray[0])
        return False                      # 返回 False → 取消关闭（GD5：隐藏到托盘）

    _RELOAD_JS = ("document.addEventListener('keydown',function(e){"
                  "if(e.key==='F5'||(e.ctrlKey&&(e.key==='r'||e.key==='R'))){"
                  "e.preventDefault();location.reload();}});")

    def on_loaded():
        # 导航锁定兜底（页面侧 shell-bridge 拦截点击/新窗；此处拦 JS 跳转漂移）
        try:
            url = window.get_current_url() or ""
            if url and not url.startswith(origin):
                log("导航漂移拦截：%s → 转系统浏览器并回主页面" % url)
                bridge.openExternal(url)
                window.load_url(origin)
        except Exception:
            log("loaded guard:\n" + traceback.format_exc())
        # WebView2 壳默认无浏览器刷新键——注入顶层 F5/Ctrl+R；焦点在 iframe 内时
        # 事件不跨 frame，由托盘「刷新页面」兜底（2026-10-07 联调需求）
        try:
            window.evaluate_js(_RELOAD_JS)
        except Exception:
            log("刷新快捷键注入失败:\n" + traceback.format_exc())

    window.events.closing += on_closing
    def _on_resized(w, h):
        # 最小化时 Win32 先报一个「最小化尺寸」（本机实测 159x27）——存了下次启动窗口
        # 只剩钳制下限（2026-10-08 真机痕迹：geometry.json 被写成 159x27）。
        # 远小于 MIN_SIZE 的一律判为最小化/异常，不落盘（move 事件侧同理已丢离屏坐标）。
        if w < 400 or h < 300:
            log("resized 事件上报异常尺寸（%dx%d），判定为最小化——不保存" % (w, h))
            return
        geo.update(width=w, height=h)

    window.events.resized += _on_resized

    def _on_moved(x, y):
        # 最小化时 Win32 会上报离屏坐标（本机实测 -25600/-25600）——存了会导致下次
        # 启动窗口开在屏幕外（任务栏可见而窗口不可见，2026-10-07 真机报告）。
        # 离屏值直接丢弃（恢复侧另有虚拟桌面钳制兜底）。
        if x < -8 or y < -8 or x > 100000 or y > 100000:
            log("moved 事件上报离屏坐标（%s, %s），判定为最小化/异常——不保存" % (x, y))
            return
        geo.update(x=x, y=y)

    window.events.moved += _on_moved
    window.events.loaded += on_loaded

    tray = [None]

    def _start_tray():
        def on_refresh(icon, item):
            # 重新加载当前 URL（HTML no-cache → 引用与 v 戳随之刷新；联调期服务端改版后用）
            try:
                url = window.get_current_url() or origin
                window.load_url(url)
                log("tray refresh: %s" % url)
            except Exception:
                log("tray refresh failed:\n" + traceback.format_exc())

        def on_open(icon, item):
            window.show()

        # 管理后台入口不对用户展示（2026-10-11 二次调整）：托盘不放后台入口，
        # 服务器端文档标注入口即可（README / 打包部署手册 §5.1）。

        def on_local_admin(icon, item):
            # 本地插件管理（原主界面悬浮入口迁入托盘，2026-10-08）：主窗口导航到本页
            try:
                window.show()
                window.load_url(origin + "/local-admin")
                log("tray -> local-admin")
            except Exception:
                log("tray local-admin failed:\n" + traceback.format_exc())

        def on_exit(icon, item):
            log("exit from tray")
            geo._flush()
            icon.stop()
            os._exit(0)                   # 同主包口径：托盘退出立即结束进程

        icon = pystray.Icon(
            product.get("name", "JZClient"), _tray_image(), display,
            menu=pystray.Menu(
                pystray.MenuItem("刷新页面", on_refresh),
                pystray.MenuItem("打开窗口", on_open, default=True),
                pystray.MenuItem("本地插件管理", on_local_admin),
                pystray.MenuItem("退出", on_exit),
            ),
        )
        tray[0] = icon
        threading.Thread(target=icon.run, daemon=True, name="tray").start()

    _start_tray()
    log("tray started")

    log("webview.start() entering")
    webview.start(
        private_mode=False,               # 会话/配置跨重启持久（WebView2 profile）
        storage_path=profile_dir,         # 按内核形态分离（fixed 独立 profile，见上注释）
        gui="edgechromium",
        debug=False,
    )
    log("webview loop ended")
