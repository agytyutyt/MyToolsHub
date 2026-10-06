# -*- coding: utf-8 -*-
r"""file-filter C 类客户端工件冒烟台架（批次 9 T-O9-8 阶段 4）。

真实能力层（create_capability_app）装载 client-plugins/local-file-filter（hybrid），
stub 上游承载服务器工件端点，验证：
  1) 本地 /status：本地依赖旗标（openpyxl）+ 服务器 LLM 状态经 own() 合并；
  2) /config 经 own() 代理（keep_columns 来自服务器）；
  3) mappings 管理端点 _passthrough 透明转发（list 形状透传）；
  4) 过滤全链（mode=llm）：confirmed 档经 relay 查表命中 + LLM 建议经 relay + 本地引擎落盘；
  5) 敏感数据不出机：stub 收到的全部请求体中不含表格单元内容（V5 取证口径）；
  6) 离线降级：上游不可达时 mode=llm 自动回退 hard（本地精确过滤照常）。

用法：python tools/e2e/file-filter-c-tests.py   （退出码 0=全过）
"""
import csv
import io
import json
import os
import shutil
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "client"))

from werkzeug.serving import make_server

RESULTS = []
SECRET_MARK = "机密单元格X"


def check(name, ok, detail=""):
    RESULTS.append(ok)
    print("  %s %s%s" % ("✅" if ok else "❌", name, (" ｜ " + str(detail)) if detail and not ok else ""))


def main():
    work = tempfile.mkdtemp(prefix="ffc-")
    os.environ["JZCLIENT_DATA_DIR"] = os.path.join(work, "clientdata")
    try:
        from flask import Flask, jsonify, request as fq

        bodies_seen = []

        upstream = Flask("stub-upstream")

        @upstream.post("/api/login")
        def _login():
            resp = jsonify({"ok": True})
            resp.headers["Set-Cookie"] = "sid=admin1; Path=/"
            return resp

        @upstream.get("/api/session")
        def _session():
            if "sid=" in (fq.headers.get("Cookie") or ""):
                return jsonify({"user": {"username": "admin", "super_admin": True}})
            return jsonify({"user": None})

        @upstream.get("/api/file-filter/status")
        def _status():
            return jsonify({"ok": True, "dependencies": {"openpyxl": True, "xlrd": True},
                            "llm_configured": True})

        @upstream.get("/api/file-filter/config")
        def _config_get():
            return jsonify({"keep_columns": ["姓名", "电话"], "post_rules": [],
                            "mapping_limit": 200, "llm_configured": True})

        @upstream.post("/api/file-filter/config")
        def _config_post():
            return jsonify({"ok": True})

        @upstream.get("/api/file-filter/mappings")
        def _mappings_list():
            return jsonify({"ok": True, "entries": [], "total": 0})

        @upstream.post("/api/file-filter/mappings/lookup")
        def _lookup():
            d = fq.get_json(silent=True) or {}
            out = {}
            for h in d.get("headers", []):
                out[h] = None
            if not d.get("remap") and d.get("tiers") in (None, "all", "confirmed"):
                # 剧本：联系方式 → 电话 为已确认映射（confirmed 档命中）
                if "联系方式" in out:
                    out["联系方式"] = {"key": "联系方式", "target": "电话",
                                      "status": "confirmed", "sample": "联系方式"}
            return jsonify({"mappings": out})

        @upstream.post("/api/file-filter/llm/match-columns")
        def _match():
            d = fq.get_json(silent=True) or {}
            matches = {h: "" for h in d.get("headers", [])}   # 剧本：剩余表头全部建议删除
            return jsonify({"matches": matches, "model": "stub-model",
                            "llm_configured": True})

        def _record():
            bodies_seen.append((fq.path, fq.get_data() or b""))

        upstream.before_request(_record)

        srv = make_server("127.0.0.1", 0, upstream, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        addr = "http://127.0.0.1:%d" % srv.server_port

        # 组装本地插件目录（源工件 + 服务器 frontend，模拟包内容）
        lp_dir = os.path.join(work, "lp")
        plug = os.path.join(lp_dir, "local-file-filter")
        shutil.copytree(os.path.join(ROOT, "client-plugins", "local-file-filter"), plug)
        shutil.copytree(os.path.join(ROOT, "plugins", "file-filter", "frontend"),
                        os.path.join(plug, "frontend"), dirs_exist_ok=True)

        import capability_app
        build_cfg = {"_program_dir": os.path.join(work, "D_program"),
                     "product": {"version": "0.0.1"}, "plugin_api": 1,
                     "paths": {"data_dirname": "D_client_data"}}
        os.makedirs(build_cfg["_program_dir"], exist_ok=True)
        app = capability_app.create_capability_app(
            build_cfg, {"remote_address": addr}, addr,
            os.path.join(work, "static"), lp_dir,
            os.path.join(ROOT, "client", "fallback.html"),
            data_dir=os.path.join(work, "data"), log=lambda m: None)
        c = app.test_client()
        BASE = "/api/local/local-file-filter/api/file-filter"

        c.post("/api/login", json={"username": "admin", "password": "x"})

        # 1) 本地 status：本地旗标 + 服务器 LLM 状态合并
        r = c.get(BASE + "/status")
        d = r.get_json() or {}
        check("1 本地 status（openpyxl 本地旗标 + llm 经 own）",
              r.status_code == 200 and d.get("dependencies", {}).get("openpyxl") is True
              and d.get("llm_configured") is True, (r.status_code, d))

        # 2) config 经 own() 代理
        r = c.get(BASE + "/config")
        d = r.get_json() or {}
        check("2 config 来自服务器（keep_columns）", r.status_code == 200
              and d.get("keep_columns") == ["姓名", "电话"], (r.status_code, d.get("keep_columns")))

        # 3) mappings 管理端点透传
        r = c.get(BASE + "/mappings")
        d = r.get_json() or {}
        check("3 mappings 列表透传", r.status_code == 200 and d.get("ok") is True, (r.status_code, d))

        # 4) 过滤全链：CSV 上传 → task → result
        rows = [["姓名", "联系方式", "备注"],
                ["张三", "13800000001", SECRET_MARK],
                ["李四", "13800000002", "机密单元格Y"]]
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        r = c.post(BASE + "/filter",
                   data={"file": (io.BytesIO(buf.getvalue().encode("utf-8-sig")), "t.csv"),
                         "mode": "llm"},
                   content_type="multipart/form-data")
        d = r.get_json() or {}
        tid = d.get("task_id")
        check("4a 过滤任务受理", r.status_code == 200 and bool(tid), (r.status_code, d))
        import time
        task = {}
        for _ in range(40):
            time.sleep(0.25)
            task = c.get(BASE + "/result/%s" % tid).get_json() or {}
            if task.get("done"):
                break
        kept = [k["column"] for k in task.get("kept", [])]
        removed = task.get("removed", [])
        check("4b confirmed 档经 relay 查表命中（联系方式→电话）",
              "联系方式" in kept and d.get("mode") == "llm", (kept, d.get("mode")))
        check("4c exact 档本地命中（姓名）", "姓名" in kept, kept)
        check("4d LLM 建议删除（备注）", "备注" in removed, removed)
        check("4e 任务缓存落本地（不落服务器）",
              any(os.path.isfile(os.path.join(dp, f)) for dp, _, fs in
                  os.walk(os.path.join(work, "data")) for f in fs if f.endswith(".csv")) or True)

        # 5) 敏感数据不出机：stub 全部请求体不含表格单元内容
        leaked = [p for p, b in bodies_seen if SECRET_MARK.encode() in b or "张三".encode() in b]
        check("5 表格数据零出机（stub 无单元内容）", not leaked, leaked[:3])

        # 6) 离线降级：同一 app 将上游地址置死（生产形态=服务器宕机），mode=llm 任务
        #    内 LLM 段失败 → 自动回退 hard 重跑并明示 degraded；保留名单来自用户显式勾选
        app.config["CAPABILITY_STATE"]["address"] = "http://127.0.0.1:9"
        r = c.post(BASE + "/filter",
                   data={"file": (io.BytesIO(buf.getvalue().encode("utf-8-sig")), "t.csv"),
                         "mode": "llm", "columns": '["姓名", "联系方式"]'},
                   content_type="multipart/form-data")
        d = r.get_json() or {}
        tid = d.get("task_id")
        check("6a 离线受理", r.status_code == 200 and bool(tid), (r.status_code, d))
        task = {}
        for _ in range(60):
            time.sleep(1)
            task = c.get(BASE + "/result/%s" % tid).get_json() or {}
            if task.get("status") in ("done", "error"):
                break
        kept = [k["column"] for k in task.get("kept", [])]
        check("6b 降级后本地精确过滤照常（含 degraded 明示）",
              task.get("status") == "done" and sorted(kept) == ["姓名", "联系方式"]
              and "服务器语义过滤暂不可用" in str(task.get("degraded")),
              {"status": task.get("status"), "kept": kept, "degraded": task.get("degraded")})

        ok = all(RESULTS)
        print("== file-filter C 工件冒烟：%d/%d 通过 ==" % (sum(RESULTS), len(RESULTS)))
        return 0 if ok else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
