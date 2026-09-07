package com.jztools.infoparse.util

import java.io.ByteArrayOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * 由逐段纯文本构建最小可用的 .docx（Office Open XML）。
 *
 * 信封 word 的 data 只承载逐段文本（协议 3.1.1，封装端不保留样式/宏/图片），
 * 无法还原原始二进制文件；此处按 OOXML 最小骨架重建可编辑的 Word 文档：
 * 一个文本行 = 一个 <w:p> 段落，空行 = 空段落。
 */
object DocxWriter {

    private const val XML_DECL = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"

    private const val CONTENT_TYPES = XML_DECL +
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">" +
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>" +
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>" +
        "<Override PartName=\"/word/document.xml\" " +
        "ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>" +
        "</Types>"

    private const val ROOT_RELS = XML_DECL +
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">" +
        "<Relationship Id=\"rId1\" " +
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" " +
        "Target=\"word/document.xml\"/>" +
        "</Relationships>"

    /** 逐段文本 → .docx 字节流（PK/ZIP 容器） */
    fun fromText(text: String): ByteArray {
        val body = text.split("\n").joinToString("") { line ->
            val p = line.trimEnd('\r')
            if (p.isEmpty()) {
                "<w:p/>"
            } else {
                "<w:p><w:r><w:t xml:space=\"preserve\">${escape(p)}</w:t></w:r></w:p>"
            }
        }
        val document = XML_DECL +
            "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">" +
            "<w:body>$body<w:sectPr/></w:body></w:document>"

        val out = ByteArrayOutputStream()
        ZipOutputStream(out).use { zip ->
            fun entry(name: String, content: String) {
                zip.putNextEntry(ZipEntry(name))
                zip.write(content.toByteArray(Charsets.UTF_8))
                zip.closeEntry()
            }
            entry("[Content_Types].xml", CONTENT_TYPES)
            entry("_rels/.rels", ROOT_RELS)
            entry("word/document.xml", document)
        }
        return out.toByteArray()
    }

    /** XML 文本转义（& < > 三类即可覆盖 w:t 内容） */
    fun escape(s: String): String =
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
}
