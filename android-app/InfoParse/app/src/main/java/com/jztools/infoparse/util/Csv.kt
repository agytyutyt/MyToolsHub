package com.jztools.infoparse.util

/** CSV 工具（文档 4.5.1）：UTF-8 BOM + \r\n 行结束 + 标准转义 */
object Csv {

    /** 二维数组 → CSV 文本（含 BOM，Excel 打开不乱码） */
    fun fromRows(rows: List<List<Any?>>): String {
        val sb = StringBuilder("\uFEFF")
        rows.forEach { row ->
            sb.append(row.joinToString(",") { escape(cell(it)) }).append("\r\n")
        }
        return sb.toString()
    }

    private fun cell(v: Any?): String = when (v) {
        null -> ""
        is Boolean -> if (v) "TRUE" else "FALSE"
        else -> v.toString()
    }

    /** 标准 CSV 转义：含 逗号/引号/换行 时加引号，内部引号翻倍 */
    fun escape(s: String): String =
        if (s.contains(',') || s.contains('"') || s.contains('\n') || s.contains('\r'))
            "\"" + s.replace("\"", "\"\"") + "\""
        else s

    /** 剪贴板用 TSV（TC-12：excel 复制为 TSV 文本） */
    fun toTsv(rows: List<List<Any?>>): String =
        rows.joinToString("\n") { row ->
            row.joinToString("\t") { v ->
                when (v) {
                    null -> ""
                    is Boolean -> if (v) "TRUE" else "FALSE"
                    else -> v.toString()
                }
            }
        }
}
