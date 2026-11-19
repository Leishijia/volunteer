"""生成评审看板：阶段 1 交付物的可浏览版本（MVP）。

为什么要一个站点
----------------
`docs/` 里现在是 7 份 Markdown + 1 份 CSV + 6 个生成物。评审者要回答"值不值得
立项"时，得在四个文件之间来回跳，还要自己在 CSV 里按断点筛。真正的问题不是
内容不够，是**结论和证据之间隔了太多次翻页**。

这个生成器把三件事合成一页：结论（要不要做）、断点（哪里坏）、台账（凭什么
这么说）。台账里的每一条都能展开看到原话，每个断点都能一键筛出关联证据——
评审可以顺着"结论 → 断点 → 证据"往下走，而不是信一句"都查过了"。

设计约束（都是刻意的）
----------------------
1. **纯静态、零外部请求**：不引 CDN、不引在线字体、不引统计脚本。离线打开
   一样能看，部署到内网也不会因为拿不到外网资源而白屏。
2. **首屏由服务端渲染**：台账 36 行直接写进 HTML，禁用 JS 也不影响阅读；
   JS 只负责筛选与展开这类增强，不给"白屏等脚本"留机会。
3. **输出必须确定**：不写生成时间、不写随机 id。harness 靠逐字节比对判断
   "站点是不是当前数据的产物"，任何非确定性内容都会让那条检查变成永远红灯。
4. **文案与数据分离**：断点、证据、样本量全部来自 `调研记录.json`；页面上
   的判断性文字（结论、边界、口径）写在本文件顶部常量里——它们是设计的一
   部分，抽进 YAML 只会让改一句话要跨两个文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import envelope
import settings

# ---------------------------------------------------------------- 页面文案
# 判断性的句子放在这里，与数据分开。数据改了下文自动跟着变，文案要人来定。

VERDICT = "问题不在「排班排不出来」，在交接那一刻没有唯一口径，也没有留下谁确认过的证据。"

VERDICT_POINTS = [
    ("值得立项", "但要把它定义成「交接时刻的口径统一与留证」，不是「做个排班工具」。"
                 "只做签到，重复核对会从三处变四处。"),
    ("范围划死", "只读核对 + 人工交接。不碰派班算法，不碰对外验收口径的最终决定权。"),
    ("试点收窄", "1 场赛事、2 个站点（一主一对照）、1 个交接窗口。样本不够就先别谈收益。"),
]

# 触发事件：四步之后现场已经没法自证。全部来自访谈与现场分析，不是演绎。
TIMELINE = [
    ("07:20", "第 1→2 班交接", "2 人迟到、1 人临时替换未落单据。"),
    ("07:35", "三处对不上", "3 个岗位与排班表 v3 不一致；现场、赛务、台账各持一版。"),
    ("08:15", "往返核对", "现场协调员在两个站点间跑了 40 分钟，靠群里问人。"),
    ("赛后 2 天", "回填台账", "靠照片与记忆补录，本应 48 小时内完成。"),
]

# 证据等级。判定标准写死在页面上，是为了让"这个数字能信到什么程度"可讨论，
# 而不是让读者自己去猜"估计"是多准。
EVIDENCE_LEVELS = [
    ("L0", "听说", "转述、推测、没有具体时间地点。不进结论。"),
    ("L1", "单人口述", "一个角色说清了一次具体事件，但没有第二个人确认。"),
    ("L2", "多人交叉", "两个以上角色独立说同一件事，口径一致。本案结论的天花板。"),
    ("L3", "原始凭证", "排班表、交接单、签到导出等原件。本案 0 份。"),
    ("L4", "系统导出", "系统侧可复算的数据。本案 0 项。"),
]

# 边界：明确不做什么，比写要做什么更能挡住范围蔓延。
NOT_DOING = [
    ("不做派班算法", "排班本身不是痛点。优化算法会让「改了版没人知道」更严重。"),
    ("不做对外验收口径的终裁", "谁对客户报数由业务负责人定，工具只负责让口径可查。"),
    ("不承诺返工人时降多少", "凭证未取得前基线只到 L2，任何百分比都是编的。"),
]

# 页面区块：(锚点, 导航名)。harness 拿这份清单检查页面结构是否完整。
SECTIONS = [
    ("verdict", "结论"),
    ("breaks", "断点"),
    ("figures", "图"),
    ("ledger", "台账"),
    ("limits", "边界"),
]


class BuildError(RuntimeError):
    """构建期错误：输入缺失、格式不对。

    带 code/msg 是为了让 `envelope.from_exception` 能直接转成统一结构，
    不必在 main 里为每种错误写一个分支。
    """

    def __init__(self, code: str, msg: str) -> None:
        """记录错误码与人读的消息。

        入参：code 取 INPUT_MISSING / INPUT_INVALID；msg 要说清改哪个文件。
        出参：无。
        失败：不抛异常（它自己就是异常类型）。
        """
        super().__init__(msg)
        self.code = code
        self.msg = msg


def esc(value: Any) -> str:
    """HTML 转义，避免中文引号和尖括号把标签撑破。

    入参：任意值，内部先转成字符串。
    出参：可直接插进 HTML 文本节点的字符串。
    失败：不抛异常；None 转成空串（页面上不显示 "None"）。
    """
    if value is None:
        return ""
    s = str(value)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def load_ledger() -> dict[str, Any]:
    """读入台账 JSON（唯一真源）。

    入参：无，路径来自 config.yaml 的 paths.ledger。
    出参：解析后的字典，含 case / breaks / interviews。
    失败：文件不存在抛 INPUT_MISSING；JSON 坏或顶层结构不对抛 INPUT_INVALID。
         这两种都不该被当成"页面渲染失败"，所以带上稳定错误码。
    """
    path = settings.path("ledger")
    if not path.exists():
        raise BuildError("INPUT_MISSING", f"找不到台账：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BuildError("INPUT_INVALID", f"台账 JSON 解析失败：{exc}") from exc
    for key in ("case", "breaks", "interviews"):
        if key not in data:
            raise BuildError("INPUT_INVALID", f"台账缺少顶层键：{key}")
    return data


def collect(data: dict[str, Any]) -> dict[str, Any]:
    """把台账压成页面要用的视图模型，并算出页面上的统计数字。

    入参：load_ledger() 的返回值。
    出参：含 case/breaks/roles/rows/stats 的字典。统计数字全部在这里算，
         模板里不出现任何写死的 36、6、11——写死就会在数据更新后悄悄过期。
    失败：字段缺失时以空值兜底（缺 quote 就少一行原话），不抛异常——
         台账的结构完整性由 harness 的 L1 负责，这里不该重复判一遍。
    """
    interviews = data["interviews"]
    rows: list[dict[str, Any]] = []
    for iv in interviews:
        for item in iv.get("items", []):
            rows.append({
                "id": item.get("id", ""),
                "who": iv.get("who", ""),
                "role": iv.get("role", ""),
                "topic": item.get("topic", ""),
                "statement": item.get("statement", ""),
                "quote": item.get("quote") or "",
                "type": item.get("type", ""),
                "breaks": list(item.get("breaks") or []),
                "cross": item.get("cross") or "",
                "conflict": item.get("conflict") or "",
                "doc": item.get("doc") or "",
            })

    # 断点 → 证据条数。用 dict 而不是在模板里 count，避免每个卡片重扫一遍。
    per_break: dict[str, int] = {}
    for r in rows:
        for b in r["breaks"]:
            per_break[b] = per_break.get(b, 0) + 1

    single = sum(1 for r in rows if r["cross"] == "单源")
    stats = {
        "records": len(rows),
        "interviews": len(interviews),
        "breaks": len(data["breaks"]),
        "crossed": len(rows) - single,
        "single": single,
        "per_break": per_break,
        "roles": [{"who": iv.get("who", ""), "role": iv.get("role", ""),
                   "focus": iv.get("focus", ""), "method": iv.get("method", ""),
                   "duration": iv.get("duration_min", 0)}
                  for iv in interviews],
    }
    return {"case": data["case"], "breaks": data["breaks"], "rows": rows, "stats": stats}


def fingerprint(payload: Any) -> str:
    """给页面数据算一个短指纹，写在页脚供人工比对。

    入参：任意可 JSON 序列化的值（通常是视图模型）。
    出参：12 位十六进制串。
    失败：不可序列化时抛 TypeError——那是代码错误，不该静默降级成空值，
         否则两个不同的数据源会算出同一个指纹，页脚就成了假证据。
    """
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def css() -> str:
    """返回整站样式。

    入参：无。
    出参：CSS 文本，直接内联进 <style>，避免多一个请求、也让页面能单文件分发。
    失败：不抛异常。
    """
    return """
:root{
  --paper:#FAF7EE; --card:#FFFFFF; --ink:#1F2B26; --ink2:#4A5A52; --ink3:#7B8A80;
  --line:#E4E7DB; --green:#2F5D4A; --green-soft:#EAF0E9; --clay:#A6553A;
  --clay-soft:#F8EEE8; --sand:#F3EFE2;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--paper); color:var(--ink);
  font-family:"Microsoft YaHei","PingFang SC","Noto Sans CJK SC","Source Han Sans SC",system-ui,sans-serif;
  font-size:15px; line-height:1.75; -webkit-font-smoothing:antialiased;
}
a{color:var(--green); text-decoration:none; border-bottom:1px solid rgba(47,93,74,.3)}
a:hover{border-bottom-color:var(--green)}
.wrap{max-width:1120px; margin:0 auto; padding:0 28px}
header.top{
  position:sticky; top:0; z-index:20; background:rgba(250,247,238,.94);
  border-bottom:1px solid var(--line); backdrop-filter:blur(6px);
}
.top-in{display:flex; align-items:center; gap:18px; height:56px}
.brand{font-weight:700; font-size:14px; letter-spacing:.02em}
.brand span{color:var(--ink3); font-weight:400; margin-left:8px}
nav{display:flex; gap:4px; margin-left:auto; flex-wrap:wrap}
nav a{
  border:0; padding:6px 10px; border-radius:6px; font-size:13.5px; color:var(--ink2);
}
nav a:hover{background:var(--green-soft); color:var(--green)}

.hero{padding:64px 0 40px}
.eyebrow{
  display:inline-block; font-size:12px; letter-spacing:.12em; color:var(--clay);
  background:var(--clay-soft); padding:4px 10px; border-radius:999px;
}
h1{font-size:34px; line-height:1.35; margin:18px 0 10px; letter-spacing:-.01em}
.org{color:var(--ink3); font-size:14px}
.verdict{
  font-size:22px; line-height:1.7; margin:28px 0 0; max-width:820px;
  padding-left:20px; border-left:3px solid var(--green);
}
.verdict b{color:var(--green); font-weight:700}

.points{display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:32px}
.point{background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px 20px}
.point h3{margin:0 0 6px; font-size:15px; color:var(--green)}
.point p{margin:0; font-size:13.5px; color:var(--ink2); line-height:1.7}

.stats{display:grid; grid-template-columns:repeat(4,1fr); gap:0; margin:36px 0 0;
       border-top:1px solid var(--line); border-bottom:1px solid var(--line)}
.stat{padding:20px 0}
.stat + .stat{border-left:1px solid var(--line); padding-left:24px}
.stat b{display:block; font-size:30px; font-weight:700; letter-spacing:-.02em; color:var(--green)}
.stat span{font-size:12.5px; color:var(--ink3)}

section{padding:56px 0; border-top:1px solid var(--line)}
h2{font-size:13px; letter-spacing:.14em; color:var(--ink3); margin:0 0 8px; font-weight:600}
h2 em{font-style:normal; color:var(--clay); margin-right:8px}
.lede{font-size:20px; margin:0 0 28px; max-width:760px; line-height:1.65}

.timeline{list-style:none; margin:0; padding:0; display:grid; gap:0}
.timeline li{display:grid; grid-template-columns:112px 1fr; gap:20px; padding:16px 0;
             border-top:1px dashed var(--line)}
.timeline li:first-child{border-top:0}
.timeline .t{font-variant-numeric:tabular-nums; font-weight:700; color:var(--clay); font-size:13.5px}
.timeline .d b{display:block; font-size:15px}
.timeline .d span{color:var(--ink2); font-size:13.5px}

.breaks{display:grid; grid-template-columns:repeat(2,1fr); gap:14px}
.break{
  background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:20px 22px; cursor:pointer; transition:border-color .15s, transform .15s;
  text-align:left; font:inherit; color:inherit;
}
.break:hover{border-color:var(--clay); transform:translateY(-1px)}
.break .bid{font-size:12px; font-weight:700; color:var(--clay); letter-spacing:.06em}
.break h3{margin:4px 0 8px; font-size:16.5px}
.break .where{font-size:12.5px; color:var(--ink3); margin:0 0 8px}
.break .effect{margin:0; font-size:13.5px; color:var(--ink2)}
.break .cnt{margin-top:12px; font-size:12px; color:var(--green); background:var(--green-soft);
            display:inline-block; padding:2px 9px; border-radius:999px}

.controls{display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-bottom:18px}
.chip{
  border:1px solid var(--line); background:var(--card); color:var(--ink2);
  padding:6px 13px; border-radius:999px; font-size:13px; cursor:pointer; font:inherit;
  font-size:13px; transition:.15s;
}
.chip:hover{border-color:var(--green); color:var(--green)}
.chip[aria-pressed="true"]{background:var(--green); border-color:var(--green); color:#fff}
input[type="search"],select{
  font:inherit; font-size:13.5px; padding:7px 12px; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--ink); outline:none;
}
input[type="search"]{flex:1; min-width:180px}
input[type="search"]:focus,select:focus{border-color:var(--green)}
.count{font-size:13px; color:var(--ink3); margin-left:auto}

.tbl-wrap{background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden}
table{width:100%; border-collapse:collapse; font-size:13.5px; table-layout:fixed}
th.c-id{width:9%} th.c-who{width:12%} th.c-stmt{width:44%}
th.c-type{width:9%} th.c-brk{width:13%} th.c-cross{width:13%}
thead th{
  text-align:left; font-size:11.5px; letter-spacing:.08em; color:var(--ink3);
  font-weight:600; padding:12px 14px; border-bottom:1px solid var(--line);
  background:var(--sand); white-space:nowrap;
}
tbody td{padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:#FCFDFA}
.id-cell{font-family:ui-monospace,Consolas,monospace; font-size:12px; color:var(--ink3); white-space:nowrap}
.stmt{color:var(--ink)}
.stmt details{margin-top:6px}
.stmt summary{
  cursor:pointer; font-size:12.5px; color:var(--green); list-style:none; display:inline-block;
}
.stmt summary::-webkit-details-marker{display:none}
.stmt summary::before{content:"＋ "; font-weight:700}
.stmt details[open] summary::before{content:"－ "}
.stmt blockquote{
  margin:8px 0 0; padding:10px 14px; background:var(--paper); border-left:2px solid var(--clay);
  color:var(--ink2); font-size:13px; border-radius:0 6px 6px 0;
}
.tag{display:inline-block; font-size:11.5px; padding:2px 8px; border-radius:5px;
     max-width:100%; overflow-wrap:anywhere}
.tag.fact{background:var(--green-soft); color:var(--green)}
.tag.judge{background:#EEF2F7; color:#3E5A78}
.tag.todo{background:var(--clay-soft); color:var(--clay)}
.tag.b{background:var(--clay-soft); color:var(--clay); font-weight:600; margin:0 4px 3px 0}
.tag.single{background:#F5F1E6; color:#8A6B2F}
.tag.crossed{background:var(--green-soft); color:var(--green)}
.who small{display:block; color:var(--ink3); font-size:11.5px}
.empty{padding:40px; text-align:center; color:var(--ink3); font-size:14px}

.figs{display:grid; gap:22px}
.fig{background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden}
.fig a{display:block; border:0; background:#FFFDF5}
.fig img{display:block; width:100%; height:auto}
.fig .cap{padding:12px 18px; font-size:13px; color:var(--ink2); border-top:1px solid var(--line);
          display:flex; gap:10px; align-items:baseline}
.fig .cap b{color:var(--ink)}

.levels{display:grid; gap:0; border-top:1px solid var(--line)}
.level{display:grid; grid-template-columns:64px 110px 1fr; gap:16px; padding:12px 0;
       border-bottom:1px solid var(--line); align-items:baseline}
.level .lv{font-family:ui-monospace,Consolas,monospace; font-weight:700; color:var(--green)}
.level .nm{font-size:13.5px; color:var(--ink)}
.level .ds{font-size:13px; color:var(--ink2)}
.level.ceiling{background:var(--green-soft); margin:0 -14px; padding:12px 14px}

.donts{display:grid; grid-template-columns:repeat(3,1fr); gap:14px; margin-top:22px}
.dont{background:var(--card); border:1px solid var(--line); border-left:3px solid var(--clay);
      border-radius:0 8px 8px 0; padding:16px 18px}
.dont b{display:block; font-size:14px; margin-bottom:4px}
.dont span{font-size:13px; color:var(--ink2)}

footer{padding:36px 0 56px; border-top:1px solid var(--line); color:var(--ink3); font-size:12.5px}
footer p{margin:0 0 8px}
code{font-family:ui-monospace,Consolas,monospace; background:var(--sand); padding:1px 6px;
     border-radius:4px; font-size:12px}

@media (max-width:900px){
  .points,.breaks,.donts{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr)}
  .stat:nth-child(3){border-left:0; padding-left:0}
  .stat:nth-child(n+3){border-top:1px solid var(--line)}
  h1{font-size:26px} .verdict{font-size:18px}
}
@media (max-width:700px){
  .wrap{padding:0 16px}
  .top-in{height:auto; padding:8px 0; flex-wrap:wrap; gap:6px}
  nav{margin-left:0; width:100%}
  nav a{padding:5px 8px; font-size:12.5px}
  .timeline li{grid-template-columns:1fr; gap:4px}
  table,thead,tbody,tr,td{display:block; width:100%}
  thead{display:none}
  tbody tr{border-bottom:1px solid var(--line); padding:12px 0}
  tbody td{border:0; padding:4px 16px}
  tbody td::before{content:attr(data-label); display:block; font-size:10.5px;
                   letter-spacing:.08em; color:var(--ink3); margin-bottom:2px}
  .count{width:100%; margin-left:0}
}
@media print{
  header.top,nav,.controls{display:none}
  body{background:#fff} section{page-break-inside:avoid}
  .break:hover{transform:none}
}
"""


def js() -> str:
    """返回页面交互脚本（纯增强，不做首屏渲染）。

    入参：无。
    出参：JS 文本。
    失败：不抛异常。脚本里所有查询都做了空值判断，某一块被删掉也不会连锁报错。
    """
    return """
(function(){
  var rows = Array.prototype.slice.call(document.querySelectorAll('#ledgerBody tr'));
  var countEl = document.getElementById('shownCount');
  var searchEl = document.getElementById('q');
  var typeEl = document.getElementById('typeFilter');
  var brkEl = document.getElementById('breakFilter');
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip[data-who]'));
  var activeWho = '';

  function apply(){
    var q = (searchEl && searchEl.value || '').trim().toLowerCase();
    var t = typeEl && typeEl.value || '';
    var b = brkEl && brkEl.value || '';
    var shown = 0;
    rows.forEach(function(tr){
      var ok = true;
      if (activeWho && tr.dataset.who !== activeWho) ok = false;
      if (ok && t && tr.dataset.type !== t) ok = false;
      if (ok && b && (tr.dataset.breaks || '').indexOf(b) === -1) ok = false;
      if (ok && q && (tr.dataset.search || '').toLowerCase().indexOf(q) === -1) ok = false;
      tr.hidden = !ok;
      if (ok) shown++;
    });
    if (countEl) countEl.textContent = '显示 ' + shown + ' / ' + rows.length + ' 条';
    if (!shown){
      var e = document.getElementById('emptyMsg');
      if (e) e.hidden = false;
    } else {
      var e2 = document.getElementById('emptyMsg');
      if (e2) e2.hidden = true;
    }
  }

  chips.forEach(function(c){
    c.addEventListener('click', function(){
      activeWho = (activeWho === c.dataset.who) ? '' : c.dataset.who;
      chips.forEach(function(x){
        x.setAttribute('aria-pressed', x.dataset.who === activeWho ? 'true' : 'false');
      });
      apply();
    });
  });

  // 断点卡片：点一下就把台账筛到该断点。评审从"哪里坏"走到"凭什么这么说"
  // 应该是零摩擦的，否则没人会真的去核对。
  Array.prototype.slice.call(document.querySelectorAll('.break')).forEach(function(card){
    card.addEventListener('click', function(){
      var bid = card.dataset.bid;
      if (brkEl) brkEl.value = bid;
      activeWho = '';
      chips.forEach(function(x){ x.setAttribute('aria-pressed', 'false'); });
      apply();
      var sec = document.getElementById('ledger');
      if (sec) sec.scrollIntoView({behavior:'smooth', block:'start'});
    });
  });

  [searchEl, typeEl, brkEl].forEach(function(el){
    if (el) el.addEventListener('input', apply);
    if (el) el.addEventListener('change', apply);
  });

  apply();
})();
"""


def render_hero(vm: dict[str, Any]) -> str:
    """渲染首屏：结论、三个判断、统计带。

    入参：collect() 的视图模型。
    出参：HTML 字符串（hero 整块）。
    失败：不抛异常；统计值为 0 时照常渲染（数字不对是数据问题，不该让页面空白）。
    """
    c, st = vm["case"], vm["stats"]
    points = "".join(
        f'<div class="point"><h3>{esc(t)}</h3><p>{esc(d)}</p></div>'
        for t, d in VERDICT_POINTS
    )
    # 结论里要强调的那半句单独加粗，靠切分而不是再写一遍全文，避免两处措辞漂移。
    head, tail = VERDICT.split("，在", 1)
    verdict = f'{esc(head)}，在<b>{esc(tail)}</b>'
    return f"""
<header class="top">
  <div class="wrap top-in">
    <div class="brand">赛事志愿者排班与交接</div>
    <nav>
      {''.join(f'<a href="#{sid}">{esc(name)}</a>' for sid, name in SECTIONS)}
    </nav>
  </div>
</header>

<main>
<div class="wrap hero" id="verdict">
  <span class="eyebrow">{esc(c.get('stage',''))} · {esc(c.get('nature',''))}</span>
  <h1>{esc(c.get('title',''))}</h1>
  <div class="org">{esc(c.get('org',''))} ｜ 案例 {esc(c.get('id',''))}</div>
  <p class="verdict">{verdict}</p>
  <div class="points">{points}</div>
  <div class="stats">
    <div class="stat"><b>{st['records']}</b><span>条调查事实</span></div>
    <div class="stat"><b>{st['interviews']}</b><span>位访谈对象 · 每位 6 条</span></div>
    <div class="stat"><b>{st['breaks']}</b><span>个流程断点</span></div>
    <div class="stat"><b>{st['crossed']}</b><span>条已交叉核对</span></div>
  </div>
</div>
"""


def render_timeline() -> str:
    """渲染触发事件时序。

    入参：无（内容来自 TIMELINE 常量）。
    出参：HTML 字符串。
    失败：不抛异常。
    """
    items = "".join(
        f'<li><div class="t">{esc(t)}</div>'
        f'<div class="d"><b>{esc(title)}</b><span>{esc(desc)}</span></div></li>'
        for t, title, desc in TIMELINE
    )
    return f"""
<section id="timeline"><div class="wrap">
  <h2><em>01</em>触发事件</h2>
  <p class="lede">2026-10-18 07:20 的一次交接，40 分钟后现场已经无法自证。</p>
  <ol class="timeline">{items}</ol>
</div></section>
"""


def render_breaks(vm: dict[str, Any]) -> str:
    """渲染六个断点卡片。

    入参：视图模型，需要 breaks 与 stats.per_break。
    出参：HTML 字符串。每张卡带 data-bid，供脚本联动台账筛选。
    失败：不抛异常；某断点没有关联证据时计数显示 0（这本身是问题，由 harness 抓）。
    """
    cards = []
    for b in vm["breaks"]:
        bid = b.get("id", "")
        n = vm["stats"]["per_break"].get(bid, 0)
        cards.append(f"""
<button class="break" data-bid="{esc(bid)}" type="button">
  <div class="bid">{esc(bid)}</div>
  <h3>{esc(b.get('name',''))}</h3>
  <p class="where">{esc(b.get('where',''))}</p>
  <p class="effect">{esc(b.get('effect',''))}</p>
  <div class="cnt">{n} 条证据 · 点击筛查</div>
</button>""")
    return f"""
<section id="breaks"><div class="wrap">
  <h2><em>02</em>六个断点</h2>
  <p class="lede">问题不在某一个环节，在六处断点串成了一条链：每一处都靠人去补，补完没有留下痕迹。</p>
  <div class="breaks">{''.join(cards)}</div>
</div></section>
"""


def render_ledger(vm: dict[str, Any]) -> str:
    """渲染台账表格（服务端直出全部行）。

    入参：视图模型，需要 rows 与 stats.roles。
    出参：HTML 字符串，含筛选控件、表头、全部数据行、空态提示。
    失败：不抛异常。行内 search 字段把编号/姓名/角色/主题/陈述/原话拼起来，
         让搜索框能一次命中任意字段，不用做多列匹配。
    """
    roles = vm["stats"]["roles"]
    chips = ''.join(
        f'<button class="chip" type="button" data-who="{esc(r["who"])}" aria-pressed="false">'
        f'{esc(r["who"])} · {esc(r["role"])}</button>'
        for r in roles
    )
    types = sorted({r["type"] for r in vm["rows"] if r["type"]})
    type_opts = ''.join(f'<option value="{esc(t)}">{esc(t)}</option>' for t in types)
    break_ids = [b.get("id", "") for b in vm["breaks"]]
    brk_opts = ''.join(f'<option value="{esc(b)}">{esc(b)}</option>' for b in break_ids)

    type_class = {"事实陈述": "fact", "判断": "judge", "待核": "todo"}
    body = []
    for r in vm["rows"]:
        tc = type_class.get(r["type"], "fact")
        tags = "".join(f'<span class="tag b">{esc(b)}</span>' for b in r["breaks"])
        cross_cls = "single" if r["cross"] == "单源" else "crossed"
        cross_txt = r["cross"] or "—"
        quote = (f'<details><summary>原话</summary><blockquote>{esc(r["quote"])}</blockquote></details>'
                 if r["quote"] else "")
        search = " ".join([r["id"], r["who"], r["role"], r["topic"],
                           r["statement"], r["quote"], r["type"], " ".join(r["breaks"])])
        body.append(f"""
<tr data-who="{esc(r['who'])}" data-type="{esc(r['type'])}"
    data-breaks="{esc(' '.join(r['breaks']))}" data-search="{esc(search)}">
  <td data-label="编号" class="id-cell">{esc(r['id'])}</td>
  <td data-label="访谈对象" class="who">{esc(r['who'])}<small>{esc(r['role'])}</small></td>
  <td data-label="主题 / 陈述" class="stmt"><b>{esc(r['topic'])}</b>
    <div>{esc(r['statement'])}</div>{quote}</td>
  <td data-label="性质"><span class="tag {tc}">{esc(r['type'])}</span></td>
  <td data-label="断点">{tags or '—'}</td>
  <td data-label="交叉核对"><span class="tag {cross_cls}">{esc(cross_txt)}</span></td>
</tr>""")

    return f"""
<section id="ledger"><div class="wrap">
  <h2><em>04</em>调研台账</h2>
  <p class="lede">{vm['stats']['records']} 条调查事实，每条都能展开看原话。标注「单源」的
  {vm['stats']['single']} 条没有第二个角色确认过，不作为结论依据。</p>
  <div class="controls">
    {chips}
    <select id="typeFilter" aria-label="按证据性质筛选">
      <option value="">全部性质</option>{type_opts}
    </select>
    <select id="breakFilter" aria-label="按断点筛选">
      <option value="">全部断点</option>{brk_opts}
    </select>
    <input id="q" type="search" placeholder="搜索编号、人名、关键词…" aria-label="搜索台账">
    <span class="count" id="shownCount">显示 {vm['stats']['records']} / {vm['stats']['records']} 条</span>
  </div>
  <div class="tbl-wrap">
    <table>
      <thead><tr>
        <th class="c-id">编号</th><th class="c-who">访谈对象</th>
        <th class="c-stmt">主题 / 陈述</th><th class="c-type">性质</th>
        <th class="c-brk">断点</th><th class="c-cross">交叉核对</th>
      </tr></thead>
      <tbody id="ledgerBody">{''.join(body)}</tbody>
    </table>
    <div class="empty" id="emptyMsg" hidden>没有匹配的记录。清空筛选条件再试。</div>
  </div>
</div></section>
"""


def render_figures() -> str:
    """渲染两张图（引用本目录 assets 下的 SVG，点击开原图）。

    入参：无，文件名由本函数固定并与 build() 的拷贝动作对应。
    出参：HTML 字符串。
    失败：不抛异常；图缺失时靠 harness 的 L6 报出来，这里不重复判断。
    """
    figs = [
        ("assets/flow.svg", "现状流程图", "五泳道 AS-IS：赛前排班 → 交接 → 异常 → 赛后回填，断点挂在具体节点上。"),
        ("assets/stakeholders.svg", "利益相关者地图", "上图＝决策链，下图＝决定权 × 受影响程度。六位访谈对象全部在图上。"),
    ]
    cards = "".join(
        f'<div class="fig"><a href="{src}" target="_blank" rel="noopener">'
        f'<img src="{src}" alt="{esc(title)}" loading="lazy"></a>'
        f'<div class="cap"><b>{esc(title)}</b><span>{esc(cap)}</span></div></div>'
        for src, title, cap in figs
    )
    return f"""
<section id="figures"><div class="wrap">
  <h2><em>03</em>两张图</h2>
  <p class="lede">图由代码生成，数据一改图就重画——不存在"文档和图各说一套"。</p>
  <div class="figs">{cards}</div>
</div></section>
"""


def render_limits(vm: dict[str, Any]) -> str:
    """渲染证据等级与项目边界。

    入参：视图模型（用于算出"已核 / 单源"的实际条数）。
    出参：HTML 字符串。
    失败：不抛异常。
    """
    st = vm["stats"]
    levels = "".join(
        f'<div class="level{" ceiling" if lv == "L2" else ""}">'
        f'<div class="lv">{esc(lv)}</div><div class="nm">{esc(nm)}</div>'
        f'<div class="ds">{esc(ds)}</div></div>'
        for lv, nm, ds in EVIDENCE_LEVELS
    )
    donts = "".join(
        f'<div class="dont"><b>{esc(t)}</b><span>{esc(d)}</span></div>' for t, d in NOT_DOING
    )
    return f"""
<section id="limits"><div class="wrap">
  <h2><em>05</em>这些结论能信到什么程度</h2>
  <p class="lede">本案最好的证据是 L2。四份原始凭证都还没拿到，所以基线只够判断
  "值不值得动"，不够当验收基线。</p>
  <div class="levels">{levels}</div>
  <p style="font-size:13.5px;color:var(--ink2);margin-top:18px">
    当前台账：{st['crossed']} 条达到 L2（多角色交叉），{st['single']} 条仍是单源，
  L3 原始凭证 0 份、L4 系统导出 0 项。任何"返工人时下降 X%"的说法在补齐凭证前都是估计。
  </p>
  <h2 style="margin-top:40px"><em>06</em>明确不做的三件事</h2>
  <div class="donts">{donts}</div>
</div></section>
"""


def render_footer(vm: dict[str, Any], fp: str, check_only: bool) -> str:
    """渲染页脚：资料性质声明、生成方式、数据指纹。

    入参：视图模型；fp 为 fingerprint() 的结果；check_only 仅用于说明来源。
    出参：HTML 字符串。
    失败：不抛异常。
    """
    c = vm["case"]
    return f"""
<footer><div class="wrap">
  <p><b>资料性质</b>：{esc(c.get('nature',''))}（{esc(c.get('id',''))}）。
  {esc(c.get('disclaimer',''))}</p>
  <p><b>数据来源</b>：<code>docs/调研记录.json</code>（唯一真源）。
  本页由 <code>python src/build_site.py</code> 生成，请勿手改；改数据后重跑即可。
  数据指纹 <code>{esc(fp)}</code>，与台账不一致说明页面过期。</p>
  <p>样本：{esc((c.get('sample') or {}).get('event',''))}　
  站点 {esc((c.get('sample') or {}).get('sites',''))} 个 ·
  每站 {esc((c.get('sample') or {}).get('posts_per_site',''))} 个岗位 ·
  {esc((c.get('sample') or {}).get('shifts',''))} 个班次 ·
  每场 {esc((c.get('sample') or {}).get('handovers_per_event',''))} 次交接</p>
</div></footer>
"""


def render_page(vm: dict[str, Any]) -> str:
    """把各区块拼成完整 HTML 文档。

    入参：collect() 的视图模型。
    出参：整页 HTML 字符串（确定性：同样的输入必然得到同样的字节）。
    失败：不抛异常；区块函数都不依赖外部状态。
    """
    fp = fingerprint(vm)
    c = vm["case"]
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(c.get('title',''))} · 阶段 1 评审看板</title>
<meta name="description" content="{esc(c.get('org',''))} {esc(c.get('title',''))}：阶段 1 业务挖掘的结论、六个断点与 {vm['stats']['records']} 条可核对证据。">
<style>{css()}</style>
</head>
<body>
{render_hero(vm)}
{render_timeline()}
{render_breaks(vm)}
{render_figures()}
{render_ledger(vm)}
{render_limits(vm)}
{render_footer(vm, fp, False)}
</main>
<script>{js()}</script>
</body>
</html>
"""


def build(check_only: bool = False) -> dict[str, Any]:
    """生成站点文件，或在 check_only 模式下只算出应有内容。

    入参：check_only=True 时不写磁盘，只返回将要产出的字节数与指纹
         （harness 用它比对"磁盘上的站点是不是当前数据的产物"）。
    出参：统一结构的 data，含 files/records/crossed/single/bytes/fingerprint。
    失败：抛 BuildError（输入问题）或 ConfigError（配置问题），由 main 转成结构。
    """
    data = load_ledger()
    vm = collect(data)
    html_text = render_page(vm)

    site_dir = settings.path("site")
    index = site_dir / "index.html"
    assets = site_dir / "assets"

    if not check_only:
        site_dir.mkdir(parents=True, exist_ok=True)
        assets.mkdir(parents=True, exist_ok=True)
        # 图用 ASCII 文件名：中文文件名在 URL 里要百分号编码，某些静态服务器
        # 处理不稳，而这个问题只在部署后才暴露，本地 file:// 打开完全正常。
        for key, target in (("flow", "flow.svg"), ("stakeholders", "stakeholders.svg")):
            _, svg_path, _, _ = settings.figure(key)
            if not svg_path.exists():
                raise BuildError(
                    "INPUT_MISSING",
                    f"找不到 {svg_path}，先跑 python src/render_"
                    f"{'flow' if key == 'flow' else 'stakeholders'}.py",
                )
            shutil.copyfile(svg_path, assets / target)
        index.write_text(html_text, encoding="utf-8")

    return {
        "files": [str(index)],
        "records": vm["stats"]["records"],
        "interviews": vm["stats"]["interviews"],
        "breaks": vm["stats"]["breaks"],
        "crossed": vm["stats"]["crossed"],
        "single": vm["stats"]["single"],
        "bytes": len(html_text.encode("utf-8")),
        "fingerprint": fingerprint(vm),
        "check_only": check_only,
    }


def main(argv: list[str]) -> int:
    """命令行入口。

    入参：argv 为去掉程序名后的参数（--check / --json）。
    出参：进程退出码（0 成功 / 1 内容问题 / 2 环境或输入问题）。
    失败：配置与输入问题不抛异常，转成统一结构输出后返回非零。
    """
    ap = argparse.ArgumentParser(description="生成阶段 1 评审看板（静态站点）")
    ap.add_argument("--check", action="store_true", help="只算不写，用于校验站点是否过期")
    ap.add_argument("--json", action="store_true", help="输出统一结构 JSON")
    args = ap.parse_args(argv)

    try:
        data = build(check_only=args.check)
        resp = envelope.ok(data)
    except (settings.ConfigError, BuildError) as exc:
        resp = envelope.from_exception(exc)

    if resp["ok"]:
        d = resp["data"]
        lines = [
            f"站点就绪：{d['records']} 条事实 / {d['interviews']} 位访谈对象 / "
            f"{d['breaks']} 个断点（交叉核对 {d['crossed']}、单源 {d['single']}），"
            f"{d['bytes'] / 1024:.1f} KB，数据指纹 {d['fingerprint']}"
        ]
        if d["check_only"]:
            lines.append("（单点自检：只算不写，交付判定请跑 python src/harness.py）")
        else:
            lines.append(f"已生成：{d['files'][0]}（图已拷到 assets/）")
        text = "\n".join(lines)
    else:
        text = f"{resp['error']['code']}：{resp['error']['msg']}"

    envelope.emit(resp, as_json=args.json, text=text)
    return envelope.exit_code(resp)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
