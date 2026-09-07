package com.jztools.infoparse.scan

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import com.google.android.gms.tasks.Tasks
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.TimeUnit

/** 相册图片 / 视频 Bitmap 二维码解码（文档 4.3 / 5.3） */
object ImageDecoder {
    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
    )

    /**
     * 同步解码单张图片，返回二维码文本列表（多码图取全部）。
     * maxDim ≥ 2048：版本 40 点阵约 177×177 模块，压缩过度会识别失败（FAQ-6）。
     */
    fun decodeUri(context: Context, uri: Uri, maxDim: Int = 2048): List<String> {
        val bitmap = loadScaled(context, uri, maxDim) ?: return emptyList()
        return decodeBitmap(bitmap)
    }

    /** 同步解码 Bitmap（视频帧复用，文档 5.3） */
    fun decodeBitmap(bitmap: Bitmap): List<String> {
        val image = InputImage.fromBitmap(bitmap, 0)
        val out = mutableListOf<String>()
        return try {
            val codes = Tasks.await(scanner.process(image), 10, TimeUnit.SECONDS)
            for (b in codes) {
                b.rawValue?.let { out.add(it) }
                // 如遇中文乱码（FAQ-5），改用 b.rawBytes 与 String(bytes, Charsets.UTF_8)
            }
            out
        } catch (e: Exception) {
            emptyList()
        }
    }

    private fun loadScaled(context: Context, uri: Uri, maxDim: Int): Bitmap? = try {
        val opts = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, opts)
        }
        var sample = 1
        while (maxOf(opts.outWidth, opts.outHeight) / sample > maxDim) sample *= 2
        val o = BitmapFactory.Options().apply { inSampleSize = sample }
        context.contentResolver.openInputStream(uri)?.use {
            BitmapFactory.decodeStream(it, null, o)
        }
    } catch (e: Exception) {
        null
    }
}
