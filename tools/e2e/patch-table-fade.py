# -*- coding: utf-8 -*-
"""补丁（2026-10-08）：目录表边缘渐隐真正落地——

1) admin-store.js：本页不引 admin-plugins.js（唯一打 .is-overflowing/.is-scrolled 的地方），
   此前这两个类**从无设置者** → 渐变带与左缘渐隐在真机上永不出现。补本页自带
   syncTableOverflow（语义与插件列表页一致：断点/滚动监听/只在真溢出且未到尽头时挂类）。
2) admin-store.html：掩码纪律修正——右缘渐隐关掉（钉列占据右缘，mask 会连钉列一起渐隐），
   左缘渐隐保留（滚动后左侧内容被隐藏）；渐变带注释与现实对齐；引用版本 ?v=8。

断言式替换：任一锚点未命中/不唯一即中止，绝不半改。
"""
import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JS = os.path.join(ROOT, "plugins/admin/frontend/js/admin-store.js")
HTML = os.path.join(ROOT, "plugins/admin/frontend/admin-store.html")


def patch(path, pairs):
    s = io.open(path, encoding="utf-8", newline="").read()
    assert "\r\n" not in s, "%s 含 CRLF，先统一行尾再打补丁" % os.path.basename(path)
    for i, (old, new) in enumerate(pairs, 1):
        n = s.count(old)
        assert n == 1, "%s 锚点%d 命中 %d 次（应为 1）" % (os.path.basename(path), i, n)
        s = s.replace(old, new, 1)
    io.open(path, "w", encoding="utf-8", newline="").write(s)
    print("ok:", os.path.basename(path), len(pairs), "处")


# ---------------- admin-store.js ----------------
patch(JS, [
    # 1) 溢出侦测函数（插在 render 之前）
    (
        """  function render() {
    var thead = document.getElementById('st-thead');
""",
        """  // 横向溢出侦测（2026-10-08）：本页不引 admin-plugins.js，需自带；驱动钉列渐变带
  // （.is-overflowing）与左缘渐隐（.is-scrolled），语义与插件列表页 syncTableOverflow 一致。
  function syncTableOverflow() {
    document.querySelectorAll('.data-table-wrap').forEach(function (w) {
      var overflowing = w.scrollWidth > w.clientWidth + 1;
      var atStart = w.scrollLeft <= 1;
      var atEnd = w.scrollLeft + w.clientWidth >= w.scrollWidth - 1;
      // 右缘渐隐/钉列渐变带 = 有溢出且没到尽头；左缘渐隐 = 有溢出且已向左滚动
      w.classList.toggle('is-overflowing', overflowing && !atEnd);
      w.classList.toggle('is-scrolled', overflowing && !atStart);
      if (!w.dataset.scrollBound) {
        w.dataset.scrollBound = '1';
        w.addEventListener('scroll', syncTableOverflow, { passive: true });
      }
    });
  }

  function render() {
    var thead = document.getElementById('st-thead');
""",
    ),
    # 2) 空表分支也要清类
    (
        """      tbody.innerHTML = '<tr><td colspan="7" class="admin-empty">' + (EMPTY[filter] || '暂无数据') + '</td></tr>';
      return;
""",
        """      tbody.innerHTML = '<tr><td colspan="7" class="admin-empty">' + (EMPTY[filter] || '暂无数据') + '</td></tr>';
      syncTableOverflow();
      return;
""",
    ),
    # 3) 渲染完重算（切页签/重载后宽度变化）
    (
        """      tbody.appendChild(tr);
    });
  }

  function renderStats() {
""",
        """      tbody.appendChild(tr);
    });
    syncTableOverflow();
  }

  function renderStats() {
""",
    ),
    # 4) 统计页表格写入后同样接管
    (
        """        + list(d.by_day, '按日') + list(d.by_user, '按用户')
        + '</div>';
    }).catch(function (e) { box.textContent = '统计加载失败：' + e; });
""",
        """        + list(d.by_day, '按日') + list(d.by_user, '按用户')
        + '</div>';
      syncTableOverflow();
    }).catch(function (e) { box.textContent = '统计加载失败：' + e; });
""",
    ),
    # 5) 视口变化重算
    (
        """  load();
})();
""",
        """  window.addEventListener('resize', syncTableOverflow);
  load();
})();
""",
    ),
])

# ---------------- admin-store.html ----------------
patch(HTML, [
    # 1) 掩码纪律：右缘关、左缘留（两条顺序不可换——同特异性后置者胜）
    (
        """    /* 操作列固定（目录管理：数据超长横向滚动时，操作列钉在右侧；2026-10-08）
       注：固定列与「边缘渐隐」互斥——mask 会把钉列一起渐隐，故溢出时关掉 mask，
       改用钉列自带左投影表达"内容被覆盖/可滚动"。 */
    #st-table-wrap.is-overflowing,
    #st-table-wrap.is-overflowing.is-scrolled {
      -webkit-mask-image: none; mask-image: none;
    }
""",
        """    /* 操作列固定（目录管理：数据超长横向滚动时，操作列钉在右侧；2026-10-08）
       边缘渐隐纪律：mask 会把钉列一起渐隐 → 右缘渐隐关掉（由钉列左缘渐变带表达覆盖），
       左缘渐隐保留（滚动后左侧内容被隐藏）；两条顺序不可换——同特异性时后置者胜。 */
    #st-table-wrap.is-overflowing {
      -webkit-mask-image: none; mask-image: none;
    }
    #st-table-wrap.is-scrolled {
      -webkit-mask-image: linear-gradient(to right, transparent, #000 28px);
      mask-image: linear-gradient(to right, transparent, #000 28px);
    }
""",
    ),
    # 2) 渐变带注释与现实对齐（类由本页自带侦测打）
    (
        """    /* 钉列左侧渐变隐藏带（2026-10-08）：滚动内容滑入钉列下缘前渐隐；
       仅溢出时显示（JS 的 .is-overflowing），避免无溢出时出现幽灵渐隐。
       钉列底色恒定（不加 hover 混合），渐隐终点色 = 列底 → 无缝。 */""",
        """    /* 钉列左侧渐变隐藏带（2026-10-08）：滚动内容滑入钉列下缘前渐隐；
       仅溢出时显示（本页自带 syncTableOverflow 打 .is-overflowing），避免幽灵渐隐。
       钉列底色恒定（不加 hover 混合），渐隐终点色 = 列底 → 无缝。 */""",
    ),
    # 3) 脚本引用版本递增（改动须递增引用处）
    (
        """<script src="/plugin/admin/js/admin-store.js?v=7"></script>""",
        """<script src="/plugin/admin/js/admin-store.js?v=8"></script>""",
    ),
])

print("patch-table-fade: 全部锚点命中")
