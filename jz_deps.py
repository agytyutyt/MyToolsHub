"""JZToolsHub 依赖判定引擎 —— 插件「能不能跑」的唯一判定入口。

职责（见 `docs/design/主体与插件解耦-设计文档.md` §5.2 / §5.3）：
    读插件 `manifest.json` 的 `requires`（三类依赖：框架第三方包 / 插件自带 vendor /
    外部程序组件）+ 主体**已安装依赖清单** `config/installed-deps.json` + 程序目录实际探测
    → 给出三态结论：

      ok        全部 required 满足 → 正常加载
      degraded  仅可选依赖缺失     → 加载，列表与卡片标"功能降级"
      blocked   任一 required 缺失（或版本门控不满足）→ **不加载**并记录缺失清单

    同一次求值同时覆盖**版本门控**（`min_app_version` / `api_version`，§5.2），
    使"能不能加载"与"能不能跑"共用一个结论（设计文档要求"同一处、同一次"）。

版本比较语义（刻意从简，§5.3）：
    只支持 `>=x.y.z` 与精确 `x.y.z`（三段式数值比较）；复杂 PEP 440 区间不支持
    （出包工具 C-10 会在构建期拒绝）。实际版本未知时（vendor / 外部组件）跳过版本比较。

降级口径（重要）：
    已安装依赖清单**不存在**时（源码开发环境）视为"不可核验"——框架第三方包一律记为
    `unknown` 且**不阻断加载**（否则源码模式下所有插件都会被打成 blocked）；冻结部署形态
    该文件由 build-deploy.ps1 生成，始终存在。

纪律（与 `jztools_data.py` 相同）：仅标准库、导入无副作用、**不得 import 任何插件**。
"""

import glob
import json
import logging
import os
import re

log = logging.getLogger("jztools.deps")

# 插件 API 版本 —— 主体对插件的承诺面版本（设计文档 §5.1 / §5.2）。
# 这是 plugin_api 的**唯一真源**：build-deploy.ps1 打包时读它写入 version.json，
# build-plugin-package.ps1 出包时读它写入 plugin-package.json / index.json 的 api_version。
# 破坏性变更 FC-1~FC-9 任一项时递增，且主体必须同时兼容上一版（一个版本窗口）。
PLUGIN_API = 1

# 依赖清单（主体侧"已安装依赖及版本"的真源，随主包分发）
DEPS_LOCK_REL = os.path.join("config", "installed-deps.json")
# 依赖组件包清单（cv2/numpy 等"不随主包、按需安装"的库；装到 runtime/pylibs/，见 T25）
DEPS_COMPONENT_REL = os.path.join("runtime", "pylibs", "manifest.json")
# 版本号写法：>=x.y.z 或 x.y.z
_REQ_RE = re.compile(r"^\s*(>=)?\s*(\d+(?:\.\d+){0,3})\s*$")

_VENDOR_REL = os.path.join("backend", "vendor")


# --------------------------------------------------------------------------- #
# 版本比较
# --------------------------------------------------------------------------- #

def parse_version(text):
    """把 '1.2.3' 解析为 (1, 2, 3)；非法返回 None。"""
    if not isinstance(text, str):
        return None
    m = re.match(r"^\s*(\d+(?:\.\d+){0,3})\s*$", text)
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def parse_requirement(require):
    """解析声明写法：'>=1.2.3' / '1.2.3' / 空 → (op, version tuple)；非法返回 None。

    op 取值：">=" 或 "=="；require 为空时返回 ("*", None) 表示不限定版本。
    """
    if require in (None, "", "*"):
        return ("*", None)
    m = _REQ_RE.match(str(require))
    if not m:
        return None
    return (">=" if m.group(1) else "==", parse_version(m.group(2)))


def version_satisfies(actual, require):
    """实际版本是否满足声明要求。

    - require 为空 → True（不限定）；
    - actual 为 None（版本未知）→ True（跳过比较，由调用方在 detail 里标注 unknown）；
    - 声明写法非法 → False（构建期应已拒绝，运行期按不满足处理并告警）。
    """
    parsed = parse_requirement(require)
    if parsed is None:
        log.warning("依赖声明写法非法（仅支持 >=x.y.z 与精确 x.y.z）：%r", require)
        return False
    op, want = parsed
    if op == "*" or want is None:
        return True
    got = parse_version(actual)
    if got is None:
        return True
    # 补齐位数后按元组比较（1.2 == 1.2.0）
    n = max(len(want), len(got))
    want = want + (0,) * (n - len(want))
    got = got + (0,) * (n - len(got))
    return got >= want if op == ">=" else got == want


# --------------------------------------------------------------------------- #
# 已安装依赖清单 / 声明读取
# --------------------------------------------------------------------------- #

def load_installed_deps(app_dir=None):
    """读主体已安装依赖清单；返回 {'available','packages','modules',...}。

    - packages：发行包名 → 版本（如 {"opencv-python": "5.0.0.93"}）
    - modules ：import 名 → 发行包名（如 {"cv2": "opencv-python"}）——
      插件声明用的是 import 名（与 C-10 扫描口径一致），需经此映射查版本。
    文件不存在或不可解析时 available=False（源码开发环境属正常，判定侧不阻断）。
    """
    app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(app_dir, DEPS_LOCK_REL)
    if not os.path.isfile(path):
        comp = _load_component_deps(app_dir)
        return {"available": False, "packages": comp.get("packages") or {}, "modules": comp.get("modules") or {},
                "components": comp.get("components", []), "path": path}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        pkgs = obj.get("packages") if isinstance(obj, dict) else None
        if not isinstance(pkgs, dict):
            raise ValueError("packages 字段缺失或格式错误")
        mods = obj.get("modules") if isinstance(obj.get("modules"), dict) else {}
        # 合并「依赖组件包」提供的包（cv2/numpy）：组件装了就等于"本机已安装"，
        # 插件声明的 cv2 才可能判为满足（设计文档 §5.3 / T25 第 1 步）。
        comp = _load_component_deps(app_dir)
        pkgs = {str(k).lower(): str(v) for k, v in pkgs.items()}
        for k, v in (comp.get("packages") or {}).items():
            pkgs.setdefault(str(k).lower(), str(v))
        for k, v in (comp.get("modules") or {}).items():
            mods.setdefault(str(k), str(v))
        return {"available": True, "components": comp.get("components", []),
                "packages": {str(k).lower(): str(v) for k, v in pkgs.items()},
                "modules": {str(k): str(v) for k, v in mods.items()},
                "path": path, "generated_at": obj.get("generated_at", ""), "python": obj.get("python", "")}
    except Exception as exc:
        log.warning("已安装依赖清单不可解析（按不可核验处理）：%s", exc)
        comp = _load_component_deps(app_dir)
        return {"available": False, "packages": comp.get("packages") or {}, "modules": comp.get("modules") or {},
                "components": comp.get("components", []), "path": path}


def _load_component_deps(app_dir):
    """读「依赖组件包」清单（runtime/pylibs/manifest.json），未装则返回空。"""
    path = os.path.join(app_dir, DEPS_COMPONENT_REL)
    if not os.path.isfile(path):
        return {"packages": {}, "modules": {}, "components": []}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        comps = obj.get("components") if isinstance(obj.get("components"), list) else []
        pkgs, mods = {}, {}
        for c in comps:
            for k, v in (c.get("packages") or {}).items():
                pkgs[str(k)] = str(v)
            for k, v in (c.get("modules") or {}).items():
                mods[str(k)] = str(v)
        if not comps and isinstance(obj.get("packages"), dict):     # 单组件简写形态
            comps = [{"id": obj.get("id", "component"), "name": obj.get("name", ""),
                      "version": obj.get("version", ""), "packages": obj.get("packages")}]
            pkgs = {str(k): str(v) for k, v in obj["packages"].items()}
            mods = {str(k): str(v) for k, v in (obj.get("modules") or {}).items()}
        return {"packages": pkgs, "modules": mods, "components": comps}
    except Exception as exc:
        log.warning("依赖组件清单不可解析（按未安装处理）：%s", exc)
        return {"packages": {}, "modules": {}, "components": []}


def read_requires(manifest):
    """从 manifest 中取出规范化的 requires（缺省视为"无第三方依赖"）。"""
    req = (manifest or {}).get("requires")
    if not isinstance(req, dict):
        req = {}
    return {
        "min_app_version": req.get("min_app_version"),
        "api_version": req.get("api_version"),
        "python_packages": [x for x in (req.get("python_packages") or []) if isinstance(x, dict)],
        "vendored": [x for x in (req.get("vendored") or []) if isinstance(x, dict)],
        "external": [x for x in (req.get("external") or []) if isinstance(x, dict)],
    }


def read_version_json(app_dir=None):
    """读程序目录 version.json（app / plugin_api），失败返回 {}。"""
    app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(app_dir, "version.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


# --------------------------------------------------------------------------- #
# 三类依赖的实测
# --------------------------------------------------------------------------- #

def _configured_soffice_paths(app_dir, plugin_id=None):
    """配置里显式指定的 soffice 路径（与知识库插件 office_render._configured_soffice 同口径）。

    ★ 这一档**优先级最高**（插件就是这么找的）：判定的探测必须与插件实际能力一致，
      否则会出现"插件能用、徽标却报缺失"的假降级（2026-09-20 真机实测踩到）。
    顺序：数据根 plugins/<id>/config.json → 程序目录 plugins/<id>/config.json；
          单个配置内 office.soffice_path（新键）优先，兼容 pdf.soffice_path（旧键）。
    plugin_id 为空时扫描所有插件配置（供"已安装依赖"清单展示用）。
    """
    cands = []
    roots = []
    try:
        import jztools_data                      # 框架模块；仅用于解析数据根
        roots.append(jztools_data.get_data_root_dir("plugins"))
    except Exception:
        pass
    roots.append(os.path.join(app_dir, "plugins"))
    ids = [plugin_id] if plugin_id else []
    if not ids:
        for r in roots:
            if os.path.isdir(r):
                try:
                    ids += [d for d in os.listdir(r) if os.path.isdir(os.path.join(r, d))]
                except OSError:
                    pass
    for pid in dict.fromkeys(ids):               # 去重且保序
        for r in roots:
            cfg = os.path.join(r, pid, "config.json")
            if not os.path.isfile(cfg):
                continue
            try:
                with open(cfg, "r", encoding="utf-8-sig") as f:
                    conf = json.load(f)
            except Exception:
                continue
            if not isinstance(conf, dict):
                continue
            for section in ("office", "pdf"):
                node = conf.get(section)
                if isinstance(node, dict):
                    v = node.get("soffice_path")
                    if isinstance(v, str) and v.strip():
                        cands.append(v.strip())
    return cands


def _probe_external(dep_id, app_dir, plugin_id=None):
    """探测外部程序组件是否可用；返回 (state, actual, note)。

    - libreoffice：复用知识库插件的候选路径约定（runtime/libreoffice/program/soffice.exe
      + 有限深度 glob；环境变量 XHR_SOFFICE 优先），只做存在性判断，不启动程序；
    - chrome：浏览器基线，按常见安装路径探测（尽力而为）；
    - 其它 id：回退为"程序目录 runtime/<id>/ 存在且非空"。
    """
    dep_id = (dep_id or "").lower()
    if dep_id == "libreoffice":
        env = os.environ.get("XHR_SOFFICE")
        if env and os.path.isfile(env):
            return ("ok", None, env)
        # 插件配置里显式指定的路径（最高优先级那一档，必须认）
        for cfg_path in _configured_soffice_paths(app_dir, plugin_id):
            if os.path.isfile(cfg_path):
                return ("ok", None, cfg_path)
        for cand in (os.path.join(app_dir, "runtime", "libreoffice", "program", "soffice.exe"),
                     os.path.join(app_dir, "runtime", "libreoffice", "soffice.exe")):
            if os.path.isfile(cand):
                return ("ok", None, cand)
        hits = glob.glob(os.path.join(app_dir, "runtime", "libreoffice", "**", "soffice.exe"), recursive=True)
        if hits:
            return ("ok", None, hits[0])
        return ("missing", None, "")
    if dep_id == "chrome":
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if not base:
                continue
            cand = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(cand):
                return ("ok", None, cand)
        return ("missing", None, "")
    d = os.path.join(app_dir, "runtime", dep_id)
    if os.path.isdir(d) and os.listdir(d):
        return ("ok", None, d)
    return ("missing", None, "")


def _check_python_package(dep, installed):
    """框架第三方包：清单里有则比版本；清单不可核验则记 unknown（不阻断）。

    声明用 import 名（如 cv2 / docx），清单用发行包名（opencv-python / python-docx），
    故先经 modules 映射解析，再回退按原名直接查（多数包两者同名）。
    """
    name = str(dep.get("name") or "").strip()
    require = dep.get("version")
    if not name:
        return ("missing", None, "声明缺少 name")
    if not installed.get("available"):
        return ("unknown", None, "已安装依赖清单不可核验（源码模式）")
    dist = installed.get("modules", {}).get(name) or name
    actual = installed["packages"].get(dist.lower())
    if actual is None:
        return ("missing", None, "主体未安装该包（%s）" % dist)
    if not version_satisfies(actual, require):
        return ("mismatch", actual, "版本不满足声明")
    return ("ok", actual, "")


def _check_vendored(dep, plugin_dir):
    """插件自带依赖：只做目录/文件存在性判断（版本未知，跳过比较）。"""
    name = str(dep.get("name") or "").strip()
    if not name:
        return ("missing", None, "声明缺少 name")
    base = os.path.join(plugin_dir, _VENDOR_REL, name)
    if os.path.isdir(base) or os.path.isfile(base + ".py"):
        return ("ok", None, "插件自带（版本由插件声明）")
    return ("missing", None, "插件目录内缺少 %s" % os.path.join(_VENDOR_REL, name))


# --------------------------------------------------------------------------- #
# 判定入口
# --------------------------------------------------------------------------- #

def evaluate(plugin_id, plugin_dir, manifest, app_dir=None, app_version=None, plugin_api=None):
    """对单个插件求值：版本门控 + 三类依赖 → 三态结论。

    返回 dict：
        {"id", "status": "ok|degraded|blocked", "missing": [名称…], "detail": [行…], "reason"}
    detail 行：{"name","kind","require","actual","state","required","note"}
    state 取值：ok / missing / mismatch / unknown。
    """
    app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))
    req = read_requires(manifest)
    installed = load_installed_deps(app_dir)

    detail = []
    blocked_reason = ""

    # ---- 版本门控（§5.2）：不满足即 blocked，与依赖缺失同一结论出口 ----
    min_app = req.get("min_app_version")
    if min_app and app_version and not version_satisfies(app_version, ">=%s" % min_app):
        detail.append({"name": "app", "kind": "framework", "require": ">=%s" % min_app,
                       "actual": app_version, "state": "mismatch", "required": True,
                       "note": "需要升级主体"})
        blocked_reason = "需要主体 ≥ %s（当前 %s）" % (min_app, app_version)
    api_req = req.get("api_version")
    if not blocked_reason and api_req is not None and plugin_api is not None:
        try:
            if int(api_req) > int(plugin_api):
                detail.append({"name": "plugin_api", "kind": "framework", "require": "<=%s" % api_req,
                               "actual": plugin_api, "state": "mismatch", "required": True,
                               "note": "插件需要更新的主体"})
                blocked_reason = "插件要求 plugin_api ≤ %s（主体 %s）" % (api_req, plugin_api)
        except (TypeError, ValueError):
            pass

    # ---- 三类依赖 ----
    for dep in req["python_packages"]:
        state, actual, note = _check_python_package(dep, installed)
        detail.append({"name": dep.get("name", ""), "kind": "python_package",
                       "require": dep.get("version"), "actual": actual, "state": state,
                       "required": bool(dep.get("required", True)), "note": note})
    for dep in req["vendored"]:
        state, actual, note = _check_vendored(dep, plugin_dir)
        detail.append({"name": dep.get("name", ""), "kind": "vendored",
                       "require": dep.get("version"), "actual": actual, "state": state,
                       "required": bool(dep.get("required", True)), "note": note})
    for dep in req["external"]:
        state, actual, note = _probe_external(dep.get("id"), app_dir, plugin_id)
        detail.append({"name": dep.get("id", ""), "kind": "external",
                       "require": dep.get("version"), "actual": actual, "state": state,
                       "required": bool(dep.get("required", True)), "note": note})

    # ---- 汇总三态 ----
    missing, degraded = [], []
    for row in detail:
        if row["state"] == "ok" or row["state"] == "unknown":
            continue
        if row["required"]:
            missing.append(row["name"])
        else:
            degraded.append(row["name"])
    if blocked_reason or missing:
        status = "blocked"
    elif degraded:
        status = "degraded"
    else:
        status = "ok"
    return {
        "id": plugin_id,
        "status": status,
        "missing": missing,
        "degraded": degraded,
        "detail": detail,
        "reason": blocked_reason,
        "lock_available": installed.get("available", False),
    }


def evaluate_all(plugin_dirs, manifests=None, app_dir=None, app_version=None, plugin_api=None):
    """批量求值：plugin_dirs = {插件id: 目录}；返回 {插件id: 结论}。"""
    manifests = manifests or {}
    return {
        pid: evaluate(pid, pdir, manifests.get(pid) or {}, app_dir=app_dir,
                      app_version=app_version, plugin_api=plugin_api)
        for pid, pdir in plugin_dirs.items()
    }
