# -*- coding: utf-8 -*-
"""客户端配置层（批次 1 T09）。

地址口径（2026-10-03 拍板）：**以配置文件为准，无首启输入向导**——
构建配置给默认地址（出包定型）→ 安装时落地到客户端配置文件（可按机器改）
→ 运行时读客户端配置文件；兜底页「改地址」= 编辑该文件（POST /local-api/config/address）。
"""
import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    _RESOURCE_DIR = getattr(sys, "_MEIPASS", _BASE_DIR)
else:
    _BASE_DIR = os.path.dirname(_here)
    _RESOURCE_DIR = os.path.dirname(_here)


def _find_build_config():
    """构建配置单源：tools/client/client-build-config.json（冻结后随包进 _internal）。"""
    candidates = [
        os.path.join(_RESOURCE_DIR, "client-build-config.json"),
        os.path.join(_BASE_DIR, "client-build-config.json"),
        os.path.join(_here, "..", "tools", "client", "client-build-config.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def load_build_config():
    path = _find_build_config()
    if not path:
        raise FileNotFoundError("client-build-config.json 未找到（查过 %s）" % candidates_str())
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def candidates_str():
    return " / ".join([
        os.path.join(_RESOURCE_DIR, "client-build-config.json"),
        os.path.join(_BASE_DIR, "client-build-config.json"),
    ])


def data_dir(build_cfg):
    """客户端自有数据目录：测试可用 JZCLIENT_DATA_DIR 覆盖（verify 脚本用）。"""
    override = os.environ.get("JZCLIENT_DATA_DIR")
    if override:
        return override
    dirname = (build_cfg.get("paths") or {}).get("data_dirname") or "JZClient"
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, dirname)


def client_config_path(build_cfg):
    return os.path.join(data_dir(build_cfg), "config.json")


def load_client_config(build_cfg):
    path = client_config_path(build_cfg)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_client_config(build_cfg, cfg):
    path = client_config_path(build_cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def hidden_tools(build_cfg):
    """本机从主界面移除的服务器侧工具 id 列表（客户端本机显示状态，不动服务器）。

    语义（2026-10-08 需求方口径）：桌面端「卸载」= 卸本机前端包副本 + 本机不再显示；
    「安装」= 装回并恢复显示。只影响本机（客户端数据目录的 config.json），
    服务器端后端包与其运行状态不受任何影响（撤下只在商店管理·超级管理员做）。
    """
    cfg = load_client_config(build_cfg) or {}
    v = cfg.get("hidden_tools")
    return [str(x) for x in v] if isinstance(v, list) else []


def set_hidden_tool(build_cfg, tool_id, hidden):
    """写入/移除本机工具显示状态；返回最新列表。"""
    cfg = load_client_config(build_cfg) or {}
    ids = [str(x) for x in (cfg.get("hidden_tools") or []) if str(x).strip()]
    if hidden:
        if tool_id not in ids:
            ids.append(tool_id)
    else:
        ids = [x for x in ids if x != tool_id]
    cfg["hidden_tools"] = ids
    save_client_config(build_cfg, cfg)
    return ids


def normalize_address(addr):
    """补协议、去尾斜杠；空值返回 None。"""
    if not addr or not str(addr).strip():
        return None
    addr = str(addr).strip().rstrip("/")
    if "://" not in addr:
        addr = "http://" + addr
    return addr


def resolve_address(build_cfg, client_cfg):
    """地址解析顺序：客户端配置文件 → 构建配置默认 → None（走兜底页）。"""
    return (normalize_address(client_cfg.get("remote_address"))
            or normalize_address((build_cfg.get("server") or {}).get("default_address")))


def static_dir():
    """主界面 static 副本：客户端自带一份（随包发版）。"""
    candidates = [
        os.path.join(_RESOURCE_DIR, "static"),
        os.path.join(_BASE_DIR, "static"),
        os.path.join(_BASE_DIR, "..", "static"),
    ]
    for p in candidates:
        if os.path.isfile(os.path.join(p, "index.html")):
            return os.path.abspath(p)
    raise FileNotFoundError("static/index.html 未找到（查过 %s）" % " / ".join(candidates))


def fallback_page():
    candidates = [
        os.path.join(_RESOURCE_DIR, "fallback.html"),
        os.path.join(_BASE_DIR, "fallback.html"),
        os.path.join(_here, "fallback.html"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("fallback.html 未找到")


def log_path():
    return os.path.join(_BASE_DIR, "client.log")


def pid_file_path():
    return os.path.join(_BASE_DIR, "client.pid")


def program_dir():
    """程序目录（frozen=exe 目录；源码=仓库根）。"""
    return _BASE_DIR


def runtime_python_exe(build_cfg):
    """子进程运行时（GD4）：构建配置指明运行时目录则用之，否则当前解释器（源码态）。

    runtime_python_dir 相对路径按**程序目录**解析（构建配置单源写 `runtime/python` 即可，
    不因进程 CWD 不同而漂移）。打包态缺 python.exe 属构建缺陷（build-client / verify-client
    均有前置断言兜底）；此处如实回落当前解释器（源码态语义）。
    """
    d = (build_cfg.get("paths") or {}).get("runtime_python_dir") or ""
    if d:
        if not os.path.isabs(d):
            d = os.path.join(program_dir(), d)
        exe = os.path.join(d, "python.exe")
        if os.path.isfile(exe):
            return exe
    return sys.executable


def host_script_path():
    candidates = [
        os.path.join(_RESOURCE_DIR, "plugin_host.py"),
        os.path.join(_here, "plugin_host.py"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    raise FileNotFoundError("plugin_host.py 未找到")


def whitelist_path():
    """jz_relay 白名单（批次 3 T17）：随包进 _internal。"""
    candidates = [
        os.path.join(_RESOURCE_DIR, "relay_whitelist.json"),
        os.path.join(_BASE_DIR, "relay_whitelist.json"),
        os.path.join(_here, "relay_whitelist.json"),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def port_file_path():
    return os.path.join(_BASE_DIR, "port.txt")


def geometry_path(build_cfg):
    return os.path.join(data_dir(build_cfg), "geometry.json")


def webview2_profile_dir(build_cfg):
    return os.path.join(data_dir(build_cfg), "webview2-profile")


def webview2_runtime_dir(build_cfg):
    """fixed 内核目录：<程序目录>\\runtime\\webview2（含 msedgewebview2.exe 才算启用）。"""
    p = os.path.join(_BASE_DIR, "runtime", "webview2")
    if os.path.isdir(p) and os.path.isfile(os.path.join(p, "msedgewebview2.exe")):
        return p
    return None
