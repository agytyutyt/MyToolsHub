package com.jztools.infoparse.util

import org.junit.Assert.assertEquals
import org.junit.Test

/** CSV 转义与 TSV 测试（文档 4.5.1） */
class CsvTest {

    @Test
    fun `基础行转 CSV 含 BOM`() {
        val csv = Csv.fromRows(
            listOf(
                listOf("姓名", "部门"),
                listOf("张三", "刑侦"),
                listOf("李四", "网安", null),
            )
        )
        assertEquals("\uFEFF姓名,部门\r\n张三,刑侦\r\n李四,网安,\r\n", csv)
    }

    @Test
    fun `含逗号引号换行的字段加引号转义`() {
        val csv = Csv.fromRows(listOf(listOf("a,b", "he said \"hi\"", "line1\nline2")))
        assertEquals("\uFEFF\"a,b\",\"he said \"\"hi\"\"\",\"line1\nline2\"\r\n", csv)
    }

    @Test
    fun `布尔转 TRUE FALSE`() {
        val csv = Csv.fromRows(listOf(listOf(true, false)))
        assertEquals("\uFEFFTRUE,FALSE\r\n", csv)
    }

    @Test
    fun `TSV 用于剪贴板`() {
        val tsv = Csv.toTsv(listOf(listOf("姓名", "部门"), listOf("张三", null)))
        assertEquals("姓名\t部门\n张三\t", tsv)
    }
}
