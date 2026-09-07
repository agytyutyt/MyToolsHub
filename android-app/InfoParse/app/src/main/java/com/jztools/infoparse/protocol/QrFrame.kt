package com.jztools.infoparse.protocol

import java.util.Base64

/** 单帧解析结果（文档 3.4.1） */
data class FrameHead(val idx: Int, val n: Int, val k: Int, val m: Int)

/**
 * QR-transfer 视频帧解析与系统位重组（文档 3.4.2 步骤 2-5）。
 * 帧内容 = 12 字符 base64 帧头 + base64 数据载荷；
 * 帧头 9 字节 = [3B idx 大端][3B n 大端][0x00 保留][k][m]。
 * （java.util.Base64 自 API 26 可用，minSdk 29 满足；同时保证 JVM 单元测试可运行）
 */
object QrFrame {

    /** 解析帧头；不合法返回 null */
    fun parseHead(text: String): Pair<FrameHead, ByteArray>? {
        if (text.length < 13) return null
        return try {
            val head = Base64.getDecoder().decode(text.substring(0, 12))
            if (head.size != 9 || head[6].toInt() != 0) return null
            val idx = u24(head, 0)
            val n = u24(head, 3)
            val k = head[7].toInt() and 0xFF
            val m = head[8].toInt() and 0xFF
            if (k < 1 || m < k) return null
            val payload = Base64.getDecoder().decode(text.substring(12))
            Pair(FrameHead(idx, n, k, m), payload)
        } catch (e: Exception) {
            null
        }
    }

    private fun u24(b: ByteArray, off: Int): Int =
        ((b[off].toInt() and 0xFF) shl 16) or
            ((b[off + 1].toInt() and 0xFF) shl 8) or
            (b[off + 2].toInt() and 0xFF)

    /**
     * 视频帧收集器（系统位优先路径）。
     * 用法：每个扫码文本 offer 一次；全部收齐后 collectToEnvelope()。
     */
    class Collector {
        var n = 0
            private set
        var k = 0
            private set
        var m = 0
            private set
        private val shares = HashMap<Int, ByteArray>() // idx -> share 字节
        private var chunkSize = -1

        val have: Int get() = shares.size

        fun offer(text: String): ScanResult {
            val (head, payload) = parseHead(text)
                ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
            if (n == 0) {
                n = head.n; k = head.k; m = head.m
            } else if (head.n != n || head.k != k || head.m != m) {
                return ScanResult.Invalid(Msg.FRAME_MISMATCH)
            }
            if (head.idx < 0 || head.idx >= n) return ScanResult.Invalid(Msg.FRAME_BAD)
            if (chunkSize < 0) chunkSize = payload.size
            // _chunk_data 已把每块 0x00 填充到等长，所有 share 等长
            if (payload.size != chunkSize) return ScanResult.Invalid(Msg.FRAME_BAD)
            shares[head.idx] = payload
            return ScanResult.Frame(head.idx, n, have)
        }

        /**
         * 系统位重组（3.4.2 步骤 2-5）。
         * 返回信封 JSON 字节；缺原始块返回 null（缺帧，E-13）。
         */
        fun assembleSystematic(): ByteArray? {
            if (n == 0 || m == 0 || have < n) return null
            val nblocks = n / m
            val total = k * nblocks
            val chunks = arrayOfNulls<ByteArray>(total)
            for ((idx, payload) in shares) {
                val g = idx % nblocks
                val s = idx / nblocks
                if (s < k) chunks[s * nblocks + g] = payload
            }
            if (chunks.any { it == null }) return null
            val joined = chunks.requireNoNulls().reduce { a, b -> a + b }
            if (joined.size < 4) return null
            // 前 4 字节大端 = 数据长度 L；[4, 4+L) = 信封 JSON（UTF-8）
            val len = ((joined[0].toInt() and 0xFF) shl 24) or
                ((joined[1].toInt() and 0xFF) shl 16) or
                ((joined[2].toInt() and 0xFF) shl 8) or
                (joined[3].toInt() and 0xFF)
            if (len < 0 || 4 + len > joined.size) return null
            return joined.copyOfRange(4, 4 + len)
        }

        /** 收齐后取信封 JSON 文本；缺帧返回 null */
        fun collectToEnvelopeText(): String? =
            assembleSystematic()?.toString(Charsets.UTF_8)
    }
}
