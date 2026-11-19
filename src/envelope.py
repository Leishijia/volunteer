"""对外接口的统一返回结构。

约定（全仓库一致，五个 CLI 与 `--json` 输出都走这里）：

    {"ok": bool, "data": any, "error": {"code": str, "msg": str} | None}

为什么统一
----------
改之前，三种入口各有一套返回方式：`build_ledger` 返回整数、`render_*` 返回
布尔式的 0/1、`harness --json` 自己拼了一个 `{verdict, checks}`。调用方
（CI、hook、以后可能有的平台集成）要分别适配三种形状，而且失败时只能看
退出码，拿不到"哪一类失败"。

现在只有一个形状、一套错误码。`ok=False` 时 `error.code` 可以程序判断，
`error.msg` 给人看；`data` 在失败时允许非空（例如"15 项通过、2 项失败"
这种部分结果，丢了反而是信息损失）。

错误码与退出码的对应也在这里定，不散落在各脚本里：
    VALIDATION_FAILED → 1（内容不合格，改内容）
    其它              → 2（环境/输入有问题，改环境）
"""
from __future__ import annotations

import json
import sys
from typing import Any

# 内容不合格：调用方应当修复被校验的对象，而不是重试
CONTENT_ERRORS = {"VALIDATION_FAILED"}


def ok(data: Any = None) -> dict[str, Any]:
    """构造成功响应。

    入参：data 为要返回的结果（可为 None）。
    出参：{"ok": True, "data": data, "error": None}。
    失败：不抛异常。
    """
    return {"ok": True, "data": data, "error": None}


def fail(code: str, msg: str, data: Any = None) -> dict[str, Any]:
    """构造失败响应。

    入参：code 为稳定错误码（见模块文档），msg 为给人看的说明，
         data 为可选的部分结果。
    出参：{"ok": False, "data": data, "error": {"code": code, "msg": msg}}。
    失败：不抛异常，也不因 code 不在已知集合里而拒绝——错误码先能造出来，
         是不是新码由使用方决定，这里不做白名单拦截。
    """
    return {"ok": False, "data": data, "error": {"code": code, "msg": msg}}


def from_exception(exc: BaseException) -> dict[str, Any]:
    """把异常转成失败响应。

    入参：任意异常。带 `code` 属性且带 `msg` 属性的（如 ConfigError）用它们，
         其余按 INTERNAL_ERROR 处理并保留异常类型名。
    出参：失败响应。
    失败：不抛异常——本函数存在的意义就是兜住异常。
    """
    code = getattr(exc, "code", None) or "INTERNAL_ERROR"
    msg = getattr(exc, "msg", None) or f"{type(exc).__name__}: {exc}"
    return fail(str(code), str(msg))


def exit_code(resp: dict[str, Any]) -> int:
    """把响应映射成进程退出码。

    入参：ok()/fail() 的返回值。
    出参：0 成功；1 内容不合格（可修复）；2 环境或自身出错。
         这个区分是给 CI 用的：1 说明交付物有毛病，2 说明跑不起来，
         两种红要修的地方不同，不该混成一个。
    失败：响应结构不对时返回 2（宁可按"环境问题"报，也不假装成功）。
    """
    if not isinstance(resp, dict) or "ok" not in resp:
        return 2
    if resp.get("ok"):
        return 0
    err = resp.get("error") or {}
    return 1 if err.get("code") in CONTENT_ERRORS else 2


def emit(resp: dict[str, Any], *, as_json: bool, text: str = "") -> None:
    """按需输出响应：JSON 给人以外的东西看，文本给人看。

    入参：resp 为统一结构；as_json=True 时打印 JSON（CI / 脚本消费）；
         text 为人类可读输出，为空时退回打印 JSON——绝不静默什么都不打，
         否则"成功但没输出"和"脚本根本没跑"无法区分。
    出参：无。
    失败：不捕获写入异常（管道被关等），那是进程层要处理的事，这里吞掉会掩盖真实问题。
    """
    if as_json or not text:
        print(json.dumps(resp, ensure_ascii=False, indent=2))
    else:
        print(text)
    sys.stdout.flush()
