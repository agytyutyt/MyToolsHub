package com.jztools.infoparse.util

import java.io.ByteArrayOutputStream
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * 由二维数组构建最小可用的 .xlsx（Office Open XML SpreadsheetML）。
 *
 * 信封 excel 的 data 只承载纯数据二维数组（协议 3.1.1，不保留样式/宏/公式），
 * 此处按 OOXML 最小骨架重建可用 Excel 工作簿：单元格内联字符串（inlineStr，
 * 免共享字符串表），数字单元格写为数值型，布尔写为 1/0。
 * 列标 A..Z、AA..（26 进制），行号从 1 起。
 */
object XlsxWriter {

    private const val XML_DECL = "<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"

    private const val CONTENT_TYPES = XML_DECL +
        "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">" +
        "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>" +
        "<Default Extension=\"xml\" ContentType=\"application/xml\"/>" +
        "<Override PartName=\"/xl/workbook.xml\" " +
        "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>" +
        "<Override PartName=\"/xl/worksheets/sheet1.xml\" " +
        "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>" +
        "<Override PartName=\"/xl/styles.xml\" " +
        "ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml\"/>" +
        "</Types>"

    private const val ROOT_RELS = XML_DECL +
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">" +
        "<Relationship Id=\"rId1\" " +
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" " +
        "Target=\"xl/workbook.xml\"/>" +
        "</Relationships>"

    private const val WORKBOOK = XML_DECL +
        "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" " +
        "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">" +
        "<sheets><sheet name=\"数据\" sheetId=\"1\" r:id=\"rId1\"/></sheets>" +
        "</workbook>"

    private const val WORKBOOK_RELS = XML_DECL +
        "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">" +
        "<Relationship Id=\"rId1\" " +
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" " +
        "Target=\"worksheets/sheet1.xml\"/>" +
        "<Relationship Id=\"rId2\" " +
        "Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles\" " +
        "Target=\"styles.xml\"/>" +
        "</Relationships>"

    private const val STYLES = XML_DECL +
        "<styleSheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">" +
        "<fonts count=\"1\"><font><sz val=\"11\"/><name val=\"Calibri\"/></font></fonts>" +
        "<fills count=\"1\"><fill><patternFill patternType=\"none\"/></fill></fills>" +
        "<borders count=\"1\"><border/></borders>" +
        "<cellStyleXfs count=\"1\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\"/></cellStyleXfs>" +
        "<cellXfs count=\"1\"><xf numFmtId=\"0\" fontId=\"0\" fillId=\"0\" borderId=\"0\" xfId=\"0\"/></cellXfs>" +
        "<cellStyles count=\"1\"><cellStyle name=\"Normal\" xfId=\"0\" builtinId=\"0\"/></cellStyles>" +
        "</styleSheet>"

    /** 数字单元格判定：-1 / 0 / 12.5 等规范写法（排除 007、1e5 等避免数据变形） */
    private val NUMERIC = Regex("-?(0|[1-9]\\d*)(\\.\\d+)?")

    /** 二维数组 → .xlsx 字节流（PK/ZIP 容器） */
    fun fromRows(rows: List<List<Any?>>): ByteArray {
        val sheet = StringBuilder(XML_DECL)
            .append("<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">")
            .append("<sheetData>")
        rows.forEachIndexed { r, row ->
            val rowNum = r + 1
            val cells = StringBuilder()
            row.forEachIndexed { c, cell ->
                val xml = cellXml(colName(c), rowNum, cell)
                if (xml != null) cells.append(xml)
            }
            sheet.append("<row r=\"").append(rowNum).append("\">").append(cells).append("</row>")
        }
        sheet.append("</sheetData></worksheet>")

        val out = ByteArrayOutputStream()
        ZipOutputStream(out).use { zip ->
            fun entry(name: String, content: String) {
                zip.putNextEntry(ZipEntry(name))
                zip.write(content.toByteArray(Charsets.UTF_8))
                zip.closeEntry()
            }
            entry("[Content_Types].xml", CONTENT_TYPES)
            entry("_rels/.rels", ROOT_RELS)
            entry("xl/workbook.xml", WORKBOOK)
            entry("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
            entry("xl/styles.xml", STYLES)
            entry("xl/worksheets/sheet1.xml", sheet.toString())
        }
        return out.toByteArray()
    }

    /** 单个单元格 → XML 片段；null/空串跳过（返回 null，等效空单元格） */
    private fun cellXml(col: String, row: Int, cell: Any?): String? {
        val ref = "$col$row"
        return when (cell) {
            null -> null
            is Boolean -> "<c r=\"$ref\" t=\"b\"><v>${if (cell) 1 else 0}</v></c>"
            is Int, is Long, is Short, is Byte -> "<c r=\"$ref\"><v>$cell</v></c>"
            is Double, is Float -> "<c r=\"$ref\"><v>$cell</v></c>"
            else -> {
                val s = cell.toString()
                when {
                    s.isEmpty() -> null
                    NUMERIC.matches(s) -> "<c r=\"$ref\"><v>$s</v></c>"
                    else -> "<c r=\"$ref\" t=\"inlineStr\"><is><t xml:space=\"preserve\">${escape(s)}</t></is></c>"
                }
            }
        }
    }

    /** 0 起列下标 → 列标（0=A，25=Z，26=AA） */
    private fun colName(index: Int): String {
        var n = index + 1
        val sb = StringBuilder()
        while (n > 0) {
            sb.insert(0, ('A' + (n - 1) % 26))
            n = (n - 1) / 26
        }
        return sb.toString()
    }

    /** XML 文本转义（& < > 三类即可覆盖 t/is 内容） */
    fun escape(s: String): String =
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
}
