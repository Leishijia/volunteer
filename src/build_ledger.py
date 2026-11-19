"""调研台账工具：把 docs/调研记录.json 编译成可导入表格的 CSV，并做一致性校验。

为什么要有这一步：
  访谈记录手写时最容易出的两个毛病——证据编号对不上、结论引用了没人说过的断点。
  这两件事靠眼睛查不住，靠脚本一秒钟就能查完。

用法：
    python src/build_ledger.py            # 校验 + 生成 docs/调研记录.csv
    python src/build_ledger.py --check    # 只校验，不生成
    python src/build_ledger.py --json     # 输出统一结构 {"ok","data","error"}

校验项：
  1. 证据编号格式 `INT-0x-nn`，且所属访谈一致；
  2. 每条记录关联的断点必须在 breaks 表里存在；
  3. 每个断点至少有一条记录支撑（不允许"结论里有、记录里没有"）；
  4. 证据性质只能是 事实陈述 / 判断 / 待核；
  5. 凭证状态只能是 待补 / 已取得 / 不适用；
  6. 标了凭证编号的条目，凭证状态不能是"不适用"。

路径、列名、枚举值全部来自 config.yaml，代码里不再出现硬编码。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import envelope
import settings

# 配置在**函数调用时**才读，不在 import 时读。
# 原因：import 期读配置会把"config.yaml 没配好"变成 import 阶段就炸的异常——
# 那时 main() 里的 try 还没建立，使用者只能看到 traceback。延迟到调用点，
# 才能把它收进统一返回结构，报出 CONFIG_INVALID / FONT_MISSING 这种可读的错误码。
_CFG_CACHE: dict[str, Any] = {}


def columns() -> list[str]:
    """台账列名（顺序即 CSV 列顺序）。

    入参：无。
    出参：列名字符串列表，顺序固定来自 config.yaml。
    失败：配置缺 ledger.columns 时抛 settings.ConfigError。
         列顺序一变，已生成的 CSV 立刻过期，由 harness 的 L1-3 抓到。
    """
    if "columns" not in _CFG_CACHE:
        _CFG_CACHE["columns"] = [str(c) for c in settings.require("ledger.columns")]
    return _CFG_CACHE["columns"]


def types() -> set[str]:
    """合法的证据性质集合。

    入参：无。
    出参：集合，如 {"事实陈述","判断","待核"}。
    失败：配置缺 ledger.types 时抛 settings.ConfigError。
    """
    if "types" not in _CFG_CACHE:
        _CFG_CACHE["types"] = {str(t) for t in settings.require("ledger.types")}
    return _CFG_CACHE["types"]


def doc_statuses() -> set[str]:
    """合法的凭证状态集合。

    入参：无。
    出参：集合，如 {"待补","已取得","不适用"}。
    失败：配置缺 ledger.doc_status 时抛 settings.ConfigError。
    """
    if "doc_status" not in _CFG_CACHE:
        _CFG_CACHE["doc_status"] = {str(s) for s in settings.require("ledger.doc_status")}
    return _CFG_CACHE["doc_status"]


def id_re() -> re.Pattern[str]:
    """证据编号的匹配式（含"访谈前缀"分组）。

    入参：无。
    出参：编译好的正则，group(1) 为 INT-0x，group(2) 为序号。
    失败：配置缺 ledger.id_pattern 抛 settings.ConfigError；正则本身非法抛 re.error。
    """
    if "id_re" not in _CFG_CACHE:
        _CFG_CACHE["id_re"] = re.compile(str(settings.require("ledger.id_pattern")))
    return _CFG_CACHE["id_re"]


def interview_re() -> re.Pattern[str]:
    """访谈编号的匹配式。

    入参：无。
    出参：编译好的正则。
    失败：配置缺 ledger.interview_pattern 时抛 settings.ConfigError。
    """
    if "interview_re" not in _CFG_CACHE:
        _CFG_CACHE["interview_re"] = re.compile(str(settings.require("ledger.interview_pattern")))
    return _CFG_CACHE["interview_re"]


def __getattr__(name: str) -> Any:
    """兼容 `ledger.COLUMNS` 这种旧写法。

    入参：模块级属性名。
    出参：COLUMNS 返回 columns() 的结果。
    失败：其它属性名抛 AttributeError（Python 的正常语义）。

    为什么保留：harness 按列名定位 CSV 差异时用 COLUMNS 读最自然；
    但列名现在来自配置，只能延迟取，不能是 import 期常量。
    """
    if name == "COLUMNS":
        return columns()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def ledger_path() -> Path:
    """台账 JSON 的位置。

    入参：无。
    出参：绝对路径（不保证存在）。
    失败：配置缺 paths.ledger 时抛 settings.ConfigError。
    """
    return settings.path("ledger")


def csv_path() -> Path:
    """台账 CSV 的位置。

    入参：无。
    出参：绝对路径（不保证存在）。
    失败：配置缺 paths.ledger_csv 时抛 settings.ConfigError。
    """
    return settings.path("ledger_csv")


def load_data(path: Path | None = None) -> dict[str, Any]:
    """读取台账 JSON 原文。

    入参：path 为台账路径，省略时取 config.yaml 的 paths.ledger。
    出参：解析后的字典，至少含 interviews 与 breaks 两个键。
    失败：文件不存在抛 FileNotFoundError；内容不是合法 JSON 抛 json.JSONDecodeError。
         两者都交给上层转成统一返回结构——这里不吞、也不改写异常类型，
         否则"文件没找到"会被说成"格式错误"，误导排查方向。
    """
    p = path or ledger_path()
    return json.loads(p.read_text(encoding="utf-8"))


def validate(data: dict[str, Any]) -> tuple[list[str], list[dict[str, str]]]:
    """校验台账并展开成 CSV 行。

    入参：data 为 load_data() 的结果。
    出参：(errors, rows)。errors 为空列表表示通过；rows 是固定列顺序的扁平记录。
    失败：不抛异常——所有问题都收集进 errors 一次性返回。为什么不在第一条就停：
         台账是人手写的，一次只报一个错会让人来回跑十几遍。
    """
    errors: list[str] = []
    breaks = {b["id"] for b in data["breaks"]}
    rows: list[dict[str, str]] = []

    for iv in data["interviews"]:
        iv_id = iv["id"]
        if not interview_re().match(iv_id):
            errors.append(f"访谈编号格式不对：{iv_id}")
        for item in iv["items"]:
            m = id_re().match(item["id"])
            # 编号前缀必须等于所属访谈编号：编号是交叉引用的唯一锚点，
            # 一旦 INT-03-05 被放进访谈 04 里，按编号检索会指向错误的人。
            if not m:
                errors.append(f"证据编号格式不对：{item['id']}")
            elif m.group(1) != iv_id:
                errors.append(f"{item['id']} 不属于访谈 {iv_id}")

            if item["type"] not in types():
                errors.append(f"{item['id']} 证据性质不合法：{item['type']}")
            if item["doc_status"] not in doc_statuses():
                errors.append(f"{item['id']} 凭证状态不合法：{item['doc_status']}")
            # 填了凭证编号却写"不适用"，等于嘴上说不需要、手上又留了凭据线索。
            # 这种自相矛盾会让取证计划里出现一项谁也不知道该不该追的凭证。
            if item.get("doc") and item["doc_status"] == "不适用":
                errors.append(f"{item['id']} 标了凭证 {item['doc']} 却写'不适用'")

            for b in item.get("breaks", []):
                if b not in breaks:
                    errors.append(f"{item['id']} 引用了不存在的断点：{b}")

            rows.append({
                "证据编号": item["id"],
                "访谈编号": iv_id,
                "访谈对象": iv["who"],
                "角色": iv["role"],
                "核对重点": iv["focus"],
                "条目主题": item["topic"],
                "陈述要点": item["statement"],
                "原话摘录": item["quote"],
                "证据性质": item["type"],
                "关联断点": " ".join(item.get("breaks", [])) or "—",
                "原始凭证": item.get("doc") or "—",
                "凭证状态": item["doc_status"],
                "交叉核对": item.get("cross") or "—",
                "差异/冲突": item.get("conflict") or "—",
            })

    # 断点覆盖：每个断点至少一条记录。
    # 这条卡的是最危险的一类错误——结论里写"断点 B5 造成返工"，但访谈里
    # 没有一个人说过这件事。结论会显得很有依据，实际是凭空长出来的。
    used = Counter(b for iv in data["interviews"] for i in iv["items"] for b in i.get("breaks", []))
    for b in sorted(breaks):
        if used[b] == 0:
            errors.append(f"断点 {b} 没有任何访谈记录支撑（结论不能凭空出现）")
    return errors, rows


def write_csv(rows: list[dict[str, str]], path: Path | None = None) -> int:
    """把台账行写成 CSV。

    入参：rows 为 validate() 产出的行；path 省略时取配置里的 CSV 路径。
    出参：写入的数据行数（不含表头）。
    失败：目录不存在抛 FileNotFoundError；磁盘写入错误原样抛出。
         不做"自动建目录"——目录结构是仓库约定，静默创建会掩盖配置写错。
    """
    p = path or csv_path()
    # utf-8-sig：带 BOM，Excel 双击打开不乱码；newline="" 交给 csv 模块
    # 自己写行尾（CRLF），否则 Windows 上会出现空行。
    with p.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns())
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def summarize(data: dict[str, Any], rows: list[dict[str, str]]) -> dict[str, Any]:
    """把台账压成可用于展示/机读的统计。

    入参：data 为台账原文，rows 为 validate() 的展开结果。
    出参：字典，含记录数、访谈数、断点数、按角色/性质/断点的分布、
         待补凭证清单、单源陈述条数。它同时是统一结构里 data 的主体，
         所以只放数据、不做格式化——格式化在 render_text()。
    失败：字段缺失会抛 KeyError，属台账结构问题，由 L1-1 先拦住。
    """
    by_role = [
        {
            "访谈编号": iv["id"],
            "访谈对象": iv["who"],
            "角色": iv["role"],
            "条数": len(iv["items"]),
        }
        for iv in data["interviews"]
    ]
    by_type = dict(Counter(r["证据性质"] for r in rows).most_common())
    break_cnt = Counter(b for r in rows for b in r["关联断点"].split() if b != "—")
    by_break = [
        {"断点": b["id"], "名称": b["name"], "支撑条数": break_cnt[b["id"]]}
        for b in data["breaks"]
    ]
    pending = sorted({r["原始凭证"] for r in rows
                      if r["凭证状态"] == "待补" and r["原始凭证"] != "—"})
    single = [r["证据编号"] for r in rows if r["交叉核对"] == "单源"]

    return {
        "records": len(rows),
        "interviews": len(data["interviews"]),
        "breaks": len(data["breaks"]),
        "by_role": by_role,
        "by_type": by_type,
        "by_break": by_break,
        "pending_docs": pending,
        "single_source": len(single),
        "single_source_ids": single,
    }


def render_text(stats: dict[str, Any], written: Path | None) -> str:
    """把统计渲染成人读的文本。

    入参：stats 为 summarize() 的结果；written 为实际写出的 CSV 路径，
         None 表示本次只校验、未生成。
    出参：多行字符串，结构与改造前逐行一致（便于人工对照历史记录）。
    失败：stats 结构不符会抛 KeyError——它是本模块自己造的，不会来自外部输入。
    """
    o: list[str] = []
    o.append(f"记录总数：{stats['records']}　访谈：{stats['interviews']}"
             f"　断点：{stats['breaks']}")
    o.append("\n按角色：")
    for r in stats["by_role"]:
        o.append(f"  {r['访谈编号']} {r['访谈对象']}（{r['角色']}）：{r['条数']} 条")
    o.append("\n按证据性质：")
    for k, v in stats["by_type"].items():
        o.append(f"  {k}：{v}")
    o.append("\n按断点支撑数：")
    for b in stats["by_break"]:
        o.append(f"  {b['断点']} {b['名称']}：{b['支撑条数']} 条")
    pend = stats["pending_docs"]
    o.append("\n待补凭证：" + ("、".join(pend) if pend else "无"))
    o.append(f"单源陈述：{stats['single_source']} 条（不进结论，需交叉或凭证）")
    if written is not None:
        o.append(f"\n已生成：{written}")
    else:
        # 说明本入口只是单点自检：它查不到"文档引用了不存在的编号"，
        # 也不判"图是不是最新"。交付判定只有 harness 一个入口。
        o.append("\n（单点自检：只管台账自身，不查文档引用与生成物是否过期；"
                 "交付判定请跑 python src/harness.py）")
    return "\n".join(o)


def run(check_only: bool = False) -> dict[str, Any]:
    """执行一次完整流程，返回统一结构。

    入参：check_only=True 时只校验不写 CSV。
    出参：{"ok","data","error"}。data 含 stats 与 csv 路径；校验不通过时
         ok=False、error.code=VALIDATION_FAILED，data.errors 里带明细。
    失败：台账缺失、JSON 坏了、配置缺失都不抛，转成 ok=False 的响应；
         只有编程错误会向上冒。
    """
    src = ledger_path()
    if not src.exists():
        return envelope.fail("INPUT_MISSING", f"找不到台账文件：{src}")

    try:
        data = load_data(src)
    except json.JSONDecodeError as exc:
        return envelope.fail("INPUT_INVALID", f"台账不是合法 JSON：{exc}")

    errors, rows = validate(data)
    if errors:
        # 不在这里 print：--json 模式下任何多余输出都会污染 stdout，
        # 让调用方解析失败。错误明细放进 data.errors，由 main() 决定怎么显示。
        # 同时带上 records 数量：即使失败也能看出台账规模是否正常（0 条说明读错了文件）。
        return envelope.fail("VALIDATION_FAILED", f"{len(errors)} 处不一致",
                             data={"errors": errors, "records": len(rows)})

    stats = summarize(data, rows)
    written: Path | None = None
    if not check_only:
        written = csv_path()
        write_csv(rows, written)

    return envelope.ok({"stats": stats, "csv": str(written) if written else None,
                        "check_only": check_only})


def main(argv: list[str]) -> int:
    """命令行入口。

    入参：argv 为去掉程序名后的参数列表。
    出参：进程退出码，由 envelope.exit_code() 统一决定（0/1/2）。
    失败：配置或输入问题不抛异常，转成 ok=False 的响应并返回非零码，
         保证 CI 与 hook 拿到的是稳定退出码而不是 traceback。
    """
    ap = argparse.ArgumentParser(description="校验调研记录并生成台账 CSV")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    ap.add_argument("--json", action="store_true", help="输出统一结构 JSON")
    args = ap.parse_args(argv)

    try:
        resp = run(check_only=args.check)
    except settings.ConfigError as exc:
        resp = envelope.from_exception(exc)

    text = ""
    if resp["ok"]:
        text = "校验通过\n" + render_text(
            resp["data"]["stats"],
            Path(resp["data"]["csv"]) if resp["data"]["csv"] else None,
        )
    elif resp["error"]["code"] == "VALIDATION_FAILED":
        # 逐条列出，而不是只说"3 处不一致"——那等于让人自己去猜哪三处。
        errs = (resp.get("data") or {}).get("errors", [])
        text = "\n".join([f"校验未通过：{resp['error']['msg']}"]
                         + [f"  · {e}" for e in errs])

    envelope.emit(resp, as_json=args.json, text=text)
    return envelope.exit_code(resp)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
