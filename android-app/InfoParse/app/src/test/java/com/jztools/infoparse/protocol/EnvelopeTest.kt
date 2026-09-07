package com.jztools.infoparse.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** 协议层测试：信封校验（3.1）、多页状态机（3.3）、错误文案（3.5） */
class EnvelopeTest {

    private fun collector() = PageCollector()

    private fun single(json: String): ScanResult =
        EnvelopeParser.handle(json, collector())

    @Test
    fun `单张 text 信封 直出`() {
        val r = single("""{"jzt":1,"fmt":"text","name":"会议通知","data":"本周五 14:00 在三楼会议室召开季度总结会，请准时参加。"}""")
        val env = (r as ScanResult.Single).envelope
        assertEquals("text", env.fmt)
        assertEquals("会议通知", env.name)
        assertEquals("本周五 14:00 在三楼会议室召开季度总结会，请准时参加。", env.textData)
    }

    @Test
    fun `markdown 信封`() {
        val r = single("""{"jzt":1,"fmt":"markdown","name":"值班安排","data":"# 值班安排\n\n- 周一：张三\n- 周二：李四"}""")
        assertEquals("markdown", (r as ScanResult.Single).envelope.fmt)
    }

    @Test
    fun `excel 信封 数字按原始文本保留`() {
        val r = single("""{"jzt":1,"fmt":"excel","name":"花名册","data":[["姓名","部门"],["张三","刑侦",1],["李四","网安",null]]}""")
        val env = (r as ScanResult.Single).envelope
        val rows = env.data as List<List<Any?>>
        assertEquals(3, rows.size)
        assertEquals("1", rows[1][2]) // Gson 1 → "1"（避免 1.0）
        assertEquals(null, rows[2][2])
        assertEquals("刑侦", rows[1][1])
    }

    @Test
    fun `file 信封 base64 还原原始字节`() {
        val raw = byteArrayOf(0x50, 0x4B, 0x03, 0x04, 0x00, 0xFF.toByte()) // 含不可打印字节
        val b64 = java.util.Base64.getEncoder().encodeToString(raw)
        val r = single("""{"jzt":1,"fmt":"file","name":"报告.docx","data":"$b64"}""")
        val env = (r as ScanResult.Single).envelope
        assertEquals("file", env.fmt)
        assertEquals("报告.docx", env.name) // file 格式 name 含扩展名
        assertTrue(env.isFile)
        assertTrue(raw.contentEquals(env.fileBytes()))
    }

    @Test
    fun `file 信封 base64 非法时 fileBytes 返回 null`() {
        val r = single("""{"jzt":1,"fmt":"file","name":"x.bin","data":"@@非法##"}""")
        val env = (r as ScanResult.Single).envelope
        assertNull(env.fileBytes())
    }

    @Test
    fun `jzt 不等于 1 拒绝`() {
        val r = single("""{"jzt":2,"fmt":"text","name":"x","data":"y"}""")
        assertEquals(Msg.BAD_VERSION, (r as ScanResult.Invalid).reason)
    }

    @Test
    fun `未知 fmt 提示原值`() {
        val r = single("""{"jzt":1,"fmt":"pdf","name":"x","data":"y"}""")
        assertEquals("未知的文档格式声明：pdf", (r as ScanResult.Invalid).reason)
    }

    @Test
    fun `excel data 非数组拒绝`() {
        val r = single("""{"jzt":1,"fmt":"excel","name":"x","data":"bad"}""")
        assertEquals(Msg.EXCEL_BAD, (r as ScanResult.Invalid).reason)
    }

    @Test
    fun `excel 元素含嵌套数组拒绝`() {
        val r = single("""{"jzt":1,"fmt":"excel","name":"x","data":[["a",["b"]]]}""")
        assertEquals(Msg.EXCEL_BAD, (r as ScanResult.Invalid).reason)
    }

    @Test
    fun `非 JSON 文本拒绝`() {
        val r = single("https://example.com/abc")
        assertEquals(Msg.NOT_ENVELOPE, (r as ScanResult.Invalid).reason)
    }

    @Test
    fun `JSON 解析失败拒绝`() {
        val r = single("{not-json")
        assertEquals(Msg.NOT_ENVELOPE, (r as ScanResult.Invalid).reason)
    }

    // ---------- 多页状态机（3.3 规则 1-6） ----------

    private fun jsonPage(i: Int, n: Int, dataFragment: String, headData: String? = null): String {
        // data 值 = dataFragment 的 JSON 字符串转义（与封装端 json.dumps 行为一致）
        val dataJson = com.google.gson.Gson().toJson(dataFragment)
        return if (headData != null) {
            """{"jzt":1,"fmt":"text","name":"长文示例","pg":{"i":$i,"n":$n},"data":$dataJson}"""
        } else {
            """{"jzt":1,"pg":{"i":$i,"n":$n},"data":$dataJson}"""
        }
    }

    private fun page(i: Int, n: Int, data: String, head: Boolean = false) =
        jsonPage(i, n, data, if (head) "head" else null)

    private fun fullEnvelope(data: String) =
        """{"jzt":1,"fmt":"text","name":"长文示例","data":"$data"}"""

    @Test
    fun `多页乱序收集后重组`() {
        val c = collector()
        // 完整信封 JSON 文本均分 3 段（模拟封装端 _build_static_pages 的字节切分）
        val envText = fullEnvelope("甲乙丙丁戊己")
        val per = envText.length / 3
        val segs = listOf(
            envText.substring(0, per),
            envText.substring(per, per * 2),
            envText.substring(per * 2),
        )
        // 乱序：3 → 1 → 2（页 1 携带 fmt/name）
        val r1 = EnvelopeParser.handle(page(3, 3, segs[2]), c)
        assertTrue(r1 is ScanResult.Page)
        val r2 = EnvelopeParser.handle(page(1, 3, segs[0], head = true), c)
        assertTrue(r2 is ScanResult.Page)
        val r3 = EnvelopeParser.handle(page(2, 3, segs[1]), c)
        assertTrue(r3 is ScanResult.Single)
        assertEquals("长文示例", (r3 as ScanResult.Single).envelope.name)
    }

    @Test
    fun `页数不一致的页被忽略`() {
        val c = collector()
        EnvelopeParser.handle(page(1, 5, "a", head = true), c)
        val r = EnvelopeParser.handle(page(2, 4, "b"), c)
        assertEquals(Msg.PAGE_MISMATCH, (r as ScanResult.Invalid).reason)
        assertEquals(1, c.have)
    }

    @Test
    fun `同页重复扫描覆盖`() {
        val c = collector()
        EnvelopeParser.handle(page(1, 3, "a1", head = true), c)
        val r = EnvelopeParser.handle(page(1, 3, "a2", head = true), c)
        assertTrue(r is ScanResult.Page)
        assertEquals(1, c.have)
    }

    @Test
    fun `未集齐提示缺失页码`() {
        val c = collector()
        EnvelopeParser.handle(page(1, 5, "a", head = true), c)
        EnvelopeParser.handle(page(2, 5, "b"), c)
        EnvelopeParser.handle(page(3, 5, "c"), c)
        assertEquals(listOf(4, 5), c.missing())
        assertEquals("已收集 3/5 张，还差：第 4、5 张", c.progressText())
    }

    @Test
    fun `缺失页码超过 5 个截断加省略号`() {
        val c = collector()
        EnvelopeParser.handle(page(1, 8, "a", head = true), c)
        assertEquals("已收集 1/8 张，还差：第 2、3、4、5、6…张", c.progressText())
    }

    @Test
    fun `拼接后 JSON 损坏 → 还原失败`() {
        val c = collector()
        EnvelopeParser.handle(page(1, 2, """{"jzt":1,"fmt""", head = true), c)
        val r = EnvelopeParser.handle(page(2, 2, "\"xx}"), c)
        assertEquals(Msg.ASSEMBLE_FAILED, (r as ScanResult.Invalid).reason)
    }

    @Test
    fun `序列化恢复后继续收集`() {
        val c = PageCollector()
        // 与「多页乱序收集后重组」一致：分片取自真实信封 JSON 切分，拼回才可解析
        val envText = fullEnvelope("甲乙丙丁戊己")
        val per = envText.length / 3
        val segs = listOf(
            envText.substring(0, per),
            envText.substring(per, per * 2),
            envText.substring(per * 2),
        )
        EnvelopeParser.handle(page(1, 3, segs[0], head = true), c)
        EnvelopeParser.handle(page(2, 3, segs[1]), c)
        val restored = PageCollector.restore(c.serialize())!!
        assertEquals(3, restored.total)
        assertEquals("text", restored.fmt)
        assertEquals("长文示例", restored.name)
        assertEquals(2, restored.have)
        val r = EnvelopeParser.handle(page(3, 3, segs[2]), restored)
        assertTrue(r is ScanResult.Single)
    }

    // ---------- 形态C：帧头解析（附录 B worked example） ----------

    @Test
    fun `帧头样例 解析正确`() {
        // 附录 B worked example：帧 0 内容前 16 字符（载荷为截断样例）
        val (head, payload) = QrFrame.parseHead("AAAAAAAKAAYK" + "AAAI")!!
        assertEquals(0, head.idx)
        assertEquals(10, head.n)
        assertEquals(6, head.k)
        assertEquals(10, head.m)
        assertEquals(3, payload.size)
        assertEquals(0x08, payload[2].toInt() and 0xFF)
    }

    @Test
    fun `非法帧头返回 null`() {
        val b64 = java.util.Base64.getEncoder()
        // 保留位非 0（第 7 字节应为 0x00）
        assertNull(QrFrame.parseHead(b64.encodeToString(byteArrayOf(0, 0, 0, 0, 0, 10, 1, 6, 10)) + "AAAA"))
        // k < 1
        assertNull(QrFrame.parseHead(b64.encodeToString(byteArrayOf(0, 0, 0, 0, 0, 10, 0, 0, 1)) + "AAAA"))
        // m < k
        assertNull(QrFrame.parseHead(b64.encodeToString(byteArrayOf(0, 0, 0, 0, 0, 10, 0, 6, 5)) + "AAAA"))
        // 长度不足
        assertNull(QrFrame.parseHead("short"))
    }

    @Test
    fun `系统位重组 完整10帧`() {
        // 构造 n=10, k=6, m=10, nblocks=1 的流：数据 = 4B 长度 + JSON
        val json = """{"jzt":1,"fmt":"text","name":"示例","data":"XXX"}"""
        val dataBytes = json.toByteArray(Charsets.UTF_8)
        val buf = ByteArray(4 + dataBytes.size + 2) { 0 } // 尾部少量填充
        buf[0] = (dataBytes.size shr 24).toByte()
        buf[1] = (dataBytes.size shr 16).toByte()
        buf[2] = (dataBytes.size shr 8).toByte()
        buf[3] = dataBytes.size.toByte()
        dataBytes.copyInto(buf, 4)
        // 分成 6 块（k=6）
        val chunk = (buf.size + 5) / 6
        val c = QrFrame.Collector()
        val b64 = java.util.Base64.getEncoder()
        for (s in 0 until 10) {
            val begin = (s % 6) * chunk
            val payload = ByteArray(chunk).also { p ->
                buf.copyOfRange(begin, minOf(begin + chunk, buf.size)).copyInto(p)
            }
            val headB64 = b64.encodeToString(
                byteArrayOf(0, 0, s.toByte(), 0, 0, 10, 0, 6, 10)
            )
            val payloadB64 = b64.encodeToString(payload)
            c.offer(headB64 + payloadB64)
        }
        assertEquals(json, c.collectToEnvelopeText())
    }

    @Test
    fun `缺帧时系统位重组返回 null`() {
        val c = QrFrame.Collector()
        val b64 = java.util.Base64.getEncoder()
        for (s in 0 until 5) { // n=10 只给 5 帧
            c.offer(
                b64.encodeToString(byteArrayOf(0, 0, s.toByte(), 0, 0, 10, 0, 6, 10)) +
                    b64.encodeToString(byteArrayOf(1, 2, 3, 4))
            )
        }
        assertNull(c.collectToEnvelopeText())
    }

    @Test
    fun `重复帧去重 同序号多次offer不影响计数与重组`() {
        // 相机传输模式：同一二维码连续重复渲染多帧、循环播放再次出现 → 同 idx 反复 offer
        // Collector 以 idx 为键覆盖存储（HashMap），have 不增长、重组不受影响
        val json = """{"jzt":1,"fmt":"text","name":"示例","data":"XXX"}"""
        val dataBytes = json.toByteArray(Charsets.UTF_8)
        val buf = ByteArray(4 + dataBytes.size + 2) { 0 }
        buf[0] = (dataBytes.size shr 24).toByte()
        buf[1] = (dataBytes.size shr 16).toByte()
        buf[2] = (dataBytes.size shr 8).toByte()
        buf[3] = dataBytes.size.toByte()
        dataBytes.copyInto(buf, 4)
        val chunk = (buf.size + 5) / 6
        val c = QrFrame.Collector()
        val b64 = java.util.Base64.getEncoder()
        fun offerIdx(s: Int) {
            val begin = (s % 6) * chunk
            val payload = ByteArray(chunk).also { p ->
                buf.copyOfRange(begin, minOf(begin + chunk, buf.size)).copyInto(p)
            }
            val headB64 = b64.encodeToString(byteArrayOf(0, 0, s.toByte(), 0, 0, 10, 0, 6, 10))
            c.offer(headB64 + b64.encodeToString(payload))
        }
        // 前 6 帧（数据位收齐）+ 每帧重复 offer 4 次（模拟重复帧渲染）
        for (s in 0 until 6) repeat(5) { offerIdx(s) }
        assertEquals(6, c.have) // 重复 offer 不增长计数
        // 后 4 帧（校验位）+ 循环播放带来的乱序重复
        for (s in 6 until 10) offerIdx(s)
        offerIdx(0); offerIdx(8); offerIdx(3)
        assertEquals(10, c.have)
        assertEquals(json, c.collectToEnvelopeText())
    }
}
