import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import bounty_triage as triage


class BountyTriageTests(unittest.TestCase):
    def test_accepts_issue_url_and_rejects_other_hosts(self):
        self.assertEqual(triage.parse_issue_url("https://github.com/owner/repo/issues/42"), ("owner", "repo", 42))
        for bad in ("https://github.com/owner/repo/pull/42", "https://github.com.evil.example/owner/repo/issues/42", "http://github.com/owner/repo/issues/42", "https://github.com/owner/repo/issues/0"):
            with self.subTest(bad=bad), self.assertRaises(triage.TriageError):
                triage.parse_issue_url(bad)

    def test_reports_unverified_amount_and_partial_pr_search(self):
        calls = []
        def fetch(path, params=None):
            calls.append((path, params))
            if path.endswith('/timeline'):
                return []
            if path.startswith('/repos/'):
                return {"title": "Fix layout for $25", "body": "Reward not confirmed", "state": "open", "assignees": [], "comments": 2, "updated_at": "2026-01-01T00:00:00Z"}
            return {"total_count": 1, "incomplete_results": True, "items": [{"title": "A fix", "html_url": "https://github.com/a/b/pull/1"}]}
        result = triage.collect("https://github.com/a/b/issues/3", fetch)
        self.assertEqual(result["amount_mentions_unverified"], ["$25"])
        self.assertEqual(result["open_prs_matching_issue_url"], 1)
        self.assertTrue(result["search_incomplete"])
        self.assertIsNone(result["search_error"])
        self.assertIn('"https://github.com/a/b/issues/3"', calls[1][1]["q"])
        self.assertIn("in:body", calls[1][1]["q"])
        self.assertIn("可能误匹配或遗漏", triage.render(result))
        self.assertIn("零结果也不证明没有竞争", triage.render(result))

    def test_search_failure_preserves_issue_and_reports_unknown_competition(self):
        def fetch(path, params=None):
            if path.endswith('/timeline'):
                return []
            if path.startswith('/repos/'):
                return {"title": "Test $25", "body": "", "state": "closed", "comments": 3}
            raise triage.TriageError("搜索不可用")
        result = triage.collect("https://github.com/a/b/issues/3", fetch)
        self.assertEqual(result["title"], "Test $25")
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["comment_count"], 3)
        self.assertIsNone(result["open_prs_matching_issue_url"])
        self.assertIsNone(result["search_incomplete"])
        self.assertIsNone(result["matching_prs"])
        self.assertEqual(result["search_error"], "搜索不可用")
        output = triage.render(result)
        self.assertIn("PR 搜索不可用，竞争情况未知：搜索不可用", output)
        self.assertIn("开放 PR：未知", output)

    def test_cli_json_reports_search_failure_as_null(self):
        def fetch(path, params=None):
            if path.endswith('/timeline'):
                return []
            if path.startswith('/repos/'):
                return {"title": "An issue", "state": "open"}
            raise triage.TriageError("GitHub API 请求达到频率限制，请稍后重试。")
        stdout = io.StringIO()
        collect = triage.collect
        with patch("bounty_triage.collect", side_effect=lambda url: collect(url, fetch)), redirect_stdout(stdout):
            self.assertEqual(triage.main(["https://github.com/a/b/issues/3", "--json"]), 0)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["title"], "An issue")
        self.assertIsNone(result["open_prs_matching_issue_url"])
        self.assertIsNone(result["matching_prs"])
        self.assertIn("频率限制", result["search_error"])

    def test_issue_fetch_failure_remains_fatal(self):
        def fetch(path, params=None):
            raise triage.TriageError("无法连接")
        with self.assertRaisesRegex(triage.TriageError, "无法连接"):
            triage.collect("https://github.com/a/b/issues/3", fetch)

    def test_timeline_prs_filter_deduplicate_and_paginate(self):
        pr = {"event": "cross-referenced", "source": {"issue": {"title": "Fix", "html_url": "https://github.com/x/y/pull/7", "state": "open", "pull_request": {}}}}
        closed = {"event": "cross-referenced", "source": {"issue": {"html_url": "https://github.com/x/y/pull/8", "state": "closed", "pull_request": {}}}}
        issue = {"event": "cross-referenced", "source": {"issue": {"html_url": "https://github.com/x/y/issues/3", "state": "open"}}}
        pages = []
        def fetch(path, params=None):
            pages.append(params["page"])
            return [pr] * 100 if params["page"] == 1 else [pr, closed, issue]
        result, incomplete = triage.timeline_prs("a", "b", 3, fetch)
        self.assertEqual(pages, [1, 2])
        self.assertFalse(incomplete)
        self.assertEqual(result, [{"title": "Fix", "url": "https://github.com/x/y/pull/7"}])

    def test_timeline_page_limit_is_explicitly_incomplete(self):
        result, incomplete = triage.timeline_prs("a", "b", 3, lambda path, params: [{}] * 100)
        self.assertTrue(incomplete)
        self.assertEqual(result, [])

    def test_timeline_failure_reports_unknown_instead_of_empty(self):
        def fetch(path, params=None):
            if path.endswith("/timeline"):
                raise triage.TriageError("时间线不可用")
            if path.startswith("/repos/"):
                return {"title": "Test", "state": "open"}
            return {"total_count": 0, "items": []}
        result = triage.collect("https://github.com/a/b/issues/3", fetch)
        self.assertIsNone(result["timeline_prs"])
        self.assertIsNone(result["timeline_incomplete"])
        self.assertIn("时间线关联 PR 未知", triage.render(result))
        self.assertEqual(result["timeline_error"], "时间线不可用")

    def test_rate_limit_is_explained_without_exposing_token(self):
        err = HTTPError("https://api.github.com", 403, "Forbidden", {"X-RateLimit-Remaining": "0"}, None)
        with patch.dict("os.environ", {"GITHUB_TOKEN": "secret-token"}), patch("bounty_triage.urlopen", side_effect=err):
            with self.assertRaises(triage.TriageError) as ctx:
                triage.api_get("/x")
        self.assertIn("频率限制", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_network_error_is_clear(self):
        with patch("bounty_triage.urlopen", side_effect=URLError("offline")):
            with self.assertRaisesRegex(triage.TriageError, "无法连接"):
                triage.api_get("/x")

    def test_cli_handles_malformed_ipv6_url_without_traceback(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(triage.main(["https://[invalid"]), 1)
        self.assertIn("请输入", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_cli_rejects_invalid_url(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(triage.main(["not-a-url"]), 1)
        self.assertIn("请输入", errors.getvalue())


class RedirectTests(unittest.TestCase):
    def test_redirect_targets_are_restricted(self):
        from urllib.request import Request
        for code in (301, 302, 303, 307, 308):
            req = Request("https://api.github.com/repos/a/b", headers={"Authorization": "Bearer test-token"})
            for target in ("https://evil.example/x", "http://api.github.com/x", "https://api.github.com.evil.example/x", "https://api.github.com@evil.example/x"):
                with self.subTest(code=code, target=target), self.assertRaises(triage.TriageError):
                    triage.GitHubRedirectHandler().redirect_request(req, None, code, "redirect", {}, target)
            if code != 308:
                good = triage.GitHubRedirectHandler().redirect_request(req, None, code, "redirect", {}, "https://api.github.com/repos/new/b")
                self.assertEqual(good.full_url, "https://api.github.com/repos/new/b")
                self.assertEqual(good.get_header("Authorization"), "Bearer test-token")

    def test_opener_blocks_cross_origin_before_second_request(self):
        from urllib.request import HTTPSHandler, build_opener, Request
        from urllib.response import addinfourl
        from email.message import Message
        for code in (301, 302, 303, 307, 308):
            seen = []
            class FakeHTTPS(HTTPSHandler):
                def https_open(self, req):
                    seen.append(req.full_url)
                    headers = Message()
                    headers["Location"] = "https://evil.example/steal"
                    response = addinfourl(io.BytesIO(b""), headers, req.full_url, code)
                    response.msg = "redirect"
                    return response
            opener = build_opener(FakeHTTPS(), triage.GitHubRedirectHandler())
            with self.subTest(code=code), self.assertRaises(triage.TriageError):
                opener.open(Request("https://api.github.com/repos/a/b", headers={"Authorization": "Bearer test-token"}))
            self.assertEqual(seen, ["https://api.github.com/repos/a/b"])


class EncodingTests(unittest.TestCase):
    def test_text_in_gbk_handles_unrepresentable_emoji(self):
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="gbk", errors="strict", write_through=True)
        triage.safe_print("标题 💰", out)
        self.assertIn(r"\U0001f4b0", raw.getvalue().decode("gbk"))

    def test_json_is_lossless_for_gbk_ascii_and_utf8(self):
        value = {"title": "任务 💰", "status": None}
        for encoding in ("gbk", "ascii", "utf-8"):
            with self.subTest(encoding=encoding):
                raw = io.BytesIO()
                out = io.TextIOWrapper(raw, encoding=encoding, errors="strict", write_through=True)
                triage.print_json(value, out)
                self.assertEqual(json.loads(raw.getvalue().decode(encoding)), value)

    def test_stringio_remains_supported(self):
        out = io.StringIO()
        triage.safe_print("任务 💰", out)
        self.assertIn("💰", out.getvalue())
        out = io.StringIO()
        triage.print_json({"title": "💰"}, out)
        self.assertEqual(json.loads(out.getvalue())["title"], "💰")


class AmountTests(unittest.TestCase):
    def test_currency_mentions_are_preserved_without_truncation(self):
        text = "悬赏25元，赏金¥50，￥1,200.50，人民币50元，50 万元，￥50万，1.5亿元，$25，€9.50"
        self.assertEqual(triage.extract_amount_mentions(text), ["25元", "¥50", "￥1,200.50", "人民币50元", "50 万元", "￥50万", "1.5亿元", "$25", "€9.50"])

    def test_dates_versions_and_invalid_decimals_do_not_match(self):
        self.assertEqual(triage.extract_amount_mentions("v1.2.3元，2026年10月，3.141元"), [])

    def test_duplicate_mentions_are_deduplicated(self):
        self.assertEqual(triage.extract_amount_mentions("悬赏25元，25元"), ["25元"])


if __name__ == "__main__":
    unittest.main()
