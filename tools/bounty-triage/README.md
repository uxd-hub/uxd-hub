# Bounty Triage

一个只读的 GitHub issue 检查小工具，用来初步查看悬赏任务的状态和竞争信号。它不会提交内容、认领任务或保证获得报酬。

## 使用

需要 Python 3.9 或更新版本，无需额外安装第三方库。下载本目录，打开终端并进入该目录，然后运行：

```bash
python bounty_triage.py https://github.com/OWNER/REPO/issues/123
```

加 `--json` 可以输出机器可读的 JSON。默认无需登录；GitHub 的匿名 API 有请求频率限制。如需更高限额，可选择在本机设置 `GITHUB_TOKEN` 环境变量，使用已授权的令牌。请不要在公共场所分享令牌。

## 输出和限制

工具展示 issue 状态、负责人、评论数量、更新时间，以及标题和正文中出现的金额。**金额只是文本提及，不表示奖金已托管、仍可领取或一定会付款。**

工具还用该 issue 的**完整网址**搜索同仓库开放 PR 的正文。GitHub 搜索不保证精确匹配，可能误匹配；其他 PR 可能只写 `#123`、来自其他仓库，或者根本没有公开提及该任务，因此也可能遗漏：显示为零不代表没有竞争。搜索服务如果返回不完整标志，工具会提示；结果最多列出 20 个 PR。

工具也检查 issue 时间线中的交叉引用事件，列出去重后的开放 PR，包括跨仓库引用。最多读取前 300 个事件，达到上限会提示可能不完整；即使没有达到上限，时间线也不保证收录所有竞争者，关联 PR 也不一定在解决该任务。搜索结果与时间线结果是两组独立线索，不应直接相加。

时间线查询失败时，文本输出显示未知及原因；JSON 的 `timeline_prs` 和 `timeline_incomplete` 为 `null`，`timeline_error` 记录原因。查询成功但达到读取上限时，`timeline_incomplete` 为 `true`。

如果 issue 已读取成功，但 PR 搜索因网络或限流等原因失败，工具仍显示 issue 信息，竞争情况显示为“未知”，同时说明搜索失败原因。JSON 输出的 `open_prs_matching_issue_url`、`search_incomplete` 和 `matching_prs` 此时均为 `null`，`search_error` 记录原因。issue 本身无法读取时，工具会报错并退出。

请人工核实任务是否接受 AI 协助、验收流程、资金状态、用户所在地的收款资格和付款方式，再决定是否投入时间。

## 测试

```bash
python -m unittest -v
```
