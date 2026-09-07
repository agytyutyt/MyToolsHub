package com.jztools.infoparse.scan

import android.content.Context
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import com.google.mlkit.vision.barcode.BarcodeScannerOptions
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/** CameraX + ML Kit 实时扫码封装（文档 4.3） */
class CameraScanner(
    context: Context,
    private val onQrText: (String) -> Unit,
    private val onBrightness: ((Int) -> Unit)? = null,
) {
    private val appContext = context.applicationContext
    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
    )
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private var lastText: String? = null
    private var stopped = false
    private var frameCount = 0

    /** 绑定相机。view 为布局中的 PreviewView */
    fun start(lifecycleOwner: LifecycleOwner, view: PreviewView) {
        stopped = false
        val providerFuture = ProcessCameraProvider.getInstance(appContext)
        providerFuture.addListener({
            if (stopped) return@addListener
            val provider = providerFuture.get()
            val preview = Preview.Builder().build().also {
                it.setSurfaceProvider(view.surfaceProvider)
            }
            val analysis = ImageAnalysis.Builder()
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
            analysis.setAnalyzer(executor) { proxy -> process(proxy) }
            provider.unbindAll()
            provider.bindToLifecycle(
                lifecycleOwner, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis
            )
        }, ContextCompat.getMainExecutor(appContext))
    }

    private fun process(proxy: ImageProxy) {
        val media = proxy.image
        if (media == null) {
            proxy.close()
            return
        }
        // 低频亮度采样（每 12 帧一次）：供 UI 按背景明暗切换高反差文字颜色
        frameCount++
        if (onBrightness != null && frameCount % 12 == 0) {
            onBrightness?.invoke(averageLuminance(proxy))
        }
        val input = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
        scanner.process(input)
            .addOnSuccessListener { codes ->
                val text = codes.firstOrNull()?.rawValue
                // 同文本去重（FAQ-4 调整）：静态码面停留只回调一次；视频流码面高速切换逐帧处理。
                // 原 500ms 时间节流会把流式捕获率压到 2 次/秒，低于 zfec 重组所需帧比例。
                if (text != null && text != lastText) {
                    lastText = text
                    onQrText(text)
                }
            }
            .addOnCompleteListener { proxy.close() }
    }

    /** 生命周期恢复后清除去重状态：重新对准同一码面可再次触发（如返回主页重扫同一信封） */
    fun resetDedupe() {
        lastText = null
    }

    /** Y 平面抽样均值亮度（0-255）。绝对索引读取，不移动 buffer 位置，不影响 ML Kit 解码 */
    private fun averageLuminance(proxy: ImageProxy): Int {
        return try {
            val plane = proxy.planes[0]
            val buf = plane.buffer
            val rowStride = plane.rowStride
            val pixelStride = plane.pixelStride
            val step = 16 // 采样步长：足够估计均值且开销可忽略
            var total = 0L
            var count = 0
            var y = 0
            while (y < proxy.height) {
                var x = 0
                val rowStart = y * rowStride
                while (x < proxy.width) {
                    total += buf.get(rowStart + x * pixelStride).toInt() and 0xFF
                    count++
                    x += step
                }
                y += step
            }
            if (count == 0) 128 else (total / count).toInt()
        } catch (e: Exception) {
            128
        }
    }

    fun stop() {
        stopped = true
        scanner.close()
        executor.shutdown()
    }
}
