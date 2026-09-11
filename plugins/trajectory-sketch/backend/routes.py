# -*- coding: utf-8 -*-
"""轨迹速写 —— JZToolsHub 后端插件路由。

流程（详见 ``docs/轨迹速写插件-设计文档.md`` §2）：

    ① /upload   上传轨迹表 → 暂存 → 调用「过滤器」插件（默认硬过滤）→ 字段自检
    ② /analyze  用户确认（可切大模型辅助）→ 异步：过滤 → 轨迹分析引擎 → 报告工作簿
    ③ /result   轮询进度与结果
    ④ /download 下载 5 个 sheet 的速写报告 .xlsx

接口前缀：``/api/trajectory-sketch``（规范 B-2）。
与其他插件的关系：**只通过 HTTP 调用** 过滤器插件的 ``/api/file-filter/apply``（B-7），
不 import 任何其他插件的后端模块；轨迹分析全部在插件自带引擎内完成（引擎零框架依赖）。
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from flask import jsonify, request, send_file

from . import config_store, excel_io, filter_bridge, report_store
from .engine import analyze_rows, available_algorithms, normalize_params, resolve_columns
from .engine.contract import AnalysisError

try:
    from jztools_admin.routes import get_session_user as _get_session_user
except Exception:                     # admin 插件缺失时兜底
    _get_session_user = None

try:
    from jztools_admin.routes import set_operation as _set_operation
except Exception:                     # 主应用未提供日志辅助时兜底
    def _set_operation(op):
        pass

API_PREFIX = "/api/trajectory-sketch"
PLUGIN_ID = "trajectory-sketch"

WORKERS = 2
_executor = ThreadPoolExecutor(max_workers=WORKERS)

ID_RE = re.compile(r"^[A-Za-z0-9]{8,32}$")

MANAGE_ROLE_IDS = {"role-admin"}
MANAGE_ROLE_NAMES = {"管理员"}

_app_ref = None


# --------------------------------------------------------------------------
# 会话与权限（与 file-filter / knowledge-base 同口径）
# --------------------------------------------------------------------------

def _viewer() -> Optional[Dict[str, Any]]:
    if _get_session_user is None:
        return None
    try:
        return _get_session_user()
    except Exception:
        return None


def _can_manage(user) -> bool:
    if not user:
        return False
    if user.get("super_admin"):
        return True
    return (user.get("role_id") in MANAGE_ROLE_IDS
            or user.get("role") in MANAGE_ROLE_NAMES)


def _cookie() -> str:
    return request.headers.get("Cookie", "") or ""


def _host_url() -> str:
    return request.host_url or ""


# --------------------------------------------------------------------------
# 自检辅助
# --------------------------------------------------------------------------

def _self_check(filtered_rows: List[List[Any]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """对**过滤后**的表做列映射自检（与正式分析共用 engine.resolve_columns）。"""
    headers = excel_io.preview_headers(filtered_rows)
    matched, missing_req, missing_opt = resolve_columns(headers, config_store.engine_config(cfg))
    required_rows = []
    for canon in (cfg["schema"].get("required") or []):
        required_rows.append({
            "canonical": canon,
            "label": config_store.column_display_name(cfg, canon),
            "matched": matched.get(canon, ""),
            "required": True,
        })
    for canon in (cfg["schema"].get("optional") or []):
        required_rows.append({
            "canonical": canon,
            "label": config_store.column_display_name(cfg, canon),
            "matched": matched.get(canon, ""),
            "required": False,
        })
    return {
        "headers": headers,
        "matched": matched,
        "missing_required": missing_req,
        "missing_optional": missing_opt,
        "fields": required_rows,
        "can_analyze": not missing_req,
        "hint": ("" if not missing_req else
                 "过滤后缺少必需字段：%s。请在「⚙️ 配置 → 保留字段名单」中补上，"
                 "并在「列映射」里为本表的表头补充别名。" % "、".join(missing_req)),
    }


def _filter_meta(res: Dict[str, Any], mode: str) -> Dict[str, Any]:
    return {
        "mode": mode,
        "kept": res.get("kept") or [],
        "removed": res.get("removed") or [],
        "replace_count": res.get("replace_count") or 0,
        "rows": len(res.get("rows") or []) - 1 if res.get("rows") else 0,
    }


# --------------------------------------------------------------------------
# 异步分析
# --------------------------------------------------------------------------

def _run_analysis(app, task_id: str, stage: Dict[str, Any], mode: str,
                  cookie: str, host_url: str) -> None:
    """后台线程：过滤 → 分析 → 写报告 → 置 done。"""
    try:
        report_store.set_task(task_id, status="running", step="字段过滤")
        cfg = config_store.load_config()
        keep_cols, _notes = config_store.expand_keep_columns(cfg)
        post_rules = None if cfg["filter"].get("use_filter_plugin_rules") \
            else cfg["filter"].get("post_rules")

        raw_rows = excel_io.read_rows(stage["path"], "upload.%s" % stage.get("ext", "xlsx"))
        if len(raw_rows) < 2:
            report_store.set_task(task_id, status="error",
                                  detail="表格只有表头，没有可分析的数据行。")
            return
        res = filter_bridge.apply_filter(app, raw_rows, mode=mode, columns=keep_cols,
                                         post_rules=post_rules, cookie=cookie,
                                         host_url=host_url)
        filtered = res.get("rows") or []
        meta = _filter_meta(res, mode)

        report_store.set_task(task_id, status="running", step="轨迹分析", filter=meta)
        result = analyze_rows(filtered, config_store.engine_config(cfg), meta=meta)

        report_store.set_task(task_id, status="running", step="生成报告")
        out_path = os.path.join(report_store.cache_dir(), "%s_report.xlsx" % task_id)
        excel_io.write_report_workbook(out_path, result)
        report_store.keep_alive(out_path)

        report_store.set_task(
            task_id, status="done", step="",
            algo=result.get("algo"),
            quality=result.get("quality") or {},
            stays=result.get("stays") or [],
            trips=result.get("trips") or [],
            report=result.get("report") or {},
            clusters=result.get("clusters") or [],
            schema=result.get("schema") or {},
            filter=meta,
            warnings=result.get("warnings") or [],
            output_path=out_path,
            filename=_report_filename(),
            download="%s/download/%s" % (API_PREFIX, task_id),
        )
    except filter_bridge.FilterError as exc:
        report_store.set_task(task_id, status="error", detail=exc.message)
    except AnalysisError as exc:
        report_store.set_task(task_id, status="error", detail=str(exc))
    except excel_io.TableError as exc:
        report_store.set_task(task_id, status="error", detail=str(exc))
    except Exception as exc:                       # SEC-5：不透出堆栈与路径
        report_store.set_task(task_id, status="error",
                              detail="分析失败（%s），请检查表格内容后重试。" % type(exc).__name__)


def _report_filename() -> str:
    from datetime import datetime
    return "轨迹速写报告-%s.xlsx" % datetime.now().strftime("%Y%m%d-%H%M")


# --------------------------------------------------------------------------
# 路由注册
# --------------------------------------------------------------------------

def register(app):
    global _app_ref
    _app_ref = app

    @app.get(f"{API_PREFIX}/ping")
    def ts_ping():
        return jsonify({"ok": True, "plugin": PLUGIN_ID})

    @app.get(f"{API_PREFIX}/status")
    def ts_status():
        deps = {
            "openpyxl": excel_io.OPENPYXL_AVAILABLE,
            "xlrd": excel_io.XLRD_AVAILABLE,
            "requests": filter_bridge.REQUESTS_AVAILABLE,
        }
        cfg = config_store.load_config()
        params = normalize_params(config_store.engine_config(cfg))
        return jsonify({
            "ok": all(deps.values()),
            "dependencies": deps,
            "filter_plugin": filter_bridge.filter_status(app),
            "keep_columns": cfg["filter"].get("keep_columns") or [],
            "algorithms": available_algorithms(),
            "algo": params.get("algo"),
            "config_warnings": _config_warnings(params),
            "can_manage": _can_manage(_viewer()),
        })

    @app.get(f"{API_PREFIX}/config")
    def ts_config_get():
        cfg = config_store.load_config()
        cols, notes = config_store.expand_keep_columns(cfg)
        params = normalize_params(config_store.engine_config(cfg))
        return jsonify({
            "config": config_store.public_config(cfg),
            "keep_expand": {"columns": cols, "notes": notes},
            "algorithms": available_algorithms(),
            "config_warnings": _config_warnings(params),
            "can_manage": _can_manage(_viewer()),
        })

    @app.post(f"{API_PREFIX}/config")
    def ts_config_post():
        user = _viewer()
        if not _can_manage(user):
            return jsonify({"error": "仅管理员可修改轨迹速写配置"}), 403
        data = request.get_json(silent=True) or {}
        cfg = config_store.load_config()

        flt = data.get("filter")
        if isinstance(flt, dict):
            if "keep_columns" in flt:
                keep = flt.get("keep_columns")
                if not isinstance(keep, list):
                    return jsonify({"error": "保留字段名单必须为数组"}), 400
                cfg["filter"]["keep_columns"] = [str(k).strip()[:100] for k in keep if str(k).strip()]
            if "mode_default" in flt:
                cfg["filter"]["mode_default"] = str(flt.get("mode_default") or "hard").strip()
            if "post_rules" in flt:
                cfg["filter"]["post_rules"] = config_store.sanitize_rules(flt.get("post_rules"))
            if "use_filter_plugin_rules" in flt:
                cfg["filter"]["use_filter_plugin_rules"] = bool(flt.get("use_filter_plugin_rules"))
            if not cfg["filter"]["keep_columns"]:
                return jsonify({"error": "保留字段名单不能为空——它是「调用过滤器插件」的依据"}), 400

        schema = data.get("schema")
        if isinstance(schema, dict):
            for key in ("required", "optional"):
                if key in schema and isinstance(schema.get(key), list):
                    vals = [str(v).strip() for v in schema[key] if str(v).strip()]
                    if key == "required" and not vals:
                        return jsonify({"error": "必需字段不能为空"}), 400
                    cfg["schema"][key] = vals
            cmap = schema.get("column_map")
            if isinstance(cmap, dict):
                merged = dict(cfg["schema"].get("column_map") or {})
                for k, v in cmap.items():
                    key = str(k).strip()
                    if not key:
                        continue
                    if isinstance(v, list):
                        merged[key] = [str(i).strip() for i in v if str(i).strip()]
                    elif str(v).strip():
                        merged[key] = [str(v).strip()]
                cfg["schema"]["column_map"] = merged
            if not cfg["schema"]["column_map"]:
                return jsonify({"error": "列映射不能为空"}), 400

        analysis = data.get("analysis")
        if isinstance(analysis, dict):
            algo = str(analysis.get("algo") or "").strip()
            if algo:
                if algo not in available_algorithms():
                    return jsonify({"error": "未知的算法版本：%s" % algo}), 400
                cfg["analysis"]["algo"] = algo
            for sect in ("clean", "cluster", "staypoint", "trip"):
                if isinstance(analysis.get(sect), dict):
                    cfg["analysis"][sect] = {**cfg["analysis"].get(sect, {}), **analysis[sect]}

        report = data.get("report")
        if isinstance(report, dict):
            cfg["report"] = {**cfg["report"], **report}

        config_store.save_config(cfg)
        _set_operation("保存轨迹速写配置")
        return jsonify({"ok": True})

    @app.post(f"{API_PREFIX}/upload")
    def ts_upload():
        """上传轨迹表 → 暂存 → 硬过滤预演 → 字段自检（同步，毫秒~秒级）。"""
        user = _viewer()
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "未选择文件"}), 400

        orig = f.filename or ""
        ext = orig.lower().rsplit(".", 1)[-1] if "." in orig else ""
        if ext not in excel_io.ALLOWED_EXTS:
            return jsonify({"error": "仅支持 xlsx / xls / csv 文件"}), 400
        blob = f.read()
        if not blob:
            return jsonify({"error": "文件为空"}), 400
        if len(blob) > report_store.MAX_UPLOAD_BYTES:
            return jsonify({"error": "文件超过 20MB 上限"}), 413

        cfg = config_store.load_config()
        keep_cols, notes = config_store.expand_keep_columns(cfg)
        if cfg["filter"].get("keep_columns") and not keep_cols:
            return jsonify({"error": "保留字段名单为空，请管理员在「⚙️ 配置」中设置"}), 400
        if not keep_cols:
            return jsonify({"error": "保留字段名单为空，请管理员在「⚙️ 配置」中设置"}), 400

        report_store.cleanup()
        stage = report_store.save_upload(blob, ext, orig, user)

        try:
            raw_rows = excel_io.read_rows(stage["path"], "upload.%s" % ext)
        except excel_io.TableError as exc:
            report_store.drop_stage(stage["staged_id"])
            return jsonify({"error": str(exc)}), 400

        if len(raw_rows) < 2:
            report_store.drop_stage(stage["staged_id"])
            return jsonify({"error": "表格只有表头，没有数据行"}), 400

        # ---- 过滤预演固定用「硬过滤」：自检阶段应当是快速、确定、无副作用的 ----
        try:
            res = filter_bridge.apply_filter(
                _app_ref, raw_rows, mode="hard", columns=keep_cols,
                post_rules=(None if cfg["filter"].get("use_filter_plugin_rules")
                            else cfg["filter"].get("post_rules")),
                cookie=_cookie(), host_url=_host_url())
        except filter_bridge.FilterError as exc:
            report_store.drop_stage(stage["staged_id"])
            return jsonify({"error": exc.message}), exc.status

        filtered = res.get("rows") or []
        check = _self_check(filtered, cfg)
        report_store.update_stage(stage["staged_id"],
                                  headers=check["headers"],
                                  row_count=len(filtered) - 1,
                                  can_analyze=bool(check["can_analyze"]),
                                  hint=check["hint"],
                                  keep_columns=keep_cols,
                                  llm_configured=_filter_llm_configured())

        _set_operation("上传轨迹表并自检")
        return jsonify({
            "ok": True,
            "staged_id": stage["staged_id"],
            "filename": orig,
            "size": len(blob),
            "row_count": len(raw_rows) - 1,
            "keep_columns": cfg["filter"].get("keep_columns") or [],
            "keep_expand": {"columns": keep_cols, "notes": notes},
            "mode_default": cfg["filter"].get("mode_default") or "hard",
            "filter": _filter_meta(res, "hard"),
            "schema": check,
            "preview": filtered[:6],
            "llm_configured": _filter_llm_configured(),
            "algorithm": normalize_params(config_store.engine_config(cfg)).get("algo"),
        })

    @app.post(f"{API_PREFIX}/analyze")
    def ts_analyze():
        """提交轨迹分析任务（异步，返回 task_id）。"""
        data = request.get_json(silent=True) or {}
        staged_id = str(data.get("staged_id") or "").strip()
        if not ID_RE.match(staged_id):
            return jsonify({"error": "非法的上传标识"}), 400
        stage = report_store.get_stage(staged_id)
        if not stage or not report_store.owned_by(stage, _viewer()):
            return jsonify({"error": "上传内容不存在或已过期，请重新上传"}), 404

        mode = str(data.get("mode") or "hard").strip().lower()
        if mode not in ("hard", "llm"):
            mode = "hard"

        cfg = config_store.load_config()
        keep_cols, _ = config_store.expand_keep_columns(cfg)
        if not keep_cols:
            return jsonify({"error": "保留字段名单为空，请管理员在「⚙️ 配置」中设置"}), 400

        # 自检结论前置校验（避免明知会失败还把任务排进队列，规范 8.5-3）
        if not stage.get("can_analyze", True):
            return jsonify({"error": stage.get("hint") or
                                     "过滤后缺少必需字段，请在「⚙️ 配置」中调整保留字段名单与列映射后重新上传。"}), 400
        if stage.get("keep_columns") and stage.get("keep_columns") != keep_cols:
            return jsonify({"error": "保留字段名单在自检之后被修改过，请重新上传表格以刷新自检结果。"}), 409

        # 前置校验（8.5-3）：避免无效任务排入队列
        if mode == "llm" and not stage.get("llm_configured"):
            return jsonify({"error": "大模型辅助过滤需要「过滤器」插件已配置大模型："
                                     "请管理员在过滤器插件页面完成 API 地址 / Key / 模型配置，"
                                     "或改用硬过滤。"}), 400
        st = filter_bridge.filter_status(_app_ref)
        if not st["available"]:
            return jsonify({"error": "过滤器插件不可用（%s）" % st["reason"]}), 503

        report_store.cleanup()
        task_id = report_store.create_task(staged_id, _viewer(), mode)
        _executor.submit(_run_analysis, _app_ref, task_id, stage, mode, _cookie(), _host_url())
        _set_operation("提交轨迹速写任务")
        return jsonify({"task_id": task_id, "mode": mode})

    @app.get(f"{API_PREFIX}/result/<task_id>")
    def ts_result(task_id):
        if not ID_RE.match(task_id or ""):
            return jsonify({"error": "非法任务 ID"}), 400
        task = report_store.get_task(task_id)
        if not task or not report_store.owned_by(task, _viewer()):
            return jsonify({"error": "任务不存在或已过期"}), 404
        payload = {"task_id": task_id, "status": task.get("status"),
                   "step": task.get("step") or "", "mode": task.get("mode")}
        if task.get("status") == "done":
            payload.update({
                "algo": task.get("algo"),
                "quality": task.get("quality") or {},
                "stays": task.get("stays") or [],
                "trips": task.get("trips") or [],
                "report": task.get("report") or {},
                "clusters": task.get("clusters") or [],
                "schema": task.get("schema") or {},
                "filter": task.get("filter") or {},
                "warnings": task.get("warnings") or [],
                "filename": task.get("filename") or "",
                "download": task.get("download") or "",
            })
            report_store.keep_alive(task.get("output_path") or "")
        elif task.get("status") == "error":
            payload["detail"] = task.get("detail") or "分析失败"
        return jsonify(payload)

    @app.get(f"{API_PREFIX}/download/<task_id>")
    def ts_download(task_id):
        if not ID_RE.match(task_id or ""):
            return jsonify({"error": "非法任务 ID"}), 400
        task = report_store.get_task(task_id)
        if not task or not report_store.owned_by(task, _viewer()):
            return jsonify({"error": "任务不存在或已过期"}), 404
        if task.get("status") != "done":
            return jsonify({"error": "任务尚未完成"}), 409
        path = task.get("output_path") or ""
        if not os.path.isfile(path):
            return jsonify({"error": "报告文件已过期，请重新生成"}), 404
        _set_operation("下载轨迹速写报告")
        return send_file(path, as_attachment=True,
                         download_name=task.get("filename") or _report_filename())


def _filter_llm_configured() -> bool:
    """过滤器插件是否已配置大模型（经其公开接口 /config 查询，不读它的配置文件）。"""
    try:
        return filter_bridge.filter_llm_configured(_app_ref)
    except Exception:
        return False


def _config_warnings(params: Dict[str, Any]) -> List[str]:
    """配置层面的告警（引擎参数校验）。"""
    try:
        from .engine.params import validate
        return validate(params)
    except Exception:
        return []
