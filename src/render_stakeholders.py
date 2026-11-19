"""利益相关者地图渲染器。

和现状流程图同一个套路：一份角色/象限/证据定义，输出三种格式，避免图和文字各说一套。
  1. docs/图/利益相关者地图.svg —— 浏览器直接看，可打印
  2. docs/图/利益相关者地图.png —— 贴汇报材料
  3. docs/图/利益相关者地图.mmd —— GitHub 直接渲染

用法：
    python src/render_stakeholders.py            # 生成到 docs/图/
    python src/render_stakeholders.py --check    # 只校验

校验项（和 build_ledger.py 一个思路：交叉对账，不靠眼睛）：
  1. 角色编号唯一，决策链与象限覆盖同一批人；
  2. 每个角色引用的证据编号必须在 docs/调研记录.json 里真实存在；
  3. 调研记录里的六位访谈对象，必须全部出现在地图上（漏一个就报错）；
  4. 象限与坐标一致（x>0.5 为高决定权，y>0.5 为高受影响）。

坐标说明：
  x = 对项目的决定权（0 低 → 1 高）
  y = 受交接现场影响的程度（0 低 → 1 高）
  veto_style="formal"   实线：章程里写明的否决点
  veto_style="informal" 虚线：没有正式否决权，但绕开即失败（用脚投票）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import envelope
import settings

# 输出目录/文件名/字体/项目身份都来自 config.yaml。这里只保留版面几何，
# 因为宽高与间距和绘制算法绑在一起，拆到配置里反而容易改崩。
EVID_RE = re.compile(r"^INT-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Role:
    rid: str
    name: str
    title: str
    x: float                     # 决定权
    y: float                     # 受影响程度
    can_decide: str              # 能定什么
    can_veto: str                # 能否决什么
    evidence: tuple[str, ...]    # 证据编号（必须在调研记录.json 中存在）
    veto_style: str = "formal"   # formal | informal
    short: str = ""              # 气泡内的角色简称（≤5 字，放不下就溢出圆外）


# 决策链：从发起走到对外报数，六段
CHAIN = [
    ("①", "发起", "顾明岚", "定范围与试点"),
    ("②", "出资", "顾明岚", "改善预算内列支"),
    ("③", "设计", "秦芮 + 唐映秋", "数据可读·流程可走"),
    ("④", "审批", "傅景", "红线与留存"),
    ("⑤", "使用", "林亦舟 + 唐映秋", "现场用不用"),
    ("⑥", "对外", "叶书", "报数口径"),
]

ROLES = [
    Role("R1", "顾明岚", "业务负责人", 0.80, 0.80,
         "立不立项、试点范围、投入口径",
         "可以一句话叫停（发起人）",
         ("INT-01-05", "INT-01-06"), short="业务负责人"),
    Role("R2", "傅景", "审核与质量负责人", 0.78, 0.62,
         "红线、留存期限、抽什么",
         "审批不过即不启动（正式否决）",
         ("INT-05-02", "INT-05-04", "INT-05-06"), short="审核质量"),
    Role("R3", "叶书", "客户协同代表", 0.62, 0.72,
         "对外怎么表述成果、怎么算数",
         "对外口径不点头就不能出数（正式否决）",
         ("INT-06-02", "INT-06-04", "INT-06-06"), short="客户协同"),
    Role("R4", "秦芮", "数字化工程师", 0.60, 0.20,
         "读写什么数据、字段口径",
         "没有可读接口就做不成（正式否决）",
         ("INT-04-03", "INT-04-06"), short="数字化"),
    Role("R5", "唐映秋", "项目流程协调员", 0.34, 0.72,
         "台账口径、责任转移点",
         "不接手台账就等于不接责（软否决）",
         ("INT-03-02", "INT-03-03", "INT-03-06"),
         veto_style="informal", short="流程协调"),
    Role("R6", "林亦舟", "赛事现场协调员", 0.20, 0.90,
         "现场怎么用、用不用",
         "绕开即失败，技术再好也没用（用脚投票）",
         ("INT-02-02", "INT-02-05", "INT-02-06"),
         veto_style="informal", short="现场协调"),
]

QUADRANTS = {
    (True, True): ("重点管理", "高决定权 · 高受影响", "#EEF3E8", "既要他们的判断，也要他们的时间"),
    (False, True): ("保持知情", "低决定权 · 高受影响", "#F7F4E8", "决定不了，但会被改变工作方式"),
    (True, False): ("保持满意", "高决定权 · 低受影响", "#F3F6F8", "平时不关心，卡住就动不了"),
    (False, False): ("观察", "低决定权 · 低受影响", "#F7F7F2", "本阶段不投入沟通成本"),
}

# 版面
W, H = 1240, 972
CHAIN_Y, CHAIN_H = 112, 78
BOX_W, BOX_GAP = 158, 26
QX, QY, QS = 80, 236, 640
PLOT_PAD = 34
R = 36


def quadrant_of(r: Role) -> tuple[bool, bool]:
    """角色落在哪个象限。

    入参：r 为角色定义（x=决定权、y=受影响程度，均为 0–1）。
    出参：(是否高决定权, 是否高受影响)，以 0.5 为界。
    失败：不抛异常；坐标越界由 validate() 拦截。
    """
    return (r.x > 0.5, r.y > 0.5)


def bubble_xy(r: Role) -> tuple[float, float]:
    """角色气泡的中心坐标。

    入参：r 为角色定义。
    出参：(x, y) 像素坐标。y 轴取反——屏幕坐标向下增长，
         而"受影响程度高"要画在上面，否则象限会上下颠倒。
    失败：不抛异常。
    """
    inner = QS - PLOT_PAD * 2
    return QX + PLOT_PAD + r.x * inner, QY + QS - PLOT_PAD - r.y * inner


def validate() -> list[str]:
    """校验地图定义与证据台账是否对得上。

    入参：无（定义是本模块的 ROLES/CHAIN/QUADRANTS，台账路径来自配置）。
    出参：问题列表；空列表表示通过。
    失败：不抛异常。台账文件缺失也记成一条问题而不是抛 FileNotFoundError，
         因为"台账没放对位置"和"地图引错了证据"对使用者是同一件事：得去查。
    """
    errs: list[str] = []
    ids = [r.rid for r in ROLES]
    if len(ids) != len(set(ids)):
        errs.append("角色编号重复")
    names = [r.name for r in ROLES]
    if len(names) != len(set(names)):
        errs.append("角色姓名重复")

    ledger = settings.path("ledger")
    if not ledger.exists():
        errs.append(f"找不到证据台账：{ledger}")
        return errs
    data = json.loads(ledger.read_text(encoding="utf-8"))
    known = {i["id"] for iv in data["interviews"] for i in iv["items"]}
    who = {iv["who"] for iv in data["interviews"]}

    for r in ROLES:
        if not (0 <= r.x <= 1 and 0 <= r.y <= 1):
            errs.append(f"{r.name} 坐标越界：({r.x}, {r.y})")
        if r.veto_style not in {"formal", "informal"}:
            errs.append(f"{r.name} 否决类型不合法：{r.veto_style}")
        # 每个角色必须挂证据：地图上"谁能否决"是要拿去做决策依据的，
        # 没有证据支撑的角色只是个印象，不该和"有访谈记录"的人画在一起。
        if not r.evidence:
            errs.append(f"{r.name} 没有引用任何证据")
        # 证据编号必须真的存在于台账：写错一位（INT-05-03 写成 INT-05-30）
        # 在图上完全看不出来，但读者按编号去查会查不到——这是最典型的
        # "看起来有依据、实际无依据"，所以这里逐条比对，不做模糊匹配。
        for e in r.evidence:
            if not EVID_RE.match(e):
                errs.append(f"{r.name} 的证据编号格式不对：{e}")
            elif e not in known:
                errs.append(f"{r.name} 引用了台账里不存在的证据：{e}")

    # 双向对账：访谈过的人必须全上图（漏一个就少一个决策环节），
    # 图上也不能出现没访谈过的人（那等于凭空捏造一个角色）。
    missing = who - set(names)
    if missing:
        errs.append("访谈对象没上地图：" + "、".join(sorted(missing)))
    extra = set(names) - who
    if extra:
        errs.append("地图上有、台账里没有的人：" + "、".join(sorted(extra)))

    chain_names = " ".join(c[2] for c in CHAIN)
    for n in names:
        if n not in chain_names:
            errs.append(f"{n} 没有出现在决策链上")

    # 气泡不能挤在一起：叠在一起时 SVG 不会报错，只会互相压字，
    # 而这张图的核心信息就是"谁离右上角更近"，压住了就读不出来。
    pts = [(r.name, *bubble_xy(r)) for r in ROLES]
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            n1, x1, y1 = pts[i]
            n2, x2, y2 = pts[j]
            if ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 < R * 2 + 8:
                errs.append(f"气泡重叠：{n1} / {n2}")
    return errs


def esc(s: str) -> str:
    """转义 XML 特殊字符。

    入参：s 为原始文本。
    出参：可放进 SVG 文本节点的字符串。
    失败：不抛异常。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def header_text() -> tuple[str, str]:
    """图头两行文字：主标题与副标题。

    入参：无（案例编号、阶段、标题来自 config.yaml 的 project 段）。
    出参：(主标题, 副标题)。SVG 与 PNG 共用，保证两种格式的图头一字不差。
    失败：project 段缺字段时抛 settings.ConfigError("CONFIG_INVALID")，
         不拼出"案例 None"这种半成品标题。
    """
    title = f"利益相关者地图 · {settings.require('project.title')}"
    sub = (f"案例 {settings.require('project.case_id')} ｜ "
           f"阶段 {settings.require('project.stage')} ｜ "
           f"上图＝决策链，下图＝决定权 × 受影响程度 ｜ 每个角色的证据编号可在 "
           f"{settings.path('ledger').name} 中逐条核对")
    return title, sub


# ---------------------------------------------------------------- SVG

def to_svg() -> str:
    """把定义渲染成 SVG 文本。

    入参：无。
    出参：完整 SVG 文档字符串（不含尾随换行）。
    失败：不抛异常；调用前应先跑 validate()——几何与连线假定定义自洽。

    返回字符串而非直接写文件，是为了让 harness 能把"当前定义产出的结果"
    与磁盘上的文件逐字比对，判断图是不是上一版的残留。
    """
    title, sub = header_text()
    o = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'font-family="Microsoft YaHei, PingFang SC, sans-serif">',
        f'<rect width="{W}" height="{H}" fill="#FFFDF5"/>',
        '<defs><marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" '
        'fill="#5C7A5C"/></marker></defs>',
        f'<text x="60" y="52" font-size="23" font-weight="700" fill="#29443B">'
        f'{esc(title)}</text>',
        f'<text x="60" y="80" font-size="13" fill="#5C7A5C">'
        f'{esc(sub)}</text>',
    ]

    # 决策链
    for i, (no, step, who, what) in enumerate(CHAIN):
        x = 60 + i * (BOX_W + BOX_GAP)
        o.append(f'<rect x="{x}" y="{CHAIN_Y}" width="{BOX_W}" height="{CHAIN_H}" rx="8" '
                 f'fill="#F7F9F3" stroke="#7A9474" stroke-width="1.3"/>')
        o.append(f'<text x="{x+12}" y="{CHAIN_Y+20}" font-size="11.5" fill="#7A9474">'
                 f'{esc(no + " " + step)}</text>')
        o.append(f'<text x="{x+12}" y="{CHAIN_Y+42}" font-size="15" font-weight="600" '
                 f'fill="#2D654F">{esc(who)}</text>')
        o.append(f'<text x="{x+12}" y="{CHAIN_Y+62}" font-size="10.5" fill="#29443B">'
                 f'{esc(what)}</text>')
        if i < len(CHAIN) - 1:
            ax = x + BOX_W + 4
            o.append(f'<line x1="{ax}" y1="{CHAIN_Y+CHAIN_H/2}" x2="{ax+BOX_GAP-8}" '
                     f'y2="{CHAIN_Y+CHAIN_H/2}" stroke="#5C7A5C" stroke-width="1.6" '
                     f'marker-end="url(#ar)"/>')

    # 象限底
    half = QS / 2
    for (hi_x, hi_y), (qname, sub, fill, note) in QUADRANTS.items():
        qx = QX if not hi_x else QX + half
        qy = QY if hi_y else QY + half
        o.append(f'<rect x="{qx}" y="{qy}" width="{half}" height="{half}" fill="{fill}" '
                 f'stroke="#DCE3D4"/>')
        lx = qx + 14 if not hi_x else qx + half - 14
        anchor = "start" if not hi_x else "end"
        o.append(f'<text x="{lx}" y="{qy+24}" text-anchor="{anchor}" font-size="13" '
                 f'font-weight="600" fill="#7A9474">{esc(qname)}</text>')
        o.append(f'<text x="{lx}" y="{qy+41}" text-anchor="{anchor}" font-size="10" '
                 f'fill="#8FA089">{esc(sub)}</text>')


    # 轴
    o.append(f'<line x1="{QX}" y1="{QY+QS/2}" x2="{QX+QS}" y2="{QY+QS/2}" stroke="#B9C4AE" '
             f'stroke-width="1.2" stroke-dasharray="5 4"/>')
    o.append(f'<line x1="{QX+QS/2}" y1="{QY}" x2="{QX+QS/2}" y2="{QY+QS}" stroke="#B9C4AE" '
             f'stroke-width="1.2" stroke-dasharray="5 4"/>')
    o.append(f'<text x="{QX+QS/2}" y="{QY+QS+22}" text-anchor="middle" font-size="12" '
             f'fill="#7A9474">决定权（能不能定 / 能不能拒） →</text>')
    o.append(f'<text x="{QX-14}" y="{QY+QS/2}" text-anchor="middle" font-size="12" fill="#7A9474" '
             f'transform="rotate(-90 {QX-14} {QY+QS/2})">受影响程度（交接丢了，谁最疼） →</text>')

    # 气泡
    for r in ROLES:
        bx, by = bubble_xy(r)
        dash = ' stroke-dasharray="7 5"' if r.veto_style == "informal" else ""
        o.append(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="{R}" fill="#FFFFFF" stroke="#5C7A5C" '
                 f'stroke-width="1.8"{dash}/>')
        o.append(f'<text x="{bx:.1f}" y="{by-4:.1f}" text-anchor="middle" font-size="14" '
                 f'font-weight="600" fill="#29443B">{esc(r.name)}</text>')
        o.append(f'<text x="{bx:.1f}" y="{by+14:.1f}" text-anchor="middle" font-size="9" '
                 f'fill="#5C7A5C">{esc(r.short or r.title)}</text>')

    # 右栏
    px = QX + QS + 40
    pw = W - px - 60
    o.append(f'<text x="{px}" y="{QY+6}" font-size="14" font-weight="600" fill="#2D654F">'
             f'谁能定什么，能否决什么</text>')
    y = QY + 34
    for r in ROLES:
        qname = QUADRANTS[quadrant_of(r)][0]
        o.append(f'<text x="{px}" y="{y}" font-size="12.5" font-weight="600" fill="#29443B">'
                 f'{esc(r.name)} · {esc(r.title)}　<tspan font-size="10" fill="#7A9474">'
                 f'[{esc(qname)}]</tspan></text>')
        o.append(f'<text x="{px+10}" y="{y+17}" font-size="10.5" fill="#29443B">'
                 f'能定：{esc(r.can_decide)}</text>')
        style = "否决" if r.veto_style == "formal" else "软否决"
        o.append(f'<text x="{px+10}" y="{y+33}" font-size="10.5" fill="#A6553A">'
                 f'{style}：{esc(r.can_veto)}</text>')
        o.append(f'<text x="{px+10}" y="{y+49}" font-size="9.5" fill="#7C8FA0">'
                 f'证据：{esc(" / ".join(r.evidence))}</text>')
        y += 74
        if r is not ROLES[-1]:
            o.append(f'<line x1="{px}" y1="{y-12}" x2="{px+pw}" y2="{y-12}" stroke="#E6EBDF"/>')

    # 图例
    o.append(f'<text x="60" y="{H-40}" font-size="11" fill="#8FA089">'
             f'四象限：重点管理＝既要他们的判断也要他们的时间　保持满意＝平时不关心、卡住就动不了　'
             f'保持知情＝左右不了决定但会被改变工作方式　观察＝本阶段不投入沟通成本</text>')
    o.append(f'<text x="60" y="{H-18}" font-size="11.5" fill="#5C7A5C">'
             f'实线气泡＝章程写明的否决点　虚线气泡＝无正式否决权但绕开即失败（用脚投票）　'
             f'位置依据见 项目章程与利益相关者地图 §4；部门头衔不作为坐标依据</text>')
    o.append("</svg>")
    return "\n".join(o)


# ---------------------------------------------------------------- Mermaid

def to_mmd() -> str:
    """把定义渲染成 Mermaid 文本。

    入参：无。
    出参：flowchart 源码，结尾带一个换行；GitHub 可直接渲染。
    失败：不抛异常；调用前应跑 validate()。
    """
    o = ["flowchart TD",
         '  subgraph chain["决策链：从发起到对外报数"]',
         "    direction LR"]
    for i, (no, step, who, what) in enumerate(CHAIN):
        o.append(f'    c{i}["{no} {step}<br/><b>{who}</b><br/>{what}"]')
    for i in range(len(CHAIN) - 1):
        o.append(f"    c{i} --> c{i+1}")
    o.append("  end")

    for key, (qname, sub, _fill, note) in QUADRANTS.items():
        o.append(f'  subgraph {"q" + ("H" if key[0] else "L") + ("H" if key[1] else "L")}'
                 f'["{qname}｜{sub}"]')
        for r in [x for x in ROLES if quadrant_of(x) == key]:
            mark = "" if r.veto_style == "formal" else "<br/><i>软否决（用脚投票）</i>"
            o.append(f'    {r.rid}(["{r.name}<br/><small>{r.title}</small>'
                     f'<br/>能定：{r.can_decide}{mark}"])')
        o.append("  end")
    o.append("  classDef formal fill:#FFFFFF,stroke:#5C7A5C,stroke-width:2px;")
    o.append("  classDef informal fill:#FFF6EC,stroke:#A6553A,stroke-width:2px,stroke-dasharray:6 4;")
    formal = [r.rid for r in ROLES if r.veto_style == "formal"]
    informal = [r.rid for r in ROLES if r.veto_style == "informal"]
    if formal:
        o.append("  class " + ",".join(formal) + " formal;")
    if informal:
        o.append("  class " + ",".join(informal) + " informal;")
    return "\n".join(o) + "\n"


# ---------------------------------------------------------------- PNG

def to_png(path: Path) -> None:
    """把定义渲染成 PNG 文件。

    入参：path 为输出路径（目录须已存在）。
    出参：无，副作用是写文件。
    失败：没装 Pillow 抛 ModuleNotFoundError；config.yaml 的字体候选全不存在
         抛 settings.ConfigError("FONT_MISSING")；写入错误原样抛出。
         都不吞——PNG 是要贴进汇报材料的最终产物，静默失败等于交旧图。
    """
    from PIL import Image, ImageDraw, ImageFont

    # 字体路径来自配置：Windows 是 msyh，Linux CI 是 Noto，写死任一个
    # 都会在另一台机器上把中文画成方块，而所有自动校验都发现不了。
    font_path = str(settings.font_path())

    def F(size: int, bold: bool = False):
        """按字号取字体对象。

        入参：size 像素字号；bold 预留参数（当前字体文件已含粗体，暂不使用）。
        出参：PIL 字体对象。
        失败：字体文件在取用后被删除时抛 OSError。
        """
        return ImageFont.truetype(font_path, size)

    f_h1, f_sub = F(23), F(13)
    f_step, f_who, f_what = F(11), F(15), F(11)
    f_quad, f_quad_sub, f_quad_note = F(13), F(10), F(10)
    f_axis = F(12)
    f_name, f_title_s = F(14), F(9)
    f_panel_h, f_line, f_ev = F(14), F(10), F(9)

    img = Image.new("RGB", (W, H), "#FFFDF5")
    d = ImageDraw.Draw(img)

    title, sub = header_text()
    d.text((60, 34), title, font=f_h1, fill="#29443B")
    d.text((60, 70), sub, font=f_sub, fill="#5C7A5C")

    # 决策链
    for i, (no, step, who, what) in enumerate(CHAIN):
        x = 60 + i * (BOX_W + BOX_GAP)
        d.rounded_rectangle([x, CHAIN_Y, x + BOX_W, CHAIN_Y + CHAIN_H], radius=8,
                            fill="#F7F9F3", outline="#7A9474", width=2)
        d.text((x + 12, CHAIN_Y + 12), f"{no} {step}", font=f_step, fill="#7A9474")
        d.text((x + 12, CHAIN_Y + 30), who, font=f_who, fill="#2D654F")
        d.text((x + 12, CHAIN_Y + 56), what, font=f_what, fill="#29443B")
        if i < len(CHAIN) - 1:
            ax = x + BOX_W + 5
            ay = CHAIN_Y + CHAIN_H // 2
            d.line([(ax, ay), (ax + BOX_GAP - 9, ay)], fill="#5C7A5C", width=2)
            d.polygon([(ax + BOX_GAP - 3, ay), (ax + BOX_GAP - 11, ay - 4),
                       (ax + BOX_GAP - 11, ay + 4)], fill="#5C7A5C")

    # 象限
    half = QS // 2
    for (hi_x, hi_y), (qname, sub, fill, note) in QUADRANTS.items():
        qx = QX if not hi_x else QX + half
        qy = QY if hi_y else QY + half
        d.rectangle([qx, qy, qx + half, qy + half], fill=fill, outline="#DCE3D4")
        anchor = "la" if not hi_x else "ra"
        lx = qx + 14 if not hi_x else qx + half - 14
        d.text((lx, qy + 12), qname, font=f_quad, fill="#7A9474", anchor=anchor)
        d.text((lx, qy + 32), sub, font=f_quad_sub, fill="#8FA089", anchor=anchor)


    d.line([(QX, QY + QS // 2), (QX + QS, QY + QS // 2)], fill="#B9C4AE", width=1)
    d.line([(QX + QS // 2, QY), (QX + QS // 2, QY + QS)], fill="#B9C4AE", width=1)
    d.text((QX + QS // 2, QY + QS + 14), "决定权（能不能定 / 能不能拒）→",
           font=f_axis, fill="#7A9474", anchor="ma")
    ylab = Image.new("RGBA", (420, 24), (0, 0, 0, 0))
    ImageDraw.Draw(ylab).text((0, 0), "受影响程度（交接丢了，谁最疼）→", font=f_axis, fill="#7A9474")
    ylab = ylab.rotate(90, expand=True)
    img.paste(ylab, (QX - 42, QY + QS // 2 - 210), ylab)

    # 气泡
    import math
    for r in ROLES:
        bx, by = bubble_xy(r)
        d.ellipse([bx - R, by - R, bx + R, by + R], fill="#FFFFFF",
                  outline=None if r.veto_style == "informal" else "#5C7A5C",
                  width=2)
        if r.veto_style == "informal":
            color = "#A6553A"
            for k in range(0, 720, 16):
                a0, a1 = math.radians(k / 2), math.radians((k + 9) / 2)
                d.line([(bx + (R-1) * math.cos(a0), by + (R-1) * math.sin(a0)),
                        (bx + (R-1) * math.cos(a1), by + (R-1) * math.sin(a1))], fill=color, width=2)
        d.text((bx, by - 4), r.name, font=f_name, fill="#29443B", anchor="mm")
        d.text((bx, by + 14), r.short or r.title, font=f_title_s, fill="#5C7A5C", anchor="mm")

    # 右栏
    px = QX + QS + 40
    pw = W - px - 60
    d.text((px, QY - 8), "谁能定什么，能否决什么", font=f_panel_h, fill="#2D654F")
    y = QY + 30
    for idx, r in enumerate(ROLES):
        qname = QUADRANTS[quadrant_of(r)][0]
        f_name_line = F(12)
        head = f"{r.name} · {r.title}"
        d.text((px, y), head, font=f_name_line, fill="#29443B")
        d.text((px + f_name_line.getlength(head) + 10, y + 3), f"[{qname}]",
               font=f_ev, fill="#7A9474")
        d.text((px + 10, y + 17), f"能定：{r.can_decide}", font=f_line, fill="#29443B")
        style = "否决" if r.veto_style == "formal" else "软否决"
        d.text((px + 10, y + 33), f"{style}：{r.can_veto}", font=f_line, fill="#A6553A")
        d.text((px + 10, y + 49), "证据：" + " / ".join(r.evidence), font=f_ev, fill="#7C8FA0")
        y += 74
        if idx < len(ROLES) - 1:
            d.line([(px, y - 12), (px + pw, y - 12)], fill="#E6EBDF")

    d.text((60, H - 48),
           "四象限：重点管理＝既要他们的判断也要他们的时间　保持满意＝平时不关心、卡住就动不了　"
           "保持知情＝左右不了决定但会被改变工作方式　观察＝本阶段不投入沟通成本",
           font=f_quad_note, fill="#8FA089")
    d.text((60, H - 26),
           "实线气泡＝章程写明的否决点　虚线气泡＝无正式否决权但绕开即失败（用脚投票）　"
           "位置依据见 项目章程与利益相关者地图 §4；部门头衔不作为坐标依据",
           font=f_sub, fill="#5C7A5C")
    img.save(path)


def render(check_only: bool = False) -> dict[str, Any]:
    """校验定义并按需生成三种格式，返回统一结构。

    入参：check_only=True 时只校验、不写文件。
    出参：{"ok","data","error"}。成功时 data 含角色/决策链/象限数与文件路径；
         定义不合法时 ok=False、code=VALIDATION_FAILED，data 里带 errors。
    失败：Pillow 缺失报 DEPENDENCY_MISSING、字体缺失报 FONT_MISSING，
         均转成失败响应，不让 traceback 冒到使用者面前。
    """
    errs = validate()
    if errs:
        # 不在这里 print：--json 模式下多余输出会污染 stdout，错误明细放 data.errors
        return envelope.fail("VALIDATION_FAILED", f"{len(errs)} 处定义问题",
                             data={"errors": errs})

    base, svg_p, png_p, mmd_p = settings.figure("stakeholders")
    formal = sum(1 for r in ROLES if r.veto_style == "formal")
    data: dict[str, Any] = {
        "figure": base,
        "roles": len(ROLES),
        "formal_veto": formal,
        "informal_veto": len(ROLES) - formal,
        "chain": len(CHAIN),
        "quadrants": len(QUADRANTS),
        "files": [],
        "check_only": check_only,
    }
    if check_only:
        return envelope.ok(data)

    svg_p.parent.mkdir(parents=True, exist_ok=True)
    try:
        svg_p.write_text(to_svg(), encoding="utf-8")
        mmd_p.write_text(to_mmd(), encoding="utf-8")
        to_png(png_p)
    except ModuleNotFoundError as exc:
        return envelope.fail(
            "DEPENDENCY_MISSING",
            f"生成 PNG 需要 Pillow：pip install Pillow（原始错误：{exc}）",
            data=data,
        )

    data["files"] = [str(svg_p), str(png_p), str(mmd_p)]
    return envelope.ok(data)


def main(argv: list[str]) -> int:
    """命令行入口。

    入参：argv 为去掉程序名后的参数。
    出参：进程退出码（0 成功 / 1 定义不合格 / 2 环境问题）。
    失败：配置与依赖问题不抛异常，转成统一结构输出。
    """
    ap = argparse.ArgumentParser(description="渲染利益相关者地图")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    ap.add_argument("--json", action="store_true", help="输出统一结构 JSON")
    args = ap.parse_args(argv)

    try:
        resp = render(check_only=args.check)
    except settings.ConfigError as exc:
        resp = envelope.from_exception(exc)

    if resp["ok"]:
        d = resp["data"]
        lines = [
            f"校验通过：{d['roles']} 个角色（正式否决 {d['formal_veto']} / "
            f"软否决 {d['informal_veto']}）、{d['chain']} 段决策链、"
            f"{d['quadrants']} 个象限；证据编号全部命中台账"
        ]
        if d["check_only"]:
            lines.append("（单点自检：只管本图定义，交付判定请跑 python src/harness.py）")
        else:
            lines.append(f"已生成：{d['files'][0]} / .png / .mmd")
        text = "\n".join(lines)
    elif resp["error"]["code"] == "VALIDATION_FAILED":
        errs = (resp.get("data") or {}).get("errors", [])
        text = "\n".join([f"校验未通过：{resp['error']['msg']}"]
                         + [f"  · {e}" for e in errs])
    else:
        text = resp["error"]["msg"]

    envelope.emit(resp, as_json=args.json, text=text)
    return envelope.exit_code(resp)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
