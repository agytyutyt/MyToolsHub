package com.jztools.infoparse.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.util.zip.ZipInputStream

/** XlsxWriter：二维数组 → 最小 .xlsx 容器（v1.6 精简传输 excel 还原） */
class XlsxWriterTest {

    private fun entries(bytes: ByteArray): Map<String, String> {
        val map = mutableMapOf<String, String>()
        ZipInputStream(ByteArrayInputStream(bytes)).use { zip ->
            var e = zip.nextEntry
            while (e != null) {
                map[e.name] = zip.readBytes().toString(Charsets.UTF_8)
                e = zip.nextEntry
            }
        }
        return map
    }

    @Test
    fun `产出为 ZIP 容器且包含 OOXML 最小件`() {
        val bytes = XlsxWriter.fromRows(listOf(listOf("姓名", "部门"), listOf("张三", "刑侦")))
        assertEquals("P", bytes[0].toInt().toChar().toString()) // PK 头
        assertEquals("K", bytes[1].toInt().toChar().toString())
        val names = entries(bytes).keys
        assertTrue(names.containsAll(listOf(
            "[Content_Types].xml", "_rels/.rels", "xl/workbook.xml",
            "xl/worksheets/sheet1.xml", "xl/styles.xml"
        )))
    }

    @Test
    fun `文本单元格 内联字符串并转义`() {
        val sheet = entries(XlsxWriter.fromRows(listOf(listOf("A&B<C>", "甲乙"))))["xl/worksheets/sheet1.xml"]!!
        assertTrue(sheet.contains("t=\"inlineStr\""))
        assertTrue(sheet.contains("A&amp;B&lt;C&gt;"))
        assertTrue(sheet.contains("甲乙"))
    }

    @Test
    fun `数字与布尔单元格按值写出`() {
        val sheet = entries(
            XlsxWriter.fromRows(listOf(listOf(12, 3.5, true, false, "007")))
        )["xl/worksheets/sheet1.xml"]!!
        assertTrue(sheet.contains("<c r=\"A1\"><v>12</v></c>"))
        assertTrue(sheet.contains("<c r=\"B1\"><v>3.5</v></c>"))
        assertTrue(sheet.contains("<c r=\"C1\" t=\"b\"><v>1</v></c>"))
        assertTrue(sheet.contains("<c r=\"D1\" t=\"b\"><v>0</v></c>"))
        // 007 是带前导零的字符串 → 内联字符串保留原样
        assertTrue(sheet.contains("t=\"inlineStr\""))
        assertTrue(sheet.contains("007"))
    }

    @Test
    fun `空单元格与 null 跳过`() {
        val sheet = entries(
            XlsxWriter.fromRows(listOf(listOf("a", null, ""), listOf("b")))
        )["xl/worksheets/sheet1.xml"]!!
        assertTrue(sheet.contains("<row r=\"1\">"))
        assertTrue(sheet.contains("<row r=\"2\">"))
        // B1/C1（null 与空串）不产出单元格
        assertTrue(!sheet.contains("r=\"B1\""))
        assertTrue(!sheet.contains("r=\"C1\""))
    }

    @Test
    fun `列标 26 进制进位`() {
        // Z 之后是 AA（用反射不可行，直接构造 27 列验证）
        val row = (1..27).map { it.toString() }
        val sheet = entries(XlsxWriter.fromRows(listOf(row)))["xl/worksheets/sheet1.xml"]!!
        assertTrue(sheet.contains("r=\"A1\""))
        assertTrue(sheet.contains("r=\"Z1\""))
        assertTrue(sheet.contains("r=\"AA1\""))
    }

    @Test
    fun `空表格产出有效容器`() {
        val map = entries(XlsxWriter.fromRows(emptyList()))
        assertTrue(map.containsKey("xl/worksheets/sheet1.xml"))
        assertTrue(map["xl/worksheets/sheet1.xml"]!!.contains("<sheetData>"))
    }
}
