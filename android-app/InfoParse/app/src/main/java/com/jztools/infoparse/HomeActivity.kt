package com.jztools.infoparse

import android.content.Intent
import android.content.res.Configuration
import android.graphics.Color
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.coordinatorlayout.widget.CoordinatorLayout
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.core.view.updateLayoutParams
import androidx.core.view.updatePadding
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.AppBarLayout
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.floatingactionbutton.FloatingActionButton
import com.jztools.infoparse.history.HistoryRecord
import com.jztools.infoparse.history.HistoryStore
import com.jztools.infoparse.ui.HistoryAdapter

/**
 * 主页（启动页）：历次识别结果以卡片列表展示，右下角相机按钮进入识别页。
 * 点卡片回看结果（可导出/复制/分享），长按删除单条，菜单可清空全部历史。
 */
class HomeActivity : AppCompatActivity() {

    private lateinit var adapter: HistoryAdapter
    private lateinit var emptyState: View

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_home)
        setupImmersive()
        setSupportActionBar(findViewById(R.id.toolbar))

        adapter = HistoryAdapter(onClick = ::openRecord, onLongClick = ::confirmDelete)
        val recycler = findViewById<RecyclerView>(R.id.recyclerHistory)
        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = adapter
        emptyState = findViewById(R.id.emptyState)

        findViewById<FloatingActionButton>(R.id.fabScan).setOnClickListener {
            startActivity(Intent(this, ScanActivity::class.java))
        }
    }

    /**
     * 沉浸式（安卓边缘到边缘规范）：状态栏/导航栏透明，内容延伸至系统栏之后。
     * 顶部品牌蓝工具栏延伸到状态栏后面（白色图标）；底部手势条区域透明，
     * 列表从其下方穿过，FAB 与列表末项按导航栏高度避让。
     */
    private fun setupImmersive() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        window.statusBarColor = Color.TRANSPARENT
        window.navigationBarColor = Color.TRANSPARENT
        window.isNavigationBarContrastEnforced = false
        val night = resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK
        WindowInsetsControllerCompat(window, findViewById(R.id.rootHome)).apply {
            isAppearanceLightStatusBars = false // 顶部始终品牌蓝 → 白色状态栏图标
            isAppearanceLightNavigationBars = night != Configuration.UI_MODE_NIGHT_YES
        }
        val appBar = findViewById<AppBarLayout>(R.id.appBar)
        val recycler = findViewById<RecyclerView>(R.id.recyclerHistory)
        val fab = findViewById<FloatingActionButton>(R.id.fabScan)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById<View>(R.id.rootHome)) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            appBar.updatePadding(top = bars.top)
            recycler.updatePadding(bottom = dp(88) + bars.bottom) // 88dp 为 FAB 净空基准
            fab.updateLayoutParams<CoordinatorLayout.LayoutParams> {
                bottomMargin = dp(20) + bars.bottom
            }
            insets
        }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    override fun onResume() {
        super.onResume()
        refresh()
    }

    private fun refresh() {
        val list = HistoryStore.list(this)
        adapter.submit(list)
        emptyState.visibility = if (list.isEmpty()) View.VISIBLE else View.GONE
    }

    /** 历史卡片 → 结果页（标记来自历史，结果页隐藏「重新扫描」） */
    private fun openRecord(rec: HistoryRecord) {
        ResultStore.current = rec.envelope
        startActivity(
            Intent(this, ResultActivity::class.java)
                .putExtra(ResultActivity.EXTRA_FROM_HISTORY, true)
        )
    }

    private fun confirmDelete(rec: HistoryRecord) {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.delete_record_title)
            .setMessage(R.string.delete_record_msg)
            .setPositiveButton(R.string.btn_delete) { _, _ ->
                HistoryStore.delete(this, rec.id)
                refresh()
            }
            .setNegativeButton(R.string.btn_cancel, null)
            .show()
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.menu_home, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        if (item.itemId == R.id.action_clear) {
            MaterialAlertDialogBuilder(this)
                .setTitle(R.string.clear_history_title)
                .setMessage(R.string.clear_history_msg)
                .setPositiveButton(R.string.btn_delete) { _, _ ->
                    HistoryStore.clear(this)
                    refresh()
                }
                .setNegativeButton(R.string.btn_cancel, null)
                .show()
            return true
        }
        return super.onOptionsItemSelected(item)
    }
}
