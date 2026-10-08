# -*- coding: utf-8 -*-
"""卸载补强补丁（2026-10-08）：依赖卸载按钮 + 卸载可选清除数据。

- capability_app：
  1) /local-api/plugins/uninstall 增 purge_data（清数据根 plugins/<id>；id 严格校验防穿越）
  2) 新增 /local-api/store/uninstall-dep：组件目录即时删（纯 Python）/占用则移入
     pylibs/.pending-remove（原生库下次启动清除）；删后刷新依赖旗标
  3) 启动清扫 .pending-remove（create_capability_app 内 setup_dep_components 之前）
- store.js：依赖条目补「卸载」按钮；插件与依赖卸载两步确认（第二步选是否清数据）
"""
import io
import py_compile

# ── 1) capability_app ──
p = "client/capability_app.py"
s = io.open(p, encoding="utf-8").read()

# 1a) 卸载端点增 purge_data
old = '''        target = os.path.join(local_plugins_dir, pid)
        if not os.path.isdir(target):
            return jsonify({"error": "本地插件不存在"}), 404
        kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
        shutil.rmtree(target, ignore_errors=True)
        _rescan()
        return jsonify({"ok": True, "restart_required": kind == "hybrid"})'''
new = '''        target = os.path.join(local_plugins_dir, pid)
        if not os.path.isdir(target):
            return jsonify({"error": "本地插件不存在"}), 404
        kind = next((m.get("kind") for m in local_manifests if m.get("id") == pid), "local")
        shutil.rmtree(target, ignore_errors=True)
        purged = False
        if data.get("purge_data"):
            # 清除数据（可选）：数据根 plugins/<id>（配置/缓存/本地业务数据）；id 先严格校验防穿越
            import re as _re
            if not _re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(pid)):
                return jsonify({"error": "非法 id，拒绝清除数据"}), 400
            droot = os.path.join(_data_dir, "plugins", pid)
            base = os.path.abspath(os.path.join(_data_dir, "plugins"))
            if os.path.abspath(droot).startswith(base + os.sep) and os.path.isdir(droot):
                shutil.rmtree(droot, ignore_errors=True)
                purged = True
        _rescan()
        return jsonify({"ok": True, "restart_required": kind == "hybrid", "data_purged": purged})'''
assert old in s, "卸载端点锚点"
s = s.replace(old, new, 1)

# 1b) 依赖卸载端点 + 启动清扫（插在 reveal 块之前，避免切片误伤——显式锚点）
anchor = '    @app.post("/local-api/store/reveal")'
assert anchor in s, "reveal 锚点"
block = '''    def _component_dirs(cid):
        pylibs = os.path.join(_program_dir, "runtime", "pylibs")
        return (os.path.join(pylibs, cid),
                os.path.join(pylibs, ".pending", cid),
                os.path.join(pylibs, ".pending-remove", cid))

    def _sweep_pending_component_removals():
        """启动清扫：.pending-remove 下的组件目录（上次卸载时被占用的）此刻未映射，直接删除。"""
        rm_root = os.path.join(_program_dir, "runtime", "pylibs", ".pending-remove")
        if not os.path.isdir(rm_root):
            return
        for name in os.listdir(rm_root):
            shutil.rmtree(os.path.join(rm_root, name), ignore_errors=True)
        try:
            os.rmdir(rm_root)
            _log("已清扫待卸载依赖组件（.pending-remove）")
        except OSError:
            pass

    _sweep_pending_component_removals()

    @app.post("/local-api/store/uninstall-dep")
    def store_uninstall_dep():
        """依赖组件卸载：纯 Python 即时删（刷新旗标）；原生库被映射无法删 → 移入
        .pending-remove，下次启动清除（与安装的 .pending 语义对偶）。"""
        data = request.get_json(force=True, silent=True) or {}
        cid = str(data.get("id") or "")
        import re as _re
        if not _re.fullmatch(r"[A-Za-z0-9_+.-]{1,64}", cid):
            return jsonify({"ok": False, "error": "非法组件 id"}), 400
        active, staged, rm = _component_dirs(cid)
        if not os.path.isdir(active) and not os.path.isdir(staged):
            return jsonify({"ok": False, "error": "该依赖组件未安装"}), 404
        pending_remove = False
        if os.path.isdir(active):
            try:
                shutil.rmtree(active)
            except OSError:
                os.makedirs(os.path.dirname(rm), exist_ok=True)
                try:
                    if os.path.isdir(rm):
                        shutil.rmtree(rm, ignore_errors=True)
                    os.rename(active, rm)      # 同卷改名，句柄不再阻塞
                    pending_remove = True
                except OSError as exc:
                    return jsonify({"ok": False, "error": "删除失败：%s" % exc}), 500
        if os.path.isdir(staged):
            shutil.rmtree(staged, ignore_errors=True)   # 待应用暂存一并取消
        try:
            jz_plugin_host.setup_dep_components(_program_dir, _log)   # 重注入（已删路径无副作用）；
            # 依赖旗标由各插件 install_refresher 在请求期刷新（既有机制）
        except Exception:
            pass
        except Exception:
            pass
        return jsonify({"ok": True, "pending_remove": pending_remove,
                        "note": "原生库已被占用，将在下次启动客户端后清除" if pending_remove else ""})

'''
s = s.replace(anchor, block + anchor, 1)
io.open(p, "w", encoding="utf-8", newline="").write(s)
py_compile.compile(p, doraise=True)
print("capability ok")
