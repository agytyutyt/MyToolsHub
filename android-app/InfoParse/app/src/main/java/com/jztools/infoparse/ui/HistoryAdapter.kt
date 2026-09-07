package com.jztools.infoparse.ui

import android.content.res.ColorStateList
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView
import com.jztools.infoparse.R
import com.jztools.infoparse.history.HistoryRecord
import com.jztools.infoparse.protocol.Fmt
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** 主页识别历史卡片（格式徽标 + 名称 + 时间/统计 + 内容摘要） */
class HistoryAdapter(
    private val onClick: (HistoryRecord) -> Unit,
    private val onLongClick: (HistoryRecord) -> Unit,
) : RecyclerView.Adapter<HistoryAdapter.VH>() {

    private val items = mutableListOf<HistoryRecord>()
    private val timeFmt = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())

    fun submit(list: List<HistoryRecord>) {
        items.clear()
        items.addAll(list)
        notifyDataSetChanged()
    }

    class VH(v: View) : RecyclerView.ViewHolder(v) {
        val badge: TextView = v.findViewById(R.id.tvFmtBadge)
        val name: TextView = v.findViewById(R.id.tvName)
        val meta: TextView = v.findViewById(R.id.tvMeta)
        val snippet: TextView = v.findViewById(R.id.tvSnippet)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH =
        VH(LayoutInflater.from(parent.context).inflate(R.layout.item_history, parent, false))

    override fun getItemCount(): Int = items.size

    override fun onBindViewHolder(holder: VH, position: Int) {
        val rec = items[position]
        val env = rec.envelope
        holder.badge.text = Fmt.label(env.fmt)
        holder.badge.backgroundTintList = ColorStateList.valueOf(
            ContextCompat.getColor(holder.itemView.context, FmtUi.badgeColorRes(env.fmt))
        )
        holder.name.text = env.name
        holder.meta.text = "${timeFmt.format(Date(rec.time))} · ${FmtUi.statsText(env)}"
        // 摘要：文本类取前 60 字符；excel 统计行数、file 统计字节数已足够，不展示内容
        if (env.isExcel || env.isFile) {
            holder.snippet.visibility = View.GONE
        } else {
            val t = env.textData ?: ""
            holder.snippet.text = if (t.length > 60) t.substring(0, 60) else t
            holder.snippet.visibility = View.VISIBLE
        }
        holder.itemView.setOnClickListener { onClick(rec) }
        holder.itemView.setOnLongClickListener { onLongClick(rec); true }
    }
}
