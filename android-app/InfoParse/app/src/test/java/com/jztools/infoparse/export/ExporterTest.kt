package com.jztools.infoparse.export

import com.jztools.infoparse.protocol.Envelope
import org.junit.Assert.assertEquals
import org.junit.Test

/** 导出文件名与 mime 规则（3.1.2 / v1.5 file 格式 / v1.6 ext 精简还原） */
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
        assertEquals("表格.xlsx", Exporter.exportFileName("excel", "表格")) // excel 重建 xlsx
    }

    @Test
    fun `ext 声明按原始后缀还原`() {
        assertEquals("报告.docx", Exporter.exportFileName("word", "报告", "docx"))
        assertEquals("表格.xlsx", Exporter.exportFileName("excel", "表格", "xlsx"))
        assertEquals("表格.xlsx", Exporter.exportFileName("excel", "表格", "xlsm")) // 宏表降为 xlsx
        assertEquals("表格.xlsx", Exporter.exportFileName("excel", "表格", "xls")) // BIFF 降为 xlsx
        assertEquals("数据.csv", Exporter.exportFileName("excel", "数据", "csv")) // csv 原样还原
        assertEquals("日志.txt", Exporter.exportFileName("text", "日志", "txt"))
        assertEquals("笔记.md", Exporter.exportFileName("markdown", "笔记", "md"))
        assertEquals("笔记.md", Exporter.exportFileName("markdown", "笔记.md", "markdown")) // 防重复后缀
        assertEquals("老稿.docx", Exporter.exportFileName("word", "老稿", "doc")) // doc 二进制降为 docx
    }

    @Test
    fun `ext 归一化规则`() {
        assertEquals("csv", Exporter.normalizedExt("excel", "csv"))
        assertEquals("xlsx", Exporter.normalizedExt("excel", "xls"))
        assertEquals("xlsx", Exporter.normalizedExt("excel", "xlsm"))
        assertEquals("docx", Exporter.normalizedExt("word", "doc"))
        assertEquals(null, Exporter.normalizedExt("excel", "docx")) // 格式与声明错配
        assertEquals(null, Exporter.normalizedExt("text", "csv"))
        assertEquals(null, Exporter.normalizedExt("word", "pdf"))
        assertEquals(null, Exporter.normalizedExt("text", null))
    }

    @Test
    fun `ext 声明对应 mime`() {
        assertEquals("text/plain", Exporter.extMime("txt"))
        assertEquals("text/markdown", Exporter.extMime("md"))
        assertEquals(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            Exporter.extMime("docx")
        )
        assertEquals(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            Exporter.extMime("xlsx")
        )
        assertEquals("text/csv", Exporter.extMime("csv"))
        assertEquals(null, Exporter.extMime(null))
        assertEquals(null, Exporter.extMime("pdf")) // 白名单外
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
