package com.jztools.infoparse.export

import org.junit.Assert.assertEquals
import org.junit.Test

/** 导出文件名与 mime 规则（3.1.2 / v1.5 file 格式） */
class ExporterTest {

    @Test
    fun `file 格式文件名原样保留扩展名`() {
        assertEquals("报告.docx", Exporter.exportFileName("file", "报告.docx"))
        assertEquals("表格.xlsx", Exporter.exportFileName("file", "表格.xlsx"))
        assertEquals("说_明.pdf", Exporter.exportFileName("file", "说:明.pdf")) // 非法字符替换
    }

    @Test
    fun `文本类导出追加扩展名且防重复`() {
        assertEquals("会议通知.txt", Exporter.exportFileName("text", "会议通知"))
        assertEquals("笔记.md", Exporter.exportFileName("markdown", "笔记.md"))
        assertEquals("纪要.docx", Exporter.exportFileName("word", "纪要")) // 兼容旧 word 码重建 docx
    }

    @Test
    fun `file mime 按扩展名推断`() {
        assertEquals(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            Exporter.fileMime("报告.DOCX")
        )
        assertEquals("application/pdf", Exporter.fileMime("a.pdf"))
        assertEquals("application/octet-stream", Exporter.fileMime("未知.abc"))
        assertEquals("application/octet-stream", Exporter.fileMime("无扩展名"))
    }
}
