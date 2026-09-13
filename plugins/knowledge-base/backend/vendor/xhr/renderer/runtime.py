"""客户端 runtime —— 对应 TS 版 ``packages/runtime/src/script.ts``（T-404）。

``runtime_script(prefix)`` 返回**自包含 IIFE 字符串**（原生 JS、零依赖、约 5KB），
由 renderer 在 with_runtime=True 时内联进输出 HTML。
⚠️ 必须是纯字符串拼接（不依赖打包器转译内联代码），双击 HTML 打开即可用。

功能：
  · tabs：事件委托点击 .xhr-tab → 切换 aria-selected + 对应 .xhr-sheet 显隐
  · search：工具栏输入 → debounce 200ms → 命中 <td> 加 .xhr-hit → 滚动到第一个
    （大表限流：最多遍历 50000 个 td）
  · print：工具栏打印按钮 + beforeprint/afterprint 切换 xhr-printing 类
"""
from __future__ import annotations

import json


def runtime_script(prefix: str) -> str:
    """生成内联脚本（字符串）。参数是 CSS 前缀（已过 sanitize_prefix）。"""
    p = json.dumps(prefix)  # 安全字面量化
    return f"""(function(){{"use strict";
var P={p};
function root(){{return document.querySelector('.'+P)||document.body}}
function currentSheet(){{var r=root();var s=r.querySelectorAll('.'+P+'-sheet');for(var i=0;i<s.length;i++){{if(!s[i].hidden)return s[i]}}return s[0]}}
/* ── tabs：事件委托 ── */
root().addEventListener('click',function(ev){{
  var tab=ev.target.closest&&ev.target.closest('.'+P+'-tab');if(!tab)return;
  var idx=tab.getAttribute('data-target');
  var r=root();
  var tabs=r.querySelectorAll('.'+P+'-tab');
  var sheets=r.querySelectorAll('.'+P+'-sheet');
  for(var i=0;i<tabs.length;i++){{tabs[i].setAttribute('aria-selected',String(i===Number(idx)))}}
  for(var j=0;j<sheets.length;j++){{sheets[j].hidden=(String(j)!==idx)}}
  clearHits();
}});
/* ── search：工具栏 + debounce + 命中高亮 ── */
var SEARCH_LIMIT=50000;
var timer=null;var input=null;var counter=null;
function ensureToolbar(){{
  var r=root();
  var tabs=r.querySelector('.'+P+'-tabs');
  if(r.querySelector('.'+P+'-toolbar'))return;
  var bar=document.createElement('div');bar.className=P+'-toolbar';
  input=document.createElement('input');input.type='search';input.className=P+'-search';
  input.placeholder='搜索（当前工作表）';input.setAttribute('aria-label','搜索单元格内容');
  counter=document.createElement('span');counter.className=P+'-count';
  var btn=document.createElement('button');btn.className=P+'-print';btn.textContent='打印';
  btn.addEventListener('click',function(){{window.print()}});
  bar.appendChild(input);bar.appendChild(counter);bar.appendChild(btn);
  r.insertBefore(bar,r.firstChild);
  input.addEventListener('input',function(){{
    clearTimeout(timer);timer=setTimeout(doSearch,200);
  }});
}}
function clearHits(){{
  var hits=root().querySelectorAll('td.'+P+'-hit');
  for(var i=0;i<hits.length;i++)hits[i].classList.remove(P+'-hit');
  if(counter)counter.textContent='';
}}
function doSearch(){{
  clearHits();
  var q=(input.value||'').trim().toLowerCase();
  if(!q)return;
  var sheet=currentSheet();
  if(!sheet)return;
  var tds=sheet.querySelectorAll('td');
  var n=Math.min(tds.length,SEARCH_LIMIT);
  var hits=[];
  for(var i=0;i<n;i++){{
    var text=(tds[i].textContent||'').toLowerCase();
    if(text.indexOf(q)!==-1){{tds[i].classList.add(P+'-hit');hits.push(tds[i])}}
  }}
  if(counter)counter.textContent=hits.length?('命中 '+hits.length+' 格'):'无命中';
  if(hits.length&&hits[0].scrollIntoView)hits[0].scrollIntoView({{block:'center'}});
}}
/* ── print：解除 max-height / 隐藏工具栏 ── */
function beforePrint(){{document.body.classList.add(P+'-printing')}}
function afterPrint(){{document.body.classList.remove(P+'-printing')}}
window.addEventListener('beforeprint',beforePrint);
window.addEventListener('afterprint',afterPrint);
/* Safari 老版本无 beforeprint */
if(window.matchMedia){{
  var mq=window.matchMedia('print');
  if(mq&&mq.addEventListener)mq.addEventListener('change',function(m){{m.matches?beforePrint():afterPrint()}});
}}
ensureToolbar();
}})();"""
