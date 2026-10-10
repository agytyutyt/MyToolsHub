# -*- coding: utf-8 -*-
r"""client-plugins/local-file-filter/backend/routes.py 适配补丁（T-O9-8 阶段 3）。

对服务器 routes.py 的拷贝做六类手术：jz_llm→网关垫片、配置/映射库→own() 垫片、
mappings 管理端点整组透传、config 保存契约适配。幂等性不保证——仅对 freshly-copied
routes.py 执行一次（重跑前先重新 cp）。
"""
import io
import re
import py_compile

P = "client-plugins/local-file-filter/backend/routes.py"
s = io.open(P, encoding="utf-8").read()

# 1) import jz_llm → 网关垫片；flask 补 Response
s = s.replace(
    "import jz_llm\n",
    "import jz_relay\n"
    "from . import llm_gateway as jz_llm  # 存量适配（T-O9-8）：jz_llm 能力经 own() 网关，Key 不落本机\n", 1)
s = s.replace("from flask import jsonify, request, send_file",
              "from flask import jsonify, request, send_file, Response", 1)

# 2) 常量区：配置/映射库权威在服务器；任务缓存本地
old_const = ('CONFIG_FILE = jztools_data.get_data_root_file("plugins", "file-filter", "config.json")\n'
             'TASK_DIR = jztools_data.get_data_root_dir("plugins", "file-filter", ".task_cache")')
new_const = ('# 存量适配（T-O9-8）：配置与映射库的**权威在服务器**——load_config/save_config 与\n'
             '# MappingStore 换为 own() 垫片；任务缓存（.task_cache）落客户端数据根（本地）。\n'
             'TASK_DIR = jztools_data.get_data_root_dir("plugins", "file-filter", ".task_cache")')
assert old_const in s, "常量区锚点未命中"
s = s.replace(old_const, new_const, 1)

old_map = ('MAPPINGS_FILE = jztools_data.get_data_root_file("plugins", "file-filter", "mappings.json")\n'
           'MAPPINGS = mapping_store.MappingStore(MAPPINGS_FILE)')
new_map = ('MAPPINGS = mapping_store.MappingStore()   # relay 垫片：三档查表经 own()，单一写方在服务器\n'
           '\n'
           '_OWN = jz_relay.own("local-file-filter")  # 自身命名空间视图（服务器工件 = file-filter）\n'
           '\n'
           '\n'
           'def _passthrough():\n'
           '    """own() 透明转发：mappings 管理端点 → 服务器同名端点（挂载已剥，路径尾一致）。"""\n'
           '    r = _OWN.request(request.method, request.path,\n'
           '                     json=request.get_json(silent=True), timeout=60)\n'
           '    hop = {"connection", "keep-alive", "transfer-encoding", "content-length"}\n'
           '    headers = [(k, v) for k, v in r.headers.items() if k.lower() not in hop]\n'
           '    return Response(r.content, status=r.status_code, headers=headers)')
assert old_map in s, "MAPPINGS 锚点未命中"
s = s.replace(old_map, new_map, 1)

# 3) load_config → own() 代理（保留规整口径）
m = re.search(r"def load_config\(\):\n(?:    .*\n|\n)*?(?=\ndef _sanitize_rules)", s)
assert m, "load_config 区块定位失败"
new_load = (
    'def load_config():\n'
    '    """配置权威在服务器（管理员统一维护）：经 own() 取回后按本地口径规整。"""\n'
    '    cfg = {}\n'
    '    try:\n'
    '        r = _OWN.get("/api/file-filter/config", timeout=30)\n'
    '        if r.status_code == 200:\n'
    '            data = r.json() or {}\n'
    '            cfg = {k: data.get(k) for k in ("keep_columns", "post_rules", "mapping_limit")}\n'
    '    except Exception:\n'
    '        cfg = {}                                 # 服务器不可达 → 空配置（精确/正则档照常）\n'
    '    keep = cfg.get("keep_columns")\n'
    '    rules = cfg.get("post_rules")\n'
    '    try:\n'
    '        limit = int(cfg.get("mapping_limit"))\n'
    '    except Exception:\n'
    '        limit = MAPPING_LIMIT_DEFAULT\n'
    '    limit = max(MAPPING_LIMIT_MIN, min(MAPPING_LIMIT_MAX, limit))\n'
    '    return {\n'
    '        "keep_columns": [str(k).strip() for k in keep if str(k).strip()] if isinstance(keep, list) else [],\n'
    '        "post_rules": _sanitize_rules(rules),\n'
    '        "mapping_limit": limit,\n'
    '    }\n')
s = s[:m.start()] + new_load + s[m.end():]

# 4) save_config → own() 代理（失败返回 Response 由调用方透传）
m = re.search(r"def save_config\(cfg\):\n(?:    .*\n|\n)*?(?=\n# =+ 任务表)", s)
assert m, "save_config 区块定位失败"
new_save = (
    'def save_config(cfg):\n'
    '    """配置写透传到服务器（管理员权限由服务器以会话判定）；失败返回错误 Response。"""\n'
    '    r = _OWN.post("/api/file-filter/config", json=cfg or {}, timeout=30)\n'
    '    if r.status_code != 200:\n'
    '        return Response(r.content, status=r.status_code,\n'
    '                        content_type=r.headers.get("Content-Type", "application/json"))\n'
    '    return None')
s = s[:m.start()] + new_save + s[m.end():]

# 5) mappings 管理端点（10 个）体 → _passthrough()
names = ["ff_mappings_list", "ff_mappings_confirm", "ff_mappings_reject",
         "ff_mappings_delete", "ff_mappings_clear", "ff_mappings_export",
         "ff_mappings_remap", "ff_mappings_vote", "ff_mappings_recheck",
         "ff_mappings_recheck_result"]
for name in names:
    pat = re.compile(r"(    @app\.(?:get|post)\(f\"\{API_PREFIX\}/mappings[^\n]*\)\n"
                     r"    def %s\(.*?\):\n)(?:.*\n)*?(?=    @app\.)" % name)
    m = pat.search(s)
    assert m, name + " 定位失败"
    s = s[:m.end(1)] + "        return _passthrough()\n\n" + s[m.end(1):]

# 6) ff_config_post 适配 save_config 新契约（返回 Response 或 None）
old_post = ('        save_config(cfg)\n'
            '        _set_operation("保存过滤器配置")\n'
            '        return jsonify({"ok": True})')
new_post = ('        res = save_config(cfg)\n'
            '        if res is not None:\n'
            '            return res                            # 服务器侧错误透传（含权限 403）\n'
            '        _set_operation("保存过滤器配置")\n'
            '        return jsonify({"ok": True})')
assert old_post in s, "ff_config_post 锚点未命中"
s = s.replace(old_post, new_post, 1)

# 7) 客户端特有增强收编（批次 9 手工增强，2026-10-11 P3-1 D6-5 起纳入重生成）：
#    mode="llm" 语义段失败 → 自动回退 hard 重跑并明示 degraded 原因（GLM §1.2 降级 UX，
#    服务器侧无此分支——服务器永远在线，降级语义只属于客户端）；degraded 原因透传页面。
old_err = ('    except llm_client.LLMError as e:\n'
           '        set_task(task_id, status="error", detail=f"大模型过滤失败：{e}")\n')
new_err = (
    '    except llm_client.LLMError as e:\n'
    '        if mode == "llm":\n'
    '            # 存量适配（T-O9-8）降级 UX（GLM §1.2）：服务器语义段不可用 → 自动回退 hard\n'
    '            # 重跑（本地精确/正则照常），任务明示 degraded 原因；网关缓存同步失效，\n'
    '            # 后续请求在 _resolve_params 处即降级，不再空跑。\n'
    '            try:\n'
    '                jz_llm.invalidate()\n'
    '                result = run_filter(headers, rows, "hard", keep, post_rules, session,\n'
    '                                    exclude, use_suggested=False, store=MAPPINGS,\n'
    '                                    username=username, suggest_limit=_mapping_limit())\n'
    '                core.write_table(out_path, out_ext, result["headers2"], result["rows2"])\n'
    '                set_task(task_id, status="done",\n'
    '                         kept=result["kept"],\n'
    '                         removed=result["removed"],\n'
    '                         removed_detail=result["removed_detail"],\n'
    '                         mappings_used=result["mappings_used"],\n'
    '                         replace_count=result["replace_count"],\n'
    '                         llm_used=False,\n'
    '                         degraded=f"服务器语义过滤暂不可用，已按本地精确过滤执行（{e}）",\n'
    '                         rows=len(result["rows2"]),\n'
    '                         filename=os.path.basename(out_path),\n'
    '                         download=f"{API_PREFIX}/download/{task_id}",\n'
    '                         output_ext=out_ext)\n'
    '                return\n'
    '            except Exception:\n'
    '                pass\n'
    '        set_task(task_id, status="error", detail=f"大模型过滤失败：{e}")\n')
assert old_err in s, "LLMError 分支锚点未命中"
s = s.replace(old_err, new_err, 1)

old_deg = '            payload["degraded"] = True\n'
new_deg = '            payload["degraded"] = task.get("degraded")   # 降级原因透传（页面明示用）\n'
assert old_deg in s, "degraded 透传锚点未命中"
s = s.replace(old_deg, new_deg, 1)

io.open(P, "w", encoding="utf-8", newline="").write(s)
py_compile.compile(P, doraise=True)
print("routes adapted + compile ok")
