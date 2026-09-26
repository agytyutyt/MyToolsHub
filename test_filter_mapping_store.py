# -*- coding: utf-8 -*-
"""过滤器「自学习字段映射存储」单元测试（mapping_store.py，自学习方案 P0）。

钉住的契约
----------
1. 两级信任：confirmed（用户确认，含 target="" 负映射）> suggested（大模型建议）；
   confirmed 永不被建议覆盖，/apply 式的 include_suggested=False 只吃 confirmed；
2. rejected 拦截：被否决的映射对不再入库（防回锅）；confirm 会把该对从拦截表摘除；
3. orphan：名单（keep_columns）变更后，指向已不存在目标的映射不参与匹配、
   列表标 orphan、不进黄金评测集；
4. 压缩键兜底：仅 confirmed、同压缩键下有效目标唯一才生效（歧义不猜）；
5. 落盘：tmp + os.replace 原子替换，坏文件按空库重来，并发 confirm 不写坏文件。

运行：
    python -m pytest test_filter_mapping_store.py -q
    python -m unittest test_filter_mapping_store -v   # 无需 pytest
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PLUGIN_DIR = os.path.join(HERE, "plugins", "file-filter")

# 与 test_filter_preview.py 同一份保留名单口径（规整后）
KEEP = {"姓名", "所属单位", "开始时间"}


def load_store_module():
    """复刻框架的 jztools_<id> 动态加载，只取 mapping_store（不拉起 routes/Flask）。"""
    backend = os.path.join(PLUGIN_DIR, "backend")
    spec = importlib.util.spec_from_file_location(
        "jztools_file_filter", os.path.join(backend, "__init__.py"),
        submodule_search_locations=[backend])
    module = importlib.util.module_from_spec(spec)
    sys.modules["jztools_file_filter"] = module
    spec.loader.exec_module(module)
    return importlib.import_module("jztools_file_filter.mapping_store")


MS = load_store_module()


class StoreCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jz-mapstore-")
        self.path = os.path.join(self.tmp, "data", "plugins", "file-filter", "mappings.json")
        self.store = MS.MappingStore(self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- 键规整 ----------

    def test_norm_and_compact_keys(self):
        """主键与硬过滤同口径（去 BOM/全角空格/首尾空白 + casefold）；压缩键再去内部空白。"""
        self.assertEqual(MS.norm_key(" 开始　时间 "), "开始 时间")
        self.assertEqual(MS.norm_key(" Name "), "name")
        self.assertEqual(MS.compact_key("开 始　时间"), "开始时间")
        self.assertEqual(MS.compact_key("A B\tC"), "abc")
        self.assertEqual(MS.compact_key("   "), "")

    # ---------- confirmed / 负映射 / 压缩键 ----------

    def test_confirm_then_lookup_exact_and_compact(self):
        res = self.store.confirm([{"key": "姓名", "target": "姓名", "sample": "姓 名"}],
                                 user="op", valid_targets=KEEP)
        self.assertEqual(res, {"confirmed": 1, "conflicts": []})
        hit = self.store.lookup("姓名", KEEP)
        self.assertEqual(hit["target"], "姓名")
        self.assertEqual(hit["status"], "confirmed")
        self.assertEqual(hit["sample"], "姓 名")
        # 压缩键兜底：内部多空格的脏表头也能命中已确认映射
        self.assertEqual(self.store.lookup("姓  名", KEEP)["target"], "姓名")
        # 程序化调用口径（include_suggested=False）不影响已确认映射
        self.assertEqual(self.store.lookup("姓名", KEEP, include_suggested=False)["target"], "姓名")

    def test_negative_mapping_confirmed_drop(self):
        """target=""（确认删除）是合法的负映射，且不依赖名单校验。"""
        self.store.confirm([{"key": "备注2", "target": ""}], user="op", valid_targets=KEEP)
        hit = self.store.lookup("备注2", set())
        self.assertEqual((hit["target"], hit["status"]), ("", "confirmed"))

    def test_compact_ambiguity_not_guessed(self):
        """同压缩键下两个 confirmed 不同目标 → 歧义，宁可不命中也不误合并。"""
        self.store.confirm([{"key": "开始 时间", "target": "姓名"}], user="a", valid_targets=KEEP)
        self.store.confirm([{"key": "开 始时间", "target": "所属单位"}], user="b", valid_targets=KEEP)
        self.assertIsNone(self.store.lookup("开始时间", KEEP))
        # 消歧后恢复命中
        self.store.delete("开 始时间")
        self.assertEqual(self.store.lookup("开始时间", KEEP)["target"], "姓名")

    # ---------- suggested：记录 / 覆盖口径 / 程序化开关 ----------

    def test_suggested_recorded_and_gated_by_include_suggested(self):
        n = self.store.record_suggestions(["人员"], {"人员": "姓名"}, KEEP,
                                          model="test-model", prompt_ver=1, user="op")
        self.assertEqual(n, 1)
        hit = self.store.lookup("人员", KEEP)
        self.assertEqual((hit["target"], hit["status"]), ("姓名", "suggested"))
        # /apply 口径：没有把关环节，建议级不参与
        self.assertIsNone(self.store.lookup("人员", KEEP, include_suggested=False))

    def test_suggested_never_overwrites_confirmed(self):
        self.store.confirm([{"key": "备注", "target": "姓名"}], user="op", valid_targets=KEEP)
        n = self.store.record_suggestions(["备注"], {"备注": "所属单位"}, KEEP)
        self.assertEqual(n, 0)
        self.assertEqual(self.store.lookup("备注", KEEP)["target"], "姓名")

    def test_suggested_latest_wins_keeps_hits(self):
        self.store.record_suggestions(["人员"], {"人员": "姓名"}, KEEP)
        self.store.lookup("人员", KEEP)  # hits +1
        self.store.record_suggestions(["人员"], {"人员": "所属单位"}, KEEP)
        hit = self.store.lookup("人员", KEEP)
        self.assertEqual((hit["target"], hit["status"]), ("所属单位", "suggested"))
        # 两次 lookup 各计一次命中；建议回写不清零 hits
        self.assertEqual(self.store.list_entries(KEEP)[0]["hits"], 2)

    def test_out_of_list_suggestion_not_recorded(self):
        """名单外的值不记录（防幻觉；match_columns 已兜底，这里兜第二道）。"""
        n = self.store.record_suggestions(["人员"], {"人员": "不存在的字段"}, KEEP)
        self.assertEqual(n, 0)

    # ---------- rejected：拦截回锅 / confirm 摘除 ----------

    def test_reject_blocks_resuggestion_and_deletes_entry(self):
        self.store.record_suggestions(["备注"], {"备注": "姓名"}, KEEP)
        self.assertTrue(self.store.reject("备注", "姓名", user="op"))
        # 同名同目标的既有建议条目被删除；该对不再入库
        self.assertIsNone(self.store.lookup("备注", KEEP))
        self.assertEqual(self.store.record_suggestions(["备注"], {"备注": "姓名"}, KEEP), 0)
        self.assertTrue(self.store.is_rejected("备注", "姓名"))
        # 否决「删除」（空目标）没有意义
        self.assertFalse(self.store.reject("备注", "", user="op"))

    def test_confirm_removes_rejected_pair(self):
        self.store.reject("人员", "姓名", user="op")
        self.store.confirm([{"key": "人员", "target": "姓名"}], user="op", valid_targets=KEEP)
        self.assertFalse(self.store.is_rejected("人员", "姓名"))
        self.assertEqual(self.store.lookup("人员", KEEP)["status"], "confirmed")

    # ---------- confirm：冲突 / 越名单 ----------

    def test_conflict_requires_force(self):
        self.store.confirm([{"key": "人员", "target": "姓名"}], user="a", valid_targets=KEEP)
        res = self.store.confirm([{"key": "人员", "target": "所属单位"}], user="b",
                                 valid_targets=KEEP)
        self.assertEqual(res["conflicts"], [{"key": "人员", "target": "所属单位",
                                             "existing_target": "姓名"}])
        self.assertEqual(self.store.lookup("人员", KEEP)["target"], "姓名")
        res = self.store.confirm([{"key": "人员", "target": "所属单位"}], user="b",
                                 valid_targets=KEEP, force=True)
        self.assertEqual(res["confirmed"], 1)
        self.assertEqual(self.store.lookup("人员", KEEP)["target"], "所属单位")

    def test_confirm_rejects_out_of_list_target(self):
        res = self.store.confirm([{"key": "人员", "target": "不存在的字段"}],
                                 user="op", valid_targets=KEEP)
        self.assertEqual(res["confirmed"], 0)
        self.assertIsNone(self.store.lookup("人员", KEEP))

    # ---------- orphan：名单变更 ----------

    def test_orphan_after_list_change(self):
        self.store.confirm([{"key": "人员", "target": "姓名"}], user="op", valid_targets=KEEP)
        narrowed = KEEP - {"姓名"}  # 模拟管理员把「姓名」从名单里删掉/改名
        self.assertIsNone(self.store.lookup("人员", narrowed))
        rows = self.store.list_entries(narrowed)
        self.assertTrue(rows[0]["orphan"])
        self.assertEqual(self.store.export_records(narrowed), [])  # 失配条目不进评测集
        self.assertEqual(self.store.export_records(KEEP)[0]["header"], "人员")

    # ---------- 落盘与并发 ----------

    def test_persistence_roundtrip_and_no_tmp_leftover(self):
        self.store.confirm([{"key": "姓名", "target": "姓名"}], user="op", valid_targets=KEEP)
        self.store.reject("人员", "籍贯", user="op")
        self.store.bump_stats(llm_saved=2, misses=5)
        again = MS.MappingStore(self.path)  # 新实例 = 模拟进程重启
        self.assertEqual(again.lookup("姓名", KEEP)["target"], "姓名")
        self.assertTrue(again.is_rejected("人员", "籍贯"))
        self.assertEqual(again.stats()["llm_saved"], 2)
        self.assertFalse(any(f.endswith(".tmp") for f in os.listdir(os.path.dirname(self.path))))

    def test_corrupt_file_starts_fresh(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "wb") as f:
            f.write(b"not json {{{")
        store = MS.MappingStore(self.path)
        self.assertIsNone(store.lookup("姓名", KEEP))
        self.assertEqual(store.confirm([{"key": "姓名", "target": "姓名"}],
                                       user="op", valid_targets=KEEP)["confirmed"], 1)

    def test_concurrent_confirms_no_corruption(self):
        def worker(n):
            for i in range(25):
                self.store.confirm([{"key": f"f{n:02d}_{i:02d}", "target": "姓名"}],
                                   user="op", valid_targets=KEEP)
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        again = MS.MappingStore(self.path)
        rows = again.list_entries(KEEP)
        self.assertEqual(len(rows), 200)
        self.assertTrue(all(r["status"] == "confirmed" for r in rows))

    # ---------- 管理：列表 / 清空 / 统计 ----------

    def test_list_entries_status_and_q_filters(self):
        self.store.confirm([{"key": "姓名", "target": "姓名"}], user="op", valid_targets=KEEP)
        self.store.record_suggestions(["人员"], {"人员": "所属单位"}, KEEP)
        self.assertEqual([r["key"] for r in self.store.list_entries(KEEP, status="confirmed")],
                         ["姓名"])
        self.assertEqual([r["key"] for r in self.store.list_entries(KEEP, status="suggested")],
                         ["人员"])
        self.assertEqual([r["key"] for r in self.store.list_entries(KEEP, q="人员")], ["人员"])

    def test_clear_keeps_stats(self):
        self.store.confirm([{"key": "姓名", "target": "姓名"}], user="op", valid_targets=KEEP)
        self.store.bump_stats(llm_saved=3)
        self.store.clear()
        self.assertEqual(self.store.list_entries(KEEP), [])
        self.assertEqual(self.store.stats()["llm_saved"], 3)

    def test_bump_stats_ignores_unknown_and_zero(self):
        before = self.store.stats()
        self.store.bump_stats(hit_exact=2, bogus_key=9, llm_saved=0)
        after = self.store.stats()
        self.assertEqual(after["hit_exact"], before["hit_exact"] + 2)
        self.assertEqual(after["llm_saved"], before["llm_saved"])
        self.assertNotIn("bogus_key", after)

    # ---------- 上限（管理员可配；不采用主动清理策略） ----------

    def test_record_limit_gates_new_suggestions_only(self):
        """上限只拦自动新增：已有条目可更新，confirm 不受限，调低不删除已有条目。"""
        self.assertEqual(self.store.record_suggestions(
            ["人员"], {"人员": "姓名"}, KEEP, limit=1), 1)
        # 到顶：新表头不再自动记录
        self.assertEqual(self.store.record_suggestions(
            ["访客"], {"访客": "姓名"}, KEEP, limit=1), 0)
        self.assertIsNone(self.store.lookup("访客", KEEP))
        # 已有条目的更新不受限
        self.assertEqual(self.store.record_suggestions(
            ["人员"], {"人员": "所属单位"}, KEEP, limit=1), 1)
        self.assertEqual(self.store.lookup("人员", KEEP)["target"], "所属单位")
        # 用户动作（confirm）不受限
        res = self.store.confirm([{"key": "访客", "target": "姓名"}], user="op",
                                 valid_targets=KEEP)
        self.assertEqual(res["confirmed"], 1)
        # 非法 limit 回退内置缺省，不抛错
        self.assertEqual(self.store.record_suggestions(
            ["临时"], {"临时": "姓名"}, KEEP, limit="abc"), 1)

    def test_count(self):
        self.assertEqual(self.store.count(), 0)
        self.store.confirm([{"key": "姓名", "target": "姓名"}], user="op", valid_targets=KEEP)
        self.store.record_suggestions(["人员"], {"人员": "所属单位"}, KEEP)
        self.assertEqual(self.store.count(), 2)


if __name__ == "__main__":
    unittest.main()
