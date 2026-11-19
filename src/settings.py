"""配置装载：全项目路径、字体、交付清单的唯一入口。

为什么要单独一层
----------------
改动前，`docs/`、`docs/图`、`调研记录.json`、`C:/Windows/Fonts/msyh.ttc`
散落在四个脚本里各写一遍。这类硬编码有两个后果：

  1. 换台机器（Linux CI、别人的 Mac）字体路径直接不存在，而报错是 PIL 的
     `OSError: cannot open resource`，看不出该改哪里；
  2. 想拿这套脚本做另一个案例，得改四个文件、还得保证四处同步。

现在所有环境相关与项目身份相关的值只在 `config.yaml` 里出现一次。
读配置失败一律抛 `ConfigError`，带上稳定的错误码，由上层转成统一返回结构——
不让"配置缺失"退化成某个路径悄悄指向别处（那种错误通常到交付前夜才暴露）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# 仓库根目录：本文件在 src/ 下，往上一级
ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.yaml"

# 进程内缓存。四个脚本各自 import 本模块时共用同一份，不会重复解析 YAML。
_CACHE: dict[str, Any] | None = None


class ConfigError(RuntimeError):
    """配置缺失、格式不对，或依赖没装。

    code 是对外统一结构里的 `error.code`，取值：
      - CONFIG_MISSING      config.yaml 找不到
      - CONFIG_INVALID      YAML 读不了 / 顶层不是映射 / 缺必填键
      - DEPENDENCY_MISSING  没装 PyYAML
      - FONT_MISSING        字体候选一个都不存在
    """

    def __init__(self, code: str, msg: str) -> None:
        """记录错误码与人读的消息。

        入参：code 见类文档；msg 要能直接告诉使用者"下一步改哪里"。
        出参：无。
        失败：不抛异常（它自己就是异常类型）。
        """
        super().__init__(msg)
        self.code = code
        self.msg = msg


def load(force: bool = False) -> dict[str, Any]:
    """读入并缓存 config.yaml。

    入参：force=True 时忽略缓存重新解析（改完配置不想重启进程时用）。
    出参：整份配置字典，键为 project/paths/ledger/fonts/figures/harness。
    失败：抛 ConfigError。缺文件 CONFIG_MISSING；没装 PyYAML 抛 DEPENDENCY_MISSING；
         YAML 语法错误或顶层不是映射抛 CONFIG_INVALID——不做"尽力猜测"，
         因为猜错的一半配置比没有配置更难查。
    """
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE

    if not CONFIG_FILE.exists():
        raise ConfigError("CONFIG_MISSING", f"找不到配置文件：{CONFIG_FILE}")

    try:
        import yaml  # 延迟导入：没装依赖时要报成配置错误，而不是 ImportError 崩栈
    except ImportError as exc:
        # 用 ImportError 而不是 ModuleNotFoundError：前者是后者的父类，
        # 也能接住"yaml 装了但依赖链坏了"的情况——对使用者是同一件事：去装依赖。
        raise ConfigError(
            "DEPENDENCY_MISSING",
            "缺少依赖 PyYAML，请执行 pip install -r requirements.txt",
        ) from exc

    try:
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("CONFIG_INVALID", f"config.yaml 解析失败：{exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("CONFIG_INVALID", "config.yaml 顶层必须是映射（key: value）")

    _CACHE = data
    return data


def get(dotted: str, default: Any = None) -> Any:
    """按点号路径取配置值。

    入参：dotted 形如 "ledger.types"；default 为取不到时的返回值。
    出参：命中的值，或 default。
    失败：不抛异常——调用方要"必须有"时用 `require()`，两者刻意分开，
         避免把"可选配置"也写成一堆 try。
    """
    cur: Any = load()
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def require(dotted: str) -> Any:
    """按点号路径取配置值，取不到即视为配置错误。

    入参：dotted 形如 "paths.ledger"。
    出参：命中的值。
    失败：抛 ConfigError("CONFIG_INVALID")，消息里带出缺失的键名。
    """
    sentinel = object()
    val = get(dotted, sentinel)
    if val is sentinel or val is None:
        raise ConfigError("CONFIG_INVALID", f"config.yaml 缺少必填项：{dotted}")
    return val


def path(key: str) -> Path:
    """取 paths.<key> 并解析成绝对路径。

    入参：key 为 paths 下的键名（readme/docs/figures/ledger/ledger_csv/report）。
    出参：绝对 Path。**不保证文件存在**——存在性由调用方判断，
         因为"该有但没有"和"路径写错"要报不同的错。
    失败：键缺失或为空抛 ConfigError("CONFIG_INVALID")。
    """
    raw = require(f"paths.{key}")
    p = Path(str(raw))
    return p if p.is_absolute() else (ROOT / p)


def figure(key: str) -> tuple[str, Path, Path, Path]:
    """取某张图的输出位置。

    入参：key 为 figures 下的键名（flow/stakeholders）。
    出参：(文件名主干, svg 路径, png 路径, mmd 路径)。三种格式同一个主干名，
         避免出现 现状流程图.svg / 现状图.png 这种对不上的组合。
    失败：键缺失抛 ConfigError("CONFIG_INVALID")。
    """
    base = str(require(f"figures.{key}.base"))
    d = path("figures")
    return base, d / f"{base}.svg", d / f"{base}.png", d / f"{base}.mmd"


def font_path() -> Path:
    """返回第一个真实存在的字体文件，供 PNG 渲染。

    入参：无（候选列表在 config.yaml 的 fonts.candidates）。
    出参：存在的字体绝对路径。
    失败：全部候选都不存在时抛 ConfigError("FONT_MISSING")，并把尝试过的路径
         原样列进消息——宁可让使用者照着消息改一行配置，也不要静默退化：
         静默换字体的后果是中文全成方块，而所有自动校验都发现不了。
    """
    cands = get("fonts.candidates") or []
    tried: list[str] = []
    for c in cands:
        p = Path(str(c))
        tried.append(str(p))
        if p.exists():
            return p
    raise ConfigError(
        "FONT_MISSING",
        "找不到可用字体，PNG 无法生成。请在 config.yaml 的 fonts.candidates 里"
        "补一个本机存在的字体路径。已尝试：" + "；".join(tried),
    )


def deliverables() -> list[str]:
    """交付清单（相对仓库根）。

    入参：无。
    出参：字符串列表，顺序即配置顺序。
    失败：缺失或为空抛 ConfigError("CONFIG_INVALID")——空清单会让 L4-1
         变成"永远通过"，等于把验收门拆了。
    """
    items = require("harness.deliverables")
    if not isinstance(items, list) or not items:
        raise ConfigError("CONFIG_INVALID", "harness.deliverables 必须是非空列表")
    return [str(i) for i in items]


def skip_docs() -> set[str]:
    """不参与文档扫描的文件名集合。

    入参：无。
    出参：文件名（不含目录）集合；未配置时返回空集合。
    失败：类型不对时抛 ConfigError("CONFIG_INVALID")。
    """
    items = get("harness.skip_docs") or []
    if not isinstance(items, list):
        raise ConfigError("CONFIG_INVALID", "harness.skip_docs 必须是列表")
    return {str(i) for i in items}
