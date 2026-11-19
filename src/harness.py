"""统一校验 harness：一条命令证明这套交付物是自洽的。

为什么要有这一步
----------------
仓库里原本有三个各自独立的 `--check`（台账、现状流程图、利益相关者地图），
但它们只管自己那一摊，管不了「跨文件」的毛病：

  - 改了 调研记录.json，忘了重新生成 调研记录.csv → 两处数字对不上；
  - 文档里引用了 INT-04-07，台账里根本没有这一条 → 结论悬空；
  - README 的交付物表指向一个已经改名的文件 → 评审点进去是 404；
  - 改了流程图定义，没重跑脚本 → 图和文字各说一套；
  - 新写了一篇文档，没进 README 索引 → 等于没交。

这几类问题单看任何一个脚本都发现不了，却偏偏最容易在交付前夜冒出来。
所以这里不加新功能，只做一件事：把「谁负责发现什么」登记成一张表，
一次跑完，输出一份可复核的报告（`docs/校验报告.md`，本地生成、不入库）。

分层
----
  L1 台账自洽      —— JSON 自身合法、编号不重复、CSV 是 JSON 的忠实投影
  L2 证据可追溯    —— 文档里每句带编号的话，都能在台账里找到出处
  L3 图与定义一致  —— 图上画的就是定义里写的，不是上一版
  L4 文档层        —— 链接能点开、交付物齐套、索引没漏、数字没过期

用法
----
    python src/harness.py                # 跑全部，并写 docs/校验报告.md
    python src/harness.py --no-write     # 只打印，不落盘
    python src/harness.py --json         # 统一结构 {"ok","data","error"}，挂 CI 用
    python src/harness.py --only L2      # 只跑某一层（可重复写多个）
    python src/harness.py --list         # 列出所有检查项及其用途

退出码（由 envelope.exit_code 统一决定）
------
    0  全部通过（warn 不影响退出码）
    1  至少一项 blocker 失败（内容问题，改内容）
    2  harness 自身出错（配置缺失、必要文件缺失、没匹配到检查项）
"""
from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import re
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import envelope
import settings

ROOT = settings.ROOT

sys.path.insert(0, str(ROOT / "src"))
import build_ledger as ledger          # noqa: E402
import render_flow as flow             # noqa: E402
import render_stakeholders as stake    # noqa: E402

EVID_RE = re.compile(r"INT-\d{2}-\d{2}")
# 只匹配"长得像断点编号"的 token，合法性交给 L2-2 判断。
# 这里故意不写成 B[1-6]：那样越界的 B7、B0 会直接匹配不上，等于放行。
BREAK_RE = re.compile(r"(?<![A-Za-z0-9])B(\d{1,2})(?![0-9])")
DOCID_RE = re.compile(r"DOC-\d{2}")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
# 行内豁免：<!-- harness:ignore --> 跳过整行；<!-- harness:ignore B7 --> 只放过指定 token。
# 有这条通道，才不会因为一次误报就把整条检查删掉——那是拿掉报警器修火警。
IGNORE_RE = re.compile(r"<!--\s*harness:ignore([^>]*)-->")


# ---------------------------------------------------------------- 路径
# 全部走函数而不是模块级常量：模块级常量会在 import 时读配置，
# 而 import 期抛异常时 main() 的 try 还没建立，使用者只能看到 traceback。


def docs_dir() -> Path:
    """文档目录（config.yaml 的 paths.docs）。

    入参：无。出参：绝对路径。失败：配置缺失抛 settings.ConfigError。
    """
    return settings.path("docs")


def fig_dir() -> Path:
    """图片目录（paths.figures）。

    入参：无。出参：绝对路径。失败：配置缺失抛 settings.ConfigError。
    """
    return settings.path("figures")


def ledger_path() -> Path:
    """台账 JSON 路径（paths.ledger）。失败：配置缺失抛 settings.ConfigError。"""
    return settings.path("ledger")


def ledger_csv_path() -> Path:
    """台账 CSV 路径（paths.ledger_csv）。失败：配置缺失抛 settings.ConfigError。"""
    return settings.path("ledger_csv")


def report_path() -> Path:
    """校验报告路径（paths.report）。失败：配置缺失抛 settings.ConfigError。"""
    return settings.path("report")


def skip_docs() -> set[str]:
    """不参与文档扫描的文件名集合（harness.skip_docs）。

    入参：无。出参：文件名集合。
    失败：类型不对抛 settings.ConfigError。
    """
    return settings.skip_docs()


# ---------------------------------------------------------------- 检查项登记


@dataclass
class Check:
    """一项检查的登记信息。

    cid 是稳定编号（报告、豁免注释、CI 报错都引用它），不要随便改。
    level 只有两种：
      blocker —— 不通过即判定不可交付（结论悬空、图过期这类会直接误导评审的）
      warn    —— 只提示，留到下一轮（索引遗漏、数字过期这类不阻断但会穿帮的）
    """

    cid: str
    layer: str
    name: str
    why: str
    level: str                       # blocker | warn
    fn: Callable[[], list[str]]


CHECKS: list[Check] = []


def register(cid: str, layer: str, name: str, why: str, level: str = "blocker"):
    """登记一项检查的装饰器。

    入参：cid 编号；layer 层（L1–L4）；name 检查项名；why 这一项在防什么；
         level 级别（blocker/warn）。
    出参：装饰器，把被装饰函数登记进 CHECKS 并原样返回。
    失败：不抛异常。被装饰函数须返回问题列表（空列表即通过）——
         约定靠文档与评审保证，不做运行时校验，因为一次类型检查
         不如一条真实断言有用。
    """

    def deco(fn: Callable[[], list[str]]):
        """把函数登记进 CHECKS。

        入参：fn 返回问题列表的检查函数。
        出参：原函数（便于单独调用测试）。
        失败：不抛异常。
        """
        CHECKS.append(Check(cid, layer, name, why, level, fn))
        return fn

    return deco


@dataclass
class Result:
    """一次检查的执行结果。

    ok=True 且 crashed 为空表示通过；crashed 非空表示检查器自身崩了，
    那与"被检查物不合格"是两回事，报告里必须分开。
    """

    check: Check
    ok: bool
    issues: list[str] = field(default_factory=list)
    note: str = ""
    ms: float = 0.0
    crashed: str = ""


# ---------------------------------------------------------------- 公共读取


def read_norm(p: Path) -> str:
    """按文本读入并统一换行。

    入参：p 文件路径。
    出参：内容字符串，CRLF 已抹平成 LF。
    失败：文件不存在抛 FileNotFoundError。

    为什么要抹平：Windows 上 write_text 会把 \\n 写成 \\r\\n，
    比对生成物时必须先统一，否则会误报"图已过期"。
    """
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def rel(p: Path) -> str:
    """仓库内相对路径，统一用正斜杠。

    入参：p 绝对路径。
    出参：相对于仓库根的 posix 路径，报告在不同系统上看起来一致。
    失败：p 不在仓库内时抛 ValueError（说明调用方传错了文件）。
    """
    return p.resolve().relative_to(ROOT).as_posix()


def exemptions(line: str) -> tuple[bool, set[str]]:
    """解析一行的行内豁免标记。

    入参：line 为 Markdown 的一行。
    出参：(是否整行豁免, 该行被豁免的 token 集合)。
    失败：不抛异常。
    """
    m = IGNORE_RE.search(line)
    if not m:
        return False, set()
    ids = {t.strip("`，,、 ") for t in m.group(1).split()}
    return (not ids), {i for i in ids if i}


def skip_token(line: str, token: str) -> bool:
    """判断某个 token 是否被本行豁免。

    入参：line 原文；token 待检查的编号。
    出参：True 表示应跳过（整行豁免或点名豁免）。
    失败：不抛异常。
    """
    whole, ids = exemptions(line)
    return whole or token in ids


def md_files() -> list[Path]:
    """所有需要扫描编号与链接的 Markdown 文件。

    入参：无。
    出参：README + docs/*.md，排除 skip_docs 里的生成物。
    失败：README 不存在时只是不包含它，不抛异常——
         "README 缺失"由 L4-1 用明确的一条报出来，比在这里崩掉清楚。
    """
    readme = settings.path("readme")
    files = [readme] + sorted(docs_dir().glob("*.md"))
    skip = skip_docs()
    return [f for f in files if f.exists() and f.name not in skip]


def load_ledger() -> dict[str, Any]:
    """读入台账 JSON。

    入参：无。出参：解析后的字典。
    失败：文件缺失抛 FileNotFoundError，格式错误抛 json.JSONDecodeError，
         二者都由 main() 兜成统一结构。
    """
    return json.loads(ledger_path().read_text(encoding="utf-8"))


def ledger_rows() -> tuple[list[str], list[dict[str, str]]]:
    """跑一遍台账校验，拿到问题与展开行。

    入参：无。出参：(errors, rows)，语义同 build_ledger.validate。
    失败：台账缺失/格式坏时抛异常（同上，由 main 兜）。
    """
    return ledger.validate(load_ledger())


def ledger_index() -> tuple[set[str], set[str], dict[str, dict]]:
    """建立台账索引。

    入参：无。
    出参：(全部证据编号, 全部凭证编号, 证据编号→条目)。
    失败：台账缺失/缺字段时抛异常。
    """
    data = load_ledger()
    ids: set[str] = set()
    docs: set[str] = set()
    by_id: dict[str, dict] = {}
    for iv in data["interviews"]:
        for it in iv["items"]:
            ids.add(it["id"])
            by_id[it["id"]] = it
            if it.get("doc"):
                docs.add(it["doc"])
    return ids, docs, by_id


def row_diff(x: str, y: str) -> tuple[str, str, str]:
    """定位两行 CSV 差在哪个字段。

    入参：x 为文件里的行，y 为应为的行。
    出参：(列名, 文件值, 应为值)，各截断 40 字。
    失败：两行都合法 CSV 时不抛；列数不同时返回 ("列数", ...)。

    为什么要定位到列：只报"整行不一致"等于让人自己去数逗号；
    而且截断后两行看起来一模一样，等于没报。
    """
    fx, fy = next(csv.reader([x])), next(csv.reader([y]))
    cols = ledger.columns()
    for i, (a, b) in enumerate(zip(fx, fy)):
        if a != b:
            col = cols[i] if i < len(cols) else f"第 {i+1} 列"
            return col, a[:40] or "（空）", b[:40] or "（空）"
    if len(fx) != len(fy):
        return "列数", f"{len(fx)} 列", f"{len(fy)} 列"
    return "整行", x[:40], y[:40]


def csv_bytes(rows: list[dict[str, str]]) -> bytes:
    """复现 build_ledger.write_csv 的字节结果。

    入参：rows 为台账展开行。
    出参：带 BOM 的 UTF-8 字节串。
    失败：不抛异常（内存中的 csv 写入不会失败）。

    为什么要在 harness 里重算一遍而不是读 build_ledger.COLUMNS：
    这里要的是"如果现在重新生成，应该是什么"，才能判断磁盘上那份是不是旧的。
    """
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=ledger.columns())
    w.writeheader()
    w.writerows(rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ---------------------------------------------------------------- L1 台账自洽


@register("L1-1", "L1", "台账结构与枚举合法",
          "编号格式、证据性质、凭证状态、断点引用；这是所有下游检查的地基", "blocker")
def l1_structure() -> list[str]:
    """台账自身是否合法。

    入参：无。出参：问题列表。失败：台账缺失抛异常（整项记为 ERROR）。
    """
    errors, _ = ledger_rows()
    return errors


@register("L1-2", "L1", "证据编号不重复",
          "同一编号出现两次，交叉核对表就会互相覆盖，改一处另一处静默失效", "blocker")
def l1_unique() -> list[str]:
    """证据编号是否唯一。

    入参：无。出参：重复项列表。失败：台账缺失抛异常。

    这条是 L1-1 补不上的洞：build_ledger 只校验格式，不查重。
    """
    data = load_ledger()
    seen: dict[str, str] = {}
    issues = []
    for iv in data["interviews"]:
        for it in iv["items"]:
            if it["id"] in seen:
                issues.append(f"{it['id']} 重复出现（{seen[it['id']]} 与 {iv['id']}）")
            else:
                seen[it["id"]] = iv["id"]
    return issues


@register("L1-3", "L1", "CSV 是 JSON 的忠实投影",
          "改了 JSON 忘了重跑 build_ledger，Excel 里看到的还是上一版数字", "blocker")
def l1_csv_sync() -> list[str]:
    """磁盘上的 CSV 是否等于"现在重新生成"的结果。

    入参：无。出参：问题列表（最多一条，定位到列）。
    失败：台账本身有错时提前返回，避免在坏数据上比对出一个误导性的差异。
    """
    errors, rows = ledger_rows()
    # 台账自己都不合法时，CSV 差异没有诊断价值：先修 L1-1，
    # 否则用户会去"重新生成 CSV"，结果生成出来的还是一份错数据。
    if errors:
        return ["台账本身有错，跳过同步比对（先修 L1-1）"]
    csv_p = ledger_csv_path()
    if not csv_p.exists():
        return [f"{rel(csv_p)} 不存在，跑 python src/build_ledger.py 生成"]
    actual = csv_p.read_bytes()
    expected = csv_bytes(rows)
    if actual == expected:
        return []

    # 字节不等不代表过期：CSV 是文本文件，git 检出时会按平台转换行尾
    # （core.autocrlf 在 Windows 给 CRLF、Linux 给 LF）。所以要比内容，不比字节，
    # 否则本地绿、CI 红——而"重新生成"也修不好，因为内容本来就一致。
    a = actual.decode("utf-8-sig").replace("\r\n", "\n")
    e = expected.decode("utf-8-sig").replace("\r\n", "\n")
    if a == e:
        return []

    la, le = a.split("\n"), e.split("\n")
    for i, (x, y) in enumerate(zip(la, le), 1):
        if x != y:
            col, got, want = row_diff(x, y)
            return [f"CSV 第 {i} 行「{col}」不一致：文件是「{got}」，应为「{want}」"
                    f"——跑 python src/build_ledger.py 重新生成"]
    if len(la) != len(le):
        return [f"CSV 行数与台账不一致（CSV {len(la)} 行 / 应为 {len(le)} 行），"
                f"跑 python src/build_ledger.py 重新生成"]
    return ["CSV 与台账不一致，跑 python src/build_ledger.py 重新生成"]


# ---------------------------------------------------------------- L2 证据可追溯


@register("L2-1", "L2", "文档引用的证据编号都存在",
          "结论里写 INT-04-07，台账里没有这一条，就等于凭空说话", "blocker")
def l2_evidence_exists() -> list[str]:
    """文档里出现的每个 INT-xx-yy 是否都在台账里。

    入参：无。出参：问题列表（含 file:line）。
    失败：不抛异常；单篇文档读不到会被跳过（缺失由 L4-1 报）。
    """
    ids, _, _ = ledger_index()
    issues = []
    for f in md_files():
        for no, line in enumerate(read_norm(f).split("\n"), 1):
            for ev in set(EVID_RE.findall(line)):
                if ev not in ids and not skip_token(line, ev):
                    issues.append(f"{rel(f)}:{no} 引用了不存在的证据 {ev}")
    return issues


@register("L2-2", "L2", "文档引用的断点编号合法",
          "断点只有 B1–B6；写个 B7 出来，读者去图上找不到这个点", "blocker")
def l2_break_valid() -> list[str]:
    """文档里出现的每个 Bx 是否都是台账登记过的断点。

    入参：无。出参：问题列表。
    失败：台账缺失抛异常。
    """
    data = load_ledger()
    valid = {b["id"] for b in data["breaks"]}
    issues = []
    for f in md_files():
        for no, line in enumerate(read_norm(f).split("\n"), 1):
            for m in BREAK_RE.finditer(line):
                bid = f"B{m.group(1)}"
                if bid not in valid and not skip_token(line, bid):
                    issues.append(f"{rel(f)}:{no} 引用了不存在的断点 {bid}")
    return issues


@register("L2-3", "L2", "文档引用的凭证编号有登记",
          "引用 DOC-05 但台账里没人登记过它，取证计划就漏了一项", "warn")
def l2_doc_registered() -> list[str]:
    """文档里出现的每个 DOC-xx 是否在台账里登记过。

    入参：无。出参：问题列表。
    失败：台账缺失抛异常。

    级别是 warn：凭证编号写错不会让结论失真，只会让取证清单少一项。
    """
    _, docs, _ = ledger_index()
    if not docs:
        return []
    issues = []
    for f in md_files():
        for no, line in enumerate(read_norm(f).split("\n"), 1):
            for d in set(DOCID_RE.findall(line)):
                if d not in docs and not skip_token(line, d):
                    issues.append(f"{rel(f)}:{no} 引用了未登记的凭证 {d}"
                                  f"（台账里已登记：{'、'.join(sorted(docs))}）")
    return issues


# ---------------------------------------------------------------- L3 图与定义一致


@register("L3-1", "L3", "流程图定义合法",
          "节点 id 唯一、泳道存在、连线端点存在、断点编号合法", "blocker")
def l3_flow_validate() -> list[str]:
    """复用 render_flow 的定义校验。

    入参：无。出参：问题列表。失败：不抛异常（定义校验本身不读配置）。
    """
    return flow.validate()


@register("L3-2", "L3", "流程图 SVG 是当前定义的产物",
          "改了流程没重跑脚本，评审看到的图还是上一版", "blocker")
def l3_flow_sync() -> list[str]:
    """磁盘上的流程图 SVG 是否等于当前定义导出的结果。

    入参：无。出参：问题列表。
    失败：文件缺失返回一条明确提示；渲染异常抛给 run_one 记为 ERROR。
    """
    svg = settings.figure("flow")[1]
    if not svg.exists():
        return [f"{rel(svg)} 不存在，跑 python src/render_flow.py"]
    if read_norm(svg) != flow.to_svg().replace("\r\n", "\n"):
        return ["流程图 SVG 与当前定义不一致，跑 python src/render_flow.py 重新生成"]
    return []


@register("L3-3", "L3", "利益相关者地图定义合法",
          "证据编号必须真的存在于台账、六位访谈对象必须全部上地图", "blocker")
def l3_stake_validate() -> list[str]:
    """复用 render_stakeholders 的定义校验（含与台账的双向对账）。

    入参：无。出参：问题列表。失败：不抛异常。
    """
    return stake.validate()


@register("L3-4", "L3", "利益相关者地图 SVG 是当前定义的产物",
          "同上：图与定义不同步，四象限就会误导决策链判断", "blocker")
def l3_stake_sync() -> list[str]:
    """磁盘上的地图 SVG 是否等于当前定义导出的结果。

    入参：无。出参：问题列表。失败：文件缺失返回明确提示。
    """
    svg = settings.figure("stakeholders")[1]
    if not svg.exists():
        return [f"{rel(svg)} 不存在，跑 python src/render_stakeholders.py"]
    if read_norm(svg) != stake.to_svg().replace("\r\n", "\n"):
        return ["地图 SVG 与当前定义不一致，跑 python src/render_stakeholders.py 重新生成"]
    return []


@register("L3-5", "L3", "流程图 Mermaid 是当前定义的产物",
          "三份生成物只校两份，漏掉的那份就会悄悄停在旧版本", "blocker")
def l3_flow_mmd_sync() -> list[str]:
    """磁盘上的流程图 Mermaid 是否等于当前定义导出的结果。

    入参：无。出参：问题列表。失败：文件缺失返回明确提示。
    """
    mmd = settings.figure("flow")[3]
    if not mmd.exists():
        return [f"{rel(mmd)} 不存在，跑 python src/render_flow.py"]
    if read_norm(mmd) != flow.to_mmd().replace("\r\n", "\n"):
        return ["流程图 Mermaid 与当前定义不一致，跑 python src/render_flow.py 重新生成"]
    return []


@register("L3-6", "L3", "地图 Mermaid 是当前定义的产物",
          "同上：漏校一份生成物，等于给「图是新的」留了一个反例", "blocker")
def l3_stake_mmd_sync() -> list[str]:
    """磁盘上的地图 Mermaid 是否等于当前定义导出的结果。

    入参：无。出参：问题列表。失败：文件缺失返回明确提示。
    """
    mmd = settings.figure("stakeholders")[3]
    if not mmd.exists():
        return [f"{rel(mmd)} 不存在，跑 python src/render_stakeholders.py"]
    if read_norm(mmd) != stake.to_mmd().replace("\r\n", "\n"):
        return ["地图 Mermaid 与当前定义不一致，跑 python src/render_stakeholders.py 重新生成"]
    return []


# ---------------------------------------------------------------- L4 文档层


@register("L4-1", "L4", "交付物齐套",
          "平台按清单验收，少一份就是少一份，不会因为写得好而豁免", "blocker")
def l4_complete() -> list[str]:
    """交付清单里每个文件是否都在磁盘上。

    入参：无。出参：缺失文件列表。失败：清单为空抛 settings.ConfigError——
         空清单会让这一项永远通过，等于把验收门拆掉。
    """
    return [f"缺文件：{p}" for p in settings.deliverables() if not (ROOT / p).exists()]


@register("L4-2", "L4", "文档内本地链接可解析",
          "README 的交付物表是唯一入口，表里点不开等于找不到", "blocker")
def l4_links() -> list[str]:
    """每篇文档里的相对链接是否都指向存在的文件。

    入参：无。出参：问题列表（含 file:line）。
    失败：不抛异常。外链（http/mailto）与纯锚点不检查——
         检查外链会让校验依赖网络，CI 一旦断网就全红。
    """
    issues = []
    for f in md_files():
        base = f.parent
        for no, line in enumerate(read_norm(f).split("\n"), 1):
            if exemptions(line)[0]:
                continue
            for raw in LINK_RE.findall(line):
                target = raw.strip()
                if not target or target.startswith(("#", "http://", "https://", "mailto:")):
                    continue
                target = target.split("#", 1)[0].split("?", 1)[0]
                if not target:
                    continue
                p = (base / target.replace("%20", " ")).resolve()
                if not p.exists():
                    issues.append(f"{rel(f)}:{no} 链接目标不存在：{target}")
    return issues


@register("L4-3", "L4", "每份交付文档都进了 README 索引",
          "写了但没进索引的文档，评审不会翻到——等于没写", "warn")
def l4_indexed() -> list[str]:
    """docs 下每篇 Markdown 是否都在 README 里被提到。

    入参：无。出参：未索引文件名列表。
    失败：README 缺失时抛出 FileNotFoundError（由 run_one 记 ERROR）。
    """
    readme = read_norm(settings.path("readme"))
    skip = skip_docs()
    issues = []
    for f in sorted(docs_dir().glob("*.md")):
        if f.name in skip:
            continue
        if f.name not in readme:
            issues.append(f"docs/{f.name} 未被 README 引用")
    return issues


@register("L4-4", "L4", "文档里的统计口径没写死",
          "台账从 32 条补到 36 条时，正文里遗留的“32 条”会当场穿帮", "warn")
def l4_numbers() -> list[str]:
    """正文里写的"N 条证据/记录"是否与台账实际条数一致。

    入参：无。出参：问题列表。
    失败：台账缺失抛异常。
    """
    data = load_ledger()
    total = sum(len(iv["items"]) for iv in data["interviews"])
    issues = []
    for f in md_files():
        for no, line in enumerate(read_norm(f).split("\n"), 1):
            for m in re.finditer(r"(\d+)\s*条(?:证据|调查事实|记录)", line):
                n = int(m.group(1))
                # 同一行里出现了正确总数，说明是在讲"从旧数补到新数"的历史，跳过
                if n != total and str(total) not in line and not skip_token(line, str(n)):
                    issues.append(f"{rel(f)}:{no} 写作“{n} 条”，"
                                  f"台账实为 {total} 条")
    return issues


@register("L4-5", "L4", "交付清单本身没过期",
          "清单是手写的：新增文件忘了登记，L4-1 就保护不到它——检查清单自己也会烂",
          "warn")
def l4_expected_sync() -> list[str]:
    """docs 下是否有文件存在却不在交付清单里。

    入参：无。出参：未登记文件列表。
    失败：不抛异常。只扫 docs/，因为清单里还有根目录文件（README、config.yaml 等），
         那些不属于"新增文档忘了登记"的场景。
    """
    listed = {(ROOT / p).resolve() for p in settings.deliverables()}
    skip = skip_docs()
    on_disk = {p.resolve() for p in docs_dir().rglob("*")
               if p.is_file() and p.name not in skip
               and "__pycache__" not in p.parts}
    return [f"{rel(p)} 存在但未登记进交付清单（新增交付物请补 config.yaml）"
            for p in sorted(on_disk - listed)]


# ---------------------------------------------------------------- L5 代码规范
# 这一层不查交付物，查"代码自己有没有守住规范"。
# 四条规范里三条能机器验（docstring、硬编码、统一返回结构），
# 第二条"关键逻辑要写为什么"没法自动判断，只能靠评审——不假装覆盖，
# 所以在报告的分层说明里如实标成"部分可验"。

ENVELOPE_KEYS = {"ok", "data", "error"}
CLI_ENTRIES = ["build_ledger.py", "render_flow.py", "render_stakeholders.py"]
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 只认"真的会被当成路径/地址"的形态：盘符开头、常见系统目录、带主机的 http(s)。
# 放宽到"字符串里带斜杠"会把 "1/9"、正则、Mermaid 语法全判成问题；
# 只写 ^https?:// 又会把代码里用来过滤外链的 "http://" 前缀常量误报成地址——
# 要求 scheme 后面真有主机名，才分得清"协议前缀"和"某个具体地址"。
HARDCODE_RES = [
    (re.compile(r"^[A-Za-z]:[\\/]"), "Windows 绝对路径"),
    (re.compile(r"^/(usr|etc|opt|home|var|System|Library)/"), "POSIX 绝对路径"),
    (re.compile(r"^https?://[A-Za-z0-9]"), "外部地址"),
]


@register("L5-1", "L5", "每个函数都有中文 docstring",
          "没有 docstring 的函数，接手的人只能读实现猜参数与失败行为；"
          "英文摘要对中文评审等于没有", "warn")
def l5_docstrings() -> list[str]:
    """扫 src/*.py 的 AST，检查每个函数（含嵌套）是否带中文 docstring。

    入参：无。
    出参：问题列表，定位到 文件:行 与函数名。
    失败：语法错误的文件会抛 SyntaxError（由 run_one 记 ERROR）——
         那种情况下校验本身已经没有意义，报"检查器崩了"比给一堆假结论诚实。

    为什么是 warn 而不是 blocker：缺 docstring 不会让评审读到错误结论，
    只会让后来的人多花时间。把它设成 blocker 会让紧急修复被一次风格问题挡住。
    """
    issues = []
    for f in sorted((ROOT / "src").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            doc = ast.get_docstring(node) or ""
            if not doc.strip():
                issues.append(f"src/{f.name}:{node.lineno} 函数 {node.name} 没有 docstring")
            elif not CJK_RE.search(doc):
                issues.append(f"src/{f.name}:{node.lineno} 函数 {node.name} 的 docstring 不含中文")
    return issues


@register("L5-2", "L5", "代码里没有硬编码路径与地址",
          "写死 C:/Windows/Fonts 这类路径，换到 CI 或别人机器上就找不到；"
          "这类值必须进 config.yaml", "blocker")
def l5_hardcoded() -> list[str]:
    """扫 src/*.py 的字符串常量，找绝对路径与外部地址。

    入参：无。
    出参：问题列表，定位到 文件:行。
    失败：语法错误抛 SyntaxError（同上，记 ERROR）。

    级别是 blocker：硬编码路径不是风格问题，它会让代码在另一台机器上直接失败，
    而且失败信息通常与真正原因无关（例如 PIL 的 cannot open resource）。
    """
    issues = []
    for f in sorted((ROOT / "src").glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for pat, label in HARDCODE_RES:
                if pat.search(node.value):
                    issues.append(f"src/{f.name}:{node.lineno} 硬编码{label}："
                                  f"{node.value[:50]}（应移到 config.yaml）")
                    break
    return issues


@register("L5-3", "L5", "三个 CLI 的 --json 输出符合统一结构",
          "接口形状一散，CI 与 hook 就要为每个脚本写一套解析；"
          "ok 与退出码不一致还会让 CI 误判通过", "blocker")
def l5_envelope() -> list[str]:
    """逐个跑三个生成器的 `--check --json`，校验返回结构与退出码一致。

    入参：无。
    出参：问题列表。
    失败：子进程超时/无法启动会抛异常（记 ERROR）。

    为什么真的去跑而不是静态看代码 import 了 envelope：
    静态只能证明"引了包"，证明不了"输出真的是这个形状"。
    这一条守的正是"接口"本身，必须验行为。
    """
    issues = []
    for name in CLI_ENTRIES:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "src" / name), "--check", "--json"],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
        try:
            resp = json.loads(proc.stdout)
        except json.JSONDecodeError:
            issues.append(f"{name} --json 输出不是合法 JSON"
                          f"（stdout 前 80 字：{proc.stdout[:80]!r}）")
            continue
        if not isinstance(resp, dict) or set(resp) != ENVELOPE_KEYS:
            got = sorted(resp) if isinstance(resp, dict) else type(resp).__name__
            issues.append(f"{name} 顶层字段应为 {sorted(ENVELOPE_KEYS)}，实际 {got}")
            continue
        if not isinstance(resp["ok"], bool):
            issues.append(f"{name} 的 ok 必须是布尔值")
        err = resp["error"]
        if err is not None and not (isinstance(err, dict) and {"code", "msg"} <= set(err)):
            issues.append(f"{name} 失败时 error 必须是 {{code,msg}}，实际 {err!r}")
        # ok 与退出码必须同向：否则 CI 看退出码、hook 看 ok，会得出相反结论
        if resp["ok"] and proc.returncode != 0:
            issues.append(f"{name} ok=True 但退出码是 {proc.returncode}")
        if not resp["ok"] and proc.returncode == 0:
            issues.append(f"{name} ok=False 但退出码是 0（CI 会误判为通过）")
    return issues


# ---------------------------------------------------------------- 运行器


def run_one(c: Check) -> Result:
    """执行一项检查并计时。

    入参：c 为检查登记项。
    出参：Result。检查函数抛异常时记 crashed（保留栈），不向上冒——
         一项崩了不该让整轮校验丢掉其余 16 项的结果。
    失败：本函数本身不抛异常。
    """
    t0 = time.perf_counter()
    try:
        issues = c.fn()
        ok = not issues
        r = Result(c, ok, [str(i) for i in issues])
    except Exception:
        r = Result(c, False, [], crashed=traceback.format_exc(limit=3))
    r.ms = (time.perf_counter() - t0) * 1000
    return r


def git_bits() -> tuple[str, int]:
    """取当前提交短 SHA 与工作区改动数。

    入参：无。出参：(SHA, 改动文件数)，取不到时返回 ("—", -1)。
    失败：不抛异常——报告里"状态未知"比整份报告生成失败要好。
    """

    def sh(*args: str) -> str:
        """跑一条 git 命令并返回 stdout。

        入参：args 为 git 参数。
        出参：去空白后的 stdout。
        失败：git 不存在或非仓库时抛异常，由外层转成 ("—", -1)。
        """
        return subprocess.run(args, cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()

    try:
        sha = sh("git", "rev-parse", "--short", "HEAD")
        raw = sh("git", "-c", "core.quotepath=false", "status", "--porcelain")
        # 报告本身排除在外：写报告这件事不该被算成"工作区脏"，
        # 否则每次生成都自证有未提交改动，这份报告永远不可能干净。
        name = report_path().name
        dirty = len([l for l in raw.split("\n") if l.strip() and name not in l])
    except Exception:
        return "—", -1
    return sha or "—", dirty


def summary_line(r: Result) -> str:
    """给一项结果打四字标签。

    入参：r 为结果。出参："OK  " / "WARN" / "FAIL" / "ERROR"。
    失败：不抛异常。
    """
    if r.crashed:
        return "ERROR"
    if r.ok:
        return "OK  "
    return "WARN" if r.check.level == "warn" else "FAIL"


def render_console(results: list[Result], total_ms: float) -> str:
    """把结果渲染成终端文本。

    入参：results 为逐项结果；total_ms 为总耗时。
    出参：完整文本（含分层标题与合计行）。
    失败：不抛异常。

    返回字符串而不是直接 print：统一结构下所有输出都由 emit() 落地，
    这里只负责把结果排版好，避免两处都往 stdout 写。
    """
    layer_titles = {
        "L1": "L1 台账自洽", "L2": "L2 证据可追溯",
        "L3": "L3 图与定义一致", "L4": "L4 文档层",
    }
    o: list[str] = []
    cur = None
    for r in results:
        if r.check.layer != cur:
            cur = r.check.layer
            o.append(f"\n{layer_titles.get(cur, cur)}")
        mark = summary_line(r)
        line = f"  [{mark}] {r.check.cid}  {r.check.name}"
        if r.ok and not r.crashed:
            o.append(line)
        else:
            o.append(line + (f"  ← {len(r.issues)} 处" if r.issues else ""))
            for i in r.issues:
                o.append(f"           · {i}")
            if r.crashed:
                o.append("           · 检查器自身异常：")
                for l in r.crashed.strip().split("\n"):
                    o.append(f"             {l}")

    ok_n = sum(1 for r in results if r.ok and not r.crashed)
    fail_n = sum(1 for r in results if not r.ok and not r.crashed and r.check.level == "blocker")
    warn_n = sum(1 for r in results if not r.ok and not r.crashed and r.check.level == "warn")
    err_n = sum(1 for r in results if r.crashed)
    o.append(f"\n{'='*60}")
    o.append(f"合计 {len(results)} 项：通过 {ok_n}｜失败 {fail_n}｜警告 {warn_n}｜异常 {err_n}"
             f"　用时 {total_ms/1000:.2f}s")
    verdict = "FAIL" if (fail_n or err_n) else "PASS"
    o.append(f"结论：{verdict}")
    return "\n".join(o)


def scope_stats() -> list[tuple[str, str]]:
    """这次校验到底覆盖了多少东西。

    入参：无。出参：[(对象, 规模, 取值来源)]。
    失败：台账缺失抛异常（此时报告不该生成）。

    只写清单不写规模，报告看起来像打卡——所以这里刻意把数字算出来。
    """
    data = load_ledger()
    n_iv = len(data["interviews"])
    n_ev = sum(len(iv["items"]) for iv in data["interviews"])
    n_br = len(data["breaks"])
    single = sum(1 for iv in data["interviews"] for it in iv["items"]
                 if (it.get("cross") or "") == "单源")
    pending = len({it["doc"] for iv in data["interviews"] for it in iv["items"]
                   if it.get("doc") and it.get("doc_status") == "待补"})
    figs = len(list(fig_dir().glob("*.*")))
    return [
        ("访谈对象", f"{n_iv} 位（每人 6 条）", "调研记录.json"),
        ("调查事实", f"{n_ev} 条，其中单源 {single} 条不进结论", "调研记录.json"),
        ("断点", f"{n_br} 个，全部有记录支撑", "调研记录.json"),
        ("待补凭证", f"{pending} 项（M1/M2/M5 停在估计的根因）", "调研记录.json"),
        ("现状流程图", f"{len(flow.NODES)} 节点 / {len(flow.EDGES)} 连线 / {len(flow.LANES)} 泳道",
         "render_flow.py"),
        ("利益相关者地图", f"{len(stake.ROLES)} 角色 / {len(stake.CHAIN)} 段决策链 / "
                          f"{len(stake.QUADRANTS)} 象限", "render_stakeholders.py"),
        ("文档", f"{len(md_files())} 篇 Markdown 全量扫描（链接、编号、数字）", "docs/ + README"),
        ("图文件", f"{figs} 个（svg/png/mmd 各两套）", "docs/图/"),
    ]


def render_markdown(results: list[Result], total_ms: float) -> str:
    """生成可复核的 Markdown 报告。

    入参：results 为逐项结果；total_ms 为总耗时。
    出参：Markdown 文本。报告写在本地（.gitignore 已排除），不入库。
    失败：取 git 状态失败不影响生成（显示"状态未知"）。
    """
    _sha, dirty = git_bits()
    ok_n = sum(1 for r in results if r.ok and not r.crashed)
    fail = [r for r in results if not r.ok and not r.crashed and r.check.level == "blocker"]
    warn = [r for r in results if not r.ok and not r.crashed and r.check.level == "warn"]
    err = [r for r in results if r.crashed]
    verdict = "FAIL" if (fail or err) else "PASS"

    o: list[str] = []
    o.append("# 校验报告")
    o.append("")
    o.append("> 本文件由 `python src/harness.py` 生成，请勿手改。")
    # 这里刻意不写提交 SHA：报告本身要进同一个提交，写死 SHA 会在 amend 后自我过期。
    # 工作区状态是稳定的（报告自身已排除），可以留。
    o.append(f"> 工作区{'有未提交改动' if dirty > 0 else '干净' if dirty == 0 else '状态未知'}"
             f"｜用时 {total_ms/1000:.2f}s｜对应代码见最近一次提交")
    o.append("")
    o.append(f"## 结论：**{verdict}**")
    o.append("")
    o.append(f"{len(results)} 项检查：通过 {ok_n}、失败 {len(fail)}、警告 {len(warn)}、异常 {len(err)}。")
    o.append("")
    if fail or err:
        o.append("### 必须先修的（blocker）")
        o.append("")
        for r in fail + err:
            o.append(f"- **{r.check.cid} {r.check.name}**")
            if r.crashed:
                o.append("  - 检查器自身异常：")
                o.append("    ```")
                for l in r.crashed.strip().split("\n"):
                    o.append(f"    {l}")
                o.append("    ```")
            for i in r.issues:
                o.append(f"  - {i}")
        o.append("")
    if warn:
        o.append("### 可以留到下一轮（warn）")
        o.append("")
        for r in warn:
            o.append(f"- **{r.check.cid} {r.check.name}**")
            for i in r.issues:
                o.append(f"  - {i}")
        o.append("")

    o.append("## 这次校验覆盖了什么")
    o.append("")
    o.append("| 对象 | 规模 | 取值来源 |")
    o.append("|---|---|---|")
    for name, val, src in scope_stats():
        o.append(f"| {name} | {val} | `{src}` |")
    o.append("")
    o.append("## 分层：每一层在防什么")
    o.append("")
    o.append("| 层 | 管什么 | 为什么单靠人工查不住 |")
    o.append("|---|---|---|")
    o.append("| L1 | 台账自身自洽，CSV 是 JSON 的忠实投影 | "
             "36 条记录里只改一处，眼睛看不出导出的 CSV 已经过期 |")
    o.append("| L2 | 文档里每个编号都能在台账里落地 | "
             "编号散落在 7 篇文档里，人工核对要一页页翻，漏一条不会报错 |")
    o.append("| L3 | 图确实是当前定义的产物 | "
             "PNG/SVG 看不出是哪个版本导出的，改了定义忘了重跑最容易被当成“图没变” |")
    o.append("| L4 | 链接能点开、交付物齐套、索引没漏、数字没过期 | "
             "失效链接和遗留的旧数字都不报错，只会静默误导读者 |")
    o.append("| L5 | 代码自己守住规范：docstring、无硬编码、接口形状 | "
             "这几条不会让当下的校验失败，只会在换机器、换人、接 CI 的时候集中爆 |")
    o.append("")
    o.append("## 全部检查项")
    o.append("")
    o.append("| 层 | 编号 | 检查项 | 这一项在防什么 | 级别 | 结论 |")
    o.append("|---|---|---|---|---|---|")
    for r in results:
        c = r.check
        mark = "**通过**" if r.ok else ("异常" if r.crashed else ("警告" if c.level == "warn" else "**失败**"))
        o.append(f"| {c.layer} | {c.cid} | {c.name} | {c.why} | {c.level} | {mark} |")
    o.append("")
    o.append("## 怎么用")
    o.append("")
    o.append("```bash")
    o.append("python src/harness.py            # 跑全部并刷新本报告")
    o.append("python src/harness.py --only L2  # 只查证据可追溯")
    o.append("python src/harness.py --json     # CI 取统一结构结果")
    o.append("```")
    o.append("")
    o.append("退出码：`0` 通过（warn 不算失败）、`1` 有 blocker 失败、`2` harness 自身出错。")
    o.append("")
    return "\n".join(o)


# ---------------------------------------------------------------- 入口


def run(only: list[str], write: bool) -> dict[str, Any]:
    """跑指定层的检查，返回统一结构。

    入参：only 为要跑的层（空列表表示全部）；write 为是否落盘报告。
    出参：{"ok","data","error"}。data 含 verdict、逐项结果、报告路径。
         存在 blocker 失败或检查器异常时 ok=False、code=VALIDATION_FAILED。
    失败：必要文件缺失返回 INPUT_MISSING；没有匹配的检查项返回 NO_CHECKS_MATCHED；
         配置问题返回 CONFIG_*。都不抛异常。
    """
    missing = [p for p in (ledger_path(), settings.path("readme")) if not p.exists()]
    if missing:
        return envelope.fail("INPUT_MISSING",
                             "找不到必要文件：" + "、".join(str(m) for m in missing))

    picked = [c for c in CHECKS if not only or c.layer in only]
    if not picked:
        return envelope.fail("NO_CHECKS_MATCHED", f"没有匹配的检查项（--only {' '.join(only)}）")

    t0 = time.perf_counter()
    results = [run_one(c) for c in picked]
    total_ms = (time.perf_counter() - t0) * 1000

    fail_n = sum(1 for r in results if not r.ok and not r.crashed and r.check.level == "blocker")
    err_n = sum(1 for r in results if r.crashed)
    verdict = "FAIL" if (fail_n or err_n) else "PASS"

    written: Path | None = None
    if write:
        written = report_path()
        written.write_text(render_markdown(results, total_ms), encoding="utf-8")

    data = {
        "verdict": verdict,
        "elapsed_ms": round(total_ms, 1),
        "commit": git_bits()[0],
        "report": rel(written) if written else None,
        "checks": [{
            "id": r.check.cid, "layer": r.check.layer, "name": r.check.name,
            "level": r.check.level, "ok": r.ok, "ms": round(r.ms, 1),
            "issues": r.issues,
            "crashed": bool(r.crashed),
            # 保留原始栈：文本输出要能看出是哪一行炸的，只给一个 bool 等于让人重跑
            "crashed_detail": r.crashed,
        } for r in results],
    }
    if verdict == "FAIL":
        # 失败时仍带上全部 data：评审要看的是"哪几项红了"，而不是只看到一个 FAIL。
        return envelope.fail("VALIDATION_FAILED",
                             f"{fail_n + err_n} 项未通过（失败 {fail_n} / 异常 {err_n}）",
                             data=data)
    return envelope.ok(data)


def main(argv: list[str]) -> int:
    """命令行入口。

    入参：argv 为去掉程序名后的参数。
    出参：进程退出码（0/1/2，见模块文档）。
    失败：配置缺失、依赖缺失、必要文件缺失都转成统一结构并返回 2，
         不把 traceback 直接甩给 CI 日志。
    """
    ap = argparse.ArgumentParser(description="统一校验 harness")
    ap.add_argument("--only", action="append", default=[],
                    help="只跑指定层，如 L2（可重复写：--only L1 --only L3）")
    ap.add_argument("--no-write", action="store_true", help="不写校验报告")
    ap.add_argument("--json", action="store_true", help="输出统一结构 JSON")
    ap.add_argument("--list", action="store_true", help="列出所有检查项")
    args = ap.parse_args(argv)

    if args.list:
        # --list 也是对外接口，同样走统一结构：
        # CI 可以用它生成"这套仓库到底校了什么"的清单。
        data = {"checks": [{"id": c.cid, "layer": c.layer, "level": c.level,
                            "name": c.name, "why": c.why} for c in CHECKS]}
        text = "\n".join(f"{c.cid}  [{c.layer}/{c.level}]  {c.name}\n      {c.why}"
                         for c in CHECKS)
        envelope.emit(envelope.ok(data), as_json=args.json, text=text)
        return 0

    try:
        resp = run(args.only, write=not args.no_write and not args.json)
    except settings.ConfigError as exc:
        resp = envelope.from_exception(exc)

    if resp["ok"]:
        d = resp["data"]
        text = _console_of(d)
        if d["report"]:
            text += f"\n报告已写入：{d['report']}"
    else:
        code = resp["error"]["code"]
        text = resp["error"]["msg"]
        # 失败时仍然把逐项结果打出来：只说"有 2 项失败"等于让人自己去猜哪两项。
        if isinstance(resp.get("data"), dict) and resp["data"].get("checks"):
            text = _console_of(resp["data"]) + "\n" + text

    envelope.emit(resp, as_json=args.json, text=text)
    return envelope.exit_code(resp)


def _console_of(d: dict[str, Any]) -> str:
    """把统一结构里的逐项结果还原成终端文本。

    入参：data 段（含 checks 列表）。
    出参：终端文本。
    失败：checks 结构不符抛 KeyError——它是本模块自己造的，不会来自外部输入。
    """
    # 复用 render_console 的排版，但数据此时已经降级成字典，这里重建轻量对象。
    fake: list[Result] = []
    for c in d["checks"]:
        chk = next((x for x in CHECKS if x.cid == c["id"]), None)
        if chk is None:
            continue
        fake.append(Result(chk, c["ok"], c["issues"], ms=c["ms"],
                           crashed=c.get("crashed_detail", "")))
    return render_console(fake, d["elapsed_ms"])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
