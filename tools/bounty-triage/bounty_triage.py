#!/usr/bin/env python3
"""Summarize public GitHub issue data without claiming a bounty is valid."""

import argparse
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener


class TriageError(Exception):
    """An expected input or API error."""


class GitHubRedirectHandler(HTTPRedirectHandler):
    """Keep authorization headers on GitHub's API origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme.lower() != "https" or target.netloc.lower() != "api.github.com":
            raise TriageError("GitHub API 重定向到不受信任的地址，已拒绝。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def urlopen(request, timeout=20):
    return build_opener(GitHubRedirectHandler()).open(request, timeout=timeout)


def parse_issue_url(value):
    parsed = urlsplit(value)
    match = re.fullmatch(r"/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]+)/issues/([1-9][0-9]*)/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc.lower() not in ("github.com", "www.github.com") or not match or parsed.query or parsed.fragment:
        raise TriageError("请输入完整的 GitHub issue 地址，例如 https://github.com/owner/repo/issues/123")
    return match.group(1), match.group(2), int(match.group(3))


def api_get(path, params=None):
    # The host is fixed; user input never controls the API host.
    url = "https://api.github.com" + path
    if params:
        url += "?" + urlencode(params)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "bounty-triage/1.0", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 403 and exc.headers.get("X-RateLimit-Remaining") == "0" or exc.code == 429:
            raise TriageError("GitHub API 请求达到频率限制，请稍后重试。") from None
        if exc.code == 404:
            raise TriageError("未找到该 issue，或者当前账号无权访问。") from None
        raise TriageError(f"GitHub API 返回 HTTP {exc.code}；请检查网络、权限或稍后重试。") from None
    except (URLError, TimeoutError, OSError):
        raise TriageError("无法连接 GitHub API，请检查网络后重试。") from None
    except (ValueError, UnicodeError):
        raise TriageError("GitHub API 返回了无法解析的数据。") from None


def timeline_prs(owner, repo, number, fetch, max_pages=3):
    """Return distinct open PRs referenced in up to 300 timeline events.

    A full final page is conservatively marked incomplete; this avoids trusting
    the absence of results when additional pages may exist.
    """
    prs = {}
    path = f"/repos/{owner}/{repo}/issues/{number}/timeline"
    for page in range(1, max_pages + 1):
        events = fetch(path, {"per_page": 100, "page": page})
        if not isinstance(events, list):
            raise TriageError("GitHub timeline 返回了无法识别的数据。")
        for event in events:
            if not isinstance(event, dict) or event.get("event") != "cross-referenced":
                continue
            source = event.get("source") or {}
            if not isinstance(source, dict):
                continue
            issue = source.get("issue") or {}
            if not isinstance(issue, dict):
                continue
            url = issue.get("html_url")
            if issue.get("pull_request") is not None and issue.get("state") == "open" and isinstance(url, str):
                prs[url] = {"title": issue.get("title"), "url": url}
        if len(events) < 100:
            return list(prs.values()), False
    return list(prs.values()), True


def collect(issue_url, fetch=api_get):
    owner, repo, number = parse_issue_url(issue_url)
    issue = fetch(f"/repos/{owner}/{repo}/issues/{number}")
    if not isinstance(issue, dict) or "pull_request" in issue:
        raise TriageError("这个地址不是普通 issue。")
    canonical_url = f"https://github.com/{owner}/{repo}/issues/{number}"
    query = f'repo:{owner}/{repo} is:pr is:open in:body "{canonical_url}"'
    try:
        search = fetch("/search/issues", {"q": query, "per_page": 20})
        search_error = None
    except TriageError as exc:
        search = None
        search_error = str(exc)
    try:
        referenced_prs, timeline_incomplete = timeline_prs(owner, repo, number, fetch)
        timeline_error = None
    except TriageError as exc:
        referenced_prs, timeline_incomplete, timeline_error = None, None, str(exc)
    text = (issue.get("title") or "") + "\n" + (issue.get("body") or "")
    mentions = list(dict.fromkeys(re.findall(r"(?<![\w])(?:\$|€|£)\s?\d[\d,]*(?:\.\d{1,2})?", text)))[:10]
    return {
        "issue_url": canonical_url,
        "title": issue.get("title"),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "assignees": [person.get("login") for person in issue.get("assignees", []) if person.get("login")],
        "comment_count": issue.get("comments"),
        "updated_at": issue.get("updated_at"),
        "amount_mentions_unverified": mentions,
        "open_prs_matching_issue_url": search.get("total_count") if search is not None else None,
        "search_incomplete": bool(search.get("incomplete_results")) if search is not None else None,
        "search_error": search_error,
        "matching_prs": [{"title": item.get("title"), "url": item.get("html_url")} for item in search.get("items", [])] if search is not None else None,
        "timeline_prs": referenced_prs,
        "timeline_incomplete": timeline_incomplete,
        "timeline_error": timeline_error,
        "limitations": ["金额仅为 issue 文本中出现的数字，不代表已托管或保证支付。", "PR 数量来自本仓库开放 PR 正文的 GitHub 搜索，查询包含 issue 完整网址；搜索可能误匹配或遗漏，零结果也不证明没有竞争。", "时间线仅包含 GitHub 记录的关联，最多读取前 300 条事件；关联不一定是解决方案，遗漏和重复引用均可能发生，零结果不证明没有竞争。", "请人工核实资金、领取资格、付款方式、AI 规则和验收要求。"],
    }


def render(result):
    def show(value):
        return str(value) if value is not None else "未知"
    lines = [
        f"任务：{show(result['title'])}",
        f"地址：{result['issue_url']}",
        f"状态：{show(result['state'])}（原因：{show(result['state_reason'])}）",
        f"负责人：{', '.join(result['assignees']) or '未列出'}",
        f"评论数：{show(result['comment_count'])}",
        f"最近更新（UTC）：{show(result['updated_at'])}",
        f"文本中提及的金额（未验证）：{', '.join(result['amount_mentions_unverified']) or '未发现'}",
        f"用完整 issue 网址搜索正文返回的开放 PR：{show(result['open_prs_matching_issue_url'])}" + ("（GitHub 报告搜索结果不完整）" if result['search_incomplete'] else ""),
    ]
    if result["search_error"]:
        lines.append(f"PR 搜索不可用，竞争情况未知：{result['search_error']}")
    lines.extend(f"  - {pr['title']}: {pr['url']}" for pr in result["matching_prs"] or [])
    if result["timeline_error"]:
        lines.append(f"时间线关联 PR 未知：{result['timeline_error']}")
    else:
        lines.append(f"时间线记录的开放关联 PR：{len(result['timeline_prs'])}" + ("（已达读取上限，结果可能不完整）" if result["timeline_incomplete"] else ""))
        lines.extend(f"  - {pr['title']}: {pr['url']}" for pr in result["timeline_prs"])
    lines.append("注意：")
    lines.extend("  - " + item for item in result["limitations"])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读检查 GitHub issue 的状态、金额提及和部分竞争信号。")
    parser.add_argument("issue_url", help="GitHub issue 地址")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    try:
        result = collect(args.issue_url)
    except TriageError as exc:
        print(f"检查失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
