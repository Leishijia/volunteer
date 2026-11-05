"""调研台账工具：把 docs/调研记录.json 编译成可导入表格的 CSV，并做一致性校验。

为什么要有这一步：
  访谈记录手写时最容易出的两个毛病——证据编号对不上、结论引用了没人说过的断点。
  这两件事靠眼睛查不住，靠脚本一秒钟就能查完。

用法：
    python src/build_ledger.py            # 校验 + 生成 docs/调研记录.csv
    python src/build_ledger.py --check    # 只校验（有问题返回非零退出码，可挂 CI）

校验项：
  1. 证据编号格式 `INT-0x-nn`，且所属访谈一致；
  2. 每条记录关联的断点必须在 breaks 表里存在；
  3. 每个断点至少有一条记录支撑（不允许"结论里有、记录里没有"）；
  4. 证据性质只能是 事实陈述 / 判断 / 待核；
  5. 凭证状态只能是 待补 / 已取得 / 不适用；
  6. 标了凭证编号的条目，凭证状态不能是"不适用"。
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "调研记录.json"
OUT = ROOT / "docs" / "调研记录.csv"

ID_RE = re.compile(r"^(INT-\d{2})-(\d{2})$")
TYPES = {"事实陈述", "判断", "待核"}
DOC_STATUS = {"待补", "已取得", "不适用"}

COLUMNS = [
    "证据编号", "访谈编号", "访谈对象", "角色", "核对重点",
    "条目主题", "陈述要点", "原话摘录", "证据性质",
    "关联断点", "原始凭证", "凭证状态", "交叉核对", "差异/冲突",
]


def load() -> dict:
    return json.loads(SRC.read_text(encoding="utf-8"))


def validate(data: dict) -> tuple[list[str], list[dict]]:
    errors: list[str] = []
    breaks = {b["id"] for b in data["breaks"]}
    rows: list[dict] = []

    for iv in data["interviews"]:
        iv_id = iv["id"]
        if not re.match(r"^INT-\d{2}$", iv_id):
            errors.append(f"访谈编号格式不对：{iv_id}")
        for item in iv["items"]:
            m = ID_RE.match(item["id"])
            if not m:
                errors.append(f"证据编号格式不对：{item['id']}")
            elif m.group(1) != iv_id:
                errors.append(f"{item['id']} 不属于访谈 {iv_id}")

            if item["type"] not in TYPES:
                errors.append(f"{item['id']} 证据性质不合法：{item['type']}")
            if item["doc_status"] not in DOC_STATUS:
                errors.append(f"{item['id']} 凭证状态不合法：{item['doc_status']}")
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

    # 3. 断点覆盖：每个断点至少一条记录
    used = Counter(b for iv in data["interviews"] for i in iv["items"] for b in i.get("breaks", []))
    for b in sorted(breaks):
        if used[b] == 0:
            errors.append(f"断点 {b} 没有任何访谈记录支撑（结论不能凭空出现）")
    return errors, rows


def write_csv(rows: list[dict]) -> None:
    # utf-8-sig：Excel 双击打开不乱码
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def report(data: dict, rows: list[dict]) -> None:
    print(f"记录总数：{len(rows)}　访谈：{len(data['interviews'])}　断点：{len(data['breaks'])}")
    print("\n按角色：")
    for iv in data["interviews"]:
        print(f"  {iv['id']} {iv['who']}（{iv['role']}）：{len(iv['items'])} 条")
    print("\n按证据性质：")
    for k, v in Counter(r["证据性质"] for r in rows).most_common():
        print(f"  {k}：{v}")
    print("\n按断点支撑数：")
    cnt = Counter(b for r in rows for b in r["关联断点"].split() if b != "—")
    for b in data["breaks"]:
        print(f"  {b['id']} {b['name']}：{cnt[b['id']]} 条")
    pending = sorted({r["原始凭证"] for r in rows if r["凭证状态"] == "待补" and r["原始凭证"] != "—"})
    print("\n待补凭证：" + ("、".join(pending) if pending else "无"))
    single = [r["证据编号"] for r in rows if r["交叉核对"] == "单源"]
    print(f"单源陈述：{len(single)} 条（不进结论，需交叉或凭证）")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="校验调研记录并生成台账 CSV")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args(argv)

    if not SRC.exists():
        print(f"找不到 {SRC}", file=sys.stderr)
        return 2
    data = load()
    errors, rows = validate(data)
    if errors:
        for e in errors:
            print("ERROR:", e)
        return 1
    print("校验通过")
    report(data, rows)
    if not args.check:
        write_csv(rows)
        print(f"\n已生成：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
