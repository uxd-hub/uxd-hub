import io
import unittest
from contextlib import redirect_stderr
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
            if path.startswith('/repos/'):
                return {"title": "Fix layout for $25", "body": "Reward not confirmed", "state": "open", "assignees": [], "comments": 2, "updated_at": "2026-01-01T00:00:00Z"}
            return {"total_count": 1, "incomplete_results": True, "items": [{"title": "A fix", "html_url": "https://github.com/a/b/pull/1"}]}
        result = triage.collect("https://github.com/a/b/issues/3", fetch)
        self.assertEqual(result["amount_mentions_unverified"], ["$25"])
        self.assertEqual(result["open_prs_matching_issue_url"], 1)
        self.assertTrue(result["search_incomplete"])
        self.assertIn('"https://github.com/a/b/issues/3"', calls[1][1]["q"])
        self.assertIn("in:body", calls[1][1]["q"])
        self.assertIn("可能误匹配或遗漏", triage.render(result))
        self.assertIn("零结果也不证明没有竞争", triage.render(result))

    def test_search_fails_instead_of_showing_zero_competitors(self):
        def fetch(path, params=None):
            if path.startswith('/repos/'):
                return {"title": "Test", "body": "", "state": "closed"}
            raise triage.TriageError("搜索不可用")
        with self.assertRaisesRegex(triage.TriageError, "搜索不可用"):
            triage.collect("https://github.com/a/b/issues/3", fetch)

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

    def test_cli_rejects_invalid_url(self):
        errors = io.StringIO()
        with redirect_stderr(errors):
            self.assertEqual(triage.main(["not-a-url"]), 1)
        self.assertIn("请输入", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
