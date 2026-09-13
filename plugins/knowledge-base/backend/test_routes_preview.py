# -*- coding: utf-8 -*-
"""routes.py 集成自测：假 admin 会话 + Flask test client 走上传→渲染→preview 全链路。"""
import io
import json
import os
import sys
import time
import types

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
sys.path.insert(0, BACKEND)

# ---- 伪造 jztools_admin（routes 顶层 try-import）----
admin = types.ModuleType("jztools_admin")
admin_routes = types.ModuleType("jztools_admin.routes")
admin_routes.get_session_user = lambda: {
    "username": "tester", "name": "测试管理员", "super_admin": True,
    "role_id": "role-admin", "unit_id": "", "department_id": ""}
admin_routes.set_operation = lambda op: None
admin.routes = admin_routes
sys.modules["jztools_admin"] = admin
sys.modules["jztools_admin.routes"] = admin_routes

# ---- 以包形式加载 routes（触发 from . import doc_convert / pdf_convert / xlsx_render）----
import importlib  # noqa: E402
pkg = types.ModuleType("kb_backend_test")
pkg.__path__ = [BACKEND]
sys.modules["kb_backend_test"] = pkg
routes = importlib.import_module("kb_backend_test.routes")

from flask import Flask  # noqa: E402

app = Flask(__name__)
routes.register(app)
client = app.test_client()

XLSX_PATH = os.path.join(BACKEND, "out", "sample.xlsx")
with open(XLSX_PATH, "rb") as f:
    blob = f.read()

# ---- 1) status 端点 ----
r = client.get("/api/knowledge-base/status")
data = r.get_json()
print("status:", r.status_code, "| xlsx_preview:", data.get("xlsx_preview"),
      "| pdf_preview:", data.get("pdf_preview"))

# ---- 2) 上传 xlsx ----
r = client.post("/api/knowledge-base/files", data={
    "file": (io.BytesIO(blob), "sample.xlsx"),
    "name": "集成测试表格",
}, content_type="multipart/form-data")
up = r.get_json()
print("upload:", r.status_code, "| html_status:", up.get("html_status"),
      "| pdf_status:", up.get("pdf_status"))
fid = up["id"]

# ---- 3) 等待后台渲染收敛 ----
deadline = time.time() + 15
while time.time() < deadline:
    r = client.get("/api/knowledge-base/files")
    rec = next(f for f in r.get_json()["items"] if f["id"] == fid)
    if rec["html_status"] in ("ok", "failed"):
        break
    time.sleep(0.3)
print("rendered:", rec["html_status"], "| html_ready:", rec["html_ready"],
      "| size:", rec["html_size"], "| error:", rec["html_error"])

# ---- 4) preview 端点 ----
r = client.get(f"/api/knowledge-base/files/{fid}/preview")
pv = r.get_json()
print("preview:", r.status_code, "| content-type:", r.content_type,
      "| sheets:", [s["name"] for s in pv.get("sheets", [])])
assert pv["sheets"][0]["html"].startswith("<table"), "sheet html 开头应为 table"

# ---- 5) 未就绪文件 404（上传前伪造 pending→直接查不存在 id）----
r = client.get("/api/knowledge-base/files/f-nonexist/preview")
print("preview miss:", r.status_code)

# ---- 6) 清理 ----
r = client.delete(f"/api/knowledge-base/files/{fid}")
print("delete:", r.status_code)
files_dir = routes.FILES_DIR
leftover = [f for f in os.listdir(files_dir) if f.startswith(fid)]
print("leftover files:", leftover)

print("ALL ROUTE CHECKS DONE")
