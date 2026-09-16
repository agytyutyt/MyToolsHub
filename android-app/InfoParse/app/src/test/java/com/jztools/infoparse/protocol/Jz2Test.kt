package com.jztools.infoparse.protocol

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayOutputStream
import java.util.zip.CRC32

/**
 * JZ2 v2 协议单元测试：帧解析（CRC 拒损坏）、信封帧解析、静态多页收集、
 * k-of-m 早停重组（ZfecCompat GF 补齐）。与桌面端 routes.py 帧格式逐字段对齐。
 */
class Jz2Test {

    // ---------- 帧构造（镜像桌面端 _jz2_frame） ----------

    private fun jz2Frame(kindFec: Boolean, segI: Int, segN: Int,
                         meta: ByteArray, payload: ByteArray,
                         comp: Int = 0): ByteArray {
        var flags = comp and 0x03
        if (kindFec) flags = flags or Jz2.FLAG_FEC
        val out = ByteArrayOutputStream()
        out.write(byteArrayOf(0x4A, 0x5A, 0x32, Jz2.VERSION.toByte(), flags.toByte()))
        out.write(byteArrayOf(((segI shr 16) and 0xFF).toByte(), ((segI shr 8) and 0xFF).toByte(), (segI and 0xFF).toByte()))
        out.write(byteArrayOf(((segN shr 16) and 0xFF).toByte(), ((segN shr 8) and 0xFF).toByte(), (segN and 0xFF).toByte()))
        out.write(byteArrayOf(((meta.size shr 8) and 0xFF).toByte(), (meta.size and 0xFF).toByte()))
        val bodyLen = payload.size
        // 4 字节大端：只取 shift = 24,16,8,0（downTo 0 不带 step 会写成 25 字节！）
        for (shift in 24 downTo 0 step 8) out.write(((bodyLen shr shift) and 0xFF))
        val crc = CRC32()
        crc.update(out.toByteArray())
        crc.update(meta)
        crc.update(payload)
        val c = crc.value
        out.write(byteArrayOf(((c shr 24) and 0xFF).toByte(), ((c shr 16) and 0xFF).toByte(),
            ((c shr 8) and 0xFF).toByte(), (c and 0xFF).toByte()))
        out.write(meta)
        out.write(payload)
        return out.toByteArray()
    }

    private fun envMeta(fmt: Int, origLen: Int, name: String, ext: String?): ByteArray {
        val nameB = name.toByteArray(Charsets.UTF_8)
        val extB = (ext ?: "").toByteArray(Charsets.UTF_8)
        val out = ByteArrayOutputStream()
        out.write(fmt)
        for (shift in 24 downTo 0 step 8) out.write(((origLen shr shift) and 0xFF))
        out.write(byteArrayOf(((nameB.size shr 8) and 0xFF).toByte(), (nameB.size and 0xFF).toByte()))
        out.write(nameB)
        out.write(extB.size)
        out.write(extB)
        return out.toByteArray()
    }

    // ---------- GF(2^8) 乘法（与 ZfecCompat 同参数，用于测试侧编码 share） ----------

    private fun gfMul(a: Int, b: Int): Int {
        var r = 0
        var x = a
        var y = b
        while (y != 0) {
            if (y and 1 != 0) r = r xor x
            x = x shl 1
            if (x and 0x100 != 0) x = x xor 0x11d
            y = y shr 1
        }
        return r
    }

    // ---------- 用例 ----------

    @Test
    fun `帧解析与 CRC 拒损坏`() {
        val frame = jz2Frame(false, 0, 1, envMeta(0, 5, "t", null), "hello".toByteArray())
        val f = Jz2.parseFrame(frame)
        assertNotNull(f)
        assertEquals("hello", String(f!!.payload, Charsets.UTF_8))
        assertFalse(f.isFec)
        // 任一字节翻转 → CRC 失败（含版本/CRC 字段自身）
        for (i in frame.indices) {
            val corrupted = frame.copyOf().also { it[i] = (it[i].toInt() xor 0x01).toByte() }
            assertNull("byte $i 未被拒绝", Jz2.parseFrame(corrupted))
        }
        // 截断
        assertNull(Jz2.parseFrame(frame.copyOfRange(0, frame.size - 1)))
        assertNull(Jz2.parseFrame(ByteArray(10)))
    }

    @Test
    fun `信封帧解析 文本与扩展名`() {
        val data = "你好，信息传输"
        val bytes = data.toByteArray(Charsets.UTF_8)
        val frame = jz2Frame(false, 0, 1, envMeta(0, bytes.size, "样例", "TXT"), bytes)
        val env = Jz2.parseEnvelope(Jz2.parseFrame(frame)!!)
        assertNotNull(env)
        assertEquals(Fmt.TEXT, env!!.fmt)
        assertEquals("样例", env.name)
        assertEquals(data, env.textData)
        assertEquals("txt", env.ext)
    }

    @Test
    fun `信封帧解析 orig_len 不符拒收`() {
        val frame = jz2Frame(false, 0, 1, envMeta(0, 999, "t", null), "hello".toByteArray())
        assertNull(Jz2.parseEnvelope(Jz2.parseFrame(frame)!!))
    }

    @Test
    fun `静态多页收集 乱序补扫还原`() {
        val payload = ("（第1段）本项目旨在解决内网环境下跨网闸的数据交换问题。（第2段）通过二维码单向通道实现可靠传输。" +
            "（第3段）v2 信封帧按容量切片分页，收齐后拼接再统一解析。" +
            "（第4段）meta 每页重复，首页确定 total，乱序无影响。").toByteArray(Charsets.UTF_8)
        val meta = envMeta(0, payload.size, "多页", "txt")
        val cap = 12
        val n = (payload.size + cap - 1) / cap
        val ec = Jz2.EnvCollector()
        // 乱序 + 同页重复
        var last: ScanResult = ScanResult.Invalid(Msg.NOT_ENVELOPE)
        for (i in n - 1 downTo 0) {
            val slice = payload.copyOfRange(i * cap, minOf(payload.size, (i + 1) * cap))
            last = ec.offer(Jz2.parseFrame(jz2Frame(false, i, n, meta, slice))!!)
            if (i == n - 1) { // 同页重扫覆盖
                last = ec.offer(Jz2.parseFrame(jz2Frame(false, i, n, meta, slice))!!)
            }
        }
        assertTrue(last is ScanResult.Single)
        val env = (last as ScanResult.Single).envelope
        assertEquals(payload.toString(Charsets.UTF_8), env.textData)
        assertEquals("txt", env.ext)
    }

    @Test
    fun `k-of-m 早停重组 缺 30 帧 仍还原`() {
        val k = 6
        val m = 10
        val chunkSize = 8
        // 重组产物 = v2 JZ2 信封帧字节（真实 v2 视频流承载形态）
        val data = "hello k-of-m"
        val envelope = jz2Frame(false, 0, 1, envMeta(0, data.length, "k测试", null),
            data.toByteArray(Charsets.UTF_8))
        val joined = ByteArray(4 + envelope.size)
        joined[0] = ((envelope.size shr 24) and 0xFF).toByte()
        joined[1] = ((envelope.size shr 16) and 0xFF).toByte()
        joined[2] = ((envelope.size shr 8) and 0xFF).toByte()
        joined[3] = (envelope.size and 0xFF).toByte()
        envelope.copyInto(joined, 4)
        // 切成 k 块等长（尾部 0 填充）
        val chunks = Array(k) { c ->
            val b = ByteArray(chunkSize)
            for (i in 0 until chunkSize) {
                val idx = c * chunkSize + i
                if (idx < joined.size) b[i] = joined[idx]
            }
            b
        }
        // zfec 系统化矩阵编码（s<k 为原始块，s>=k 为校验块）
        val enc = ZfecCompat.buildEncodeMatrix(k, m)
        val shares = Array(m) { s ->
            if (s < k) chunks[s]
            else ByteArray(chunkSize) { b ->
                var acc = 0
                for (c in 0 until k) acc = acc xor gfMul(enc[s][c], chunks[c][b].toInt() and 0xFF)
                acc.toByte()
            }
        }
        val frames = Array(m) { s ->
            jz2Frame(true, s, m, byteArrayOf(k.toByte(), m.toByte()), shares[s])
        }

        val fc = QrFrame.Collector()
        // 前 5 帧（<k）→ 不满足
        for (s in 0 until 5) {
            val r = fc.offerJz2Share(Jz2.parseFrame(frames[s])!!)
            assertTrue(r is ScanResult.Frame)
        }
        assertFalse(fc.isSatisfied())
        assertNull(fc.assembleBytes())
        // 补到 7 帧（丢 3 帧 = 30%）→ 满足且可重组
        for (s in 5 until 7) fc.offerJz2Share(Jz2.parseFrame(frames[s])!!)
        assertTrue(fc.isSatisfied())
        val bytes = fc.assembleBytes()
        assertNotNull(bytes)
        assertTrue(Jz2.isJz2(bytes))
        val env = EnvelopeParser.handleBytes(bytes!!, PageCollector(), Jz2.EnvCollector())
        assertTrue(env is ScanResult.Single)
        assertEquals(data, ((env as ScanResult.Single).envelope).textData)
        assertEquals("k测试", (env.envelope).name)
    }

    @Test
    fun `v1 文本帧收集回归不变`() {
        // 全收齐走系统位快路径，行为与 v1 一致（EnvelopeTest 覆盖细节，此处冒烟）
        val text = "回归"
        val envelope = """{"jzt":1,"fmt":"text","name":"v1","data":"$text"}"""
        val payload = envelope.toByteArray(Charsets.UTF_8)
        val k = 2; val m = 3
        // chunkSize 动态计算：joined = 4B 长度前缀 + payload，须 ≤ k×chunkSize
        val chunkSize = (payload.size + 4 + k - 1) / k
        val joined = ByteArray(4 + k * chunkSize)
        joined[0] = ((payload.size shr 24) and 0xFF).toByte()
        joined[1] = ((payload.size shr 16) and 0xFF).toByte()
        joined[2] = ((payload.size shr 8) and 0xFF).toByte()
        joined[3] = (payload.size and 0xFF).toByte()
        payload.copyInto(joined, 4)
        val chunks = Array(k) { c ->
            val b = ByteArray(chunkSize)
            for (i in 0 until chunkSize) {
                val idx = c * chunkSize + i
                if (idx < joined.size) b[i] = joined[idx]
            }
            b
        }
        val enc = ZfecCompat.buildEncodeMatrix(k, m)
        val shares = Array(m) { s ->
            if (s < k) chunks[s]
            else ByteArray(chunkSize) { b ->
                var acc = 0
                for (c in 0 until k) acc = acc xor gfMul(enc[s][c], chunks[c][b].toInt() and 0xFF)
                acc.toByte()
            }
        }
        val fc = QrFrame.Collector()
        for (s in 0 until m) {
            // v1 帧头 9B = [3B idx][3B n][0x00][k][m] → base64 12 字符
            val head = java.util.Base64.getEncoder().encodeToString(byteArrayOf(
                0, 0, s.toByte(),
                0, 0, m.toByte(),
                0, k.toByte(), m.toByte()))
            val body = java.util.Base64.getEncoder().encodeToString(shares[s])
            fc.offer(head + body)
        }
        assertEquals(envelope, fc.collectToEnvelopeText())
    }
}
