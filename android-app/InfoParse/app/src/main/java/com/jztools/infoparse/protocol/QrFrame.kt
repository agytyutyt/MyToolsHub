package com.jztools.infoparse.protocol

import java.util.Base64

/** 单帧解析结果（文档 3.4.1） */
data class FrameHead(val idx: Int, val n: Int, val k: Int, val m: Int)

/**
 * QR-transfer 视频帧解析与重组（文档 3.4.2 步骤 2-5；v2 见 Jz2.kt）。
 * v1 帧内容 = 12 字符 base64 帧头 + base64 数据载荷；
 * 帧头 9 字节 = [3B idx 大端][3B n 大端][0x00 保留][k][m]。
 * v2 帧 = JZ2 二进制（fec share 帧），经 [offerBytes] 进入同一收集器。
 * 帧序约定（桌面端 _fec_encode）：idx = share*组数 + 组号。
 * （java.util.Base64 自 API 26 可用，minSdk 29 满足；同时保证 JVM 单元测试可运行）
 */
object QrFrame {

    /** 解析 v1 帧头；不合法返回 null */
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
     * 视频帧收集器（v1 文本帧 + v2 JZ2 share 帧共用）。
     * 用法：每个扫码内容 offer/offerBytes 一次；满足条件后 collectToEnvelope*()。
     *
     * T09 k-of-m 早停：v1 原判据「收齐全部 n 帧」放宽为「每个分块组集齐 ≥k 帧」
     * （缺的系统位由 ZfecCompat GF 解码补齐）。全收齐时仍走系统位快路径（免 GF 运算）。
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

        /** v1 文本帧入口 */
        fun offer(text: String): ScanResult {
            val (head, payload) = parseHead(text)
                ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
            return offerShare(head, payload)
        }

        /** 双协议入口：v2 JZ2 share 帧 / v1 文本帧；非帧内容返回 null（调用方忽略） */
        fun offerBytes(data: ByteArray): ScanResult? {
            if (Jz2.isJz2(data)) {
                val f = Jz2.parseFrame(data) ?: return null
                if (!f.isFec) return null // 信封帧不进视频收集器
                return offerJz2Share(f)
            }
            val t = String(data, Charsets.UTF_8)
            return if (parseHead(t) != null) offer(t) else null
        }

        /** v2 JZ2 share 帧入口（已解析的帧直接入队） */
        fun offerJz2Share(f: Jz2.Frame): ScanResult {
            if (f.meta.size != 2) return ScanResult.Invalid(Msg.FRAME_BAD)
            val kk = f.meta[0].toInt() and 0xFF
            val mm = f.meta[1].toInt() and 0xFF
            if (kk < 1 || mm < kk) return ScanResult.Invalid(Msg.FRAME_BAD)
            return offerShare(FrameHead(f.segI, f.segN, kk, mm), f.payload)
        }

        private fun offerShare(head: FrameHead, payload: ByteArray): ScanResult {
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

        /** T09 k-of-m 早停判据：每个分块组（idx % nblocks）都已集齐 ≥k 个 share */
        fun isSatisfied(): Boolean {
            if (n == 0 || k == 0 || m == 0) return false
            val nblocks = n / m
            if (nblocks <= 0) return false
            for (g in 0 until nblocks) {
                var c = 0
                for (idx in shares.keys) if (idx % nblocks == g) c++
                if (c < k) return false
            }
            return true
        }

        /**
         * 重组信封字节（剥 4B 长度前缀）。
         * 全收齐 → 系统位直接归位；仅满足 k-of-m → 逐组 ZfecCompat GF 解码。
         * 缺帧（任一组 < k）返回 null。
         */
        fun assembleBytes(): ByteArray? {
            if (n == 0 || k == 0 || m == 0) return null
            val nblocks = n / m
            if (nblocks <= 0) return null
            val chunks = arrayOfNulls<ByteArray>(k * nblocks)
            if (have >= n) {
                // 快路径：全部收齐 → 系统位（s < k）直接归位，与 v1 行为一致
                for ((idx, payload) in shares) {
                    val g = idx % nblocks
                    val s = idx / nblocks
                    if (s < k) chunks[s * nblocks + g] = payload
                }
            } else {
                for (g in 0 until nblocks) {
                    val present = HashMap<Int, ByteArray>()
                    for ((idx, payload) in shares) {
                        if (idx % nblocks == g) present[idx / nblocks] = payload
                    }
                    if (present.size < k) return null
                    val originals = try {
                        ZfecCompat.decode(k, m, present)
                    } catch (e: Exception) {
                        return null
                    }
                    for (s in 0 until k) chunks[s * nblocks + g] = originals[s]
                }
            }
            if (chunks.any { it == null }) return null
            val joined = chunks.requireNoNulls().reduce { a, b -> a + b }
            if (joined.size < 4) return null
            // 前 4 字节大端 = 数据长度 L；[4, 4+L) = 信封（v1 JSON 文本 / v2 JZ2 帧）
            val len = ((joined[0].toInt() and 0xFF) shl 24) or
                ((joined[1].toInt() and 0xFF) shl 16) or
                ((joined[2].toInt() and 0xFF) shl 8) or
                (joined[3].toInt() and 0xFF)
            if (len < 0 || 4 + len > joined.size) return null
            return joined.copyOfRange(4, 4 + len)
        }

        /** v1 流：收齐后取信封 JSON 文本；缺帧返回 null */
        fun collectToEnvelopeText(): String? =
            assembleBytes()?.toString(Charsets.UTF_8)
    }
}
