# -*- coding: utf-8 -*-
r"""admin 主流程台架（P3-1 D1 固化，源自一次性验证脚本）：隔离数据根 + test_client。

覆盖登录/鉴权面、组织/账号三簇读写全路径（P3-1 D1 拆出 admin_org.py 的端点）、
插件管理面与商店管理面读路径。只读台架（数据落在临时目录，随跑随弃）。

用法：
    python tools\e2e\admin-flow-tests.py
退出码：0 = 全过；非 0 = 有断言失败。
"""
import json
import os
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "plugins", "admin", "backend"))

tmp = tempfile.mkdtemp(prefix="jz-admin-flow-")
os.environ["JZTOOLS_DATA_ROOT"] = tmp
os.makedirs(os.path.join(tmp, "config"), exist_ok=True)
with open(os.path.join(tmp, "config", "tools.json"), "w", encoding="utf-8") as f:
    json.dump({"tools": [{"id": "admin", "name": "管理后台", "enabled": True, "hidden": True},
                         {"id": "store", "name": "商店", "enabled": True, "grant_all": True}]},
              f, ensure_ascii=False)

from flask import Flask            # noqa: E402
import routes as admin_routes      # noqa: E402

app = Flask(__name__)
admin_routes.register(app)
c = app.test_client()
n = 0


def hit(name, method, path, expect=200, **kw):
    global n
    r = c.open(path, method=method, **kw)
    flag = r.status_code == expect
    n += flag
    print("  %s %s %s → %d" % ("[OK]" if flag else "[!!]", method, path, r.status_code))
    assert flag, (name, r.status_code, r.get_data(as_text=True)[:200])
    return r.get_json(silent=True) or {}


def main():
    global n
    # 匿名 / 错密 / 登录
    hit("匿名 units", "GET", "/api/admin/units", expect=401)
    hit("错密登录", "POST", "/api/login", expect=401,
        json={"username": "admin", "password": "wrong"})
    r = c.post("/api/login", json={"username": "admin", "password": "admin123"})
    print("  [OK] POST /api/login → %d" % r.status_code)
    assert r.status_code == 200
    n += 1

    # 主流程读面
    for p in ("/api/session", "/api/admin/units", "/api/admin/departments",
              "/api/admin/users", "/api/admin/permissions", "/api/admin/permission-points",
              "/api/admin/summary", "/api/admin/org-tree"):
        hit(p, "GET", p)

    # org 簇写路径：建单位→建部门→建角色→建用户→改（权限点/单位/部门）→删（逆序）
    u = hit("建单位", "POST", "/api/admin/units", json={"name": "D1单位", "description": "测试"})
    uid = u["unit"]["id"]
    d = hit("建部门", "POST", "/api/admin/departments", json={"name": "D1部门", "unit_id": uid})
    did = d["department"]["id"]
    role = hit("建角色", "POST", "/api/admin/permissions",
               json={"name": "D1角色", "modules": ["user"]})
    rid = role["permission"]["id"]
    hit("改角色", "PUT", "/api/admin/permissions/" + rid,
        json={"name": "D1角色改", "modules": ["user", "unit"]})
    hit("建用户", "POST", "/api/admin/users",
        json={"username": "d1user", "name": "测试员", "password": "secret1",
              "unit_id": uid, "department_id": did, "role": rid,
              "idcard": "110101199001010011",
              "llm": {"format": "openai", "base_url": "https://x", "model": "m"}})
    hit("改用户（含权限点）", "PUT", "/api/admin/users/d1user",
        json={"name": "测试员改", "permissions": ["store"], "llm": {"api_key": "sk-test"}})
    hit("改单位", "PUT", "/api/admin/units/" + uid, json={"name": "D1单位改"})
    hit("改部门", "PUT", "/api/admin/departments/" + did, json={"name": "D1部门改"})
    hit("删用户", "DELETE", "/api/admin/users/d1user")
    hit("删角色", "DELETE", "/api/admin/permissions/" + rid)
    hit("删部门", "DELETE", "/api/admin/departments/" + did)
    hit("删单位", "DELETE", "/api/admin/units/" + uid)

    # 插件管理面（读面 + 超管门槛）
    hit("插件页", "GET", "/admin/plugins")
    hit("插件列表", "GET", "/api/admin/plugins")
    hit("依赖清单", "GET", "/api/admin/deps")
    hit("非法 id 校验", "POST", "/api/admin/plugins/enable",
        json={"id": "../evil", "enabled": True}, expect=400)

    # 商店管理面（页面 + list/stats + 缺参拒绝）
    hit("商店页", "GET", "/admin/store")
    st = hit("store/list", "GET", "/plugin/admin/store/list")
    assert st.get("ok") is True and "packages" in st
    hit("store/stats", "GET", "/plugin/admin/store/stats")
    hit("store/meta 缺 id", "POST", "/plugin/admin/store/meta", json={}, expect=400)
    hit("store/upload 无文件", "POST", "/plugin/admin/store/upload",
        data={"runtime": "local"}, expect=400)

    # 登出
    hit("登出", "POST", "/api/logout")
    hit("登出后 session", "GET", "/api/session", expect=200)

    print("\n== admin 主流程台架：%d 项通过 ==" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
