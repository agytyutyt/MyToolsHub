package com.jztools.infoparse

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.res.ColorStateList
import android.content.res.Configuration
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.HorizontalScrollView
import android.widget.TableLayout
import android.widget.TableRow
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.TooltipCompat
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.view.updatePadding
import com.google.android.material.appbar.AppBarLayout
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.bottomnavigation.BottomNavigationView
import com.jztools.infoparse.export.Exporter
import com.jztools.infoparse.protocol.Envelope
import com.jztools.infoparse.protocol.Fmt
import com.jztools.infoparse.ui.FmtUi
import com.jztools.infoparse.util.Csv
import com.jztools.infoparse.util.DocxWriter
import com.jztools.infoparse.util.XlsxWriter

/**
 * 结果展示页（FR-04/05/06/07）：格式徽标、来源名、摘要、内容预览、导出/分享/复制。
 * 内容卡片可点击：按信封声明还原为对应文件格式后，调用外部应用（如 WPS）打开阅读。
 * 从识别页进入显示「重新扫描」；从主页历史卡片进入（EXTRA_FROM_HISTORY）隐藏该按钮。
 */
class ResultActivity : AppCompatActivity() {

    private lateinit var env: Envelope
    private var exported: Uri? = null

    private val rows: List<List<Any?>>
        get() = @Suppress("UNCHECKED_CAST") (env.data as? List<List<Any?>>)
            ?: emptyList()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        env = ResultStore.current ?: run {
            finish()
            return
        }
        setContentView(R.layout.activity_result)
        setupImmersive()

        val badge = findViewById<TextView>(R.id.tvFmtBadge)
        val stats = findViewById<TextView>(R.id.tvStats)
        val textPreview = findViewById<TextView>(R.id.tvPreview)
        val tableScroll = findViewById<HorizontalScrollView>(R.id.tableScroll)
        val table = findViewById<TableLayout>(R.id.tablePreview)

        findViewById<MaterialToolbar>(R.id.toolbar).apply {
            title = env.name
            setNavigationOnClickListener { finish() }
        }
        badge.text = Fmt.label(env.fmt) + (env.ext?.let { " · .$it" } ?: "")
        badge.backgroundTintList = ColorStateList.valueOf(
            ContextCompat.getColor(this, FmtUi.badgeColorRes(env.fmt))
        )

        // 统计行（4.4 规则）：excel 行数 / 文本字符数；处理说明按传输模式展示
        stats.text = FmtUi.statsText(env)
        findViewById<TextView>(R.id.tvSummaryNote).text = if (env.isFile) {
            getString(R.string.summary_note_file)
        } else {
            getString(R.string.summary_note)
        }

        // 内容预览：文本类最多 4000 字符；excel 表格最多 100 行（E-11）；file 不展示内容
        if (env.isExcel) {
            textPreview.visibility = View.GONE
            renderTable(table)
            tableScroll.visibility = View.VISIBLE
        } else if (env.isFile) {
            textPreview.text = "原始文件「${env.name}」已完整还原。\n\n点击内容卡片可调用其他应用（如 WPS）打开阅读。"
            tableScroll.visibility = View.GONE
        } else {
            val text = env.textData ?: ""
            textPreview.text = if (text.length > 4000) {
                text.substring(0, 4000) + "…（内容较长，导出文件可查看全部）"
            } else text
            tableScroll.visibility = View.GONE
        }

        // 内容卡片点击 → 还原为对应格式文件并用外部应用打开（WPS 等）；file 直接还原原文件
        val contentCard = findViewById<View>(R.id.contentCard)
        contentCard.setOnClickListener { openWithExternal() }

        // 底部动作导航栏：导出/复制/分享文件（纯动作，不保持选中态）
        val bottomNav = findViewById<BottomNavigationView>(R.id.bottomNav)
        bottomNav.setOnItemSelectedListener { item ->
            when (item.itemId) {
                R.id.action_export -> doExport()
                R.id.action_copy -> doCopy()
                R.id.action_share_file -> doShareFile()
            }
            false
        }
        // 长按图标 → 系统气泡显示功能名称
        for (i in 0 until bottomNav.menu.size()) {
            val item = bottomNav.menu.getItem(i)
            bottomNav.findViewById<View>(item.itemId)?.let {
                TooltipCompat.setTooltipText(it, item.title)
            }
        }
        findViewById<Button>(R.id.btnRescan).apply {
            // 来自主页历史卡片时没有"当前识别流程",隐藏重新扫描(返回键即回主页)
            visibility = if (intent.getBooleanExtra(EXTRA_FROM_HISTORY, false)) {
                View.GONE
            } else {
                View.VISIBLE
            }
            setOnClickListener { finish() }
        }
    }

    /** excel 表格预览：HorizontalScrollView 包 TableLayout，首行加粗 */
    private fun renderTable(table: TableLayout) {
        val limited = rows.take(100)
        for ((idx, row) in limited.withIndex()) {
            val tr = TableRow(this)
            for (cell in row) {
                val tv = TextView(this).apply {
                    text = when (cell) {
                        null -> ""
                        is Boolean -> if (cell) "TRUE" else "FALSE"
                        else -> cell.toString()
                    }
                    setPadding(16, 12, 16, 12)
                    textSize = 13f
                    if (idx == 0) {
                        setTypeface(typeface, android.graphics.Typeface.BOLD)
                    }
                    gravity = Gravity.CENTER_VERTICAL
                }
                tr.addView(tv)
            }
            table.addView(tr)
        }
        if (rows.size > 100) {
            val tr = TableRow(this)
            tr.addView(TextView(this).apply {
                text = "…（仅预览前 100 行，导出文件可查看全部 ${rows.size} 行）"
                setPadding(16, 12, 16, 12)
                textSize = 13f
            })
            table.addView(tr)
        }
    }

    /** 精简声明后缀归一后的还原扩展名（doc→docx、xls/xlsm→xlsx、csv 保留） */
    private fun normalizedExt(): String? = Exporter.normalizedExt(env.fmt, env.ext)

    /**
     * 导出内容字节（FR-05）：file → base64 还原原始文件；word → 重建 .docx；
     * excel → 重建 .xlsx（纯数据，声明 csv 时还原 .csv 文本）；
     * text/markdown → 原文 UTF-8。base64 解码失败返回 null。
     */
    private fun exportBytes(): ByteArray? = when {
        env.isFile -> env.fileBytes()
        env.isExcel -> if (normalizedExt() == "csv") {
            Csv.fromRows(rows).toByteArray(Charsets.UTF_8)
        } else {
            XlsxWriter.fromRows(rows)
        }
        env.fmt == Fmt.WORD -> DocxWriter.fromText(env.textData ?: "")
        else -> (env.textData ?: "").toByteArray(Charsets.UTF_8)
    }

    /** 导出 mime：精简传输按归一后缀，file 按扩展名推断，其余按格式导出规则 */
    private fun exportMime(): String = when {
        env.isFile -> Exporter.fileMime(env.name)
        else -> Exporter.extMime(normalizedExt()) ?: Exporter.exportSpec(env.fmt).second
    }

    private fun doExport() {
        val bytes = exportBytes() ?: run {
            Toast.makeText(this, "文件数据异常", Toast.LENGTH_LONG).show()
            return
        }
        val fileName = Exporter.exportFileName(env.fmt, env.name, env.ext)
        val uri = Exporter.saveToDownloads(this, fileName, bytes, exportMime())
        if (uri != null) {
            exported = uri
            Toast.makeText(this, "已保存到下载：$fileName", Toast.LENGTH_LONG).show()
        }
    }

    /** 复制全部（FR-07）：excel 用 TSV（TC-12）；file 无文本可复制 */
    private fun doCopy() {
        if (env.isFile) {
            Toast.makeText(this, "原始文件不提供文本复制，请导出文件", Toast.LENGTH_SHORT).show()
            return
        }
        val text = if (env.isExcel) Csv.toTsv(rows) else env.textData ?: ""
        val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        cm.setPrimaryClip(ClipData.newPlainText(env.name, text))
        Toast.makeText(this, "已复制全部内容", Toast.LENGTH_SHORT).show()
    }

    private fun doShareFile() {
        val bytes = exportBytes() ?: run {
            Toast.makeText(this, "文件数据异常", Toast.LENGTH_LONG).show()
            return
        }
        val fileName = Exporter.exportFileName(env.fmt, env.name, env.ext)
        val uri = exported ?: Exporter.saveToDownloads(this, fileName, bytes, exportMime())
        if (uri != null) {
            exported = uri
            Exporter.shareFile(this, uri, exportMime())
        }
    }

    /**
     * 内容卡片点击（FR-04 扩展）：按信封声明还原为对应文件格式（file 直接还原原文件，
     * word→.docx、excel→.xlsx、markdown→.md、text→.txt），保存后调用外部应用
     * （如 WPS Office）打开阅读；无可处理应用时提示。
     */
    private fun openWithExternal() {
        val bytes = exportBytes() ?: run {
            Toast.makeText(this, "文件数据异常", Toast.LENGTH_LONG).show()
            return
        }
        val fileName = Exporter.exportFileName(env.fmt, env.name, env.ext)
        val mime = exportMime()
        val uri = exported ?: Exporter.saveToDownloads(this, fileName, bytes, mime)
        if (uri == null) {
            Toast.makeText(this, "文件保存失败，无法打开", Toast.LENGTH_LONG).show()
            return
        }
        exported = uri
        if (!Exporter.openFile(this, uri, mime)) {
            Toast.makeText(this, "未找到可打开该文件的应用（可安装 WPS Office）", Toast.LENGTH_LONG).show()
        }
    }

    /**
     * 沉浸式（安卓边缘到边缘规范）：状态栏/导航栏透明。
     * 顶部品牌蓝工具栏延伸到状态栏后面（白色状态栏图标）；底部导航栏背景延伸至手势条
     * 区域（图标保持在线上方），导航栏图标颜色随日夜模式切换。只用局部 findViewById。
     */
    private fun setupImmersive() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        window.isNavigationBarContrastEnforced = false
        val night = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = false // 顶部始终品牌蓝 → 白色状态栏图标
            isAppearanceLightNavigationBars = night != Configuration.UI_MODE_NIGHT_YES
        }
        val appBar = findViewById<AppBarLayout>(R.id.appBar)
        val bottomNav = findViewById<BottomNavigationView>(R.id.bottomNav)
        ViewCompat.setOnApplyWindowInsetsListener(window.decorView) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            appBar.updatePadding(top = bars.top)
            // 手势条高度作为导航栏内边距：图标在线上方，藏蓝背景铺满手势区
            bottomNav.updatePadding(bottom = bars.bottom)
            insets
        }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    companion object {
        const val EXTRA_FROM_HISTORY = "from_history"
    }
}
