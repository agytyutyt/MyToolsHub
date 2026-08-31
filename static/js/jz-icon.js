/* JZIcon — 跨浏览器 emoji / SVG 图标兼容层
 *
 * 背景：Windows 7 及部分旧浏览器缺少彩色 emoji 字体（Segoe UI Emoji 是 Win8+ 才有），
 * 直接把 emoji 写进页面会渲染成「方框」。本工具在运行时检测当前环境是否能正常渲染
 * 彩色 emoji：
 *   - 支持（现代浏览器/系统）→ 继续用 emoji 文本，保持原生观感；
 *   - 不支持（Win7 等）→ 自动替换为 static/icons/ 下的 Twemoji SVG 图片。
 *
 * 用法：
 *   1. 页面引入 <script src="/static/js/jz-icon.js"></script>；
 *   2. JS 动态生成：使用 JZIcon.html('📄') 返回「emoji 或 <img>」的 HTML；
 *   3. 静态 HTML：在元素上写 data-jz-icon="📄"（如 <span data-jz-icon="📄"></span>），
 *      脚本在 DOMContentLoaded 时自动处理，支持环境优先用 emoji、否则换成 SVG。
 */
(function (global) {
  'use strict';

  // 已知 emoji → Twemoji SVG 文件名映射（static/icons/ 下已内置）
  var MAP = {
    '📄': '1f4c4.svg',   // shared-docs Word
    '📊': '1f4ca.svg',   // shared-docs Excel
    '📝': '1f4dd.svg',   // shared-docs 文档
    '📢': '1f4e2.svg',   // notice-board 公告板
    '📭': '1f4ed.svg',   // notice-board 空状态
    '📋': '1f4cb.svg',   // case-report 战果录入
    '🗺️': '1f5fa.svg',   // map-marker 地图标点
    '📷': '1f4f7.svg',   // map-marker 二维码识别
    '⚙️': '2699.svg',    // 配置
    '▶': '25b6.svg',     // 播放
    '⏮': '23ee.svg',     // 回到起点
    '⏸': '23f8.svg',     // 暂停
    '🖱': '1f5b1.svg',    // character-graph 鼠标提示
    '✏️': '270f.svg',     // 编辑
    '🙈': '1f648.svg',    // 隐藏
    '🔍': '1f50d.svg',    // 解析
    '💾': '1f4be.svg',    // 保存
    '👤': '1f464.svg',    // 用户
    '🏢': '1f3e2.svg',    // 部门
    '🏛️': '1f3db.svg',    // 单位
    '💡': '1f4a1.svg',    // 提示
    '✅': '2705.svg',     // 校验通过
    '❌': '274c.svg',     // 校验失败
    '⚠️': '26a0.svg',     // 冲突警告
    '🧩': '1f9e9.svg',     // 拼图（通用 fallback 图标）
    '🔐': '1f510.svg',     // base64
    '🌌': '1f30c.svg',     // character-graph
    '🎨': '1f3a8.svg',     // color-picker
    '#️⃣': '23-20e3.svg',   // md5-generator
    '📽️': '1f4fd.svg',     // qr-video-decode
    '🛰️': '1f6f0.svg',     // trajectory-convert
  };

  var _supported = null;

  // sessionStorage 缓存键：仅首次进入站点时做一次 canvas 检测，之后（含 iframe 插件页）
  // 直接读缓存，避免每个页面重复执行像素检测的开销。
  var CACHE_KEY = 'jz_emoji_supported_v2';

  function readCache() {
    try {
      var v = window.sessionStorage.getItem(CACHE_KEY);
      if (v === '1') { _supported = true; return true; }
      if (v === '0') { _supported = false; return true; }
    } catch (e) { /* 隐私模式等场景忽略 */ }
    return false;
  }

  function writeCache(val) {
    try {
      window.sessionStorage.setItem(CACHE_KEY, val ? '1' : '0');
    } catch (e) { /* 忽略 */ }
  }

  // 检测是否能渲染彩色 emoji：在画布上画出 😀，统计字形像素的「色相多样性」。
  // 彩色 emoji 字体（Segoe UI Emoji 等，Win8+）会渲染出多种颜色（黄脸/红嘴/黑眼）；
  // 而 Win7 等缺少彩色 emoji 字体时，字形回退为单色「方框」（tofu）或单色字形，
  // 无论画布 fillStyle 设成什么颜色，字形始终只有一种色相 → 判定不支持，改用 SVG。
  function supportsColorEmoji() {
    if (_supported !== null) return _supported;
    if (readCache()) return _supported;
    try {
      var canvas = document.createElement('canvas');
      canvas.width = 32;
      canvas.height = 32;
      var ctx = canvas.getContext('2d');
      ctx.fillStyle = '#000000';
      ctx.fillRect(0, 0, 32, 32);
      ctx.font = '28px "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", "Twemoji Mozilla", sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillStyle = '#ff0000';
      ctx.fillText('\uD83D\uDE00', 2, 2); // 😀
      var data = ctx.getImageData(0, 0, 32, 32).data;
      var hueBins = {};
      var colored = 0;
      for (var i = 0; i < data.length; i += 4) {
        var r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
        if (a < 10) continue;               // 透明像素跳过
        if (r + g + b < 30) continue;       // 近黑背景/描边跳过
        // 灰阶像素（单色字形/反锯齿）跳过
        if (Math.abs(r - g) <= 24 && Math.abs(g - b) <= 24 && Math.abs(r - b) <= 24) continue;
        var max = Math.max(r, g, b), min = Math.min(r, g, b);
        if (max - min < 16) continue;       // 低饱和（接近灰）跳过
        // 粗略色相分桶（12 桶），统计出现几种颜色
        var hue = 0;
        if (max === r) hue = ((g - b) / (max - min)) % 6;
        else if (max === g) hue = (b - r) / (max - min) + 2;
        else hue = (r - g) / (max - min) + 4;
        if (hue < 0) hue += 6;
        var bin = Math.floor(hue / 6 * 12);
        if (bin > 11) bin = 11;
        hueBins[bin] = (hueBins[bin] || 0) + 1;
        colored++;
      }
      // 至少 2 个不同色相（各 ≥2 像素）才判定为彩色；单色方框只有 1 种色相
      var bins = 0;
      for (var k in hueBins) {
        if (hueBins[k] >= 2) bins++;
      }
      _supported = colored > 5 && bins >= 2;
    } catch (e) {
      _supported = false;
    }
    writeCache(_supported);
    return _supported;
  }

  function svgFile(emoji) {
    return MAP[emoji] || null;
  }

  // 生成单个图标的 HTML：支持彩色 emoji 时返回原 emoji，否则返回 SVG <img>
  function html(emoji) {
    if (!emoji) return '';
    if (supportsColorEmoji()) return emoji;
    var f = svgFile(emoji);
    if (f) return '<img class="jz-icon" src="/static/icons/' + encodeURIComponent(f) + '" alt="">';
    return emoji; // 无对应 SVG 时保留 emoji 文本
  }

  // 处理静态 HTML 中的 data-jz-icon 占位元素
  // 支持彩色 emoji 时填入 emoji 文本，否则替换为 SVG <img>（两种情况都填充，
  // 避免空 span 导致图标不显示）。
  function processDom(root) {
    var useEmoji = supportsColorEmoji();
    var scope = root && root.querySelectorAll ? root : document;
    var els = scope.querySelectorAll('[data-jz-icon]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      var emoji = el.getAttribute('data-jz-icon') || '';
      if (useEmoji) {
        el.textContent = emoji;
        continue;
      }
      var f = svgFile(emoji);
      if (f) el.innerHTML = '<img class="jz-icon" src="/static/icons/' + encodeURIComponent(f) + '" alt="">';
      else el.textContent = emoji;
    }
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { processDom(document); });
    } else {
      processDom(document);
    }
  }

  global.JZIcon = {
    supportsColorEmoji: supportsColorEmoji,
    svgFile: svgFile,
    html: html,
    processDom: processDom,
  };
})(window);
