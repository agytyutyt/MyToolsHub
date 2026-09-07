package com.jztools.infoparse.ui

import androidx.annotation.ColorRes
import com.jztools.infoparse.R
import com.jztools.infoparse.protocol.Envelope
import com.jztools.infoparse.protocol.Fmt

/** 格式徽标在各界面（主页卡片 / 结果页）共用的着色 */
object FmtUi {

    @ColorRes
    fun badgeColorRes(fmt: String): Int = when (fmt) {
        Fmt.TEXT -> R.color.badge_text
        Fmt.MARKDOWN -> R.color.badge_markdown
        Fmt.WORD -> R.color.badge_word
        Fmt.EXCEL -> R.color.badge_excel
        Fmt.FILE -> R.color.badge_file
        else -> R.color.primary
    }

    /** 卡片摘要统计：excel 行数 / 文件字节数 / 文本字符数 */
    fun statsText(env: Envelope): String = when {
        env.isExcel -> "共 ${(env.data as List<*>).size} 行"
        env.isFile -> {
            val b64 = env.textData ?: ""
            val pad = when {
                b64.endsWith("==") -> 2
                b64.endsWith("=") -> 1
                else -> 0
            }
            val bytes = (b64.length / 4 * 3 - pad).coerceAtLeast(0)
            "共 ${humanBytes(bytes)}"
        }
        else -> "共 ${(env.textData ?: "").length} 字符"
    }

    /** 字节数可读化（base64 长度换算，无需解码） */
    fun humanBytes(n: Int): String = when {
        n >= 1 shl 20 -> String.format(java.util.Locale.US, "%.2f MB", n / 1048576.0)
        n >= 1 shl 10 -> String.format(java.util.Locale.US, "%.1f KB", n / 1024.0)
        else -> "$n B"
    }
}
