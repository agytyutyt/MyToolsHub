package com.jztools.infoparse

import android.content.Context
import android.media.MediaMetadataRetriever
import android.net.Uri
import com.jztools.infoparse.protocol.Envelope
import com.jztools.infoparse.protocol.EnvelopeParser
import com.jztools.infoparse.protocol.Jz2
import com.jztools.infoparse.protocol.Msg
import com.jztools.infoparse.protocol.PageCollector
import com.jztools.infoparse.protocol.QrFrame
import com.jztools.infoparse.protocol.ScanResult
import com.jztools.infoparse.scan.ImageDecoder

/**
 * 二期：二维码视频（MP4）流解析（文档 5.1/5.3，FR-09；v2 协议见 Jz2.kt）。
 * 封装端帧率恒为 15fps：第 i 帧时刻 = i × 1_000_000 / 15 微秒。
 * T09：逐帧收集满「每组 ≥k 帧」即早停（ZfecCompat GF 补齐缺失系统位），
 * 不再要求收齐全部 n 帧——缺帧 ≤ m-k 时无需重看全片。
 */
object VideoParseHelper {

    private const val FPS = 15L
    private const val FRAME_US = 1_000_000L / FPS

    /** 解析结论 */
    sealed class Result {
        /** 成功还原信封 */
        data class Ok(val envelope: Envelope) : Result()

        /** 失败（带用户文案） */
        data class Fail(val message: String) : Result()

        /** 视频中没有二维码 */
        object NoQr : Result()
    }

    /** 阻塞解析；无码返回 NoQr */
    fun parse(context: Context, uri: Uri): Result {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(context, uri)
            val durationMs = retriever.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_DURATION
            )?.toLongOrNull() ?: return Result.Fail(Msg.VIDEO_NO_QR)
            val durationUs = durationMs * 1000
            val collector = QrFrame.Collector()
            var t = 0L
            var sawAny = false
            while (t < durationUs) {
                val bmp = retriever.getFrameAtTime(
                    t, MediaMetadataRetriever.OPTION_CLOSEST
                ) ?: break
                for (data in ImageDecoder.decodeBitmap(bmp)) {
                    sawAny = true
                    collector.offerBytes(data) // 非本视频任务的帧会被收集器忽略
                }
                bmp.recycle()
                if (collector.isSatisfied()) break // T09 k-of-m 早停
                t += FRAME_US
            }
            when {
                !sawAny || collector.n == 0 -> Result.NoQr
                !collector.isSatisfied() ->
                    Result.Fail(Msg.VIDEO_INCOMPLETE + "（${collector.have}/${collector.n} 帧）")
                else -> when (val r = collector.assembleBytes()
                    ?.let { EnvelopeParser.handleBytes(it, PageCollector(), Jz2.EnvCollector()) }) {
                    is ScanResult.Single -> Result.Ok(r.envelope)
                    is ScanResult.Invalid -> Result.Fail(r.reason)
                    else -> Result.Fail(Msg.VIDEO_INCOMPLETE)
                }
            }
        } catch (e: Exception) {
            Result.Fail(e.message ?: Msg.VIDEO_NO_QR)
        } finally {
            try {
                retriever.release()
            } catch (e: Exception) {
            }
        }
    }
}
