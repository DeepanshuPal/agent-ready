import unittest
from unittest.mock import Mock

from checks import (
    check_agent_access,
    check_llms_txt,
    check_robots,
    check_server_rendered,
    check_status_codes,
    classify_agent_response,
    find_product_url,
)

PAGE = "<html><body><main><h1>Shop</h1>" + "<p>Real store copy here.</p>" * 60 + "</main></body></html>"


def resp(code=200, text="", url="https://shop.example/x", headers=None):
    return Mock(status_code=code, text=text, url=url, headers=headers or {})


def session_for(mapping):
    s = Mock()
    s.get.side_effect = lambda url, **kw: mapping(url)
    return s


class TieredRobotsTests(unittest.TestCase):
    def robots(self, text):
        return check_robots(session_for(lambda u: resp(200, text)), "https://shop.example", 5)

    def test_training_opt_out_is_cheap(self):
        r = self.robots("User-agent: GPTBot\nDisallow: /\n\nSitemap: https://shop.example/sitemap.xml\n")
        self.assertEqual(r.status, "pass")
        self.assertGreaterEqual(r.score, 95)

    def test_blocking_shopper_fetcher_is_costly(self):
        r = self.robots("User-agent: ChatGPT-User\nDisallow: /\n\nSitemap: https://shop.example/s.xml\n")
        self.assertEqual(r.status, "fail")
        self.assertLessEqual(r.score, 75)
        self.assertIn("ChatGPT-User", r.summary)

    def test_blocking_search_indexer_warns(self):
        r = self.robots("User-agent: OAI-SearchBot\nDisallow: /\n\nSitemap: https://shop.example/s.xml\n")
        self.assertEqual(r.status, "warn")
        self.assertLess(r.score, 90)

    def test_sitemap_earns_points(self):
        with_map = self.robots("User-agent: *\nAllow: /\nSitemap: https://shop.example/s.xml\n")
        without = self.robots("User-agent: *\nAllow: /\n")
        self.assertEqual(with_map.score - without.score, 10)


class LlmsTxtTests(unittest.TestCase):
    GOOD = ("# Shop Example\n\nIndependent tea shop selling loose-leaf teas and brewing gear.\n\n"
            "## Catalog\n- [Products](https://shop.example/products.json): full catalog as JSON\n"
            "- [Shipping](https://shop.example/pages/shipping): rates and delivery times\n")

    def run_check(self, body, home_html=""):
        return check_llms_txt(session_for(lambda u: resp(200, body, url=u)), "https://shop.example", 5,
                              home_html=home_html)

    def test_full_marks_need_shape_and_link(self):
        r = self.run_check(self.GOOD, home_html='<footer><a href="/llms.txt">For AI agents</a></footer>')
        self.assertEqual(r.score, 100)
        self.assertEqual(r.status, "pass")

    def test_unlinked_file_loses_points(self):
        r = self.run_check(self.GOOD)
        self.assertEqual(r.score, 80)
        self.assertTrue(any("does not link" in d for d in r.details))

    def test_link_rel_counts_as_linked(self):
        r = self.run_check(self.GOOD, home_html='<link rel="alternate" type="text/plain" href="https://shop.example/llms.txt">')
        self.assertEqual(r.score, 100)

    def test_bare_file_scores_low(self):
        r = self.run_check("just some text that is long enough to count here")
        self.assertEqual(r.score, 40)

    def test_html_catch_all_is_not_llms_txt(self):
        r = self.run_check("<!DOCTYPE html><html><head><title>Home</title></head><body>hi</body></html>")
        self.assertEqual(r.status, "fail")


class AgentAccessTests(unittest.TestCase):
    def test_all_agents_served(self):
        r, observed = check_agent_access("https://shop.example", PAGE, 5,
                                         fetch=lambda url, ua: resp(200, PAGE, url))
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.score, 100)
        self.assertEqual(len(observed), 3)

    def test_blocked_agent_is_named(self):
        def fetch(url, ua):
            return resp(403, "Forbidden", url) if "ChatGPT-User" in ua else resp(200, PAGE, url)
        r, _ = check_agent_access("https://shop.example", PAGE, 5, product_url="https://shop.example/products/a",
                                  fetch=fetch)
        self.assertIn("ChatGPT-User", r.summary)
        self.assertAlmostEqual(r.score, 100 * 4 / 6)

    def test_challenge_and_empty_shell(self):
        self.assertEqual(classify_agent_response(resp(200, "<title>Just a moment...</title> cf-chl"), 2000), "challenge")
        self.assertEqual(classify_agent_response(resp(200, "<html><body><div id=app></div></body></html>"), 2000), "empty")
        self.assertEqual(classify_agent_response(resp(200, PAGE), 1000), "ok")


class StatusCodeTests(unittest.TestCase):
    def test_honest_404(self):
        s = session_for(lambda u: resp(404, "not found", url=u))
        r = check_status_codes(s, "https://shop.example", 5, observed=[resp(200, PAGE)], token="t")
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.score, 100)

    def test_soft_404(self):
        s = session_for(lambda u: resp(200, PAGE, url="https://shop.example/"))
        r = check_status_codes(s, "https://shop.example", 5, observed=[], token="t")
        self.assertEqual(r.score, 40)
        self.assertTrue(any("soft 404" in d for d in r.details))

    def test_silent_challenge_and_unhinted_429(self):
        s = session_for(lambda u: resp(404, "", url=u))
        r = check_status_codes(s, "https://shop.example", 5, token="t",
                               observed=[resp(200, "<title>Just a moment...</title>"), resp(429, "slow down")])
        self.assertEqual(r.score, 60)
        r2 = check_status_codes(s, "https://shop.example", 5, token="t",
                                observed=[resp(429, "slow down", headers={"Retry-After": "30"})])
        self.assertEqual(r2.score, 100)


class ServerRenderedTests(unittest.TestCase):
    HOME = '<a href="/products/green-tea">Green tea</a>'
    SSR = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"Green Tea",'
           '"offers":{"price":"12.50","priceCurrency":"USD"}}</script></head>'
           '<body><h1>Green Tea</h1><span>$12.50</span></body></html>')
    CSR = ('<html><head><script type="application/ld+json">{"@type":"Product","name":"Green Tea",'
           '"offers":{"price":"12.50"}}</script></head><body><div id="root"></div>'
           '<script>render()</script></body></html>')

    def run_check(self, page, platform="Custom", products=None):
        s = session_for(lambda u: resp(200, page, url=u))
        return check_server_rendered(s, "https://shop.example", self.HOME, 5, platform, products)

    def test_facts_in_html(self):
        r = self.run_check(self.SSR)
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.score, 100)

    def test_facts_only_after_js(self):
        r = self.run_check(self.CSR)
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.score, 30)

    def test_skipped_on_shopify_with_feed(self):
        r = self.run_check(self.SSR, platform="Shopify", products=[{"handle": "x"}])
        self.assertEqual(r.status, "skip")

    def test_product_link_discovery(self):
        self.assertEqual(find_product_url("https://shop.example", self.HOME), "https://shop.example/products/green-tea")
        self.assertIsNone(find_product_url("https://shop.example", '<a href="https://other.example/products/x">x</a>'))


if __name__ == "__main__":
    unittest.main()


AKAMAI_QUEUE = ("<html><body>Hang Tight! Routing to checkout... Sit tight We've "
                "got our hands full at the moment but we should be up and moving shortly. "
                "This page will automatically refresh and bring you into the website.</body></html>")


class TestQueueBaseline(unittest.TestCase):
    def test_queue_page_is_challenge(self):
        from checks import looks_like_challenge
        self.assertTrue(looks_like_challenge(AKAMAI_QUEUE))

    def test_marketing_copy_not_challenge(self):
        from checks import looks_like_challenge
        self.assertFalse(looks_like_challenge(
            "<html><body>Sit tight for our big sale! We have got our hands full of deals</body></html>"))

    def test_degraded_baseline_fails_fast(self):
        result, observed = check_agent_access("https://shop.example", AKAMAI_QUEUE, 5,
                                              fetch=lambda url, ua: resp(200, AKAMAI_QUEUE))
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.score, 0.0)
        self.assertEqual(observed, [])

    def test_agent_getting_queue_page_is_challenge_not_ok(self):
        fetch = lambda url, ua: resp(200, AKAMAI_QUEUE) if "ChatGPT" in ua else resp(200, PAGE)
        result, _ = check_agent_access("https://shop.example", PAGE, 5, fetch=fetch)
        self.assertNotEqual(result.score, 100.0)
