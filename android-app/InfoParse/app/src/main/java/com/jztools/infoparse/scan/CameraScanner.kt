package com.jztools.infoparse.scan

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import androidx.camera.core.Camera
import androidx.camera.core.CameraControl
import androidx.camera.core.CameraInfo
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.camera2.interop.Camera2CameraInfo
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

/** CameraX + ML Kit 实时扫码封装（文档 4.3；T08 起回调原始字节 rawBytes，v2 二进制帧无损） */
class CameraScanner(
    context: Context,
    private val onQrBytes: (ByteArray) -> Unit,
) {
    private val appContext = context.applicationContext
    private val scanner = BarcodeScanning.getClient(
        BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .build()
    )
    private val executor: ExecutorService = Executors.newSingleThreadExecutor()
    private var lastBytes: ByteArray? = null
    private var stopped = false
    private var camera: Camera? = null

    /** 相机变焦参数（绑定成功后可用；null 表示未绑定或设备不支持变焦） */
    var zoomInfo: ZoomInfo? = null
        private set

    /** 变焦控制句柄：查询当前/最大变焦比并设置 */
    class ZoomInfo internal constructor(
        private val control: CameraControl,
        private val info: CameraInfo,
    ) {
        /** 最小变焦比（通常 1.0） */
        val minRatio: Float = info.zoomState.value?.minZoomRatio ?: 1f

        /** 最大变焦比（设备能力上限，超广角机型的广角镜头通常 ≥2） */
        val maxRatio: Float = info.zoomState.value?.maxZoomRatio ?: 1f

        /** 当前变焦比 */
        val currentRatio: Float
            get() = info.zoomState.value?.zoomRatio ?: 1f

        /** 设置变焦比（超出范围由 CameraX 自动截断） */
        fun setRatio(ratio: Float) {
            control.setZoomRatio(ratio)
        }
    }

    /**
     * 主广角优先选择器（A 方案）：DEFAULT_BACK_CAMERA 只约束"后置"，多摄机型可能落到超广角上
     * （视场角大、码面占比小、解析度低）。在后置候选内剔除变焦下限 <0.99 的超广角镜头；
     * 特性值读不到的镜头保守保留（视为普通镜头）。剔除后无候选则回退 DEFAULT_BACK_CAMERA。
     */
    private fun mainWideAngleCameraSelector(provider: ProcessCameraProvider): CameraSelector {
        val candidates = provider.availableCameraInfos.filter { info ->
            val info2 = Camera2CameraInfo.from(info)
            val isBack = info2.getCameraCharacteristic(CameraCharacteristics.LENS_FACING) ==
                CameraCharacteristics.LENS_FACING_BACK
            val range = info2.getCameraCharacteristic(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE)
            val isUltraWide = range != null && range.lower.toFloat() < 0.99f
            isBack && !isUltraWide
        }
        return if (candidates.isEmpty()) CameraSelector.DEFAULT_BACK_CAMERA
        else CameraSelector.Builder().addCameraFilter { candidates }.build()
    }

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
            camera = provider.bindToLifecycle(
                lifecycleOwner, mainWideAngleCameraSelector(provider), preview, analysis
            )
            camera?.let { cam -> zoomInfo = ZoomInfo(cam.cameraControl, cam.cameraInfo) }
        }, ContextCompat.getMainExecutor(appContext))
    }

    private fun process(proxy: ImageProxy) {
        val media = proxy.image
        if (media == null) {
            proxy.close()
            return
        }
        val input = InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees)
        scanner.process(input)
            .addOnSuccessListener { codes ->
                // T08：rawValue 会把非 UTF-8 字节破坏（替换为 U+FFFD），v2 二进制帧
                // 必须取 rawBytes 原始字节；v1 文本帧 String(bytes, UTF_8) 与原值等价。
                val bytes = codes.firstOrNull()?.rawBytes
                // 同内容去重（FAQ-4 调整）：静态码面停留只回调一次；视频流码面高速切换逐帧处理。
                // 原 500ms 时间节流会把流式捕获率压到 2 次/秒，低于 zfec 重组所需帧比例。
                if (bytes != null && !bytes.contentEquals(lastBytes)) {
                    lastBytes = bytes
                    onQrBytes(bytes)
                }
            }
            .addOnCompleteListener { proxy.close() }
    }

    /** 生命周期恢复后清除去重状态：重新对准同一码面可再次触发（如返回主页重扫同一信封） */
    fun resetDedupe() {
        lastBytes = null
    }

    fun stop() {
        stopped = true
        camera = null
        zoomInfo = null
        scanner.close()
        executor.shutdown()
    }
}
