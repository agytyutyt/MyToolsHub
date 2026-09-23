# -*- coding: utf-8 -*-
"""数据迁移（导出 / 导入）回归测试。

覆盖范围
--------
1. **打包口径**：按插件分类（`plugins/<id>/…`）、框架三段、排除易变文件（`.task_cache`、
   `*.bak`、`*.tmp`）、清单（manifest）字段完整；
2. **加密口径（本套件的重点）**：
   - 容器没有口令打不开（口令错误 / 篡改 / 非本系统文件各有可读报错）；
   - 包内**不出现明文密钥**，也不出现"用本机密钥加密的原密文"；
   - 账号与大模型配置里的密文字段改封为口令信封（`JZMIG1:`）；
3. **跨机器导入**：用 A 机密钥导出、导入到密钥完全不同的 B 机后，
   密码 / 身份证 / 用户 LLM Key / 全局 LLM Key **都能用 B 机密钥解开**（这是本功能存在的意义）；
   且 B 机的 `config/.admin_key` 不被改动（导入不搬密钥文件）；
4. **导入安全**：只写勾选的分段；白名单外的条目一律跳过（zip 穿越 / 未声明插件）；
   内容一致的文件跳过（幂等）；被覆盖的文件先备份；备份只留最近 N 份；
5. **HTTP 层**：仅超级管理员可用（普通用户 403、未登录 401）；口令太短 / 空选段 → 400；
   导出下载文件名、解析预览、两步导入（解析 → 确认）全链路；
   导入账号数据后**新账号能登录**（端到端验证"密文改封"真的可用）。

运行：
    python -m pytest test_data_migrate.py -q
    python -m unittest test_data_migrate -v
"""

import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "plugins", "admin", "backend"))

from cryptography.fernet import Fernet  # noqa: E402

import data_migrate as dm  # noqa: E402


def _enc(fernet, value):
    return fernet.encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def make_root(prefix, key, *, username="admin", llm_key="sk-user-secret",
              global_key="sk-global-secret"):
    """造一份"数据根"：账号（含加密的密码/身份证/用户 LLM Key）+ 全局大模型 + 工具注册 + 插件数据。"""
    root = tempfile.mkdtemp(prefix=prefix)
    f = Fernet(key)
    os.makedirs(os.path.join(root, "config"), exist_ok=True)
    os.makedirs(os.path.join(root, "plugins", "case-report", "data"), exist_ok=True)
    os.makedirs(os.path.join(root, "plugins", "case-report", ".task_cache"), exist_ok=True)
    with open(os.path.join(root, "config", "admin.json"), "w", encoding="utf-8") as fp:
        json.dump({
            "secret_key": "x" * 64,
            "units": [{"id": "u1", "name": "管理单位", "departments": [
                {"id": "d1", "name": "管理部门", "users": [
                    {"username": username, "name": "系统管理员",
                     "password": _enc(f, "pbkdf2:sha256:hash"), "idcard": _enc(f, "110101199001011234"),
                     "role": "role-admin", "permissions": ["case-report"],
                     "llm": {"format": "openai", "base_url": "http://x/chat/completions",
                             "api_key": _enc(f, llm_key), "model": "m"}}]}]}],
            "permissions": [{"id": "role-admin", "name": "管理员", "modules": []}],
        }, fp, ensure_ascii=False, indent=2)
    with open(os.path.join(root, "config", "llm.json"), "w", encoding="utf-8") as fp:
        json.dump({"mode": "user", "fallback": True, "provider": {
            "format": "openai", "url": "http://g/chat/completions",
            "api_key": _enc(f, global_key), "model": "gm"}}, fp, ensure_ascii=False, indent=2)
    with open(os.path.join(root, "config", "tools.json"), "w", encoding="utf-8") as fp:
        json.dump({"site": {}, "tools": [{"id": "case-report", "enabled": True}]}, fp, ensure_ascii=False)
    with open(os.path.join(root, "plugins", "case-report", "data", "r1.json"), "w", encoding="utf-8") as fp:
        json.dump({"id": "r1", "fields": {"案件名": "8·16 案"}}, fp, ensure_ascii=False)
    with open(os.path.join(root, "plugins", "case-report", "prompt.json"), "w", encoding="utf-8") as fp:
        json.dump({"system": "提示词"}, fp, ensure_ascii=False)
    # 易变文件：不应进包
    with open(os.path.join(root, "plugins", "case-report", ".task_cache", "junk.tmp"), "w") as fp:
        fp.write("x")
    with open(os.path.join(root, "plugins", "case-report", "data", "old.bak"), "w") as fp:
        fp.write("x")
    return root


def make_base(prefix="jzbase-"):
    """程序目录（只需 manifest 以判定"已安装"）。"""
    base = tempfile.mkdtemp(prefix=prefix)
    os.makedirs(os.path.join(base, "plugins", "case-report"), exist_ok=True)
    with open(os.path.join(base, "plugins", "case-report", "manifest.json"), "w", encoding="utf-8") as fp:
        json.dump({"id": "case-report", "version": "1.3.0"}, fp)
    return base


ALL_SECTIONS = ["accounts", "llm", "tools", "plugin:case-report"]
PASS = "pa55word-strong"


class Base(unittest.TestCase):
    def setUp(self):
        self.key_a = Fernet.generate_key()
        self.key_b = Fernet.generate_key()
        self.fa, self.fb = Fernet(self.key_a), Fernet(self.key_b)
        self.base = make_base()
        self.root_a = make_root("jzA-", self.key_a)
        self.root_b = make_root("jzB-", self.key_b, username="other", llm_key="sk-b-own")

    def tearDown(self):
        for p in (self.base, self.root_a, self.root_b):
            shutil.rmtree(p, ignore_errors=True)

    def export(self, sections=None, passphrase=PASS, root=None):
        return dm.build_package(root or self.root_a, self.base, sections or ALL_SECTIONS,
                                passphrase, self.fa, app_version="2.4.0", source_label="PC-A")

    def zip_of(self, blob, passphrase=PASS):
        zip_bytes, env_key = dm.decrypt_container(blob, passphrase)
        return zipfile.ZipFile(io.BytesIO(zip_bytes)), env_key

    def admin_of(self, root):
        with open(os.path.join(root, "config", "admin.json"), encoding="utf-8") as fp:
            return json.load(fp)

    def first_user(self, root):
        cfg = self.admin_of(root)
        return cfg["units"][0]["departments"][0]["users"][0]


# ==========================================================================
# 1. 打包口径
# ==========================================================================

class TestPacking(Base):
    def test_plan_lists_framework_and_plugins(self):
        plan = dm.plan_export(self.root_a, self.base, {"case-report": "战果录入"})
        by_id = {s["id"]: s for s in plan}
        self.assertEqual(set(by_id), set(ALL_SECTIONS))
        self.assertEqual(by_id["accounts"]["label"], "账号与权限数据")
        self.assertEqual(by_id["plugin:case-report"]["label"], "战果录入")
        self.assertEqual(by_id["plugin:case-report"]["version"], "1.3.0")
        self.assertTrue(by_id["plugin:case-report"]["installed"])
        # .task_cache 与 *.bak 不计入
        self.assertEqual(by_id["plugin:case-report"]["files"], 2)

    def test_zip_layout_is_per_plugin(self):
        blob, manifest = self.export()
        zf, _env = self.zip_of(blob)
        names = sorted(zf.namelist())
        self.assertIn("manifest.json", names)
        self.assertIn("config/admin.json", names)
        self.assertIn("config/llm.json", names)
        self.assertIn("config/tools.json", names)
        self.assertIn("plugins/case-report/data/r1.json", names)
        self.assertIn("plugins/case-report/prompt.json", names)
        self.assertFalse([n for n in names if ".task_cache" in n], names)
        self.assertFalse([n for n in names if n.endswith(".bak")], names)
        self.assertFalse([n for n in names if n.startswith("logs/")], names)
        # 机器密钥绝不进包
        self.assertNotIn("config/.admin_key", names)
        self.assertFalse([n for n in names if ".admin_key" in n or ".app_state" in n], names)

    def test_manifest_fields(self):
        _blob, manifest = self.export()
        self.assertEqual(manifest["format"], dm.FORMAT)
        self.assertEqual(manifest["schema"], dm.SCHEMA)
        self.assertEqual(manifest["source"], "PC-A")
        self.assertEqual(manifest["app_version"], "2.4.0")
        self.assertTrue(manifest["created_at"])
        self.assertEqual({s["id"] for s in manifest["sections"]}, set(ALL_SECTIONS))
        self.assertEqual(manifest["totals"]["files"],
                         sum(s["files"] for s in manifest["sections"]))

    def test_plugin_label_written_into_manifest(self):
        """包要自描述：段名（中文）写进清单，目标机没装该插件时预览也能显示中文名。"""
        _blob, manifest = dm.build_package(self.root_a, self.base, ALL_SECTIONS, PASS, self.fa,
                                           plugin_labels={"case-report": "战果录入"})
        sec = next(s for s in manifest["sections"] if s["id"] == "plugin:case-report")
        self.assertEqual(sec["label"], "战果录入")
        # 预览时本机称呼优先（权威），包内名字兜底
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b, {"case-report": "本机叫法"})
        self.assertEqual(next(s for s in info["sections"]
                              if s["id"] == "plugin:case-report")["label"], "本机叫法")
        info2 = dm.inspect(blob, PASS, self.root_b)
        self.assertEqual(next(s for s in info2["sections"]
                              if s["id"] == "plugin:case-report")["label"], "case-report")

    def test_plugin_not_installed_is_flagged(self):
        """数据根里有数据但程序目录没装的插件：仍可导出，但标注未安装。"""
        os.makedirs(os.path.join(self.root_a, "plugins", "ghost", "data"), exist_ok=True)
        with open(os.path.join(self.root_a, "plugins", "ghost", "data", "a.json"), "w") as fp:
            fp.write("{}")
        plan = dm.plan_export(self.root_a, self.base)
        ghost = next(s for s in plan if s["id"] == "plugin:ghost")
        self.assertFalse(ghost["installed"])

    def test_short_passphrase_rejected(self):
        with self.assertRaises(dm.MigrateError) as ctx:
            self.export(passphrase="short")
        self.assertIn("8 位", str(ctx.exception))

    def test_empty_sections_rejected(self):
        with self.assertRaises(dm.MigrateError):
            dm.build_package(self.root_a, self.base, [], PASS, self.fa)


# ==========================================================================
# 2. 加密口径
# ==========================================================================

class TestEncryption(Base):
    def test_wrong_passphrase_and_tamper(self):
        blob, _m = self.export()
        with self.assertRaises(dm.MigrateError) as ctx:
            dm.decrypt_container(blob, "wrong-pass")
        self.assertIn("口令不正确", str(ctx.exception))
        # 篡改容器末尾（HMAC 覆盖）→ 认证失败
        tampered = blob[:-4] + (b"\x00" if blob[-4:-3] != b"\x00" else b"\x01") + blob[-3:]
        with self.assertRaises(dm.MigrateError):
            dm.decrypt_container(tampered, PASS)
        # 不是本系统的文件
        with self.assertRaises(dm.MigrateError) as ctx:
            dm.decrypt_container(b"PK\x03\x04 not a jzdata file" + b"x" * 40, PASS)
        self.assertIn("不是有效的数据迁移包", str(ctx.exception))

    def test_no_plaintext_and_no_source_machine_ciphertext(self):
        """包内既不能有明文 Key，也不能有"用本机密钥加密的原密文"。"""
        blob, _m = self.export()
        zf, _env = self.zip_of(blob)
        raw = zf.read("config/admin.json").decode("utf-8")
        llm_raw = zf.read("config/llm.json").decode("utf-8")
        self.assertNotIn("sk-user-secret", raw)
        self.assertNotIn("sk-global-secret", llm_raw)
        # 原密文（A 机密钥加密）不得出现：随机挑一个字段重新加密成新密文，
        # 只要包里的值不是"原样搬过来的密文"即可——用 A 机密钥尝试解包内值应当失败
        for token in (json.loads(raw)["units"][0]["departments"][0]["users"][0]["llm"]["api_key"],
                      json.loads(llm_raw)["provider"]["api_key"]):
            self.assertTrue(token.startswith(dm.ENVELOPE_PREFIX), token[:16])
            with self.assertRaises(Exception):
                self.fa.decrypt(token[len(dm.ENVELOPE_PREFIX):].encode("ascii"))

    def test_secret_count_reported(self):
        _blob, manifest = self.export()
        by_id = {s["id"]: s for s in manifest["sections"]}
        self.assertEqual(by_id["accounts"]["secrets"], 3)      # 密码 + 身份证 + 用户 LLM Key
        self.assertEqual(by_id["llm"]["secrets"], 1)           # 全局 LLM Key
        self.assertEqual(by_id["tools"]["secrets"], 0)

    def test_non_secret_json_kept_byte_identical(self):
        """没有密钥字段的 JSON 原字节进包（不重排用户文件格式）。"""
        blob, _m = self.export()
        zf, _env = self.zip_of(blob)
        with open(os.path.join(self.root_a, "plugins", "case-report", "data", "r1.json"), "rb") as fp:
            self.assertEqual(zf.read("plugins/case-report/data/r1.json"), fp.read())


# ==========================================================================
# 3. 跨机器导入（本功能的核心价值）
# ==========================================================================

class TestCrossMachineImport(Base):
    def test_secrets_decrypt_with_target_key(self):
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b, {"case-report": "战果录入"})
        report = dm.apply_import(info["zip_bytes"], info["envelope_key"], self.root_b,
                                 set(ALL_SECTIONS), self.fb)
        self.assertEqual(report["written"], 2)          # admin.json + llm.json 被改写
        self.assertTrue(report["accounts_written"])
        self.assertEqual(report["backed_up"], 2)

        user = self.first_user(self.root_b)
        self.assertEqual(self.fb.decrypt(user["password"].encode()).decode(), "pbkdf2:sha256:hash")
        self.assertEqual(self.fb.decrypt(user["idcard"].encode()).decode(), "110101199001011234")
        self.assertEqual(self.fb.decrypt(user["llm"]["api_key"].encode()).decode(), "sk-user-secret")
        with open(os.path.join(self.root_b, "config", "llm.json"), encoding="utf-8") as fp:
            llm = json.load(fp)
        self.assertEqual(self.fb.decrypt(llm["provider"]["api_key"].encode()).decode(),
                         "sk-global-secret")
        # A 机密钥解不开（说明确实改封成 B 机密钥了）
        with self.assertRaises(Exception):
            self.fa.decrypt(user["password"].encode())

    def test_target_machine_key_file_untouched(self):
        key_file = os.path.join(self.root_b, "config", ".admin_key")
        with open(key_file, "wb") as fp:
            fp.write(self.key_b)
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b)
        dm.apply_import(info["zip_bytes"], info["envelope_key"], self.root_b, set(ALL_SECTIONS), self.fb)
        with open(key_file, "rb") as fp:
            self.assertEqual(fp.read(), self.key_b)     # 导入不搬机器密钥

    def test_plugin_data_imported(self):
        # 改一下 B 机的插件数据，确保导入真的覆盖
        with open(os.path.join(self.root_b, "plugins", "case-report", "data", "r1.json"), "w",
                  encoding="utf-8") as fp:
            json.dump({"id": "r1", "fields": {"案件名": "B 机旧数据"}}, fp, ensure_ascii=False)
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b)
        report = dm.apply_import(info["zip_bytes"], info["envelope_key"], self.root_b,
                                 {"plugin:case-report"}, self.fb)
        self.assertEqual(report["plugins"], ["case-report"])
        self.assertEqual(report["written"], 1)
        with open(os.path.join(self.root_b, "plugins", "case-report", "data", "r1.json"),
                  encoding="utf-8") as fp:
            self.assertEqual(json.load(fp)["fields"]["案件名"], "8·16 案")

    def test_idempotent_second_import(self):
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b)
        dm.apply_import(info["zip_bytes"], info["envelope_key"], self.root_b, set(ALL_SECTIONS), self.fb)
        info2 = dm.inspect(blob, PASS, self.root_b)
        again = dm.apply_import(info2["zip_bytes"], info2["envelope_key"], self.root_b,
                                set(ALL_SECTIONS), self.fb)
        # 同机二次导入：账号 / 大模型两段里的密文每次都会重新加密（Fernet 的 IV 不同），
        # 故这两段仍会写；而插件业务数据与工具注册表内容逐字节一致 → 全部跳过（幂等）
        self.assertEqual(again["plugins"], [])
        self.assertEqual(again["skipped"], 3)
        self.assertEqual(again["written"], 2)


# ==========================================================================
# 4. 导入安全
# ==========================================================================

class TestImportSafety(Base):
    def _inject(self, blob, entries):
        """往包里塞额外条目（模拟恶意/异常包），返回新的包字节。

        **复用原容器的盐**重新封装：字段信封密钥与盐绑定（见 dm.encrypt_container 的告警），
        换盐重封会让包内原有的 JZMIG1 字段全部解不开，那样测的就不是"路径白名单"了。
        """
        zip_bytes, env_key = dm.decrypt_container(blob, PASS)
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as src, \
                zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                dst.writestr(info.filename, src.read(info.filename))
            for name, data in entries.items():
                dst.writestr(name, data)
        iterations, salt = dm.read_container_head(blob)
        container_key, _env = dm._derive_keys(PASS, salt, iterations)
        return dm._wrap_container(buf.getvalue(), salt, iterations, container_key), env_key

    def test_zip_slip_and_unknown_entries_skipped(self):
        blob, _m = self.export()
        blob2, _env = self._inject(blob, {
            "../evil.txt": "x",
            "plugins/not-declared/data.json": "{}",
            "config/secret_key.txt": "x",
        })
        info = dm.inspect(blob2, PASS, self.root_b)
        before = set(os.listdir(self.root_b))
        report = dm.apply_import(info["zip_bytes"], info["envelope_key"], self.root_b,
                                 set(ALL_SECTIONS), self.fb)
        self.assertGreaterEqual(report["skipped"], 3)
        # 白名单外的东西一个都没写出去
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.root_b), "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root_b, "plugins", "not-declared")))
        self.assertFalse(os.path.exists(os.path.join(self.root_b, "config", "secret_key.txt")))
        self.assertFalse(set(os.listdir(self.root_b)) - before - {"backups"})

    def test_only_picked_sections_written(self):
        with open(os.path.join(self.root_b, "config", "admin.json"), "w", encoding="utf-8") as fp:
            json.dump({"units": [], "permissions": []}, fp)
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b)
        dm.apply_import(info["zip_bytes"], info["envelope_key"], self.root_b, {"tools"}, self.fb)
        # 只导工具注册表：账号文件保持 B 机原样（空 units）
        self.assertEqual(self.admin_of(self.root_b)["units"], [])

    def test_backup_and_retention(self):
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b)
        backup_root = os.path.join(self.root_b, "backups", "migrate")
        import datetime as _dt
        stamps = []
        for i in range(5):
            info_i = dm.inspect(blob, PASS, self.root_b)
            rep = dm.apply_import(info_i["zip_bytes"], info_i["envelope_key"], self.root_b,
                                  {"accounts"}, self.fb,
                                  backup_root=backup_root,
                                  now=_dt.datetime(2026, 9, 23, 10, i, 0))
            stamps.append(os.path.basename(rep["backup_dir"]))
        self.assertEqual(dm.cleanup_backups(backup_root, keep=3), 2)
        left = sorted(os.listdir(backup_root))
        self.assertEqual(len(left), 3)
        self.assertEqual(left, sorted(stamps)[-3:])

    def test_unpickable_sections_rejected(self):
        blob, _m = self.export()
        info = dm.inspect(blob, PASS, self.root_b)
        with self.assertRaises(dm.MigrateError):
            dm.apply_import(info["zip_bytes"], info["envelope_key"], self.root_b, set(), self.fb)

    def test_inspect_does_not_write(self):
        snapshot = {}
        for base, _dirs, files in os.walk(self.root_b):
            for name in files:
                full = os.path.join(base, name)
                with open(full, "rb") as fp:
                    snapshot[full] = fp.read()
        blob, _m = self.export()
        dm.inspect(blob, PASS, self.root_b, {"case-report": "战果录入"})
        for full, data in snapshot.items():
            with open(full, "rb") as fp:
                self.assertEqual(fp.read(), data, full)
        self.assertEqual(set(snapshot), {os.path.join(b, n) for b, _d, f in os.walk(self.root_b)
                                          for n in f})


# ==========================================================================
# 5. HTTP 层（需要完整应用）
# ==========================================================================

PLUGINS_UNDER_TEST = ("admin",)
FRAMEWORK_MIGRATE_ROUTES = (
    ("/api/admin/migrate/plan", "admin_api_migrate_plan", ("GET",)),
    ("/api/admin/migrate/export", "admin_api_migrate_export", ("POST",)),
    ("/api/admin/migrate/inspect", "admin_api_migrate_inspect", ("POST",)),
    ("/api/admin/migrate/import", "admin_api_migrate_import", ("POST",)),
    ("/admin/migrate", "admin_migrate_page", ("GET",)),
)


def _load_plugin(pid):
    """按框架的加载方式（jztools_<id> 包 + backend/routes.py）载入插件后端。"""
    pkg_name = "jztools_" + pid.replace("-", "_")
    for name in [n for n in sys.modules if n == pkg_name or n.startswith(pkg_name + ".")]:
        sys.modules.pop(name, None)
    backend = os.path.join(HERE, "plugins", pid, "backend")
    spec = importlib.util.spec_from_file_location(
        pkg_name, os.path.join(backend, "__init__.py"), submodule_search_locations=[backend])
    module = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = module
    spec.loader.exec_module(module)
    return importlib.import_module(pkg_name + ".routes")


class TestHttpLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from flask import Flask
        cls.tmp = tempfile.mkdtemp(prefix="jz-migrate-http-")
        cls._env = os.environ.get("JZTOOLS_DATA_ROOT")
        os.environ["JZTOOLS_DATA_ROOT"] = cls.tmp
        import app as A
        cls.A = A
        A.init_data_root()
        cls.app = Flask("jz-migrate-test")
        cls.routes = _load_plugin("admin")
        cls.routes.register(cls.app)
        # 造点插件数据，让导出有内容
        pdir = os.path.join(cls.tmp, "plugins", "case-report", "data")
        os.makedirs(pdir, exist_ok=True)
        with open(os.path.join(pdir, "r1.json"), "w", encoding="utf-8") as fp:
            json.dump({"id": "r1", "fields": {"案件名": "HTTP 用例"}}, fp, ensure_ascii=False)

    @classmethod
    def tearDownClass(cls):
        for name in list(sys.modules):
            if name.startswith("jztools_"):
                sys.modules.pop(name, None)
        mod = sys.modules.get("app")
        if mod and os.path.abspath(getattr(mod, "__file__", "") or "").startswith(HERE):
            del sys.modules["app"]
        if cls._env is None:
            os.environ.pop("JZTOOLS_DATA_ROOT", None)
        else:
            os.environ["JZTOOLS_DATA_ROOT"] = cls._env
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _client(self, username="admin", password="admin123"):
        c = self.app.test_client()
        r = c.post("/api/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        return c

    def test_anonymous_rejected(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/api/admin/migrate/plan").status_code, 401)
        self.assertEqual(c.get("/admin/migrate").status_code, 302)

    def test_non_super_admin_rejected(self):
        """普通账号拿不到数据迁移：这是能导出全站数据的接口。

        注意用 role-case-handler（modules 为空 = 普通账号）；role-admin 在本系统里
        "拥有全部管理模块"因而**就是超级管理员**，用它测不出越权。
        """
        import json as _json
        p = os.path.join(self.tmp, "config", "admin.json")
        cfg = _json.load(open(p, encoding="utf-8"))
        users = cfg["units"][0]["departments"][0]["users"]
        if not any(u["username"] == "zhangsan" for u in users):
            users.append({"username": "zhangsan", "password": users[0]["password"],
                          "name": "张三", "idcard": "", "role": "role-case-handler",
                          "permissions": [], "llm": {"format": "openai", "base_url": "",
                                                     "api_key": "", "model": ""}})
            with open(p, "w", encoding="utf-8") as fp:
                _json.dump(cfg, fp, ensure_ascii=False, indent=2)
        c = self._client("zhangsan")
        self.assertEqual(c.get("/api/admin/migrate/plan").status_code, 403)
        self.assertEqual(c.post("/api/admin/migrate/export",
                                json={"passphrase": "longenough1",
                                      "sections": ["accounts"]}).status_code, 403)
        self.assertEqual(c.post("/api/admin/migrate/inspect",
                                data={"file": (io.BytesIO(b"x"), "p.jzdata"),
                                      "passphrase": "x"},
                                content_type="multipart/form-data").status_code, 403)
        self.assertEqual(c.post("/api/admin/migrate/import",
                                json={"token": "x", "sections": ["accounts"]}).status_code, 403)
        self.assertEqual(c.get("/admin/migrate").status_code, 302)   # 页面回后台首页

    def test_export_then_import_roundtrip(self):
        admin = self._client()
        # ① 盘点
        plan = admin.get("/api/admin/migrate/plan").get_json()
        ids = [s["id"] for s in plan["sections"]]
        self.assertIn("accounts", ids)
        self.assertIn("plugin:case-report", ids)

        # ② 口令校验
        r = admin.post("/api/admin/migrate/export", json={"passphrase": "123", "sections": ids})
        self.assertEqual(r.status_code, 400)
        self.assertIn("8 位", r.get_json()["error"])
        r = admin.post("/api/admin/migrate/export", json={"passphrase": "longenough1", "sections": []})
        self.assertEqual(r.status_code, 400)

        # ③ 导出下载
        r = admin.post("/api/admin/migrate/export",
                       json={"passphrase": "longenough1", "sections": ids})
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        self.assertIn(".jzdata", r.headers.get("Content-Disposition", ""))
        blob = r.data
        self.assertTrue(blob.startswith(dm.MAGIC))
        self.assertNotIn(b"pbkdf2:sha256", blob)          # 包内无明文口令哈希

        # ④ 解析预览（口令错 → 400）
        r = admin.post("/api/admin/migrate/inspect",
                       data={"file": (io.BytesIO(blob), "p.jzdata"), "passphrase": "wrong-pass"},
                       content_type="multipart/form-data")
        self.assertEqual(r.status_code, 400)
        r = admin.post("/api/admin/migrate/inspect",
                       data={"file": (io.BytesIO(blob), "p.jzdata"), "passphrase": "longenough1"},
                       content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        preview = r.get_json()
        self.assertIn("plugin:case-report", [s["id"] for s in preview["sections"]])
        self.assertTrue(preview["token"])
        self.assertNotIn("envelope_key", preview)          # 密钥材料绝不回传前端

        # ⑤ 导入
        r = admin.post("/api/admin/migrate/import",
                       json={"token": preview["token"], "sections": ["plugin:case-report"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertEqual(r.get_json()["plugins"], [])
        # ⑥ token 一次性：再导一次应提示过期
        r = admin.post("/api/admin/migrate/import",
                       json={"token": preview["token"], "sections": ["plugin:case-report"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("过期", r.get_json()["error"])

    def test_imported_account_can_login(self):
        """端到端：导出账号 → 改密码 → 导入还原 → 用包里的密码仍能登录。"""
        admin = self._client()
        plan = admin.get("/api/admin/migrate/plan").get_json()
        r = admin.post("/api/admin/migrate/export",
                       json={"passphrase": "longenough1", "sections": ["accounts"]})
        blob = r.data
        # 改掉当前管理员密码
        r = admin.post("/api/account/password",
                       json={"old_password": "admin123", "new_password": "changed123"})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:200])
        c2 = self.app.test_client()
        self.assertEqual(c2.post("/api/login", json={"username": "admin",
                                                     "password": "admin123"}).status_code, 401)
        # 导入还原
        admin2 = self._client("admin", "changed123")
        r = admin2.post("/api/admin/migrate/inspect",
                        data={"file": (io.BytesIO(blob), "p.jzdata"), "passphrase": "longenough1"},
                        content_type="multipart/form-data")
        tok = r.get_json()["token"]
        r = admin2.post("/api/admin/migrate/import",
                        json={"token": tok, "sections": ["accounts"]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        self.assertTrue(r.get_json()["accounts_written"])
        # 还原后：原密码可登录（密文改封回本机密钥成功）
        c3 = self.app.test_client()
        self.assertEqual(c3.post("/api/login", json={"username": "admin",
                                                     "password": "admin123"}).status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
