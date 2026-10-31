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
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise TriageError("请输入完整的 GitHub issue 地址，例如 https://github.com/owner/repo/issues/123") from None
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


def extract_amount_mentions(text):
    """Find currency mentions without asserting that they are rewards."""
    number = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?'
    prefix_tail = r'(?:\s*(?:[万亿]元?|元))?'
    unit = r'(?:USDC|USDT|USD)(?![A-Za-z0-9_])'
    pattern = re.compile(r'(?<![A-Za-z0-9_.,])(?:人民币\s*' + number + r'(?!\d|[.,]\d)' + prefix_tail + r'|\$\s*' + number + r'(?!\d|[.,]\d)' + prefix_tail + r'(?:\s*' + unit + r')?|[€£¥￥]\s*' + number + r'(?!\d|[.,]\d)' + prefix_tail + r'|' + number + r'\s*' + unit + r'|' + number + r'\s*[万亿]?元)', re.IGNORECASE)
    return list(dict.fromkeys(pattern.findall(text)))[:10]


def recent_relevant_comments(owner, repo, number, total, reporter, fetch, amount_comments=None):
    """Inspect up to 300 comments ordered by creation, not edits."""
    page_size, max_pages = 100, 3
    known_total = isinstance(total, int) and not isinstance(total, bool) and total >= 0
    last_page = max(1, (total + page_size - 1) // page_size) if known_total else 1
    start = max(1, last_page - max_pages + 1)
    relevant = []
    inspected = 0
    full_last = False
    path = f"/repos/{owner}/{repo}/issues/{number}/comments"
    for page in range(start, start + max_pages):
        comments = fetch(path, {"per_page": page_size, "page": page})
        if not isinstance(comments, list):
            raise TriageError("GitHub 评论返回了无法识别的数据。")
        inspected += len(comments)
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            user = comment.get("user") or {}
            author = user.get("login") if isinstance(user, dict) else None
            association = comment.get("author_association")
            if amount_comments is not None:
                body = comment.get("body")
                mentions = extract_amount_mentions(body) if isinstance(body, str) else []
                if mentions:
                    amount_comments.append({
                        "author": author, "url": comment.get("html_url"),
                        "updated_at": comment.get("updated_at"), "amounts": mentions,
                    })
                    if len(amount_comments) > 11:
                        del amount_comments[0]
            if association not in ("OWNER", "MEMBER", "COLLABORATOR") and not (
                isinstance(author, str) and isinstance(reporter, str) and author.lower() == reporter.lower()
            ):
                continue
            body = comment.get("body")
            body = body if isinstance(body, str) else ""
            relevant.append({
                "author": author, "association": association,
                "created_at": comment.get("created_at"), "updated_at": comment.get("updated_at"),
                "url": comment.get("html_url"), "body": body[:1200],
                "body_truncated": len(body) > 1200,
            })
        full_last = len(comments) == page_size
        if not full_last:
            break
    # Counts can change during pagination; matching counts are not a guarantee.
    incomplete = (not known_total or start > 1 or full_last or len(relevant) > 5 or
                  (start == 1 and known_total and inspected != total))
    return relevant[-5:], incomplete


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
    try:
        user = issue.get("user") or {}
        reporter = user.get("login") if isinstance(user, dict) else None
        amount_comments = []
        comments, comments_incomplete = recent_relevant_comments(
            owner, repo, number, issue.get("comments"), reporter, fetch, amount_comments)
        comment_amounts_incomplete = comments_incomplete or len(amount_comments) > 10
        comment_amounts = amount_comments[-10:]
        comments_error = None
    except TriageError as exc:
        comments, comments_incomplete, comments_error = None, None, str(exc)
        comment_amounts, comment_amounts_incomplete = None, None
    text = (issue.get("title") or "") + "\n" + (issue.get("body") or "")
    mentions = extract_amount_mentions(text)
    return {
        "issue_url": canonical_url,
        "title": issue.get("title"),
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason"),
        "assignees": [person.get("login") for person in issue.get("assignees", []) if person.get("login")],
        "labels": [label["name"] for label in (issue.get("labels") or []) if isinstance(label, dict) and isinstance(label.get("name"), str) and label["name"]],
        "comment_count": issue.get("comments"),
        "relevant_comments": comments,
        "comments_incomplete": comments_incomplete,
        "comments_error": comments_error,
        "comment_amounts_unverified": comment_amounts,
        "comment_amounts_incomplete": comment_amounts_incomplete,
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
        f"标签：{', '.join(result['labels']) or '未列出'}",
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
    if result["comments_error"]:
        lines.append(f"相关评论未知：{result['comments_error']}")
    else:
        lines.append("最近创建的相关评论（仅所读范围）：" +
                     ("（可能不完整）" if result["comments_incomplete"] else ""))
        for comment in result["relevant_comments"]:
            lines.append(f"  - {comment['author']} ({comment['association']}) "
                         f"创建：{comment['created_at']} 更新：{comment['updated_at']}")
            lines.append(f"    {comment['url']}")
            lines.append(f"    {comment['body']}" + ("…（已截断）" if comment["body_truncated"] else ""))
    if result["comments_error"]:
        lines.append("评论中的金额未知，原因同上。")
    else:
        lines.append("评论中的金额（未验证，仅所读范围）：" +
                     ("（可能不完整）" if result["comment_amounts_incomplete"] else ""))
        for entry in result["comment_amounts_unverified"]:
            lines.append(f"  - {entry['author']} 更新：{entry['updated_at']} "
                         f"{', '.join(entry['amounts'])} {entry['url']}")
    lines.append("评论金额仅为文本提及，不代表仍有效、已托管或保证支付。")
    lines.append("评论可能因分页变化、编辑、作者范围或读取上限而遗漏；请查看原始讨论。")
    lines.append("注意：")
    lines.extend("  - " + item for item in result["limitations"])
    return "\n".join(lines)


def safe_print(text, stream):
    encoding = getattr(stream, "encoding", None)
    if encoding:
        text = text.encode(encoding, errors="backslashreplace").decode(encoding)
    print(text, file=stream)


def print_json(result, stream):
    text = json.dumps(result, ensure_ascii=False, indent=2)
    encoding = getattr(stream, "encoding", None)
    if encoding:
        try:
            text.encode(encoding)
        except UnicodeEncodeError:
            text = json.dumps(result, ensure_ascii=True, indent=2)
    print(text, file=stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读检查 GitHub issue 的状态、金额提及和部分竞争信号。")
    parser.add_argument("issue_url", help="GitHub issue 地址")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    try:
        result = collect(args.issue_url)
    except TriageError as exc:
        safe_print(f"检查失败：{exc}", sys.stderr)
        return 1
    print_json(result, sys.stdout) if args.json else safe_print(render(result), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
