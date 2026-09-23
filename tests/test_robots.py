import unittest
from unittest.mock import Mock

from checks import _parse_robots_groups, check_robots


class RobotsParserTests(unittest.TestCase):
    def test_consecutive_user_agents_share_rules(self):
        groups = _parse_robots_groups(
            """User-agent: GPTBot
User-agent: ClaudeBot
Disallow: /

User-agent: PerplexityBot
Allow: /
"""
        )
        self.assertEqual(groups["gptbot"], ["disallow: /"])
        self.assertEqual(groups["claudebot"], ["disallow: /"])
        self.assertEqual(groups["perplexitybot"], ["allow: /"])

    def test_comments_and_field_case_are_normalized(self):
        groups = _parse_robots_groups(
            "USER-AGENT: GPTBot # model crawler\nDISALLOW: /private # internal\n"
        )
        self.assertEqual(groups["gptbot"], ["disallow: /private"])

    def test_audit_flags_every_agent_in_shared_block(self):
        response = Mock(status_code=200, text=(
            "User-agent: GPTBot\n"
            "User-agent: ClaudeBot\n"
            "Disallow: /\n"
        ))
        session = Mock()
        session.get.return_value = response

        result = check_robots(session, "https://example.com", 5)

        # Both are training crawlers: each is still detected, but the opt-out
        # costs little because shopping agents can still reach the store.
        detail = " ".join(result.details)
        self.assertIn("GPTBot", detail)
        self.assertIn("ClaudeBot", detail)
        self.assertGreaterEqual(result.score, 80)
        self.assertNotEqual(result.status, "fail")


if __name__ == "__main__":
    unittest.main()
