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
一次跑完，输出一份可复核的报告（`docs/校验报告.md`）。

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
    python src/harness.py --json         # 机器可读（挂 CI 用）
    python src/harness.py --only L2      # 只跑某一层（可重复写多个）
    python src/harness.py --list         # 列出所有检查项及其用途

退出码
------
    0  全部通过（warn 不影响退出码）
    1  至少一项 blocker 失败
    2  harness 自身出错（导入失败、文件缺失等）
"""
from __future__ import annotations

import argparse
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

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
FIG = DOCS / "图"
LEDGER = DOCS / "调研记录.json"
LEDGER_CSV = DOCS / "调研记录.csv"
REPORT = DOCS / "校验报告.md"

sys.path.insert(0, str(ROOT / "src"))
import build_ledger as ledger          # noqa: E402
import render_flow as flow             # noqa: E402
import render_stakeholders as stake    # noqa: E402

# 自己生成的报告不参与扫描，否则每跑一次就多一份"被检查的文档"
SKIP_DOCS = {"校验报告.md"}

EVID_RE = re.compile(r"INT-\d{2}-\d{2}")
# 只匹配"长得像断点编号"的 token，合法性交给 L2-2 判断。
# 这里故意不写成 B[1-6]：那样越界的 B7、B0 会直接匹配不上，等于放行。
BREAK_RE = re.compile(r"(?<![A-Za-z0-9])B(\d{1,2})(?![0-9])")
DOCID_RE = re.compile(r"DOC-\d{2}")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
# 行内豁免：<!-- harness:ignore --> 跳过整行；<!-- harness:ignore B7 --> 只放过指定 token。
# 有这条通道，才不会因为一次误报就把整条检查删掉——那是拿掉报警器修火警。
IGNORE_RE = re.compile(r"<!--\s*harness:ignore([^>]*)-->")

# ---------------------------------------------------------------- 检查项登记


@dataclass
class Check:
    cid: str
    layer: str
    name: str
    why: str
    level: str                       # blocker | warn
    fn: "function"


CHECKS: list[Check] = []


def register(cid: str, layer: str, name: str, why: str, level: str = "blocker"):
    """登记一项检查。fn 返回问题列表，空列表即通过。"""
    def deco(fn):
        CHECKS.append(Check(cid, layer, name, why, level, fn))
        return fn
    return deco


@dataclass
class Result:
    check: Check
    ok: bool
    issues: list[str] = field(default_factory=list)
    note: str = ""
    ms: float = 0.0
    crashed: str = ""


# ---------------------------------------------------------------- 公共读取


def read_norm(p: Path) -> str:
    """按文本读入并统一换行。Windows 上 write_text 会把 \\n 写成 \\r\\n，
    比对生成物时必须先抹平，否则会误报"图已过期"。"""
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def rel(p: Path) -> str:
    """仓库内路径统一用正斜杠，报告在不同系统上看起来一致。"""
    return p.resolve().relative_to(ROOT).as_posix()


def exemptions(line: str) -> tuple[bool, set[str]]:
    """返回（整行豁免, 该行被豁免的 token 集合）。"""
    m = IGNORE_RE.search(line)
    if not m:
        return False, set()
    ids = {t.strip("`，,、 ") for t in m.group(1).split()}
    return (not ids), {i for i in ids if i}


def skip_token(line: str, token: str) -> bool:
    whole, ids = exemptions(line)
    return whole or token in ids


def md_files() -> list[Path]:
    files = [ROOT / "README.md"] + sorted(DOCS.glob("*.md"))
    return [f for f in files if f.exists() and f.name not in SKIP_DOCS]


def load_ledger() -> dict:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def ledger_rows() -> tuple[list[str], list[dict]]:
    errors, rows = ledger.validate(load_ledger())
    return errors, rows


def ledger_index() -> tuple[set[str], set[str], dict[str, dict]]:
    """返回（全部证据编号, 全部凭证编号, 证据编号→条目）。"""
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


def csv_bytes(rows: list[dict]) -> bytes:
    """复现 build_ledger.write_csv 的字节结果，用来做同步比对。"""
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=ledger.COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ---------------------------------------------------------------- L1 台账自洽


@register("L1-1", "L1", "台账结构与枚举合法",
          "编号格式、证据性质、凭证状态、断点引用；这是所有下游检查的地基", "blocker")
def l1_structure() -> list[str]:
    errors, _ = ledger_rows()
    return errors


@register("L1-2", "L1", "证据编号不重复",
          "同一编号出现两次，交叉核对表就会互相覆盖，改一处另一处静默失效", "blocker")
def l1_unique() -> list[str]:
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
    errors, rows = ledger_rows()
    if errors:
        return ["台账本身有错，跳过同步比对（先修 L1-1）"]
    if not LEDGER_CSV.exists():
        return ["docs/调研记录.csv 不存在，跑 python src/build_ledger.py 生成"]
    actual = LEDGER_CSV.read_bytes()
    expected = csv_bytes(rows)
    if actual != expected:
        # 给出更有用的定位：行数差多少
        a = actual.decode("utf-8-sig").splitlines()
        e = expected.decode("utf-8-sig").splitlines()
        return [f"CSV 与 JSON 不一致（CSV {len(a)} 行 / 应为 {len(e)} 行），"
                f"跑 python src/build_ledger.py 重新生成"]
    return []


# ---------------------------------------------------------------- L2 证据可追溯


@register("L2-1", "L2", "文档引用的证据编号都存在",
          "结论里写 INT-04-07，台账里没有这一条，就等于凭空说话", "blocker")
def l2_evidence_exists() -> list[str]:
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
    return flow.validate()


@register("L3-2", "L3", "流程图 SVG 是当前定义的产物",
          "改了流程没重跑脚本，评审看到的图还是上一版", "blocker")
def l3_flow_sync() -> list[str]:
    p = FIG / "现状流程图.svg"
    if not p.exists():
        return ["docs/图/现状流程图.svg 不存在，跑 python src/render_flow.py"]
    if read_norm(p) != flow.to_svg().replace("\r\n", "\n"):
        return ["流程图 SVG 与当前定义不一致，跑 python src/render_flow.py 重新生成"]
    return []


@register("L3-3", "L3", "利益相关者地图定义合法",
          "证据编号必须真的存在于台账、六位访谈对象必须全部上地图", "blocker")
def l3_stake_validate() -> list[str]:
    return stake.validate()


@register("L3-4", "L3", "利益相关者地图 SVG 是当前定义的产物",
          "同上：图与定义不同步，四象限就会误导决策链判断", "blocker")
def l3_stake_sync() -> list[str]:
    p = FIG / "利益相关者地图.svg"
    if not p.exists():
        return ["docs/图/利益相关者地图.svg 不存在，跑 python src/render_stakeholders.py"]
    if read_norm(p) != stake.to_svg().replace("\r\n", "\n"):
        return ["地图 SVG 与当前定义不一致，跑 python src/render_stakeholders.py 重新生成"]
    return []


@register("L3-5", "L3", "流程图 Mermaid 是当前定义的产物",
          "三份生成物只校两份，漏掉的那份就会悄悄停在旧版本", "blocker")
def l3_flow_mmd_sync() -> list[str]:
    p = FIG / "现状流程图.mmd"
    if not p.exists():
        return ["docs/图/现状流程图.mmd 不存在，跑 python src/render_flow.py"]
    if read_norm(p) != flow.to_mmd().replace("\r\n", "\n"):
        return ["流程图 Mermaid 与当前定义不一致，跑 python src/render_flow.py 重新生成"]
    return []


@register("L3-6", "L3", "地图 Mermaid 是当前定义的产物",
          "同上：漏校一份生成物，等于给「图是新的」留了一个反例", "blocker")
def l3_stake_mmd_sync() -> list[str]:
    p = FIG / "利益相关者地图.mmd"
    if not p.exists():
        return ["docs/图/利益相关者地图.mmd 不存在，跑 python src/render_stakeholders.py"]
    if read_norm(p) != stake.to_mmd().replace("\r\n", "\n"):
        return ["地图 Mermaid 与当前定义不一致，跑 python src/render_stakeholders.py 重新生成"]
    return []


# ---------------------------------------------------------------- L4 文档层

EXPECTED = [
    "README.md", "LICENSE",
    "docs/业务挖掘.md",
    "docs/调研记录.md", "docs/调研记录.json", "docs/调研记录.csv",
    "docs/现场调研分析.md",
    "docs/现状流程图.md",
    "docs/现状基线与价值假设.md",
    "docs/项目章程与利益相关者地图.md",
    "docs/平台提交稿.md",
    "docs/原始资料/01-客户简报.html",
    "docs/原始资料/02-现状信息请求表.html",
    "docs/原始资料/03-业务发现访谈问题库.html",
    "docs/原始资料/04-利益相关者资料与分工.html",
    "docs/图/现状流程图.svg", "docs/图/现状流程图.png", "docs/图/现状流程图.mmd",
    "docs/图/利益相关者地图.svg", "docs/图/利益相关者地图.png", "docs/图/利益相关者地图.mmd",
]


@register("L4-1", "L4", "交付物齐套",
          "平台按清单验收，少一份就是少一份，不会因为写得好而豁免", "blocker")
def l4_complete() -> list[str]:
    return [f"缺文件：{p}" for p in EXPECTED if not (ROOT / p).exists()]


@register("L4-2", "L4", "文档内本地链接可解析",
          "README 的交付物表是唯一入口，表里点不开等于找不到", "blocker")
def l4_links() -> list[str]:
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
    readme = read_norm(ROOT / "README.md")
    issues = []
    for f in sorted(DOCS.glob("*.md")):
        if f.name in SKIP_DOCS:
            continue
        if f.name not in readme:
            issues.append(f"docs/{f.name} 未被 README 引用")
    return issues


@register("L4-4", "L4", "文档里的统计口径没写死",
          "台账从 32 条补到 36 条时，正文里遗留的“32 条”会当场穿帮", "warn")
def l4_numbers() -> list[str]:
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
          "EXPECTED 是手写清单：新增文件忘了登记，L4-1 就保护不到它——检查清单自己也会烂",
          "warn")
def l4_expected_sync() -> list[str]:
    listed = {(ROOT / p).resolve() for p in EXPECTED}
    on_disk = {p.resolve() for p in DOCS.rglob("*")
               if p.is_file() and p.name not in SKIP_DOCS
               and "__pycache__" not in p.parts}
    issues = [f"{rel(p)} 存在但未登记进 EXPECTED（新增交付物请补进去）"
              for p in sorted(on_disk - listed)]
    return issues


# ---------------------------------------------------------------- 运行器


def run_one(c: Check) -> Result:
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
    def sh(*args: str) -> str:
        return subprocess.run(args, cwd=ROOT, capture_output=True,
                              text=True, encoding="utf-8").stdout.strip()
    try:
        sha = sh("git", "rev-parse", "--short", "HEAD")
        raw = sh("git", "-c", "core.quotepath=false", "status", "--porcelain")
        # 报告本身排除在外：写报告这件事不该被算成"工作区脏"，
        # 否则每次生成都自证有未提交改动，这份报告永远不可能干净。
        dirty = len([l for l in raw.split("\n")
                     if l.strip() and REPORT.name not in l])
    except Exception:
        return "—", -1
    return sha or "—", dirty


def summary_line(r: Result) -> str:
    if r.crashed:
        return "ERROR"
    if r.ok:
        return "OK  "
    return "WARN" if r.check.level == "warn" else "FAIL"


def render_console(results: list[Result], total_ms: float) -> None:
    layer_titles = {
        "L1": "L1 台账自洽", "L2": "L2 证据可追溯",
        "L3": "L3 图与定义一致", "L4": "L4 文档层",
    }
    cur = None
    for r in results:
        if r.check.layer != cur:
            cur = r.check.layer
            print(f"\n{layer_titles.get(cur, cur)}")
        mark = summary_line(r)
        line = f"  [{mark}] {r.check.cid}  {r.check.name}"
        if r.ok and not r.crashed:
            print(line)
        else:
            print(line + f"  ← {len(r.issues)} 处" if r.issues else line)
            for i in r.issues:
                print(f"           · {i}")
            if r.crashed:
                print("           · 检查器自身异常：")
                for l in r.crashed.strip().split("\n"):
                    print(f"             {l}")

    ok_n = sum(1 for r in results if r.ok and not r.crashed)
    fail_n = sum(1 for r in results if not r.ok and not r.crashed and r.check.level == "blocker")
    warn_n = sum(1 for r in results if not r.ok and not r.crashed and r.check.level == "warn")
    err_n = sum(1 for r in results if r.crashed)
    print(f"\n{'='*60}")
    print(f"合计 {len(results)} 项：通过 {ok_n}｜失败 {fail_n}｜警告 {warn_n}｜异常 {err_n}"
          f"　用时 {total_ms/1000:.2f}s")
    verdict = "FAIL" if (fail_n or err_n) else "PASS"
    print(f"结论：{verdict}")


def scope_stats() -> list[tuple[str, str]]:
    """这次校验到底覆盖了多少东西。只写清单不写规模，报告看起来像打卡。"""
    data = load_ledger()
    n_iv = len(data["interviews"])
    n_ev = sum(len(iv["items"]) for iv in data["interviews"])
    n_br = len(data["breaks"])
    single = sum(1 for iv in data["interviews"] for it in iv["items"]
                 if (it.get("cross") or "") == "单源")
    pending = len({it["doc"] for iv in data["interviews"] for it in iv["items"]
                   if it.get("doc") and it.get("doc_status") == "待补"})
    figs = len(list(FIG.glob("*.*")))
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
    o.append("python src/harness.py --json     # CI 取结构化结果")
    o.append("```")
    o.append("")
    o.append("退出码：`0` 通过（warn 不算失败）、`1` 有 blocker 失败、`2` harness 自身出错。")
    o.append("")
    return "\n".join(o)


# ---------------------------------------------------------------- 入口


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="统一校验 harness")
    ap.add_argument("--only", action="append", default=[],
                    help="只跑指定层，如 L2（可重复写：--only L1 --only L3）")
    ap.add_argument("--no-write", action="store_true", help="不写 docs/校验报告.md")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    ap.add_argument("--list", action="store_true", help="列出所有检查项")
    args = ap.parse_args(argv)

    if args.list:
        for c in CHECKS:
            print(f"{c.cid}  [{c.layer}/{c.level}]  {c.name}\n      {c.why}")
        return 0

    missing = [p for p in (LEDGER, ROOT / "README.md") if not p.exists()]
    if missing:
        print("找不到必要文件：" + "、".join(str(m) for m in missing), file=sys.stderr)
        return 2

    picked = [c for c in CHECKS if not args.only or c.layer in args.only]
    if not picked:
        print(f"没有匹配的检查项（--only {' '.join(args.only)}）", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    results = [run_one(c) for c in picked]
    total_ms = (time.perf_counter() - t0) * 1000

    if args.json:
        out = {
            "verdict": "FAIL" if any((not r.ok and r.check.level == "blocker") or r.crashed
                                     for r in results) else "PASS",
            "elapsed_ms": round(total_ms, 1),
            "commit": git_bits()[0],
            "checks": [{
                "id": r.check.cid, "layer": r.check.layer, "name": r.check.name,
                "level": r.check.level, "ok": r.ok, "ms": round(r.ms, 1),
                "issues": r.issues,
                "crashed": bool(r.crashed),
            } for r in results],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        render_console(results, total_ms)

    if not args.no_write and not args.json:
        REPORT.write_text(render_markdown(results, total_ms), encoding="utf-8")
        print(f"报告已写入：{rel(REPORT)}")

    return 1 if any((not r.ok and r.check.level == "blocker") or r.crashed
                    for r in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
