#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""知识库 vendor 补丁核验（只读）——P3-2 ⑥「vendor 补丁脚本化」的**检测**半件。

真源：`plugins/knowledge-base/backend/vendor/README.md` 的补丁清单（1–7 条）。
vendor/ 是外部引擎（xhr/dhr）的**原样拷贝**，升级时"整目录替换后需逐条重打"——
手工重打最怕漏条，且漏条通常是**静默**的（例如补丁 1 只在 Python ≤3.13 上炸）。
本脚本按每条补丁的可核验签名（源码锚点/结构）逐条断言：任一缺失即非零退出。

为什么是"核验"而非"重放（apply）"：本仓不含上游原始源（拷贝自
`D:\TestWorkSpace\xlsx-html-preview\python\`），无法生成权威 diff；对逻辑型补丁
（2/3/4/5）做文本重放会引入不可控风险。因此本脚本负责"替换后一条命令检出缺哪条"，
重打动作仍按 vendor/README.md 逐条执行。见 §B-31 登记。

用法：
    python tools\e2e\kb-vendor-patch-check.py
退出码：0 = 七条齐全；1 = 有缺失。
"""
import io
import os
import re
import sys

# 输出流按 UTF-8 走：结果行含「✅/❌」等非 GBK 字符（同 lint-static-rules 口径）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VENDOR = os.path.join(REPO, "plugins", "knowledge-base", "backend", "vendor")

# 每条：{no, title, checks: [(相对 vendor 的路径, 说明, 正则)]}
# 正则在文件全文（re.M）上匹配；同一补丁可多点位（全部命中才算在）。
PATCHES = [
    {"no": 1, "title": "xhr/__init__.py 补 Optional 导入（上限 ≤3.13 导入即 NameError）",
     "checks": [
         ("xhr/__init__.py", "本地补丁锚点注释", r"本地补丁：见 vendor/README\.md"),
         ("xhr/__init__.py", "Optional 导入", r"^from typing import [^\n]*\bOptional\b"),
     ]},
    {"no": 2, "title": "xhr/renderer/table.py 空单元格不落 style_table[0]（染色 bug）",
     "checks": [
         ("xhr/renderer/table.py", "空格按无样式渲染",
          r"XML 里不存在的格子没有任何样式|缺格一律按无样式"),
         ("xhr/renderer/table.py", "cell 为 None 时 style=None",
          r"if cell is not None:[\s\S]{0,200}else:\s*\n\s*style = None"),
     ]},
    {"no": 3, "title": "dhr/numbering.py numId=0 系「取消编号」非「未定义」",
     "checks": [
         ("dhr/numbering.py", "numId=0 语义注释",
          r"numId=0 是 OOXML 的「取消编号」值|numId=0[\s\S]{0,40}取消编号"),
         ("dhr/numbering.py", "num_id==0 短路处理",
          r"if int\(num_id\) == 0:"),
     ]},
    {"no": 4, "title": "dhr 字符缩进（leftChars…）解析＋em 输出（缩进丢失/覆盖）",
     "checks": [
         ("dhr/styles.py", "形态标记 _fl_form", r"setattr\(p, \"_fl_form\""),
         ("dhr/styles.py", "字符缩进 em 记录", r"setattr\(p, \"_left_chars_em\""),
         ("dhr/renderer/css.py", "字符缩进按 em 输出", r"margin-left:\{_num\(left_em\)\}em"),
     ]},
    {"no": 5, "title": "dhr/__init__.py normalize_doc 按渲染模式分流警告文案",
     "checks": [
         ("dhr/__init__.py", "normalize_doc 带 mode 形参",
          r"def normalize_doc\([^)]*mode"),
         ("dhr/__init__.py", "_doc_warning(mode) 分流", r"_doc_warning\(mode\)"),
     ]},
    {"no": 6, "title": "xhr/normalize/libreoffice.py profile 提供者接缝（本地适配 · P2-21）",
     "checks": [
         ("xhr/normalize/libreoffice.py", "接缝锚点", r"【本地适配 · P2-21】profile 提供者接缝"),
         ("xhr/normalize/libreoffice.py", "set_profile_provider", r"def set_profile_provider\("),
         ("xhr/normalize/libreoffice.py", "_acquire_profile", r"def _acquire_profile\("),
     ]},
    {"no": 7, "title": "dhr/normalize.py profile 提供者接缝（本地适配 · P2-21）",
     "checks": [
         ("dhr/normalize.py", "接缝锚点", r"【本地适配 · P2-21】profile 提供者接缝"),
         ("dhr/normalize.py", "set_profile_provider", r"def set_profile_provider\("),
         ("dhr/normalize.py", "_acquire_profile", r"def _acquire_profile\("),
     ]},
]


def run():
    print("知识库 vendor 补丁核验（真源：vendor/README.md 清单；%d 条）" % len(PATCHES))
    print("vendor 目录：%s" % os.path.relpath(VENDOR, REPO))
    print()
    missing = 0
    for p in PATCHES:
        ok = True
        lines = []
        for rel, why, rx in p["checks"]:
            path = os.path.join(VENDOR, rel.replace("/", os.sep))
            if not os.path.isfile(path):
                ok = False
                lines.append("        ❌ %s：文件不存在 %s" % (why, rel))
                continue
            txt = io.open(path, encoding="utf-8", errors="replace").read()
            if re.search(rx, txt, re.M):
                lines.append("        ✅ %s" % why)
            else:
                ok = False
                lines.append("        ❌ %s（未命中 /%s/）" % (why, rx))
        head = "✅ 补丁 %d" % p["no"] if ok else "❌ 补丁 %d" % p["no"]
        print("%s —— %s" % (head, p["title"]))
        for ln in lines:
            print(ln)
        if not ok:
            missing += 1
    print()
    if missing:
        print("结果：%d/%d 条补丁缺失或签名不符——上游整目录替换后请按 vendor/README.md 重打。" %
              (missing, len(PATCHES)))
        return 1
    print("结果：%d/%d 条补丁签名齐全 [OK]。" % (len(PATCHES), len(PATCHES)))
    return 0


if __name__ == "__main__":
    sys.exit(run())
