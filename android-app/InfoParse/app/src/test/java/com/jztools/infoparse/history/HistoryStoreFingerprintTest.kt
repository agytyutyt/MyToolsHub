package com.jztools.infoparse.history

import com.jztools.infoparse.protocol.Envelope
import com.jztools.infoparse.protocol.Fmt
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** 连续去重指纹：同一文档反复入镜不重复建档，内容不同必须可区分 */
class HistoryStoreFingerprintTest {

    private fun textEnv(
        name: String = "会议通知",
        data: String = "本周五 14:00 开会",
        ext: String? = "txt",
    ) = Envelope(Fmt.TEXT, name, data, ext)

    @Test
    fun `同一文档两次解析 指纹一致`() {
        val a = textEnv()
        val b = textEnv()
        assertEquals(HistoryStore.fingerprint(a), HistoryStore.fingerprint(b))
    }

    @Test
    fun `内容或元数据不同 指纹不同`() {
        val base = HistoryStore.fingerprint(textEnv())
        assertNotEquals(base, HistoryStore.fingerprint(textEnv(name = "另一份文档")))
        assertNotEquals(base, HistoryStore.fingerprint(textEnv(data = "内容不同")))
        assertNotEquals(base, HistoryStore.fingerprint(textEnv(ext = null)))
        assertNotEquals(base, HistoryStore.fingerprint(Envelope(Fmt.MARKDOWN, "会议通知", "本周五 14:00 开会", "txt")))
        assertNotEquals(base, HistoryStore.fingerprint(textEnv(data = "内容不同", ext = null)))
    }

    @Test
    fun `excel 内容等价 指纹一致 结构不同 指纹不同`() {
        val rows = listOf(listOf("姓名", "部门"), listOf("张三", "刑侦"), listOf("李四", null))
        val a = HistoryStore.fingerprint(Envelope(Fmt.EXCEL, "花名册", rows, "xlsx"))
        val b = HistoryStore.fingerprint(
            Envelope(Fmt.EXCEL, "花名册", listOf(listOf("姓名", "部门"), listOf("张三", "刑侦"), listOf("李四", null)), "xlsx")
        )
        assertEquals(a, b)
        // 行序 / 单元格值不同 → 不同文档
        val c = HistoryStore.fingerprint(
            Envelope(Fmt.EXCEL, "花名册", listOf(listOf("张三", "刑侦"), listOf("姓名", "部门"), listOf("李四", null)), "xlsx")
        )
        assertNotEquals(a, c)
        val d = HistoryStore.fingerprint(
            Envelope(Fmt.EXCEL, "花名册", listOf(listOf("姓名", "部门"), listOf("张三", "网安"), listOf("李四", null)), "xlsx")
        )
        assertNotEquals(a, d)
    }

    @Test
    fun `rebuilt 标注不参与指纹`() {
        val a = HistoryStore.fingerprint(Envelope(Fmt.FILE, "report.pdf", "AAAA", null, rebuilt = false))
        val b = HistoryStore.fingerprint(Envelope(Fmt.FILE, "report.pdf", "AAAA", null, rebuilt = true))
        assertEquals(a, b)
    }

    @Test
    fun `指纹为十六进制摘要 稳定可比较`() {
        val fp = HistoryStore.fingerprint(textEnv())
        assertEquals(64, fp.length)
        assertTrue(fp.all { it in "0123456789abcdef" })
    }
}
