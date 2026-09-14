# -*- coding: utf-8 -*-
r"""dhr 引擎补丁回归自测：编号 numId=0 语义 + 字符缩进/形态覆盖。

对应 vendor/README.md 补丁清单第 3、4 条（上游同步时的验收依据）：
1. numId=0（OOXML「取消编号」）→ 静默按普通段落，不产生告警；
   真正未定义的 numId 仍告警，且消息里 numId 为整数格式（3 而非 3.0）。
2. w:ind 字符单位（leftChars/rightChars/startChars/endChars）→ 输出 margin em；
   w:start/w:end（Strict 别名）在 left/right 缺席时兜底。
3. 首行/悬挂缩进的 px 与 em 两形态按「最近来源整组覆盖」：
   样式的字符缩进不得压过段落直接写的 twips 缩进（反之亦然）。

运行：python test_dhr_indent_numid.py（纯标准库 + 引擎自身，无需 Flask）。
"""
import os
import re
import sys
import zipfile
import io

BACKEND = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND)

import office_render  # noqa: E402

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')

_CT = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>'''

_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

_DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>'''


def _para(text, ppr=""):
    return f'<w:p><w:pPr>{ppr}</w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _docx(paras, styles_xml, numbering_xml=None):
    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                f'<w:document {NS}><w:body>{"".join(paras)}'
                f'<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                f'<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>'
                f'</w:sectPr></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/styles.xml", styles_xml)
        if numbering_xml:
            z.writestr("word/numbering.xml", numbering_xml)
    return buf.getvalue()


_STYLES_BASE = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {NS}><w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Calibri" w:eastAsia="宋体"/><w:sz w:val="24"/>
</w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
{{extra}}</w:styles>'''

_NUMBERING = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering {NS}>
<w:abstractNum w:abstractNumId="0">
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="·"/>
<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>'''


def _css_of(html):
    m = re.search(r"<style>([\s\S]*?)</style>", html)
    assert m, "引擎输出缺少 <style> 块"
    css_text = m.group(1)
    return {mm.group(1): mm.group(2) for mm in
            re.finditer(r"\.kbdoc \.kbdoc-c(\d+)\{([^}]*)\}", css_text)}


def _para_css(html):
    """→ [(段落文本前 6 字, 生效 CSS 声明串)]（class 规则展开）。"""
    css = _css_of(html)
    out = []
    for m in re.finditer(r'<p([^>]*)>(.*?)</p>', html):
        attrs, body = m.group(1), m.group(2)
        text = re.sub(r"<[^>]+>", "", body)[:6]
        cid = re.search(r'class="kbdoc-c(\d+)"', attrs)
        style = re.search(r'style="([^"]*)"', attrs)
        out.append((text, css.get(cid.group(1), "") if cid else (style.group(1) if style else "")))
    return out


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if not cond else ""))
    return bool(cond)


def test_numid_semantics():
    paras = [
        _para("零号", '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="0"/></w:numPr>'),
        _para("正常", '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'),
        _para("缺号", '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="3"/></w:numPr>'),
    ]
    r = office_render.render_word(_docx(paras, _STYLES_BASE.format(extra=""), _NUMBERING))
    ok = True
    ok &= check("numId=0 不告警", not any("numId=0" in w for w in r["warnings"]), str(r["warnings"]))
    ok &= check("未定义 numId 仍告警", any("numId=3 " in w for w in r["warnings"]), str(r["warnings"]))
    ok &= check("告警消息 numId 为整数格式", not any(".0 未在" in w for w in r["warnings"]), str(r["warnings"]))
    ok &= check("numId=1 正常出列表", 'role="listitem"' in r["html"])
    ok &= check("HTML 提示条不含 numId=0",
                "numId=0" not in re.search(r'<div class="kbdoc-warnings">[\s\S]*?</div>', r["html"]).group(0)
                if "kbdoc-warnings" in r["html"] else True)
    return ok


def test_char_indent():
    paras = [
        _para("左缩", '<w:ind w:leftChars="200" w:firstLineChars="200"/>'),
        _para("右缩", '<w:ind w:rightChars="100"/>'),
        _para("别名", '<w:ind w:start="420" w:end="210"/>'),
        _para("悬挂", '<w:ind w:hangingChars="200" w:leftChars="400"/>'),
    ]
    r = office_render.render_word(_docx(paras, _STYLES_BASE.format(extra="")))
    got = dict(_para_css(r["html"]))
    ok = True
    ok &= check("leftChars+firstLineChars → 2em/2em",
                got.get("左缩") == "margin-left:2em;text-indent:2em", repr(got.get("左缩")))
    ok &= check("rightChars → margin-right:1em",
                got.get("右缩") == "margin-right:1em", repr(got.get("右缩")))
    ok &= check("start/end 别名 → 28px/14px",
                got.get("别名") == "margin-left:28px;margin-right:14px", repr(got.get("别名")))
    ok &= check("hangingChars+leftChars → 4em/-2em",
                got.get("悬挂") == "margin-left:4em;text-indent:-2em;padding-left:2em", repr(got.get("悬挂")))
    return ok


def test_form_override():
    """样式（Chars）← 段落（twips）整组覆盖，反向同理。"""
    extra = ('<w:style w:type="paragraph" w:styleId="StyChars"><w:name w:val="StyChars"/>'
             '<w:pPr><w:ind w:leftChars="200" w:firstLineChars="200"/></w:pPr></w:style>'
             '<w:style w:type="paragraph" w:styleId="StyPx"><w:name w:val="StyPx"/>'
             '<w:pPr><w:ind w:left="420" w:firstLine="480"/></w:pPr></w:style>')
    paras = [
        _para("继字", '<w:pStyle w:val="StyChars"/>'),
        _para("继像", '<w:pStyle w:val="StyPx"/>'),
        _para("字转像", '<w:pStyle w:val="StyChars"/><w:ind w:left="420" w:firstLine="480"/>'),
        _para("像转字", '<w:pStyle w:val="StyPx"/><w:ind w:leftChars="200" w:firstLineChars="200"/>'),
        _para("首行零", '<w:pStyle w:val="StyChars"/><w:ind w:firstLine="0"/>'),
    ]
    r = office_render.render_word(_docx(paras, _STYLES_BASE.format(extra=extra)))
    got = dict(_para_css(r["html"]))
    ok = True
    ok &= check("样式 Chars 继承", got.get("继字") == "margin-left:2em;text-indent:2em", repr(got.get("继字")))
    ok &= check("样式 Px 继承", got.get("继像") == "margin-left:28px;text-indent:32px", repr(got.get("继像")))
    ok &= check("样式Chars+段落Px：段落整组赢",
                got.get("字转像") == "margin-left:28px;text-indent:32px", repr(got.get("字转像")))
    ok &= check("样式Px+段落Chars：段落整组赢",
                got.get("像转字") == "margin-left:2em;text-indent:2em", repr(got.get("像转字")))
    ok &= check("样式Chars首行+段落首行0：段落赢",
                got.get("首行零") == "margin-left:2em;text-indent:0px", repr(got.get("首行零")))
    return ok


if __name__ == "__main__":
    if office_render._dhr is None:  # noqa: SLF001
        print("引擎未加载，无法测试")
        sys.exit(2)
    results = [test_numid_semantics(), test_char_indent(), test_form_override()]
    print(f"\n{'全部通过' if all(results) else '存在失败'}（{sum(results)}/{len(results)} 组）")
    sys.exit(0 if all(results) else 1)
