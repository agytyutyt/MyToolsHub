package com.jztools.infoparse.protocol

import com.google.gson.JsonParser
import java.util.zip.CRC32

/**
 * JZ2 二进制帧协议（v2，与桌面端 routes.py parse_frame_jz2 / parse_envelope_v2 逐字段对齐）。
 *
 * 帧结构（21B 定长帧头 + meta + payload）：
 *   magic 3B "JZ2" | ver 1B | flags 1B | seg_i 3B | seg_n 3B | meta_len 2B | body_len 4B | crc32 4B
 *   flags bit0-1 = 压缩算法（0=none 1=zlib）；bit2 = mode；bit3 = 帧类型（0=信封帧 1=FEC share 帧）。
 *   CRC32 覆盖「帧头（除 CRC 字段自身）+ meta + payload」。
 * 两种帧：
 *   信封帧（静态码）：meta = fmt(1B)+orig_len(4B)+name_len(2B)+name+ext_len(1B)+ext；
 *       payload = 数据原始字节（压缩与否由 flags 标注）。静态多页时 meta 每页重复、
 *       payload 为整份数据的切片，收齐后拼接再解压。
 *   share 帧（视频流）：meta = k(1B)+m(1B)；payload = zfec share 原始字节。
 */
object Jz2 {

    val MAGIC = byteArrayOf(0x4A, 0x5A, 0x32) // "JZ2"
    const val VERSION = 2
    const val HEADER_LEN = 21

    const val COMP_NONE = 0
    const val COMP_ZLIB = 1

    const val FLAG_REBUILD = 0x04
    const val FLAG_FEC = 0x08

    /** fmt 编码（与桌面端 JZ2_FMT_CODES 一致） */
    val FMT_NAMES = mapOf(
        0 to Fmt.TEXT,
        1 to Fmt.MARKDOWN,
        2 to Fmt.WORD,
        3 to Fmt.EXCEL,
        4 to Fmt.FILE,
    )

    /** 解析成功的帧 */
    class Frame(
        val comp: Int,
        val mode: Int,
        val isFec: Boolean,
        val segI: Int,
        val segN: Int,
        val meta: ByteArray,
        val payload: ByteArray,
    )

    /** 内容以 "JZ2" 魔数开头（v2 双协议判别入口） */
    fun isJz2(b: ByteArray?): Boolean =
        b != null && b.size >= 3 &&
            b[0] == MAGIC[0] && b[1] == MAGIC[1] && b[2] == MAGIC[2]

    /** 校验并解析 JZ2 帧；长度/magic/版本/CRC 任一不符返回 null（损坏与截断 100% 被拒） */
    fun parseFrame(frame: ByteArray): Frame? {
        if (frame.size < HEADER_LEN || !isJz2(frame)) return null
        if (frame[3].toInt() and 0xFF != VERSION) return null
        val flags = frame[4].toInt() and 0xFF
        val segI = u24(frame, 5)
        val segN = u24(frame, 8)
        val metaLen = u16(frame, 11)
        val bodyLen = u32(frame, 13)
        val crc = u32(frame, 17).toLong() and 0xFFFFFFFFL
        if (frame.size < HEADER_LEN + metaLen + bodyLen) return null
        val crcCalc = CRC32()
        crcCalc.update(frame, 0, HEADER_LEN - 4)
        crcCalc.update(frame, HEADER_LEN, metaLen + bodyLen)
        if (crcCalc.value != crc) return null
        val meta = frame.copyOfRange(HEADER_LEN, HEADER_LEN + metaLen)
        val payload = frame.copyOfRange(HEADER_LEN + metaLen, HEADER_LEN + metaLen + bodyLen)
        return Frame(
            comp = flags and 0x03,
            mode = if (flags and FLAG_REBUILD != 0) 1 else 0,
            isFec = flags and FLAG_FEC != 0,
            segI = segI, segN = segN, meta = meta, payload = payload,
        )
    }

    /**
     * JZ2 信封帧 → Envelope（与桌面端 parse_envelope_v2 语义一致）。
     * - fmt=file：payload（原始文件字节）转回 base64 字符串；
     * - fmt=excel：payload JSON 还原为二维数组（word 精简载荷是纯文本，原样返回）；
     * - 压缩载荷自动解压；orig_len 长度校验不符时返回 null。
     */
    fun parseEnvelope(f: Frame): Envelope? {
        if (f.isFec || f.meta.size < 8) return null
        val fmt = FMT_NAMES[f.meta[0].toInt() and 0xFF] ?: return null
        val origLen = u32(f.meta, 1)
        val nameLen = u16(f.meta, 5)
        var pos = 7
        if (pos + nameLen > f.meta.size) return null
        val name = try {
            String(f.meta, pos, nameLen, Charsets.UTF_8).ifEmpty { "未命名" }
        } catch (e: Exception) {
            "未命名"
        }
        pos += nameLen
        val extLen = if (pos < f.meta.size) f.meta[pos].toInt() and 0xFF else 0
        pos += 1
        val ext = if (extLen > 0 && pos + extLen <= f.meta.size) try {
            String(f.meta, pos, extLen, Charsets.UTF_8).trim('.').lowercase().takeIf { it.isNotEmpty() }
        } catch (e: Exception) {
            null
        } else null
        var payload = f.payload
        if (f.comp == COMP_ZLIB) {
            payload = try {
                com.jztools.infoparse.util.Zlib.inflate(payload)
            } catch (e: Exception) {
                return null
            }
        }
        if (origLen > 0 && payload.size != origLen) return null
        val data: Any = when (fmt) {
            Fmt.FILE -> java.util.Base64.getEncoder().encodeToString(payload)
            Fmt.EXCEL -> {
                val arr = try {
                    JsonParser.parseString(String(payload, Charsets.UTF_8)).asJsonArray
                } catch (e: Exception) {
                    return null
                }
                EnvelopeParser.jsonToRows(arr) ?: return null
            }
            else -> String(payload, Charsets.UTF_8)
        }
        return Envelope(fmt, name, data, ext)
    }

    private fun u16(b: ByteArray, off: Int): Int =
        ((b[off].toInt() and 0xFF) shl 8) or (b[off + 1].toInt() and 0xFF)

    private fun u24(b: ByteArray, off: Int): Int =
        ((b[off].toInt() and 0xFF) shl 16) or
            ((b[off + 1].toInt() and 0xFF) shl 8) or
            (b[off + 2].toInt() and 0xFF)

    private fun u32(b: ByteArray, off: Int): Int =
        ((b[off].toInt() and 0xFF) shl 24) or
            ((b[off + 1].toInt() and 0xFF) shl 16) or
            ((b[off + 2].toInt() and 0xFF) shl 8) or
            (b[off + 3].toInt() and 0xFF)

    /**
     * JZ2 静态多页收集器（信封帧，seg_i 0 基 → 页码 1 基，与 v1 PageCollector 语义对齐）。
     * 规则与桌面端 _parse_scanned_codes 一致：首页定 total；meta 不一致 → 页码不属当前任务；
     * 收齐 1..n 后按序拼 payload、统一解压并解析信封。
     */
    class EnvCollector {
        var total: Int = 0
            private set
        private var meta: ByteArray? = null
        private val parts = HashMap<Int, Frame>() // 1 基页序 -> 帧

        val have: Int get() = parts.size
        val isComplete: Boolean get() = total in 1..have

        fun missing(): List<Int> = (1..total).filter { it !in parts }
        fun progressText(): String = Msg.progress(have, total, missing())

        /** 收到一页（已确认为 JZ2 信封帧）。返回给 UI 的结论 */
        fun offer(f: Frame): ScanResult {
            if (f.segN < 1 || f.segI < 0 || f.segI >= f.segN) return ScanResult.Invalid(Msg.FRAME_BAD)
            if (total == 0) {
                total = f.segN
                meta = f.meta
            } else if (f.segN != total || !f.meta.contentEquals(meta)) {
                return ScanResult.Invalid(Msg.PAGE_MISMATCH)
            }
            parts[f.segI + 1] = f // 同页重扫覆盖
            if (isComplete) {
                val env = assemble()
                return env?.let { ScanResult.Single(it) }
                    ?: ScanResult.Invalid(Msg.ASSEMBLE_FAILED)
            }
            return ScanResult.Page(f.segI + 1, total, have)
        }

        /** 集齐后重组：按序拼接 payload → 统一解压 → 信封 */
        fun assemble(): Envelope? {
            if (!isComplete) return null
            var joined = ByteArray(0)
            for (i in 1..total) joined += parts.getValue(i).payload
            val first = parts.getValue(1)
            return parseEnvelope(Frame(first.comp, first.mode, false, 0, 1, first.meta, joined))
        }

        /** 清空收集器（还原失败时重来） */
        fun reset() {
            total = 0
            meta = null
            parts.clear()
        }
    }
}
