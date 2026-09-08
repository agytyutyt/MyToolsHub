package com.jztools.infoparse.util

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import java.util.zip.DataFormatException

/** Zlib.inflate：与 Python 端 zlib.compress（RFC1950 流）互通（v1.7 压缩信封） */
class ZlibTest {

    /** 用 Deflater（nowrap=false，与 Python zlib 一致）压缩 */
    private fun deflate(raw: ByteArray, level: Int = java.util.zip.Deflater.BEST_COMPRESSION): ByteArray {
        val deflater = java.util.zip.Deflater(level, false)
        deflater.setInput(raw)
        deflater.finish()
        val out = java.io.ByteArrayOutputStream()
        val buf = ByteArray(8192)
        while (!deflater.finished()) out.write(buf, 0, deflater.deflate(buf))
        deflater.end()
        return out.toByteArray()
    }

    @Test
    fun `往返 压缩解压一致`() {
        val raw = "第一段正文\n第二段 A&B<C>\n".repeat(100).toByteArray(Charsets.UTF_8)
        assertArrayEquals(raw, Zlib.inflate(deflate(raw)))
    }

    @Test
    fun `大文本 多缓冲块输出`() {
        val raw = ByteArray(100_000) { (it % 251).toByte() } // 不可压缩，强制多轮 inflate
        assertArrayEquals(raw, Zlib.inflate(deflate(raw)))
    }

    @Test
    fun `空字节数组抛异常`() {
        assertThrows(DataFormatException::class.java) { Zlib.inflate(ByteArray(0)) }
    }

    @Test
    fun `截断数据抛异常`() {
        val raw = "重复文本重复文本重复文本".repeat(50).toByteArray(Charsets.UTF_8)
        val full = deflate(raw)
        assertThrows(DataFormatException::class.java) { Zlib.inflate(full.copyOf(full.size / 2)) }
    }

    @Test
    fun `非压缩数据抛异常`() {
        assertThrows(DataFormatException::class.java) { Zlib.inflate(byteArrayOf(1, 2, 3, 4, 5)) }
    }
}
