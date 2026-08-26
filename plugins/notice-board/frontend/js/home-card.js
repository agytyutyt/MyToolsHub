/* 公告板 —— 首页工具卡片动态化
 * 把首页 notice-board 卡片的 description 替换为最新一条公告的内容，
 * 并将原关键词（features）区域替换为该公告的发布时间徽章。
 * 无可读公告或接口异常时保持 tools.json / manifest 中的静态文案不变。
 *
 * 时序说明：main.js 异步渲染工具卡片，本脚本以 MutationObserver 等待
 * 卡片节点出现后应用一次，应用成功即停止观察。
 */
(function () {
  "use strict";

  var PLUGIN_ID = "notice-board";
  var MAX_DESC_LEN = 64;
  var applied = false;

  function truncate(s, n) {
    s = String(s == null ? "" : s).trim();
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  function tryApply(item) {
    var card = document.querySelector('.tool-card[data-tool-id="' + PLUGIN_ID + '"]');
    if (!card) return false;
    var desc = card.querySelector(".tool-desc");
    var feats = card.querySelector(".tool-features");
    if (!desc || !feats) return false;

    desc.textContent = "📢 " + truncate(item.content, MAX_DESC_LEN);

    feats.innerHTML = "";
    var chip = document.createElement("span");
    chip.className = "chip";
    chip.title = "最新公告发布时间";
    chip.textContent = "🕐 " + (item.created_at || "");
    feats.appendChild(chip);
    return true;
  }

  fetch("/api/notice-board/latest")
    .then(function (res) { return res.ok ? res.json() : null; })
    .then(function (data) {
      if (!data || !data.ok || !data.item || applied) return;
      var item = data.item;
      if (tryApply(item)) { applied = true; return; }
      // 卡片尚未渲染：监听网格变化，出现后应用一次
      var grid = document.getElementById("tool-grid") || document.body;
      var mo = new MutationObserver(function () {
        if (!applied && tryApply(item)) {
          applied = true;
          mo.disconnect();
        }
      });
      mo.observe(grid, { childList: true, subtree: true });
    })
    .catch(function () { /* 静默：保持静态文案 */ });
})();
