"""现状流程图（AS-IS）渲染器。

同一份节点/连线定义，输出三种格式，避免图和文字各说一套：
  1. docs/图/现状流程图.svg  —— 业务方直接用浏览器看，可打印
  2. docs/图/现状流程图.png  —— 贴到汇报材料里
  3. docs/图/现状流程图.mmd  —— GitHub / 支持 mermaid 的编辑器里直接渲染

用法：
    python src/render_flow.py            # 生成到 docs/图/
    python src/render_flow.py --check    # 只校验定义（节点 id 唯一、连线端点存在、断点编号合法）

约定：
  - 节点标签里 `\\n` 换行，`B{n}` 表示该节点对应文档中的断点编号。
  - `system=False` 的节点画虚线：表示这个动作不产生系统记录，只在纸面 / 群里。
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "图"

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
    nid: str
    lane: str
    row: int
    text: str
    system: bool = True          # False -> 虚线：不进系统
    breaks: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Edge:
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
    return PAD_X + [l[0] for l in LANES].index(lane_id) * LANE_W


def node_xy(n: Node) -> tuple[int, int]:
    x = lane_x(n.lane) + (LANE_W - NODE_W) // 2
    y = PAD_Y + LANE_HEADER_H + n.row * ROW_H
    return x, y


def validate() -> list[str]:
    errs: list[str] = []
    ids = [n.nid for n in NODES]
    if len(ids) != len(set(ids)):
        errs.append("节点 id 重复")
    lane_ids = {l[0] for l in LANES}
    for n in NODES:
        if n.lane not in lane_ids:
            errs.append(f"{n.nid} 的泳道 {n.lane} 不存在")
        for b in n.breaks:
            if b not in VALID_BREAKS:
                errs.append(f"{n.nid} 的断点编号 {b} 不合法")
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
    """正交走线。同一行就横着连，跨行就下-横-下，避免绕到别的节点上。"""
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
    parts = n.text.split("\n")
    if n.breaks:
        parts.append("断点 " + " ".join(n.breaks))
    return parts


# ---------------------------------------------------------------- SVG

def to_svg() -> str:
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
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------- Mermaid

def to_mmd() -> str:
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
    from PIL import Image, ImageDraw, ImageFont

    font_path = "C:/Windows/Fonts/msyh.ttc"
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


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="渲染现状流程图（AS-IS）")
    ap.add_argument("--check", action="store_true", help="只校验定义，不写文件")
    args = ap.parse_args(argv)

    errs = validate()
    if errs:
        for e in errs:
            print("ERROR:", e)
        return 1
    print(f"定义校验通过：{len(NODES)} 个节点、{len(EDGES)} 条连线、{len(LANES)} 条泳道")
    if args.check:
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "现状流程图.svg").write_text(to_svg(), encoding="utf-8")
    (OUT_DIR / "现状流程图.mmd").write_text(to_mmd(), encoding="utf-8")
    to_png(OUT_DIR / "现状流程图.png")
    print(f"已生成：{OUT_DIR / '现状流程图.svg'} / .png / .mmd")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
