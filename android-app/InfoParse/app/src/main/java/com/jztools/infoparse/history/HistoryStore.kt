package com.jztools.infoparse.history

import android.content.Context
import com.google.gson.JsonArray
import com.google.gson.JsonNull
import com.google.gson.JsonObject
import com.google.gson.JsonParser
import com.google.gson.JsonPrimitive
import com.jztools.infoparse.protocol.Envelope
import com.jztools.infoparse.protocol.EnvelopeParser
import com.jztools.infoparse.protocol.Fmt
import java.io.File
import java.util.UUID

/**
 * 识别历史记录（一条结果 = 一个 JSON 文件，存于 filesDir/history/）。
 * 与协议层解耦：只依赖 Envelope 与 EnvelopeParser.jsonToRows。
 */
data class HistoryRecord(
    val id: String,
    val time: Long,
    val envelope: Envelope,
)

object HistoryStore {

    private const val DIR = "history"

    private fun dir(context: Context): File = File(context.filesDir, DIR).apply { mkdirs() }

    /** 保存一条识别结果，返回生成的记录（id 同时用作文件名） */
    fun save(context: Context, env: Envelope): HistoryRecord {
        val rec = HistoryRecord(
            id = UUID.randomUUID().toString(),
            time = System.currentTimeMillis(),
            envelope = env,
        )
        val root = JsonObject().apply {
            addProperty("id", rec.id)
            addProperty("time", rec.time)
            addProperty("fmt", env.fmt)
            addProperty("name", env.name)
            if (env.isExcel) add("data", rowsToJson(env.data as List<List<Any?>>))
            else addProperty("data", env.textData ?: "")
        }
        try {
            File(dir(context), rec.id + ".json").writeText(root.toString())
        } catch (e: Exception) {
            // 历史落盘失败不影响识别主流程
        }
        return rec
    }

    /** 全部记录，按时间倒序；损坏文件静默跳过 */
    fun list(context: Context): List<HistoryRecord> =
        dir(context).listFiles { f -> f.isFile && f.name.endsWith(".json") }
            ?.mapNotNull { restore(it) }
            ?.sortedByDescending { it.time }
            ?: emptyList()

    fun delete(context: Context, id: String) {
        File(dir(context), "$id.json").delete()
    }

    fun clear(context: Context) {
        dir(context).listFiles()?.forEach { it.delete() }
    }

    private fun restore(file: File): HistoryRecord? {
        return try {
            val o = JsonParser.parseString(file.readText()).asJsonObject
            val id = o.get("id")?.takeIf { it.isJsonPrimitive }?.asString ?: return null
            val time = o.get("time")?.takeIf { it.isJsonPrimitive }?.asLong ?: return null
            val fmt = o.get("fmt")?.takeIf { it.isJsonPrimitive }?.asString ?: return null
            if (fmt !in Fmt.ALL) return null
            val name = o.get("name")?.takeIf { it.isJsonPrimitive }?.asString ?: "未命名"
            val data: Any = if (fmt == Fmt.EXCEL) {
                // 与扫码解码一致：数字按字符串还原、布尔还原为布尔（EnvelopeParser.jsonToRows）
                EnvelopeParser.jsonToRows(o.getAsJsonArray("data")) ?: return null
            } else {
                o.get("data")?.takeIf { it.isJsonPrimitive }?.asString ?: return null
            }
            HistoryRecord(id, time, Envelope(fmt, name, data))
        } catch (e: Exception) {
            null
        }
    }

    /** excel 行数据 → JSON 二维数组（数字已按字符串存储，见 EnvelopeParser.jsonToRows） */
    private fun rowsToJson(rows: List<List<Any?>>): JsonArray {
        val arr = JsonArray()
        for (row in rows) {
            val r = JsonArray()
            for (cell in row) {
                r.add(
                    when (cell) {
                        null -> JsonNull.INSTANCE
                        is Boolean -> JsonPrimitive(cell)
                        else -> JsonPrimitive(cell.toString())
                    }
                )
            }
            arr.add(r)
        }
        return arr
    }
}
