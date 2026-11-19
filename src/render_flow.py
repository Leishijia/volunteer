"""现状流程图（AS-IS）渲染器。

同一份节点/连线定义，输出三种格式，避免图和文字各说一套：
  1. docs/图/现状流程图.svg  —— 业务方直接用浏览器看，可打印
  2. docs/图/现状流程图.png  —— 贴到汇报材料里
  3. docs/图/现状流程图.mmd  —— GitHub / 支持 mermaid 的编辑器里直接渲染

用法：
    python src/render_flow.py            # 生成到 docs/图/
    python src/render_flow.py --check    # 只校验定义（节点 id 唯一、连线端点存在、断点编号合法）
    python src/render_flow.py --json     # 输出统一结构 {"ok","data","error"}

约定：
  - 节点标签里 `\\n` 换行，`B{n}` 表示该节点对应文档中的断点编号。
  - `system=False` 的节点画虚线：表示这个动作不产生系统记录，只在纸面 / 群里。

输出目录、文件名主干、字体都来自 config.yaml（paths.figures / figures.flow.base /
fonts.candidates），代码里不写死。版面几何留在代码里——它和绘制算法绑在一起。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import envelope
import settings

LANES = [
    ("lan0", "流程协调员（赛务）"),
    ("lan1", "志愿者（一线）"),
    ("lan2", "现场协调员（站点）"),
    ("lan3", "数字化（系统）"),
    ("lan4", "下游（审核 / 客户）"),
]

VALID_BREAKS = {"B1", "B2", "B3", "B4", "B5", "B6"}


@dataclass(frozen=True)
class Node:
    """流程图上的一个节点（泳道 + 行号定位，一行只能放一个）。"""

    nid: str
    lane: str
    row: int
    text: str
    system: bool = True          # False -> 虚线：不进系统
    breaks: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Edge:
    """一条连线。kind 决定线型与颜色：flow 实线 / record 虚线 / handover 褐色虚线。"""

    src: str
    dst: str
    kind: str = "flow"           # flow | record | handover
    note: str = ""


NODES = [
    Node("n1", "lan0", 0, "排班表 v1（Excel）"),
    Node("n2", "lan0", 1, "变更 → v2 / v3（群公告）", breaks=("B1",)),
    Node("n3", "lan0", 2, "T-1 封版 v3、分站打印", breaks=("B1",)),
    Node("n4", "lan1", 2, "到场、纸质签到"),
    Node("n5", "lan1", 3, "接班：口头领岗"),
    Node("n6", "lan1", 4, "异常：迟到 / 缺席 / 换人", system=False, breaks=("B3",)),
    Node("n7", "lan2", 3, "交接点名、翻群记录", breaks=("B2",)),
    Node("n8", "lan2", 4, "发现名单不符（3 个岗位）", breaks=("B2",)),
    Node("n9", "lan2", 5, "现场回溯、临时补位", breaks=("B3",)),
    Node("n10", "lan0", 6, "赛后台账回填（≤48h）", breaks=("B4", "B5")),
    Node("n11", "lan0", 7, "台账封版"),
    Node("n12", "lan3", 2, "扫码签到（小程序）", breaks=("B4",)),
    Node("n13", "lan3", 6, "赛后导出 CSV"),
    Node("n14", "lan4", 7, "对客户报数（口径现问）", breaks=("B6",)),
    Node("n15", "lan4", 8, "审核抽查：找不到确认人", breaks=("B2",)),
]

EDGES = [
    Edge("n1", "n2"),
    Edge("n2", "n3"),
    Edge("n3", "n4", "handover", "封版名单下发现场"),
    Edge("n4", "n5"),
    Edge("n5", "n7", "handover", "口头交接"),
    Edge("n5", "n6"),
    Edge("n6", "n8", "handover", "群内一句话，无单据"),
    Edge("n7", "n8"),
    Edge("n8", "n9"),
    Edge("n12", "n4", "record", "扫码失败退回纸质"),
    Edge("n9", "n10", "record"),
    Edge("n13", "n10", "record"),
    Edge("n10", "n11"),
    Edge("n11", "n14"),
    Edge("n14", "n15", "handover", "报数口径回溯不到确认记录"),
]

# 版面参数（竖排泳道：泳道在 x 轴，步骤在 y 轴向下）
LANE_W = 258
PAD_X = 34
PAD_Y = 92
ROW_H = 116
NODE_W = 214
NODE_H = 82
LANE_HEADER_H = 46


def lane_x(lane_id: str) -> int:
    """泳道左边缘的 x 坐标。

    入参：lane_id 为泳道编号（lan0…lan4）。
    出参：该泳道左边缘的像素 x。
    失败：编号不存在时抛 ValueError。不做兜底——画到错误位置比直接报错
         更难发现，而这张图的用途正是给人看"东西在哪一步丢了"。
    """
    return PAD_X + [l[0] for l in LANES].index(lane_id) * LANE_W


def node_xy(n: Node) -> tuple[int, int]:
    """节点的左上角像素坐标。

    入参：n 为节点定义。
    出参：(x, y)。
    失败：n.lane 非法时由 lane_x() 抛 ValueError。
    """
    x = lane_x(n.lane) + (LANE_W - NODE_W) // 2
    y = PAD_Y + LANE_HEADER_H + n.row * ROW_H
    return x, y


def validate() -> list[str]:
    """校验图定义自身是否自洽。

    入参：无（定义就是本模块的 LANES/NODES/EDGES）。
    出参：问题列表；空列表表示通过。
    失败：不抛异常，所有问题收集后一并返回——图定义是手改的，
         一次只报一条要来回跑好几遍。
    """
    errs: list[str] = []
    ids = [n.nid for n in NODES]
    if len(ids) != len(set(ids)):
        errs.append("节点 id 重复")
    lane_ids = {l[0] for l in LANES}
    for n in NODES:
        if n.lane not in lane_ids:
            errs.append(f"{n.nid} 的泳道 {n.lane} 不存在")
        # 断点编号必须落在真实存在的断点集合里：图上的红字是读者
        # 翻回文档找依据的入口，写个 B7 出来就是死链。
        for b in n.breaks:
            if b not in VALID_BREAKS:
                errs.append(f"{n.nid} 的断点编号 {b} 不合法")
    # 同泳道同行只能放一个节点：两个框叠在一起时 SVG 不会报错，
    # 只会静默变成"后画的盖住先画的"，而读者以为其中一个丢了。
    pos = {}
    for n in NODES:
        key = (n.lane, n.row)
        if key in pos:
            errs.append(f"{n.nid} 与 {pos[key]} 位置重叠")
        pos[key] = n.nid
    for e in EDGES:
        if e.src not in ids or e.dst not in ids:
            errs.append(f"连线 {e.src}->{e.dst} 端点不存在")
        if e.src == e.dst:
            errs.append(f"连线 {e.src} 自环")
    return errs


def route(s: Node, d: Node) -> tuple[list[tuple[float, float]], str]:
    """计算两个节点之间的正交走线。

    入参：s 起点节点，d 终点节点。
    出参：(折线点列表, 箭头方向)。方向取值 right/left/down，供 PNG 画箭头用。
    失败：节点坐标非法时由 node_xy() 抛 ValueError。

    走线策略：同一行就横着连，跨行就"下-横-下"，避免绕到别的节点上。
    """
    sx, sy = node_xy(s)
    dx, dy = node_xy(d)
    if s.row == d.row:
        y = sy + NODE_H / 2
        if dx > sx:
            return [(sx + NODE_W, y), (dx - 3, y)], "right"
        return [(sx, y), (dx + NODE_W + 3, y)], "left"
    x1, y1 = sx + NODE_W / 2, sy + NODE_H
    x2, y2 = dx + NODE_W / 2, dy
    ym = (y1 + y2) / 2
    return [(x1, y1), (x1, ym), (x2, ym), (x2, y2 - 3)], "down"


def node_label(n: Node) -> list[str]:
    """节点的显示文本行。

    入参：n 为节点定义（text 里用 \\n 分行）。
    出参：文本行列表；有断点时最后一行是"断点 Bx By"，渲染时单独用红色。
    失败：不抛异常。
    """
    parts = n.text.split("\n")
    if n.breaks:
        parts.append("断点 " + " ".join(n.breaks))
    return parts


# ---------------------------------------------------------------- SVG

def to_svg() -> str:
    """把定义渲染成 SVG 文本。

    入参：无。
    出参：完整 SVG 文档字符串（不含尾随换行）。
    失败：不抛异常；但调用前应先跑 validate()——几何计算假定定义自洽，
         端点不存在时会在这里抛 StopIteration。

    为什么返回字符串而不是直接写文件：harness 要拿"当前定义能产出的结果"
    与磁盘上的文件逐字比对，判断图是不是上一版的残留。写成文件就没法比。
    """
    width = PAD_X * 2 + LANE_W * len(LANES)
    rows = max(n.row for n in NODES) + 1
    height = PAD_Y + LANE_HEADER_H + rows * ROW_H + 56
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="Microsoft YaHei, PingFang SC, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="#FFFDF5"/>',
        '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#5C7A5C"/></marker></defs>',
    ]
    # 泳道底
    for i, (lid, lname) in enumerate(LANES):
        x = PAD_X + i * LANE_W
        out.append(f'<rect x="{x}" y="{PAD_Y}" width="{LANE_W}" height="{height - PAD_Y - 24}" '
                   f'fill="#F7F9F3" stroke="#DCE3D4"/>')
        out.append(f'<text x="{x + LANE_W/2}" y="{PAD_Y + 28}" text-anchor="middle" '
                   f'font-size="16" font-weight="600" fill="#2D654F">{lname}</text>')

    # 连线
    for e in EDGES:
        s = next(n for n in NODES if n.nid == e.src)
        d = next(n for n in NODES if n.nid == e.dst)
        points, _ = route(s, d)
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        dash = ' stroke-dasharray="6 5"' if e.kind != "flow" else ""
        color = "#5C7A5C" if e.kind == "flow" else ("#A6553A" if e.kind == "handover" else "#7C8FA0")
        out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6"'
                   f'{dash} marker-end="url(#ah)"/>')

    # 节点
    for n in NODES:
        x, y = node_xy(n)
        dash = ' stroke-dasharray="7 5"' if not n.system else ""
        fill = "#FFFFFF" if n.system else "#FFF6EC"
        out.append(f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="9" '
                   f'fill="{fill}" stroke="#7A9474" stroke-width="1.4"{dash}/>')
        lines = node_label(n)
        base = y + 26 if len(lines) == 2 else y + 32
        for i, ln in enumerate(lines):
            size = 13 if i == len(lines) - 1 and n.breaks else 14
            color = "#A6553A" if n.breaks and i == len(lines) - 1 else "#29443B"
            out.append(f'<text x="{x + NODE_W/2}" y="{base + i*19}" text-anchor="middle" '
                       f'font-size="{size}" fill="{color}">{escape(ln)}</text>')

    # 图例
    ly = height - 18
    out.append(f'<text x="{PAD_X}" y="{ly}" font-size="12.5" fill="#5C7A5C">'
               f'实线＝有系统记录　虚线＝不进系统（纸面 / 群）　褐色连线＝人与人之间的交接　红字＝断点编号（见 现场调研分析 §5）</text>')
    out.append("</svg>")
    return "\n".join(out)


def escape(s: str) -> str:
    """转义 XML 特殊字符。

    入参：s 为原始文本。
    出参：可安全放进 SVG <text> 的字符串。
    失败：不抛异常。只处理 & < > 三个——引号在文本节点里不构成风险，
         多余转义只会让图上的字变成 &quot;。
    """
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- Mermaid

def to_mmd() -> str:
    """把定义渲染成 Mermaid 文本。

    入参：无。
    出参：可直接被 GitHub / 编辑器渲染的 flowchart 源码，结尾带一个换行。
    失败：不抛异常；端点缺失同样会画出一条指向不存在节点的连线，
         所以调用前应跑 validate()。
    """
    out = ["flowchart TD"]
    for lid, lname in LANES:
        out.append(f'  subgraph {lid}["{lname}"]')
        out.append("    direction TB")
        for n in [x for x in NODES if x.lane == lid]:
            label = n.text.replace("\n", "<br/>")
            if n.breaks:
                label += f"<br/><i>断点 {' '.join(n.breaks)}</i>"
            shape_l = "([" if not n.system else "["
            shape_r = "])" if not n.system else "]"
            out.append(f'    {n.nid}{shape_l}"{label}"{shape_r}')
        out.append("  end")
    for e in EDGES:
        arrow = "-->" if e.kind == "flow" else "-.->"
        out.append(f"  {e.src} {arrow} {e.dst}")
    out.append("  classDef nosys fill:#FFF6EC,stroke:#A6553A,stroke-dasharray:6 4;")
    nosys = [n.nid for n in NODES if not n.system]
    if nosys:
        out.append("  class " + ",".join(nosys) + " nosys;")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- PNG

def to_png(path: Path) -> None:
    """把定义渲染成 PNG 文件。

    入参：path 为输出文件路径（目录须已存在）。
    出参：无，副作用是写文件。
    失败：没装 Pillow 抛 ModuleNotFoundError；config.yaml 里字体候选全不存在
         抛 settings.ConfigError("FONT_MISSING")；磁盘写入错误原样抛出。
         三种错误都不吞——PNG 是给人看的最终产物，静默失败等于交了一张旧图。
    """
    from PIL import Image, ImageDraw, ImageFont

    # 字体由配置决定，不写死路径：Windows 有 msyh，Linux CI 上是 Noto，
    # 写死任一个都会在另一台机器上变成方块字。
    font_path = str(settings.font_path())
    f_title = ImageFont.truetype(font_path, 17)
    f_node = ImageFont.truetype(font_path, 15)
    f_small = ImageFont.truetype(font_path, 13)
    f_legend = ImageFont.truetype(font_path, 13)

    width = PAD_X * 2 + LANE_W * len(LANES)
    rows = max(n.row for n in NODES) + 1
    height = PAD_Y + LANE_HEADER_H + rows * ROW_H + 70
    img = Image.new("RGB", (width, height), "#FFFDF5")
    d = ImageDraw.Draw(img)

    for i, (lid, lname) in enumerate(LANES):
        x = PAD_X + i * LANE_W
        d.rectangle([x, PAD_Y, x + LANE_W, height - 40], fill="#F7F9F3", outline="#DCE3D4")
        d.text((x + LANE_W / 2, PAD_Y + 22), lname, font=f_title, fill="#2D654F", anchor="mm")

    def arrow(points, head, color="#5C7A5C", dashed=False):
        """画一条折线并在末端加箭头。

        入参：points 折线点列表；head 箭头方向（down/right/left）；
              color 颜色；dashed 是否画虚线。
        出参：无，直接画在 d 上。
        失败：点列表少于两个时什么都不画（不会抛异常）。
        """
        for a, b in zip(points, points[1:]):
            if dashed:
                dx, dy = b[0] - a[0], b[1] - a[1]
                dist = max(abs(dx), abs(dy)) or 1
                for t in range(0, int(dist), 12):
                    s = t / dist
                    e = min((t + 7) / dist, 1.0)
                    d.line([(a[0] + dx * s, a[1] + dy * s), (a[0] + dx * e, a[1] + dy * e)], fill=color, width=2)
            else:
                d.line([a, b], fill=color, width=2)
        x2, y2 = points[-1]
        if head == "down":
            d.polygon([(x2, y2 + 3), (x2 - 5, y2 - 7), (x2 + 5, y2 - 7)], fill=color)
        elif head == "right":
            d.polygon([(x2 + 3, y2), (x2 - 7, y2 - 5), (x2 - 7, y2 + 5)], fill=color)
        else:
            d.polygon([(x2 - 3, y2), (x2 + 7, y2 - 5), (x2 + 7, y2 + 5)], fill=color)

    for e in EDGES:
        s = next(n for n in NODES if n.nid == e.src)
        t = next(n for n in NODES if n.nid == e.dst)
        color = "#5C7A5C" if e.kind == "flow" else ("#A6553A" if e.kind == "handover" else "#7C8FA0")
        points, head = route(s, t)
        arrow(points, head, color, dashed=(e.kind != "flow"))

    for n in NODES:
        x, y = node_xy(n)
        fill = "#FFFFFF" if n.system else "#FFF6EC"
        d.rounded_rectangle([x, y, x + NODE_W, y + NODE_H], radius=10, fill=fill, outline="#7A9474", width=2)
        if not n.system:
            for px in range(x + 6, x + NODE_W - 6, 14):
                d.line([(px, y + NODE_H - 4), (min(px + 8, x + NODE_W - 4), y + NODE_H - 4)], fill="#A6553A", width=2)
        lines = node_label(n)
        base = y + 28 if len(lines) == 2 else y + 36
        for i, ln in enumerate(lines):
            is_b = bool(n.breaks) and i == len(lines) - 1
            d.text((x + NODE_W / 2, base + i * 21), ln,
                   font=f_small if is_b else f_node,
                   fill="#A6553A" if is_b else "#29443B", anchor="mm")

    d.text((PAD_X, height - 26),
           "实线＝有系统记录　虚线＝不进系统（纸面 / 群）　褐色连线＝人与人之间的交接　红字＝断点编号（见 现场调研分析 §5）",
           font=f_legend, fill="#5C7A5C")
    img.save(path)


def render(check_only: bool = False) -> dict[str, Any]:
    """校验定义并按需生成三种格式，返回统一结构。

    入参：check_only=True 时只校验、不写文件。
    出参：{"ok","data","error"}。成功时 data 含节点/连线/泳道数与三个文件路径；
         定义不合法时 ok=False、code=VALIDATION_FAILED，data.errors 里带明细。
    失败：Pillow 缺失报 DEPENDENCY_MISSING，字体缺失报 FONT_MISSING，
         均转成失败响应而不是抛栈——调用方（harness / CI / hook）
         只认统一结构，不该去解析 traceback。
    """
    errs = validate()
    if errs:
        # 不在这里 print：--json 模式下多余输出会污染 stdout，错误明细放 data.errors
        return envelope.fail("VALIDATION_FAILED", f"{len(errs)} 处定义问题",
                             data={"errors": errs})

    base, svg_p, png_p, mmd_p = settings.figure("flow")
    data: dict[str, Any] = {
        "figure": base,
        "nodes": len(NODES),
        "edges": len(EDGES),
        "lanes": len(LANES),
        "files": [],
        "check_only": check_only,
    }
    if check_only:
        return envelope.ok(data)

    # 目录不存在就建：图是产物，产物目录属于本工具自己管理。
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
    失败：配置与依赖问题不抛异常，转成统一结构输出，保证 CI 拿到稳定退出码。
    """
    ap = argparse.ArgumentParser(description="渲染现状流程图（AS-IS）")
    ap.add_argument("--check", action="store_true", help="只校验定义，不写文件")
    ap.add_argument("--json", action="store_true", help="输出统一结构 JSON")
    args = ap.parse_args(argv)

    try:
        resp = render(check_only=args.check)
    except settings.ConfigError as exc:
        resp = envelope.from_exception(exc)

    if resp["ok"]:
        d = resp["data"]
        lines = [f"定义校验通过：{d['nodes']} 个节点、{d['edges']} 条连线、{d['lanes']} 条泳道"]
        if d["check_only"]:
            lines.append("（单点自检：只管本图定义，交付判定请跑 python src/harness.py）")
        else:
            lines.append(f"已生成：{d['files'][0]} / .png / .mmd")
        text = "\n".join(lines)
    elif resp["error"]["code"] == "VALIDATION_FAILED":
        errs = (resp.get("data") or {}).get("errors", [])
        text = "\n".join([f"定义校验未通过：{resp['error']['msg']}"]
                         + [f"  · {e}" for e in errs])
    else:
        text = resp["error"]["msg"]

    envelope.emit(resp, as_json=args.json, text=text)
    return envelope.exit_code(resp)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
