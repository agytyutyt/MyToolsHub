package com.jztools.infoparse

import android.content.Context
import android.media.MediaMetadataRetriever
import android.net.Uri
import com.jztools.infoparse.protocol.Msg
import com.jztools.infoparse.protocol.QrFrame
import com.jztools.infoparse.scan.ImageDecoder

/**
 * 二期：二维码视频（MP4）流解析（文档 5.1/5.3，FR-09）。
 * 封装端帧率恒为 15fps：第 i 帧时刻 = i × 1_000_000 / 15 微秒。
 */
object VideoParseHelper {

    private const val FPS = 15L
    private const val FRAME_US = 1_000_000L / FPS

    /** 阻塞解析，返回信封 JSON 文本；失败返回以 "!" 开头的提示文本；无码返回 null */
    fun parse(context: Context, uri: Uri): String? {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(context, uri)
            val durationMs = retriever.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_DURATION
            )?.toLongOrNull() ?: return null
            val durationUs = durationMs * 1000
            val collector = QrFrame.Collector()
            var t = 0L
            var sawAny = false
            while (t < durationUs) {
                val bmp = retriever.getFrameAtTime(
                    t, MediaMetadataRetriever.OPTION_CLOSEST
                ) ?: break
                for (txt in ImageDecoder.decodeBitmap(bmp)) {
                    sawAny = true
                    collector.offer(txt) // 非本视频任务的帧会被 Collector 忽略并返回 Invalid
                }
                bmp.recycle()
                t += FRAME_US
            }
            when {
                !sawAny -> null
                collector.n == 0 -> "!" + Msg.VIDEO_NO_QR
                collector.have < collector.n ->
                    "!" + Msg.VIDEO_INCOMPLETE + "（${collector.have}/${collector.n} 帧）"
                else -> collector.collectToEnvelopeText() ?: ("!" + Msg.VIDEO_INCOMPLETE)
            }
        } catch (e: Exception) {
            "!" + (e.message ?: Msg.VIDEO_NO_QR)
        } finally {
            try {
                retriever.release()
            } catch (e: Exception) {
            }
        }
    }
}
