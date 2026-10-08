# -*- coding: utf-8 -*-
"""store.js 卸载补强：依赖卸载按钮 + 插件卸载两步确认（清数据选项）。"""
import io

p = "plugins/store/frontend/store.js"
s = io.open(p, encoding="utf-8").read()

# 1) 依赖条目：已安装态旁补「卸载」按钮
old = """      } else if (e.runtime === 'dep-slot') {
        html += '<button class="st-btn-install" disabled title="依赖组件已在本机生效">已安装</button>';
      }
    }"""
new = """      } else if (e.runtime === 'dep-slot') {
        html += '<button class="st-btn-install" disabled title="依赖组件已在本机生效">已安装</button>';
      }
    }
    if (IN_SHELL && e.runtime === 'dep-slot' && (st.installed || st.entry)) {
      html += '<button class="st-btn-danger" data-act="uninstall-dep" data-id="' + esc(e.id) + '">卸载</button>';
    }"""
assert old in s, "依赖已安装态锚点"
s = s.replace(old, new, 1)

# 2) 卸载动作：依赖走新分支；插件两步确认（第二步选清数据）
old2 = """    if (act === 'uninstall') {
      if (!confirm('卸载 ' + id + '？\\n本地包副本将被删除；数据默认保留。')) return;
      post('/local-api/plugins/uninstall', { id: id }).then(function (d) {
        msg(d.ok ? '已卸载（数据保留在数据目录）' : ('卸载失败：' + (d.error || '')), !d.ok);
        loadLocalState().then(render);
      });
      return;
    }"""
new2 = """    if (act === 'uninstall-dep') {
      if (!confirm('卸载依赖组件 ' + id + '？\\n依赖组件无独立业务数据；正在被映射的原生库将在下次启动客户端后清除。')) return;
      post('/local-api/store/uninstall-dep', { id: id }).then(function (d) {
        msg(d.ok ? ('已卸载依赖 ' + id + (d.pending_remove ? '（原生库将于下次启动清除）' : ''))
                 : ('卸载失败：' + (d.error || '')), !d.ok);
        loadLocalState().then(render);
      });
      return;
    }
    if (act === 'uninstall') {
      if (!confirm('卸载 ' + id + '？\\n本地包副本将被删除。\\n点击「确定」进入下一步（选择是否清除数据）。')) return;
      var purge = confirm('是否同时清除该插件的数据？\\n\\n'
        + '数据 = 数据根 plugins 下的插件目录（配置 / 缓存 / 本地业务数据）。\\n'
        + '确定 = 清除数据；取消 = 保留数据（可稍后再装回）。');
      post('/local-api/plugins/uninstall', { id: id, purge_data: purge }).then(function (d) {
        msg(d.ok ? ('已卸载' + (d.data_purged ? '（数据已清除）' : '（数据保留在数据目录）'))
                 : ('卸载失败：' + (d.error || '')), !d.ok);
        loadLocalState().then(render);
      });
      return;
    }"""
assert old2 in s, "卸载动作锚点"
s = s.replace(old2, new2, 1)

io.open(p, "w", encoding="utf-8", newline="").write(s)
print("store.js uninstall patch ok")
