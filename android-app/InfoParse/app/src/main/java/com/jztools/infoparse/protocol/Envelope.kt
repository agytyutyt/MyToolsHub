package com.jztools.infoparse.protocol

import com.google.gson.JsonArray
import com.google.gson.JsonObject
import com.google.gson.JsonParser

/** 用户可见文案（与文档 3.5 错误文案对照表逐字一致） */
object Msg {
    const val NOT_ENVELOPE = "未识别到「信息传输」封装的二维码内容"
    const val BAD_VERSION = "信封协议版本不受支持"
    const val PAGE_MISMATCH = "该二维码页数与当前任务不一致，已忽略"
    const val EXCEL_BAD = "Excel 数据结构异常"
    const val ZIPPED_BAD = "压缩数据异常，无法解压还原"
    const val IMAGE_NO_QR = "图片中未识别到二维码"
    const val ASSEMBLE_FAILED = "数据还原失败，请重新扫描"
    const val FRAME_MISMATCH = "该二维码与当前视频任务不一致，已忽略"
    const val FRAME_BAD = "视频帧数据异常"
    const val VIDEO_INCOMPLETE = "视频帧不全，请重新解析或重新传输原始视频文件"
    const val VIDEO_NO_QR = "视频中未识别到二维码帧"

    fun unknownFmt(fmt: String) = "未知的文档格式声明：$fmt"

    /** 「已收集 x/n 张，还差：第 a、b 张」（缺失页最多列 5 个，超出以 … 收尾） */
    fun progress(have: Int, total: Int, missing: List<Int>): String {
        if (total <= 0) return "已收集 $have 张"
        val shown = missing.take(5).joinToString("、")
        val tail = when {
            missing.isEmpty() -> ""
            missing.size > 5 -> "，还差：第 $shown…张"   // 3.5 规范：省略号后无空格
            else -> "，还差：第 $shown 张"
        }
        return "已收集 $have/$total 张$tail"
    }
}

/** 文档格式常量 */
object Fmt {
    const val TEXT = "text"
    const val MARKDOWN = "markdown"
    const val WORD = "word"
    const val EXCEL = "excel"

    /** 原始文件（v2）：data 为文件字节的 base64，name 为完整文件名（含扩展名） */
    const val FILE = "file"
    val ALL = setOf(TEXT, MARKDOWN, WORD, EXCEL, FILE)

    fun label(f: String) = when (f) {
        TEXT -> "纯文本"
        MARKDOWN -> "Markdown"
        WORD -> "Word 文档"
        EXCEL -> "Excel 表格"
        FILE -> "原始文件"
        else -> f
    }
}

/** 解析成功后的信封 */
data class Envelope(
    val fmt: String,
    val name: String,
    /** fmt=text/markdown/word 为 String；fmt=excel 为 List<List<Any?>>；fmt=file 为 base64 String */
    val data: Any,
    /** 精简传输声明的原始文件后缀名（如 docx/xlsx/md/txt，无点号）；旧二维码或粘贴文字为 null */
    val ext: String? = null,
) {
    val isExcel: Boolean get() = fmt == Fmt.EXCEL && data is List<*>
    val isFile: Boolean get() = fmt == Fmt.FILE && data is String

    /** 文本类信封的原文（excel 无此值，用 Csv 另行处理；file 为 base64 文本） */
    val textData: String? get() = data as? String

    /** file 信封的原始文件字节（base64 解码失败返回 null） */
    fun fileBytes(): ByteArray? {
        val b64 = textData?.takeIf { isFile } ?: return null
        return try {
            java.util.Base64.getDecoder().decode(b64)
        } catch (e: IllegalArgumentException) {
            null
        }
    }
}

/** 单次扫码文本的解析结论 */
sealed class ScanResult {
    /** 形态A：单张完整信封，直接出结果 */
    data class Single(val envelope: Envelope) : ScanResult()

    /** 形态B：多页分片之一，已进入收集器 */
    data class Page(val i: Int, val n: Int, val have: Int) : ScanResult()

    /** 形态C：视频帧（二期） */
    data class Frame(val idx: Int, val n: Int, val have: Int) : ScanResult()

    /** 无法处理 */
    data class Invalid(val reason: String) : ScanResult()
}

/**
 * 信封解析器（文档 3.1 / 3.2）。
 * 判别顺序不可变：以 "{" 开头 → JSON；否则按 base64 帧头解析（QrFrame）。
 */
object EnvelopeParser {

    /** 顶层入口：把扫到的文本交给对应处理器 */
    fun handle(text: String, collector: PageCollector): ScanResult {
        val t = text.trim()
        if (t.startsWith("{")) return handleJson(t, collector)
        // 二期视频帧在 QrFrame.Collector 内处理；此处按一期规则拒绝
        return ScanResult.Invalid(Msg.NOT_ENVELOPE)
    }

    /** 处理 JSON 形态（形态A / 形态B） */
    private fun handleJson(t: String, collector: PageCollector): ScanResult {
        val obj = try {
            JsonParser.parseString(t).asJsonObject
        } catch (e: Exception) {
            return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        }
        return if (obj.has("pg")) collector.offer(obj) else single(obj)
    }

    /** 校验并解析完整信封（形态A，或收集器重组完成后的 JSON） */
    fun single(obj: JsonObject): ScanResult {
        val jzt = obj.get("jzt")?.takeIf { it.isJsonPrimitive }?.asInt
        if (jzt == null || jzt != 1) return ScanResult.Invalid(Msg.BAD_VERSION)
        val fmt = obj.get("fmt")?.takeIf { it.isJsonPrimitive }?.asString
            ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        if (fmt !in Fmt.ALL) return ScanResult.Invalid(Msg.unknownFmt(fmt))
        val name = obj.get("name")?.takeIf { it.isJsonPrimitive }?.asString ?: "未命名"
        // 精简传输声明的原始后缀名（可选字段；去掉点号并小写归一）
        val ext = obj.get("ext")?.takeIf { it.isJsonPrimitive }?.asString
            ?.trim('.')?.lowercase()?.takeIf { it.isNotEmpty() }
        // v1.7 压缩信封：data 为 base64(zlib(载荷))，解压后为文本/二维数组
        val zipped = obj.get("zip")?.takeIf { it.isJsonPrimitive }?.asInt == 1
        val dataEl = obj.get("data") ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        val data: Any = if (zipped) {
            unpackZipped(fmt, dataEl)
                ?: return ScanResult.Invalid(Msg.ZIPPED_BAD)
        } else if (fmt == Fmt.EXCEL) {
            if (!dataEl.isJsonArray) return ScanResult.Invalid(Msg.EXCEL_BAD)
            jsonToRows(dataEl.asJsonArray) ?: return ScanResult.Invalid(Msg.EXCEL_BAD)
        } else {
            // text/markdown/word/file 的 data 均为字符串（file 为文件字节 base64）
            dataEl.takeIf { it.isJsonPrimitive }?.asString
                ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        }
        return ScanResult.Single(Envelope(fmt, name, data, ext))
    }

    /** zip=1 信封：data → base64 解码 → zlib inflate → 文本 / excel 二维数组；失败返回 null */
    private fun unpackZipped(fmt: String, el: com.google.gson.JsonElement): Any? {
        val b64 = el.takeIf { it.isJsonPrimitive }?.asString ?: return null
        val packed = try {
            java.util.Base64.getDecoder().decode(b64)
        } catch (e: IllegalArgumentException) {
            return null
        }
        val raw = try {
            com.jztools.infoparse.util.Zlib.inflate(packed)
        } catch (e: Exception) {
            return null
        }
        return if (fmt == Fmt.EXCEL) {
            val arr = try {
                JsonParser.parseString(raw.toString(Charsets.UTF_8)).asJsonArray
            } catch (e: Exception) {
                return null
            }
            jsonToRows(arr) ?: return null
        } else {
            raw.toString(Charsets.UTF_8)
        }
    }

    /** 兼容文档示例的便捷方法：合法返回信封，否则 null */
    fun validateEnvelope(obj: JsonObject): Envelope? =
        (single(obj) as? ScanResult.Single)?.envelope

    /**
     * excel 二维数组 → List<List<Any?>>。
     * 单元格只允许 string/number/boolean/null；数字按原始文本保留（避免 1 → 1.0）。
     */
    fun jsonToRows(arr: JsonArray): List<List<Any?>>? {
        val rows = mutableListOf<List<Any?>>()
        for (rowEl in arr) {
            if (!rowEl.isJsonArray) return null
            val row = mutableListOf<Any?>()
            for (cellEl in rowEl.asJsonArray) {
                row.add(
                    when {
                        cellEl.isJsonNull -> null
                        cellEl.isJsonPrimitive -> {
                            val p = cellEl.asJsonPrimitive
                            when {
                                p.isBoolean -> p.asBoolean
                                else -> p.asString
                            }
                        }
                        else -> return null
                    }
                )
            }
            rows.add(row)
        }
        return rows
    }
}
