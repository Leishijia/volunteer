"""一键发布：重建站点 → 全量校验 → 提交 → 推送。

为什么要有这个脚本
------------------
"上线"实际是两件不同步的事：**云上那份页面**（评审者打开看到的）和**仓库里
那份代码**（CI 与接手的人看到的）。分成两步手做就一定漂：

- 重新生成了站点，但忘了提交 → 线上是新的，仓库还是旧的，CI 全绿；
- 提交了代码，但没重新生成 → 仓库是新的，线上还是旧结论，本地也全绿。

这两种状态在本地都不会报错，只有下一个人打开链接/拉代码时才发现。所以把
"生成 → 校验 → 提交 → 推送"串成一条命令，任何一步失败就停在那儿，不留
"提交了一半、推了一半"的中间态。

为什么不在这个脚本里做云发布
----------------------------
云发布走的是本机的发布通道（不在仓库里，也没有可以提交的凭证文件）。脚本
硬要模拟它，等于把认证问题塞进一个本该只做确定性事情的仓库里。所以分工是：
**云发布由发布通道完成，这个脚本负责"仓库这份和站点一致，并且推到远端"**。

`--check` 只回答一个问题：磁盘上的站点是不是当前台账的产物。它**不跑
harness**——harness 的 L5-3 会反过来调用本脚本的 `--check`，那样会自我递归。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import build_site
import envelope
import settings

# 仓库根目录。src/ 的上一级；不写死绝对路径，换机器克隆到哪儿都能跑。
ROOT = Path(__file__).resolve().parent.parent

# 默认提交信息。带"发布"前缀，是为了让人在 log 里一眼分出"内容改动"与
# "只是把生成物同步了一遍"——后者不该被当成一次调研进展。
DEFAULT_MESSAGE = "chore(发布): 重建站点并同步生成物"


class PublishError(RuntimeError):
    """发布过程中的可预期失败：站点过期、git 不可用、推送被拒。

    带 code/msg 是为了让 `envelope.from_exception` 直接转成统一结构，
    不必在 main 里为每种失败写一个分支。
    """

    def __init__(self, code: str, msg: str) -> None:
        """记录错误码与人读的消息。

        入参：code 取 VALIDATION_FAILED（内容问题）或 GIT_FAILED / PUSH_FAILED
             （环境问题）；msg 要说清下一步该做什么。
        出参：无。
        失败：不抛异常（它自己就是异常类型）。
        """
        super().__init__(msg)
        self.code = code
        self.msg = msg


# 单个 git 子命令的上限。推送要留足时间：Windows 上凭据管理器
# （git-credential-manager）首次取凭据实测要 30 秒以上，把超时设短会误报失败。
GIT_TIMEOUT = 300


def git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """在仓库根目录执行一条 git 命令。

    入参：args 为 git 子命令与参数（如 "add", "-A"）；timeout 为秒数上限。
    出参：CompletedProcess，含 returncode/stdout/stderr。
    失败：git 不存在抛 PublishError(GIT_FAILED)；超时也抛 GIT_FAILED——
         脚本绝不能挂在等待输入上。

    为什么 stdin 指向 DEVNULL：第一次写成继承父进程 stdin，`git push` 在
    凭据管理器要交互时直接无限等待，进程挂了 5 分钟没有任何输出。禁掉 stdin
    之后，需要人处理的认证会立刻失败并报出来，而不是静默卡住。
    """
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", stdin=subprocess.DEVNULL, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PublishError("GIT_FAILED", "找不到 git 命令，请先安装或加入 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PublishError(
            "GIT_FAILED",
            f"git {' '.join(args)} 超过 {timeout}s 没结束，已放弃。"
            "检查网络与凭据管理器（可能需要先在终端里手动登录一次）。",
        ) from exc


def git_or_fail(*args: str) -> str:
    """执行 git 命令，失败就抛异常，成功返回 stdout。

    入参：args 同 git()。
    出参：标准输出（已去掉首尾空白）。
    失败：非零退出抛 PublishError(GIT_FAILED)，消息里带上 git 自己的 stderr——
         把 "! [rejected]" 换成人话只会丢掉排查线索。
    """
    proc = git(*args)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"退出码 {proc.returncode}"
        raise PublishError("GIT_FAILED", f"git {' '.join(args)} 失败：{detail}")
    return proc.stdout.strip()


def expected_html() -> str:
    """按当前台账算出"站点应有"的完整 HTML（不写盘）。

    入参：无。
    出参：整页 HTML 文本。
    失败：台账缺失/损坏抛 build_site.BuildError，配置问题抛 settings.ConfigError，
         两者都由 main 统一转成结构——这里不吞异常，否则"过期"和"读不到数据"
         会被报成同一件事。
    """
    data = build_site.load_ledger()
    return build_site.render_page(build_site.collect(data))


def site_is_current() -> tuple[bool, str]:
    """判断磁盘上的站点是不是当前台账的产物。

    入参：无。
    出参：(是否最新, 说明)。说明里带行号与两侧片段，或"文件不存在"。
    失败：读不到台账时由 expected_html() 抛异常（INPUT_MISSING 等），本函数不吞——
         因为"数据读不到"和"站点过期"要修的是两件事，混成一句会误导。

    为什么逐行比而不是只比指纹：指纹只回答"不一样"，不回答"哪一行不一样"。
    报错的下一步应该是"去改那一行"，而不是"再猜一次"。
    """
    index = settings.path("site") / "index.html"
    if not index.exists():
        return False, f"站点文件不存在：{index}"
    want = expected_html()
    have = index.read_text(encoding="utf-8")
    if have == want:
        return True, "站点是最新产物"
    a, b = have.splitlines(), want.splitlines()
    for no, (line_a, line_b) in enumerate(zip(a, b), start=1):
        if line_a != line_b:
            left, right = build_site_diff(line_a, line_b)
            return False, f"第 {no} 行已过期（磁盘：{left} ｜ 应为：{right}）"
    return False, f"行数不一致（磁盘 {len(a)} 行，应为 {len(b)} 行）"


def build_site_diff(a: str, b: str, ctx: int = 12, width: int = 40) -> tuple[str, str]:
    """从两行的第一个差异处截片段，供"已过期"的报错使用。

    入参：a 磁盘上的行，b 应有的行；ctx 差异点前保留的字符数；width 往后截的长度。
    出参：(磁盘片段, 应有片段)。
    失败：不抛异常；两行相同时返回两段空串（调用方只在不等时调用）。

    为什么不直接 `line[:60]`：页面前 60 个字符往往是同一段模板前缀，截断后
    报错会变成"磁盘是 X，应为 X"，读的人还是得自己跑一次 diff。
    """
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    start = max(0, i - ctx)
    return a[start:i + width], b[start:i + width]


def changed_files() -> list[str]:
    """列出工作区里所有改动（含未跟踪文件）。

    入参：无。
    出参：相对仓库根目录的路径列表；干净时为空列表。
    失败：git 失败抛 PublishError(GIT_FAILED)。

    为什么不用 git_or_fail：它对整段输出做了 strip，会把**第一行的状态位前
    那个空格**吃掉，于是 " M .gitignore" 变成 "M .gitignore"，再按第 4 个
    字符切就切出了 "gitignore"。这里只 rstrip 行尾换行。
    """
    proc = git("status", "--porcelain")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"退出码 {proc.returncode}"
        raise PublishError("GIT_FAILED", f"git status 失败：{detail}")
    # 改名行形如 "R  old -> new"，只关心最终路径；也顺带去掉 git 对特殊
    # 文件名的双引号包裹（中文名在 porcelain 里不会被引，但空格会）。
    files = []
    for line in proc.stdout.rstrip("\r\n").splitlines():
        if not line.strip():
            continue
        path = line[3:].split(" -> ")[-1].strip()
        files.append(path.strip('"'))
    return files


def run_harness() -> dict[str, Any]:
    """以子进程跑一次全量校验，取回结果。

    入参：无。
    出参：harness `--json` 的 data 段（含 verdict/checks），另附 passed/failed 计数。
    失败：harness 报红时抛 PublishError(VALIDATION_FAILED)——发布流程必须在这里
         停住；输出不是 JSON 时抛 HARNESS_FAILED（那是环境问题，不是内容问题）。
    """
    proc = subprocess.run(
        [sys.executable, str(ROOT / "src" / "harness.py"), "--json"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    try:
        resp = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PublishError(
            "HARNESS_FAILED",
            f"harness 输出不是合法 JSON（退出码 {proc.returncode}）："
            f"{(proc.stderr or proc.stdout).strip()[:200]}",
        ) from exc
    if not resp.get("ok"):
        err = resp.get("error") or {}
        raise PublishError("VALIDATION_FAILED",
                           f"校验未通过，已停止上传：{err.get('msg', '')}")
    data = resp.get("data") or {}
    checks = data.get("checks") or []
    passed = sum(1 for c in checks if c.get("ok"))
    return {"verdict": data.get("verdict", "PASS"), "passed": passed,
            "failed": len(checks) - passed, "total": len(checks)}


def commit_and_push(message: str) -> dict[str, Any]:
    """把当前改动提交并推送到远端同名分支。

    入参：message 为提交信息（为空时用 DEFAULT_MESSAGE）。
    出参：含 committed/pushed/commit/files 的字典。
    失败：无改动时 committed=False（这不是失败）；提交或推送失败抛 PublishError。

    为什么推送前不 pull：这个仓库的发布动作由单一发布者触发，pull 会把"远端
    有新提交"这种需要人判断的冲突直接变成一次静默合并。宁可让 push 被拒，
    报出来让人看一眼。
    """
    files = changed_files()
    if not files:
        return {"committed": False, "pushed": False, "commit": "", "files": []}

    git_or_fail("add", "-A")
    git_or_fail("commit", "-m", message or DEFAULT_MESSAGE)
    sha = git_or_fail("rev-parse", "--short", "HEAD")

    # 推送单独兜一层：走到这里提交一定已经写成，报错必须带上这个事实，
    # 否则使用者会以为"没推上去＝什么都没发生"，然后重跑一次生成第二次提交。
    try:
        push = git("push", timeout=GIT_TIMEOUT)
    except PublishError as exc:
        raise PublishError(
            "PUSH_FAILED",
            f"已提交 {sha}，但推送未完成：{exc.msg}。本地提交保留，"
            "处理好网络/凭据后直接重跑一次 git push 即可，不必重新发布。",
        ) from exc
    if push.returncode != 0:
        detail = (push.stderr or push.stdout).strip() or "未知原因"
        raise PublishError("PUSH_FAILED",
                           f"已提交 {sha} 但推送失败：{detail}{tls_hint(detail)}"
                           "本地提交保留，处理好远端后重跑一次即可。")
    return {"committed": True, "pushed": True, "commit": sha, "files": files}


def tls_hint(detail: str) -> str:
    """给 TLS 握手失败补一句可操作的排查方向，其他错误返回空串。

    入参：git push 的 stderr/stdout。
    出参：一句提示（以句号结尾），或空串。
    失败：不抛异常。

    为什么要专门识别这一句：GitHub 推送在本机失败过一次，报的是
    "TLS connect error: unexpected eof while reading"——连接根本没建立，
    和账号、权限、仓库状态都无关。这类错误读起来像"认证问题"，很容易
    让人去改 token，方向完全错。实测把 TLS 后端换成 Windows 自带的
    schannel 就能连上，所以直接把这条路写进提示。
    """
    if "TLS" not in detail and "SSL" not in detail:
        return ""
    return ("（这是本机到远端的 TLS 握手被切断，不是账号权限问题。可试 "
            "git config --local http.sslBackend schannel 后重推。）")


def publish(message: str, check_only: bool) -> dict[str, Any]:
    """发布主流程。

    入参：message 为提交信息；check_only=True 时只判断站点是否最新，不写不推。
    出参：统一结构里的 data 段：files/commit/harness 概要/是否最新。
    失败：抛 PublishError。check 模式下站点过期按 VALIDATION_FAILED 抛——
         那是内容问题（跑一次 build_site.py 就修好），必须体现为红，不能
         只打印"已过期"却返回 0：那样 CI 与 hook 会把它当通过。
    """
    if check_only:
        current, note = site_is_current()
        if not current:
            raise PublishError(
                "VALIDATION_FAILED",
                f"{note}。重跑 python src/build_site.py 后再发布。",
            )
        return {
            "site_current": True,
            "note": note,
            "pending": changed_files(),
            "check_only": True,
        }

    # 先重建再校验：顺序反了就会拿旧站点过 L6，等于自己给自己发通行证。
    info = build_site.build()
    result = run_harness()
    shipped = commit_and_push(message)
    return {
        "site_current": True,
        "site": {"files": info["files"], "bytes": info["bytes"],
                 "fingerprint": info["fingerprint"]},
        "harness": result,
        "publish": shipped,
        "check_only": False,
    }


def main(argv: list[str]) -> int:
    """命令行入口。

    入参：argv 为去掉程序名后的参数（--check / --json / -m 提交信息）。
    出参：进程退出码（0 成功 / 1 站点过期 / 2 环境或 git 问题）。
    失败：可预期失败不抛异常，转成统一结构输出后返回非零。
    """
    ap = argparse.ArgumentParser(description="重建站点、跑校验、提交并推送")
    ap.add_argument("--check", action="store_true",
                    help="只判断站点是否最新、有哪些改动待提交，不写不推")
    ap.add_argument("--json", action="store_true", help="输出统一结构 JSON")
    ap.add_argument("-m", "--message", default="", help="提交信息")
    args = ap.parse_args(argv)

    try:
        data = publish(args.message, args.check)
        resp = envelope.ok(data)
    except PublishError as exc:
        resp = envelope.from_exception(exc)
    except (settings.ConfigError, build_site.BuildError) as exc:
        resp = envelope.from_exception(exc)

    if resp["ok"]:
        d = resp["data"]
        if d["check_only"]:
            note = d["note"]
            pending = d["pending"]
            lines = [f"{'最新' if d['site_current'] else '已过期'}：{note}"]
            lines.append(f"待提交 {len(pending)} 个文件" if pending
                         else "工作区干净，没有待提交的改动")
            lines.append("（单点自检：只判断不写不推；真正上传请去掉 --check）")
            text = "\n".join(lines)
        else:
            p = d["publish"]
            h = d["harness"]
            lines = [
                f"站点已重建：{d['site']['bytes'] / 1024:.1f} KB，"
                f"数据指纹 {d['site']['fingerprint']}",
                f"校验：{h.get('verdict', 'PASS')}（{h.get('passed', 0)} 项通过、"
                f"{h.get('failed', 0)} 项失败）",
            ]
            if p["committed"]:
                lines.append(f"已提交 {p['commit']}：{len(p['files'])} 个文件")
                lines.append("已推送到远端" if p["pushed"] else "未推送")
            else:
                lines.append("工作区无改动，跳过提交与推送")
            text = "\n".join(lines)
    else:
        text = f"{resp['error']['code']}：{resp['error']['msg']}"

    envelope.emit(resp, as_json=args.json, text=text)
    return envelope.exit_code(resp)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
