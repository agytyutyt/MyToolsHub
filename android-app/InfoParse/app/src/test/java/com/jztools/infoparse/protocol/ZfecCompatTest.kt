package com.jztools.infoparse.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Test

/** 附录 C 自测向量（来自真实 zfec，文档 7.3 附录 C 要求全部通过） */
class ZfecCompatTest {

    private fun hex(s: String) = s.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
    private fun toHex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }

    @Test
    fun `向量1 k2 m3 用 s1+s2 还原原始两块`() {
        // 真实 zfec 输出：s0=01020304 s1=05060708 s2=090a0b1c
        val out = ZfecCompat.decode(
            2, 3,
            mapOf(1 to hex("05060708"), 2 to hex("090a0b1c"))
        )
        assertEquals("01020304", toHex(out[0]))
        assertEquals("05060708", toHex(out[1]))
    }

    @Test
    fun `向量2 k3 m5 用 s1+s2+s3 还原原始三块`() {
        // 真实 zfec 输出：55555555 / 29292929 为校验块
        val out = ZfecCompat.decode(
            3, 5,
            mapOf(1 to hex("42424242"), 2 to hex("43434343"), 3 to hex("55555555"))
        )
        assertEquals("41414141", toHex(out[0]))
        assertEquals("42424242", toHex(out[1]))
        assertEquals("43434343", toHex(out[2]))
    }

    @Test
    fun `向量2 混合校验块 s0+s3+s4 也可还原`() {
        val out = ZfecCompat.decode(
            3, 5,
            mapOf(0 to hex("41414141"), 3 to hex("55555555"), 4 to hex("29292929"))
        )
        assertEquals("41414141", toHex(out[0]))
        assertEquals("42424242", toHex(out[1]))
        assertEquals("43434343", toHex(out[2]))
    }

    @Test
    fun `编码矩阵对角验证 全 share 系统位`() {
        // 用编码矩阵验证 k=2,m=3：s2 = V[2] × [s0, s1]
        val enc = ZfecCompat.buildEncodeMatrix(2, 3)
        val s0 = hex("01020304")
        val s1 = hex("05060708")
        val s2 = ByteArray(4) { b ->
            var acc = 0
            acc = acc xor gmul(enc[2][0], s0[b].toInt() and 0xFF)
            acc = acc xor gmul(enc[2][1], s1[b].toInt() and 0xFF)
            acc.toByte()
        }
        assertEquals("090a0b1c", toHex(s2))
    }

    private fun gmul(a: Int, b: Int): Int = ZfecCompatTestGmul.gmul(a, b)
}

private object ZfecCompatTestGmul {
    // 与 ZfecCompat 相同的 GF 乘法（测试辅助）
    private const val PP = 0x11d
    private val EXP = IntArray(512)
    private val LOG = IntArray(256)

    init {
        var x = 1
        for (i in 0 until 255) {
            EXP[i] = x; LOG[x] = i
            x = x shl 1
            if (x and 0x100 != 0) x = x xor PP
        }
        for (i in 255 until 512) EXP[i] = EXP[i - 255]
    }

    fun gmul(a: Int, b: Int): Int =
        if (a == 0 || b == 0) 0 else EXP[LOG[a] + LOG[b]]
}
