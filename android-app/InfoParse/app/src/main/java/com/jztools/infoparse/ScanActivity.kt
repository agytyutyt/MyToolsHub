package com.jztools.infoparse

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.view.updatePadding
import com.jztools.infoparse.history.HistoryStore
import com.jztools.infoparse.protocol.Envelope
import com.jztools.infoparse.protocol.EnvelopeParser
import com.jztools.infoparse.protocol.Msg
import com.jztools.infoparse.protocol.PageCollector
import com.jztools.infoparse.protocol.QrFrame
import com.jztools.infoparse.protocol.ScanResult
import com.jztools.infoparse.scan.CameraScanner
import com.jztools.infoparse.scan.ImageDecoder
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/**
 * 识别页（FR-01 相机实时扫码 / FR-02 图片导入 / FR-03 多页收集 / FR-08 本地暂存）。
 * 同时提供二期「视频解析」入口（FR-09，文档第 5 章）与相机视频流采集（FR-11）。
 * 启动即持续识别：按内容自动分流——单张信封 / 多页拆分码 / QR-transfer 视频流帧。
 * 「放弃」（待继续任务框）可清空当前多页收集任务。
 */
class ScanActivity : AppCompatActivity() {

    private lateinit var scanner: CameraScanner
    private lateinit var statusView: TextView
    private lateinit var pendingBox: LinearLayout
    private lateinit var btnResume: Button
    private lateinit var btnDiscard: Button

    private var collector = PageCollector()
    private var frameCollector: QrFrame.Collector? = null
    private var lastInvalidAt = 0L

    /** 双指捏合变焦：小码/远距扫码时放大画面提升解析率 */
    private var scaleDetector: ScaleGestureDetector? = null

    /** 重置代数：每次重置 +1，仍在运行的后台解析回调据此作废，避免旧结果回写界面 */
    private val generation = AtomicInteger(0)

    private val pendingFile by lazy {
        java.io.File(filesDir, PENDING_FILE)
    }

    private val permLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { ok ->
            if (ok) startCamera()
            else {
                // E-10：拒绝相机权限 → 停用扫码、保留图片导入入口
                Toast.makeText(this, "需要相机权限才能扫码", Toast.LENGTH_LONG).show()
                statusView.text = "相机权限被拒绝，可通过「导入图片」识别"
            }
        }

    private val pickImage =
        registerForActivityResult(ActivityResultContracts.GetMultipleContents()) { uris ->
            if (uris.isNotEmpty()) decodeImages(uris)
        }

    private val pickVideo =
        registerForActivityResult(ActivityResultContracts.GetContent()) { uri ->
            uri?.let { startVideoParse(it) }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_scan)
        setupImmersive()
        statusView = findViewById(R.id.tvStatus)
        pendingBox = findViewById(R.id.boxPending)
        btnResume = findViewById(R.id.btnResume)
        btnDiscard = findViewById(R.id.btnDiscard)

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
            == PackageManager.PERMISSION_GRANTED
        ) startCamera() else permLauncher.launch(Manifest.permission.CAMERA)

        findViewById<Button>(R.id.btnImportImage).setOnClickListener {
            pickImage.launch("image/*")
        }
        findViewById<Button>(R.id.btnImportVideo).setOnClickListener {
            pickVideo.launch("video/mp4")
        }
        btnResume.setOnClickListener { onResumePending() }
        btnDiscard.setOnClickListener {
            collector.reset()
            frameCollector = null
            pendingFile.delete()
            pendingBox.visibility = View.GONE
            statusView.text = ""
        }
        restorePending()
    }

    override fun onResume() {
        super.onResume()
        if (::scanner.isInitialized) scanner.resetDedupe()
    }

    private fun startCamera() {
        scanner = CameraScanner(this, onQrText = { text -> runOnUiThread { onScanned(text) } })
        val previewView = findViewById<androidx.camera.view.PreviewView>(R.id.previewView)
        scanner.start(this, previewView)
        setupPinchZoom(previewView)
    }

    /** 双指捏合变焦： CameraScanner 绑定成功后调用，捏合缩放画面便于对准远处/小尺寸二维码 */
    private fun setupPinchZoom(view: androidx.camera.view.PreviewView) {
        val detector = ScaleGestureDetector(this, object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(d: ScaleGestureDetector): Boolean {
                val zi = scanner.zoomInfo ?: return false
                val next = (zi.currentRatio * d.scaleFactor).coerceIn(zi.minRatio, zi.maxRatio)
                zi.setRatio(next)
                return true
            }
        })
        scaleDetector = detector
        view.setOnTouchListener { v, ev ->
            detector.onTouchEvent(ev)
            if (ev.actionMasked == MotionEvent.ACTION_UP) v.performClick()
            true
        }
    }

    /** 沉浸式状态栏：相机预览延伸至系统栏之后（状态栏/导航栏透明），白色系统栏图标 */
    private fun setupImmersive() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        window.isNavigationBarContrastEnforced = false
        val root = findViewById<View>(R.id.rootScan)
        WindowInsetsControllerCompat(window, root).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }
        // 底部堆叠（提示胶囊 + 控制面板）按系统栏高度避让，避免按钮被导航栏/手势条遮挡
        val stack = findViewById<View>(R.id.bottomStack)
        ViewCompat.setOnApplyWindowInsetsListener(stack) { v, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            v.updatePadding(bottom = bars.bottom + dp(16))
            WindowInsetsCompat.CONSUMED
        }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    override fun onDestroy() {
        if (::scanner.isInitialized) scanner.stop()
        super.onDestroy()
    }

    /** 单次扫码文本 → 按内容自动分流：QR-transfer 帧 → 视频流收集器；JSON 信封 → 单张/多页 */
    private fun onScanned(text: String) {
        val t = text.trim()
        if (!t.startsWith("{") && QrFrame.parseHead(t) != null) {
            onFrameText(t)
            return
        }
        when (val r = EnvelopeParser.handle(t, collector)) {
            is ScanResult.Single -> openResult(r.envelope)
            is ScanResult.Page -> {
                statusView.text = collector.progressText()
                savePending()
            }
            is ScanResult.Invalid -> {
                // E-07：拼接还原失败 → 清空收集器重来
                if (r.reason == Msg.ASSEMBLE_FAILED) collector.reset()
                showInvalid(r.reason)
                statusView.text = if (collector.total > 0) collector.progressText() else ""
            }
            else -> {}
        }
    }

    /** 无效码提示限流：高速扫描下模糊/乱码文本频发，同类 Toast 至少间隔 1 秒 */
    private fun showInvalid(reason: String) {
        val now = System.currentTimeMillis()
        if (now - lastInvalidAt >= 1000) {
            lastInvalidAt = now
            Toast.makeText(this, reason, Toast.LENGTH_SHORT).show()
        }
    }

    /** 相机直扫二维码视频流：帧文本 → QrFrame 收集器（自动识别，无需手动开启） */
    private fun onFrameText(text: String) {
        val fc = frameCollector ?: QrFrame.Collector().also { frameCollector = it }
        when (val r = fc.offer(text)) {
            is ScanResult.Frame -> {
                statusView.text = getString(R.string.stream_progress, r.have, r.n)
                if (r.have == r.n) completeFrameCollect()
            }
            is ScanResult.Invalid -> {
                if (r.reason == Msg.FRAME_MISMATCH) {
                    // 与当前任务不一致 → 视为新视频流，重置后吸收当前帧
                    frameCollector = QrFrame.Collector().also { fresh ->
                        val r2 = fresh.offer(text)
                        if (r2 is ScanResult.Frame) {
                            statusView.text = getString(R.string.stream_progress, r2.have, r2.n)
                        }
                    }
                } else {
                    statusView.text = r.reason
                }
            }
            else -> {}
        }
    }

    private fun completeFrameCollect() {
        val envText = frameCollector?.collectToEnvelopeText()
        frameCollector = null // 完成或失败 → 回到待机，下次扫到帧自动新建收集器
        when {
            envText == null -> statusView.text = Msg.VIDEO_INCOMPLETE
            else -> when (val r = EnvelopeParser.handle(envText, PageCollector())) {
                is ScanResult.Single -> openResult(r.envelope)
                is ScanResult.Invalid -> statusView.text = r.reason
                else -> statusView.text = Msg.VIDEO_INCOMPLETE
            }
        }
    }

    /** 图片批量导入（FR-02）：可多选，逐张解码 → 同一收集器 → 立即尝试重组 */
    private fun decodeImages(uris: List<Uri>) {
        statusView.text = "正在解析图片…"
        val gen = generation.get()
        thread {
            val texts = mutableListOf<String>()
            var noQr = 0
            for (u in uris) {
                val ts = ImageDecoder.decodeUri(this, u)
                if (ts.isEmpty()) noQr++ else texts += ts
            }
            runOnUiThread {
                if (gen != generation.get()) return@runOnUiThread // 已重置 → 作废本次解析
                if (texts.isEmpty()) {
                    // E-09：图片里扫不出码
                    statusView.text = Msg.IMAGE_NO_QR
                    return@runOnUiThread
                }
                var result: ScanResult? = null
                for (t in texts) {
                    result = EnvelopeParser.handle(t, collector)
                    if (result is ScanResult.Single) break
                }
                when (val r = result) {
                    is ScanResult.Single -> openResult(r.envelope)
                    is ScanResult.Page -> {
                        statusView.text = collector.progressText() + "，请补扫缺失页"
                        savePending()
                    }
                    else -> statusView.text = (r as? ScanResult.Invalid)?.reason ?: ""
                }
            }
        }
    }

    // ---------- FR-08 多页收集本地暂存 ----------

    private fun savePending() {
        try {
            pendingFile.writeText(collector.serialize())
        } catch (e: Exception) {
            // 暂存失败不影响扫码主流程
        }
    }

    private fun restorePending() {
        if (!pendingFile.exists()) return
        val saved = PageCollector.restore(pendingFile.readText()) ?: run {
            pendingFile.delete()
            return
        }
        collector = saved
        if (!collector.isComplete) {
            pendingBox.visibility = View.VISIBLE
            btnResume.text = getString(R.string.btn_resume) + "（${collector.progressText()}）"
            statusView.text = ""
        } else {
            collector.assemble()?.let { openResult(it) }
        }
    }

    private fun onResumePending() {
        pendingBox.visibility = View.GONE
        statusView.text = collector.progressText()
    }

    // ---------- 二期：视频解析入口 ----------

    private fun startVideoParse(uri: Uri) {
        statusView.text = "正在解析视频…"
        val gen = generation.get()
        thread {
            val envText = VideoParseHelper.parse(this, uri)
            runOnUiThread {
                if (gen != generation.get()) return@runOnUiThread // 已重置 → 作废本次解析
                when {
                    envText == null -> statusView.text = Msg.VIDEO_NO_QR
                    envText.startsWith("!") -> statusView.text = envText.removePrefix("!")
                    else -> {
                        val collector2 = PageCollector()
                        when (val r = EnvelopeParser.handle(envText, collector2)) {
                            is ScanResult.Single -> openResult(r.envelope)
                            is ScanResult.Invalid -> statusView.text = r.reason
                            else -> statusView.text = Msg.VIDEO_INCOMPLETE
                        }
                    }
                }
            }
        }
    }

    /** 信封 → 写入识别历史 → 结果页（内存单例传递） */
    private fun openResult(env: Envelope) {
        HistoryStore.save(this, env)
        ResultStore.current = env
        pendingFile.delete() // 收集完成 → 清除暂存（FR-08）
        startActivity(Intent(this, ResultActivity::class.java))
    }

    companion object {
        private const val PENDING_FILE = "pending_collect.json"
    }
}
