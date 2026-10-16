# -*- coding: utf-8 -*-
"""更新源抽象（批次 8 T-O2-7，接缝②）：商店目录的两种来源，读取端零改动切换。

    ServerSource  默认：商店插件 API（/plugin/store/api/*，用户会话经镜像 Cookie）
    ShareSource   兜底：共享盘 index.json + 同目录包文件（离线 / 商店不可用时）

配置链（写客户端配置文件免重启）：客户端 config.json 的 `plugin_source`
    {"type": "server"} | {"type": "share", "index_path": "\\\\...\\index.json"}
缺省读构建配置 plugin_source 段，再缺省 server。读取端（preflight/install/download/
check-update）只认 get_catalog / download 两个方法。

安全口径：源是分发面，sha256 必校验（客户端对 catalog 声明值）；ShareSource 的
index.json 与共享盘同信任域（服务器域信任同级，设计文档 §3.4）。
"""

import json
import os
import shutil
import threading

import requests


#: 共享盘文件 IO 的等待上限（秒）。SMB 不可达/慢盘时 open/isfile/copyfile 会阻塞在
#: OS 层（系统默认超时可达数十秒），不设上限则「检查更新 / 安装」会长时间无响应。
#: 可用环境变量覆盖（排障用）。口径同 ServerSource 的 HTTP timeout：宁可如实报
#: 「超时 / 不可达」，也不要挂着界面。
SHARE_IO_TIMEOUT = float(os.environ.get("JZ_SHARE_IO_TIMEOUT") or 20)


def normalize_share_path(path):
    r"""共享盘路径归一化：长路径前缀 → 普通形态（UNC 两形态统一）。

    - ``\\?\UNC\server\share\...`` → ``\\server\share\...``
    - ``\\?\C:\...``                → ``C:\...``
    其余（盘符 / 映射盘 / 普通 UNC / 含空格与中文的路径）原样返回——这些由 ``os.path``
    与 ``open`` 原生支持，无需特殊处理。

    为什么要去掉 ``\\?\``：该前缀绕过 Win32 的路径规范化，与 ``os.path.abspath`` /
    ``normpath`` / ``join`` 混用时行为分叉（abspath 可能吃掉前缀或折叠成非法形态），
    故内部一律用普通形态；原始值保留在日志与报错文案里（见 ShareSource.__init__）。

    ⚠ 本函数在 `plugins/admin/backend/plugin_admin.py` 有**逐字一致的兄弟副本**
      （client 与插件分属不同代码树，且插件间禁止互相 import —— C-11）。
      改这里请同步改那边。
    """
    if not path:
        return path
    p = str(path)
    # 前缀识别在「把 / 视作路径分隔符」的副本上做；命中后一并统一分隔符，
    # 未命中前缀的路径原样返回（不做任何改写）。
    up = p.replace("/", "\\")
    if up.startswith("\\\\?\\UNC\\"):
        return "\\\\" + up[len("\\\\?\\UNC\\"):]
    if up.startswith("\\\\?\\"):
        return up[4:]
    return p


def _with_timeout(fn, timeout):
    """在独立线程里执行一次可能阻塞的文件 IO 并限时等待。

    → (True, 返回值) / (False, 异常) / (False, "timeout")。
    超时**不中断**底层 syscall（Python 无法安全打断阻塞中的 SMB 调用），线程会在自身
    返回后结束；但调用方得以即时给出「超时」文案，而不是让界面无限等待。
    """
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:          # noqa: BLE001
            box["e"] = exc

    th = threading.Thread(target=run, daemon=True, name="share-io")
    th.start()
    th.join(timeout)
    if th.is_alive():
        return False, "timeout"
    if "e" in box:
        return False, box["e"]
    return True, box.get("v")


def _io_reason(what, path, err, timeout):
    """把共享盘 IO 失败收敛成四类可区分文案（未配置由调用方单独判定）。"""
    if err == "timeout":
        return "共享盘%s读取超时（%.0fs 无响应）：%s —— 共享盘不可达或响应过慢" % (what, timeout, path)
    if isinstance(err, FileNotFoundError):
        return "共享盘%s不存在：%s" % (what, path)
    if isinstance(err, json.JSONDecodeError):
        return "共享盘%s不是合法 JSON：%s（%s）" % (what, path, err)
    if isinstance(err, OSError):
        return "共享盘%s不可达：%s（%s）" % (what, path, err)
    return "共享盘%s读取失败：%s（%s）" % (what, path, err)


def _read_index_file(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def load_source_config(build_cfg, client_cfg):
    """解析生效的源配置：客户端配置 > 构建配置 > 缺省 server。"""
    for cfg in (client_cfg.get("plugin_source") if isinstance(client_cfg, dict) else None,
                build_cfg.get("plugin_source") if isinstance(build_cfg, dict) else None):
        if isinstance(cfg, dict) and cfg.get("type") in ("server", "share"):
            return dict(cfg)
    # 旧构建配置的共享盘通道（plugin_store.index_path）映射为 share 兜底
    legacy = build_cfg.get("plugin_store") if isinstance(build_cfg, dict) else None
    if isinstance(legacy, dict) and legacy.get("index_path"):
        return {"type": "share", "index_path": legacy["index_path"]}
    return {"type": "server"}


class ServerSource:
    """商店插件 API 源：catalog / download 都带用户会话（镜像 Cookie）。"""

    type = "server"

    def __init__(self, upstream, address_getter, mirror, log=None, token_getter=None):
        self._upstream = upstream
        self._addr = address_getter
        self._mirror = mirror
        self._token = token_getter or (lambda: "")
        self._log = log or (lambda m: None)

    def _headers(self):
        cookie = self._mirror.header()
        h = {"Cookie": cookie} if cookie else {}
        # 壳标识（2026-10-11 需求① G2-2）：商店 API 属业务面，服务器壳门按此放行
        token = self._token()
        if token:
            h["X-JZ-Shell"] = token
        return h

    def get_catalog(self):
        """→ (catalog dict, None) 或 (None, error)。catalog 为商店 flat 投影。"""
        addr = self._addr()
        if not addr:
            return None, "远程服务器地址未配置"
        try:
            r = self._upstream.get(addr + "/plugin/store/api/catalog",
                                   headers=self._headers(), timeout=(5, 60))
        except requests.RequestException as exc:
            return None, "商店目录获取失败：%s" % exc
        if r.status_code == 401:
            self._mirror.clear()
            return None, "登录会话已失效，请重新登录"
        if r.status_code != 200:
            return None, "商店目录获取失败（HTTP %s）" % r.status_code
        try:
            return r.json(), None
        except Exception as exc:
            return None, "商店目录不可解析：%s" % exc

    def download(self, pid, version, dest_path, artifact=None, progress=None):
        """拉包（via=client，计入下载统计）流式落盘到 dest_path。

        artifact：server 条目双包工件选择（None/"backend"=后端包；"frontend"=前端包）。
        progress：可选回调 progress(received, total|None)——分块上报（下载进度条用）。
        → (path, None) | (None, err)
        """
        addr = self._addr()
        if not addr:
            return None, "远程服务器地址未配置"
        url = "%s/plugin/store/api/download/%s/%s?via=client" % (addr, pid, version)
        if artifact:
            url += "&artifact=" + artifact
        try:
            r = self._upstream.get(url, headers=self._headers(), stream=True, timeout=(5, 600))
        except requests.RequestException as exc:
            return None, "拉包失败：%s" % exc
        if r.status_code != 200:
            return None, "拉包失败（HTTP %s）" % r.status_code
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        total = int(r.headers.get("Content-Length") or 0) or None
        received = 0
        try:
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
                        if progress:
                            try:
                                progress(received, total)
                            except Exception:
                                pass
        except OSError as exc:
            return None, "拉包落盘失败：%s" % exc
        return dest_path, None


class ShareSource:
    r"""共享盘索引源（现状语义：index.json 与包文件同目录）。

    UNC 加固（2026-10-17）：① 路径先归一化（`\\?\UNC\` 长前缀 → 普通 UNC，见
    ``normalize_share_path``）；② 索引与包文件的 isfile/读取/复制全部**限时**
    （`SHARE_IO_TIMEOUT`，慢盘或不可达时给「超时」文案而不是挂住界面）；
    ③ 错误文案区分「未配置 / 不存在 / 不可达 / 超时 / 解析失败」。
    含空格与中文的路径无需特殊处理（os.path + open 原生支持），已有用例锁定。
    """

    type = "share"

    def __init__(self, index_path, log=None, io_timeout=None):
        self._raw_index_path = index_path or ""
        self._index_path = normalize_share_path(self._raw_index_path)
        self._log = log or (lambda m: None)
        self._timeout = SHARE_IO_TIMEOUT if io_timeout is None else float(io_timeout)
        if self._raw_index_path and self._raw_index_path != self._index_path:
            self._log("共享盘索引路径已归一化：%s → %s"
                      % (self._raw_index_path, self._index_path))

    def _isfile(self, path):
        return _with_timeout(lambda: os.path.isfile(path), self._timeout)

    def get_catalog(self):
        if not self._index_path:
            return None, "未配置共享盘索引（源类型=share 但 index_path 为空）"
        ok, val = self._isfile(self._index_path)
        if not ok:
            return None, _io_reason("索引", self._index_path, val, self._timeout)
        if not val:
            return None, "共享盘索引不存在或不可达：%s" % self._index_path
        ok, val = _with_timeout(lambda: _read_index_file(self._index_path), self._timeout)
        if not ok:
            return None, _io_reason("索引", self._index_path, val, self._timeout)
        index = val if isinstance(val, dict) else {}
        base = os.path.dirname(os.path.abspath(self._index_path))
        pkgs = []
        for raw in index.get("packages") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            item = {"id": str(raw["id"]), "runtime": "local", "requires": {},
                    "version": str(raw.get("version") or ""),
                    "file": str(raw.get("file") or ""),
                    "sha256": str(raw.get("sha256") or ""),
                    "size": raw.get("size") or 0,
                    "min_app_version": str(raw.get("min_app_version") or ""),
                    "requires_restart": bool(raw.get("requires_restart")),
                    "changelog": str(raw.get("changelog") or "")}
            rel = item["file"]
            item["path"] = os.path.join(base, *rel.replace("/", os.sep).split(os.sep)) if rel else ""
            pkgs.append(item)
        return {"schema": 1, "packages": pkgs}, None

    def download(self, pid, version, dest_path, artifact=None, progress=None):
        """共享盘 = 文件复制（sha256 由调用方对索引声明值校验）；artifact 忽略（单包形态）。"""
        catalog, err = self.get_catalog()
        if err:
            return None, err
        entry = next((p for p in catalog["packages"]
                      if p["id"] == pid and (not version or p["version"] == version)), None)
        if not entry or not entry.get("path"):
            return None, "共享盘中找不到该包文件（索引未登记 %s%s）" % (
                pid, (" v" + version) if version else "")
        ok, val = self._isfile(entry["path"])
        if not ok:
            return None, _io_reason("包文件", entry["path"], val, self._timeout)
        if not val:
            return None, "共享盘中找不到该包文件：%s" % entry["path"]
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        ok, val = _with_timeout(
            lambda: shutil.copyfile(entry["path"], dest_path), self._timeout)
        if not ok:
            return None, _io_reason("包文件", entry["path"], val, self._timeout)
        return dest_path, None


def make_source(build_cfg, client_cfg, upstream, address_getter, mirror, log=None):
    """按生效配置构造源实例（每次调用读取最新配置——切源免重启）。"""
    cfg = load_source_config(build_cfg, client_cfg)
    if cfg["type"] == "share":
        return ShareSource(cfg.get("index_path") or "", log)
    from client_config import resolve_token, load_client_config
    # 壳标识密钥每次现读（连接设置保存后免重启生效，G2-2）
    token_getter = lambda: resolve_token(build_cfg, load_client_config(build_cfg))  # noqa: E731
    return ServerSource(upstream, address_getter, mirror, log, token_getter)
