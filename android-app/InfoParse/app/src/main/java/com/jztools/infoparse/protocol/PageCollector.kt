package com.jztools.infoparse.protocol

import com.google.gson.JsonObject
import com.google.gson.JsonParser

/**
 * 多页收集器：形态 B 的状态机（文档 3.3 规则 1-6）。
 *
 * 规则要点：
 * 1. 首个入集的页确定 total = pg.n；之后任何 pg.n ≠ total 的页忽略并提示；
 * 2. 以 i 为键存片段；同 i 重复扫描 → 覆盖；乱序无影响；
 * 3. 集齐 1..n 后按 i 升序直接拼接 data → 校验解析信封。
 */
class PageCollector(
    total: Int = 0,
    fmt: String? = null,
    name: String? = null,
    parts: Map<Int, String> = emptyMap(),
) {
    var total: Int = total
        private set
    var fmt: String? = fmt
        private set
    var name: String? = name
        private set
    private val parts: LinkedHashMap<Int, String> = LinkedHashMap(parts)

    val have: Int get() = parts.size
    val isComplete: Boolean get() = total in 1..have

    /** 全部缺失页码（升序） */
    fun missing(): List<Int> = (1..total).filter { it !in parts }

    /** 缺失页码（最多 5 个，用于提示） */
    fun missingPreview(): List<Int> = missing().take(5)

    /** 进度展示文案（3.5 表） */
    fun progressText(): String = Msg.progress(have, total, missing())

    /** 收到一页（已确认为带 pg 的 JSON）。返回给 UI 的结论 */
    fun offer(obj: JsonObject): ScanResult {
        val pgEl = obj.get("pg") ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        if (!pgEl.isJsonObject) return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        val pg = pgEl.asJsonObject
        val i = pg.get("i")?.takeIf { it.isJsonPrimitive }?.asInt
            ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        val n = pg.get("n")?.takeIf { it.isJsonPrimitive }?.asInt
            ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        val data = obj.get("data")?.takeIf { it.isJsonPrimitive }?.asString
            ?: return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        if (i < 1 || n < 1 || i > n) return ScanResult.Invalid(Msg.NOT_ENVELOPE)
        obj.get("jzt")?.takeIf { it.isJsonPrimitive }?.asInt
            ?.let { if (it != 1) return ScanResult.Invalid(Msg.BAD_VERSION) }

        if (total == 0) {
            total = n
        } else if (n != total) {
            // 规则 2：页数与当前任务不一致 → 忽略
            return ScanResult.Invalid(Msg.PAGE_MISMATCH)
        }
        if (i == 1) {
            fmt = obj.get("fmt")?.takeIf { it.isJsonPrimitive }?.asString
            name = obj.get("name")?.takeIf { it.isJsonPrimitive }?.asString
        }
        parts[i] = data // 规则 3：同 i 覆盖
        if (isComplete) {
            val env = assemble()
            return env?.let { ScanResult.Single(it) }
                ?: ScanResult.Invalid(Msg.ASSEMBLE_FAILED)
        }
        return ScanResult.Page(i, total, have)
    }

    /** 集齐后重组：拼接 → 校验 → 信封 */
    fun assemble(): Envelope? {
        if (!isComplete) return null
        val merged = (1..total).joinToString("") { parts[it]!! }
        val obj = try {
            JsonParser.parseString(merged).asJsonObject
        } catch (e: Exception) {
            return null
        }
        return EnvelopeParser.validateEnvelope(obj)
    }

    /** 清空收集器（E-07：还原失败时重来） */
    fun reset() {
        total = 0
        fmt = null
        name = null
        parts.clear()
    }

    // ---------- FR-08 本地暂存（JSON 序列化，写入 filesDir/pending_collect.json） ----------

    fun serialize(): String {
        val root = JsonObject()
        root.addProperty("total", total)
        fmt?.let { root.addProperty("fmt", it) }
        name?.let { root.addProperty("name", it) }
        val ps = JsonObject()
        parts.forEach { (k, v) -> ps.addProperty(k.toString(), v) }
        root.add("parts", ps)
        return root.toString()
    }

    companion object {
        fun restore(json: String): PageCollector? = try {
            val map = JsonParser.parseString(json).asJsonObject
            val ps = map.getAsJsonObject("parts") ?: JsonObject()
            val partMap = LinkedHashMap<Int, String>()
            for (entry in ps.entrySet()) partMap[entry.key.toInt()] = entry.value.asString
            PageCollector(
                total = map.get("total")?.takeIf { it.isJsonPrimitive }?.asInt ?: 0,
                fmt = map.get("fmt")?.takeIf { it.isJsonPrimitive }?.asString,
                name = map.get("name")?.takeIf { it.isJsonPrimitive }?.asString,
                parts = partMap,
            )
        } catch (e: Exception) {
            null
        }
    }
}
