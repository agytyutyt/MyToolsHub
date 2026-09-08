package com.jztools.infoparse.export

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.MediaStore
import android.widget.Toast
import com.jztools.infoparse.protocol.Fmt

/** 导出与分享（文档 4.5.2 / FR-05 / FR-06） */
object Exporter {

    private val ILLEGAL = Regex("""[\\/:*?"<>|]""")

    /** name 中非法文件名字符替换为 _（3.1.2 / E-12） */
    fun safeFileName(name: String): String = name.replace(ILLEGAL, "_")

    /**
     * 保存到「下载」目录（API 29+ 免存储权限）。
     * 返回保存后的 content Uri；重名时 MediaStore 自动追加 (1)。
     */
    fun saveToDownloads(context: Context, fileName: String, bytes: ByteArray, mime: String): Uri? {
        return try {
            val values = android.content.ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, fileName)
                put(MediaStore.Downloads.MIME_TYPE, mime)
            }
            val resolver = context.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: return null
            resolver.openOutputStream(uri)?.use {
                it.write(bytes)
            }
            uri
        } catch (e: Exception) {
            Toast.makeText(context, "保存失败：${e.message}", Toast.LENGTH_LONG).show()
            null
        }
    }

    /** 导出扩展名与 mime。file 由 base64 还原原始文件；word 重建 .docx；excel 重建 .xlsx；其余按 3.1.2 */
    fun exportSpec(fmt: String): Pair<String, String> = when (fmt) {
        Fmt.MARKDOWN -> "md" to "text/markdown"
        Fmt.WORD -> "docx" to "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        Fmt.EXCEL -> "xlsx" to "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        else -> "txt" to "text/plain"
    }

    /** 精简传输允许声明还原的后缀白名单（与封装端格式配置对齐） */
    private val LITE_EXTS = setOf("txt", "md", "markdown", "docx", "doc", "xlsx", "xlsm", "xls", "csv")

    /**
     * 精简声明的后缀 → 实际还原扩展名；不适用返回 null（回退默认）。
     * 旧版容器格式无法原样重建（.doc 二进制 / .xls BIFF / .xlsm 宏容器），
     * 数据为纯文本/纯表格，统一降级为现代等价格式，保证字节与扩展名一致。
     */
    fun normalizedExt(fmt: String, ext: String?): String? {
        val declared = ext?.lowercase()?.takeIf { it in LITE_EXTS } ?: return null
        val ok = when (fmt) {
            Fmt.WORD -> declared == "docx" || declared == "doc"
            Fmt.EXCEL -> declared == "xlsx" || declared == "xlsm" ||
                    declared == "xls" || declared == "csv"
            Fmt.TEXT -> declared == "txt"
            Fmt.MARKDOWN -> declared == "md" || declared == "markdown"
            else -> false
        }
        if (!ok) return null
        return when (declared) {
            "markdown" -> "md"
            "xlsm", "xls" -> "xlsx"
            "doc" -> "docx"
            else -> declared
        }
    }

    /**
     * 导出文件名（3.1.2 / E-12 防重复后缀）：
     * - file：name 原样（已含扩展名）；
     * - 精简传输（ext 声明）：按声明还原，如 excel+csv → name.csv、word+doc → name.docx；
     * - 旧码：text/markdown/word/excel = name + 各格式默认扩展名。
     */
    fun exportFileName(fmt: String, name: String, ext: String? = null): String {
        val base = safeFileName(name)
        if (fmt == Fmt.FILE) return base
        val use = normalizedExt(fmt, ext)
        if (use != null) {
            return if (base.lowercase().endsWith(".$use")) base else "$base.$use"
        }
        val (defExt, _) = exportSpec(fmt)
        return if (base.lowercase().endsWith(".$defExt")) base else "$base.$defExt"
    }

    /** 精简传输还原扩展名 → mime（与 normalizedExt 输出对齐；不在白名单回退默认） */
    fun extMime(ext: String?): String? = when (ext?.lowercase()) {
        "txt" -> "text/plain"
        "md", "markdown" -> "text/markdown"
        "docx", "doc" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        "xlsx", "xlsm", "xls" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        "csv" -> "text/csv"
        else -> null
    }

    /** file 信封的 mime：按文件名扩展名推断，未知类型用通用二进制流 */
    fun fileMime(fileName: String): String = when (fileName.lowercase().substringAfterLast('.', "")) {
        "docx" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        "doc" -> "application/msword"
        "xlsx" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        "xls" -> "application/vnd.ms-excel"
        "pptx" -> "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        "ppt" -> "application/vnd.ms-powerpoint"
        "pdf" -> "application/pdf"
        "zip" -> "application/zip"
        "png" -> "image/png"
        "jpg", "jpeg" -> "image/jpeg"
        "gif" -> "image/gif"
        "txt" -> "text/plain"
        "md", "markdown" -> "text/markdown"
        "csv" -> "text/csv"
        "mp4" -> "video/mp4"
        "mp3" -> "audio/mpeg"
        else -> "application/octet-stream"
    }

    fun shareText(context: Context, text: String) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_TEXT, text)
        }
        context.startActivity(Intent.createChooser(intent, "分享内容"))
    }

    fun shareFile(context: Context, uri: Uri, mime: String) {
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = mime
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(intent, "分享文件"))
    }

    /** 用其他应用（如 WPS Office）打开已还原的文件；无可处理应用返回 false */
    fun openFile(context: Context, uri: Uri, mime: String): Boolean {
        return try {
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, mime)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            }
            context.startActivity(intent)
            true
        } catch (e: Exception) {
            false
        }
    }
}
