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

    /** 导出扩展名与 mime。file 由 base64 还原原始文件；word 兼容旧码重建 .docx；其余按 3.1.2 */
    fun exportSpec(fmt: String): Pair<String, String> = when (fmt) {
        Fmt.MARKDOWN -> "md" to "text/markdown"
        Fmt.WORD -> "docx" to "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        Fmt.EXCEL -> "csv" to "text/csv"
        else -> "txt" to "text/plain"
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

    /** 导出文件名：text/markdown/word/excel = name + 扩展名（防重复后缀，E-12）；file = name 原样（已含扩展名） */
    fun exportFileName(fmt: String, name: String): String {
        val base = safeFileName(name)
        if (fmt == Fmt.FILE) return base
        val (ext, _) = exportSpec(fmt)
        return if (base.lowercase().endsWith(".$ext")) base else "$base.$ext"
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
}
