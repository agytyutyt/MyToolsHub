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

import requests


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

    def __init__(self, upstream, address_getter, mirror, log=None):
        self._upstream = upstream
        self._addr = address_getter
        self._mirror = mirror
        self._log = log or (lambda m: None)

    def _headers(self):
        cookie = self._mirror.header()
        return {"Cookie": cookie} if cookie else {}

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
    """共享盘索引源（现状语义：index.json 与包文件同目录）。"""

    type = "share"

    def __init__(self, index_path, log=None):
        self._index_path = index_path or ""
        self._log = log or (lambda m: None)

    def get_catalog(self):
        if not self._index_path or not os.path.isfile(self._index_path):
            return None, "未配置共享盘索引或索引不存在"
        try:
            with open(self._index_path, "r", encoding="utf-8-sig") as f:
                index = json.load(f)
        except Exception as exc:
            return None, "索引不可解析：%s" % exc
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
        if not entry or not entry.get("path") or not os.path.isfile(entry["path"]):
            return None, "共享盘中找不到该包文件"
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copyfile(entry["path"], dest_path)
        return dest_path, None


def make_source(build_cfg, client_cfg, upstream, address_getter, mirror, log=None):
    """按生效配置构造源实例（每次调用读取最新配置——切源免重启）。"""
    cfg = load_source_config(build_cfg, client_cfg)
    if cfg["type"] == "share":
        return ShareSource(cfg.get("index_path") or "", log)
    return ServerSource(upstream, address_getter, mirror, log)
