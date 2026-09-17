"""Individual agent-readiness checks for ecommerce stores.

Each check returns a CheckResult with a status (pass/warn/fail/info),
a score 0-100, a one-line summary, and a list of detail strings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Crawlers and agents operated by AI labs / answer engines. Stores that
# block these are invisible to the agents doing the shopping.
AI_USER_AGENTS = [
    "GPTBot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "ClaudeBot",
    "Claude-User",
    "anthropic-ai",
    "PerplexityBot",
    "Perplexity-User",
    "Google-Extended",
    "Applebot-Extended",
    "Meta-ExternalAgent",
    "CCBot",
    "Bytespider",
    "Amazonbot",
]


@dataclass
class CheckResult:
    name: str
    status: str  # pass | warn | fail | info | skip
    score: float  # 0-100
    summary: str
    details: list[str] = field(default_factory=list)


def _get(session: requests.Session, url: str, timeout: int) -> requests.Response | None:
    try:
        return session.get(url, timeout=timeout, allow_redirects=True)
    except requests.RequestException:
        return None


def normalize_url(raw: str) -> str:
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw.rstrip("/")


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


def _parse_robots_groups(text: str) -> dict[str, list[str]]:
    """Parse robots.txt groups, preserving consecutive User-agent records.

    A robots group may name several user agents before its first rule. Splitting
    on every User-agent line loses the rules for all but the final agent.
    """
    groups: dict[str, list[str]] = {}
    current_agents: list[str] = []
    current_rules: list[str] = []

    def flush() -> None:
        if not current_agents:
            return
        for agent in current_agents:
            groups.setdefault(agent, []).extend(current_rules)

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if current_rules:
                flush()
                current_agents = []
                current_rules = []
            current_agents.append(value.lower())
        elif current_agents:
            current_rules.append(f"{field}: {value}")

    flush()
    return groups


def check_robots(session, base_url, timeout):
    resp = _get(session, base_url + "/robots.txt", timeout)
    if resp is None or resp.status_code != 200 or "user-agent" not in resp.text.lower():
        return CheckResult(
            "robots.txt", "warn", 40,
            "No usable robots.txt found",
            ["Agents get no crawl guidance; some will assume the worst."],
        )

    text = resp.text
    agent_blocks = _parse_robots_groups(text)

    blocked, allowed = [], []
    for bot in AI_USER_AGENTS:
        rules = agent_blocks.get(bot.lower())
        if rules is None:
            continue
        disallows = [r for r in rules if r.lower().startswith("disallow:")]
        allows = [r for r in rules if r.lower().startswith("allow:")]
        full_block = any(r.split(":", 1)[1].strip() == "/" for r in disallows)
        explicit_allow = any(r.split(":", 1)[1].strip() in ("", "/") for r in allows)
        if full_block and not explicit_allow:
            blocked.append(bot)
        else:
            allowed.append(bot)

    star_rules = agent_blocks.get("*", [])
    star_full_block = any(
        r.lower().startswith("disallow:") and r.split(":", 1)[1].strip() == "/"
        for r in star_rules
    )
    sitemaps = re.findall(r"(?im)^sitemap:\s*(\S+)", text)

    details = [f"{len(sitemaps)} sitemap(s) declared"]
    if sitemaps:
        details.append("First sitemap: " + sitemaps[0])
    if blocked:
        details.append("Explicitly blocked AI agents: " + ", ".join(blocked))
    if allowed:
        details.append("Explicitly allowed AI agents: " + ", ".join(allowed))

    if star_full_block:
        return CheckResult("robots.txt", "fail", 0,
                           "robots.txt blocks ALL crawlers (Disallow: / under User-agent: *)",
                           details)
    if blocked:
        ratio = len(allowed) / (len(allowed) + len(blocked))
        score = 60 * ratio
        return CheckResult(
            "robots.txt", "warn", score,
            f"{len(blocked)} AI agent(s) explicitly blocked: {', '.join(blocked[:4])}",
            details)
    return CheckResult("robots.txt", "pass", 100,
                       "No AI shopping agents blocked in robots.txt", details)


# --------------------------------------------------------------------------
# llms.txt
# --------------------------------------------------------------------------

def check_llms_txt(session, base_url, timeout):
    resp = _get(session, base_url + "/llms.txt", timeout)
    if resp is not None and resp.status_code == 200 and len(resp.text.strip()) > 20:
        lines = len(resp.text.strip().splitlines())
        return CheckResult("llms.txt", "pass", 100,
                           f"llms.txt present ({lines} lines of agent-facing docs)",
                           [resp.url])
    return CheckResult(
        "llms.txt", "fail", 0,
        "No llms.txt - agents get no curated map of the store",
        ["llms.txt is the emerging convention for telling LLM agents what a site "
         "offers and where things live. Cheap to add, increasingly expected."])


# --------------------------------------------------------------------------
# Shopify products.json
# --------------------------------------------------------------------------

def check_products_json(session, base_url, timeout):
    resp = _get(session, base_url + "/products.json?limit=250", timeout)
    if resp is None or resp.status_code != 200:
        code = resp.status_code if resp is not None else "no response"
        return CheckResult(
            "products.json", "fail", 0,
            f"products.json not available (HTTP {code})",
            ["The Shopify products.json feed is the fastest structured product "
             "source an agent can use. If this store is on Shopify, the feed "
             "may be disabled or the storefront is headless."]), None
    try:
        data = resp.json()
        products = data.get("products")
        if not isinstance(products, list):
            raise ValueError("no products list")
    except (ValueError, json.JSONDecodeError):
        return CheckResult("products.json", "fail", 0,
                           "products.json returned non-JSON or an unexpected shape",
                           [resp.text[:200]]), None

    if not products:
        return CheckResult("products.json", "warn", 30,
                           "products.json responds but contains 0 products", []), []

    n = len(products)
    with_desc = sum(1 for p in products if (p.get("body_html") or "").strip())
    with_images = sum(1 for p in products if p.get("images"))
    with_vendor = sum(1 for p in products if (p.get("vendor") or "").strip())
    variants = [v for p in products for v in p.get("variants", [])]
    with_price = sum(1 for v in variants if v.get("price") not in (None, ""))
    with_sku = sum(1 for v in variants if (v.get("sku") or "").strip())
    with_avail = sum(1 for v in variants if "available" in v)

    def pct(a, b):
        return round(100 * a / b) if b else 0

    details = [
        f"{n} products in first page (limit=250), {len(variants)} variants",
        f"description coverage: {pct(with_desc, n)}%",
        f"image coverage: {pct(with_images, n)}%",
        f"vendor coverage: {pct(with_vendor, n)}%",
        f"variant price coverage: {pct(with_price, len(variants))}%",
        f"variant SKU coverage: {pct(with_sku, len(variants))}%",
        f"variant availability flags: {pct(with_avail, len(variants))}%",
        f"sample product: {products[0].get('title', '?')} "
        f"({len(products[0].get('variants', []))} variants)",
    ]

    richness = (pct(with_desc, n) + pct(with_images, n) +
                pct(with_price, len(variants)) + pct(with_avail, len(variants))) / 4
    if richness >= 70:
        status = "pass"
    elif richness >= 40:
        status = "warn"
    else:
        status = "fail"
    return CheckResult("products.json", status, richness,
                       f"Machine-readable catalog exposed: {n} products, "
                       f"{pct(with_desc, n)}% with descriptions, "
                       f"{pct(with_price, len(variants))}% of variants priced",
                       details), products


# --------------------------------------------------------------------------
# schema.org structured data
# --------------------------------------------------------------------------

def _jsonld_types(html: str) -> list[str]:
    types: list[str] = []
    for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I):
        blob = m.group(1).strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            # common: multiple objects concatenated; try a lenient split
            try:
                data = json.loads("[" + re.sub(r"}\s*{", "},{", blob) + "]")
            except json.JSONDecodeError:
                continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            if t:
                types.extend(t if isinstance(t, list) else [t])
            for node in item.get("@graph", []) or []:
                if isinstance(node, dict) and node.get("@type"):
                    nt = node["@type"]
                    types.extend(nt if isinstance(nt, list) else [nt])
    return types


def check_structured_data(session, base_url, home_html, timeout, products=None):
    home_types = _jsonld_types(home_html)
    details = ["Homepage JSON-LD types: " +
               (", ".join(sorted(set(home_types))) if home_types else "none")]

    product_types: list[str] = []
    product_url = None
    if products:
        handle = products[0].get("handle")
        if handle:
            product_url = base_url + "/products/" + handle
    if product_url:
        resp = _get(session, product_url, timeout)
        if resp is not None and resp.status_code == 200:
            product_types = _jsonld_types(resp.text)
            details.append(f"Product page ({product_url}) JSON-LD types: " +
                           (", ".join(sorted(set(product_types))) if product_types else "none"))
            if "Product" in product_types:
                m = re.search(r'"price"\s*:\s*"?([\d.]+)', resp.text)
                if m:
                    details.append(f"Product schema includes price ({m.group(1)})")
        else:
            details.append(f"Product page fetch failed: {product_url}")

    has_product = "Product" in product_types
    has_product_group = "ProductGroup" in product_types
    has_org = any(t in ("Organization", "Store", "WebSite") for t in home_types)

    if has_product:
        return CheckResult("structured data", "pass", 100,
                           "schema.org Product markup present on product pages", details)
    if has_product_group:
        return CheckResult(
            "structured data", "pass", 90,
            "Product pages use schema.org ProductGroup (newer type; older agent "
            "parsers looking for Product may miss it)", details)
    if has_org or home_types or product_types:
        return CheckResult("structured data", "warn", 45,
                           "Some schema.org markup, but no Product/ProductGroup "
                           "type on the sampled product page", details)
    return CheckResult("structured data", "fail", 0,
                       "No schema.org JSON-LD markup detected",
                       details + ["Agents rely on structured data to extract price, "
                                  "availability and identity. Without it they fall "
                                  "back to fragile scraping."])


# --------------------------------------------------------------------------
# cart / checkout handoff
# --------------------------------------------------------------------------

def check_checkout(session, base_url, timeout, products=None):
    details = []
    score = 0

    cart = _get(session, base_url + "/cart.js", timeout)
    cart_ok = cart is not None and cart.status_code == 200
    if cart_ok:
        try:
            cart.json()
            details.append("/cart.js returns live cart state as JSON (Shopify AJAX API open)")
            score += 50
        except ValueError:
            details.append("/cart.js responded but not with JSON")
    else:
        details.append(f"/cart.js unavailable (HTTP {cart.status_code if cart is not None else 'no response'})")

    variant_id = None
    if products:
        for p in products:
            for v in p.get("variants", []):
                if v.get("id"):
                    variant_id = v["id"]
                    break
            if variant_id:
                break
    if variant_id:
        # A GET against cart/add.js with an id is the classic deep-link checkout
        # handoff: /cart/{id}:1 also works. We only probe with GET, never POST,
        # so nothing is ever added to a real cart.
        probe = _get(session, f"{base_url}/cart/{variant_id}:1", timeout)
        if probe is not None and probe.status_code == 200:
            details.append(f"Deep-link add-to-cart works (/cart/{variant_id}:1 -> cart page)")
            score += 50
        else:
            details.append("Deep-link add-to-cart (/cart/{variant}:1) did not resolve")
    else:
        details.append("No variant id available to test deep-link add-to-cart")

    if score >= 80:
        status = "pass"
    elif score >= 40:
        status = "warn"
    else:
        status = "fail"
    summary = ("Agent can read cart state and hand off to checkout via deep links"
               if status == "pass" else
               "Checkout handoff is limited - agents may not be able to complete purchases")
    return CheckResult("checkout handoff", status, score, summary, details)


# --------------------------------------------------------------------------
# page structure cleanliness
# --------------------------------------------------------------------------

class _StructureProbe(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.h1 = 0
        self.imgs = 0
        self.imgs_alt = 0
        self.scripts = 0
        self.semantic = set()
        self.text_len = 0
        self._in_script = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h1":
            self.h1 += 1
        elif tag == "img":
            self.imgs += 1
            if (a.get("alt") or "").strip():
                self.imgs_alt += 1
        elif tag == "script":
            self.scripts += 1
            self._in_script = True
        elif tag in ("main", "nav", "header", "footer", "article", "section"):
            self.semantic.add(tag)

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False

    def handle_data(self, data):
        if not self._in_script:
            self.text_len += len(data.strip())


def check_page_structure(home_html: str):
    probe = _StructureProbe()
    probe.feed(home_html)
    alt_pct = round(100 * probe.imgs_alt / probe.imgs) if probe.imgs else 100
    kb = round(len(home_html) / 1024)

    details = [
        f"{probe.h1} <h1> tag(s) on homepage",
        f"image alt-text coverage: {alt_pct}% ({probe.imgs_alt}/{probe.imgs})",
        f"semantic landmarks: {', '.join(sorted(probe.semantic)) or 'none'}",
        f"{probe.scripts} <script> tags, {kb} KB of HTML, "
        f"~{probe.text_len} chars of visible text",
    ]

    score = 100.0
    if probe.h1 == 0:
        score -= 25
        details.append("Missing <h1> - agents lose the primary page topic")
    elif probe.h1 > 1:
        score -= 10
    if alt_pct < 60:
        score -= 25
        details.append("Low alt-text coverage hurts vision-capable agents")
    elif alt_pct < 85:
        score -= 10
    if not probe.semantic:
        score -= 20
        details.append("No semantic landmarks (main/nav/header) - harder to segment the page")
    text_ratio = probe.text_len / max(len(home_html), 1)
    if text_ratio < 0.05:
        score -= 20
        details.append(f"Very low text-to-markup ratio ({text_ratio:.1%}) - "
                       "page is mostly code, costly for context windows")

    score = max(score, 0)
    status = "pass" if score >= 80 else "warn" if score >= 50 else "fail"
    return CheckResult("page structure", status, score,
                       f"Homepage parseability: {alt_pct}% alt coverage, "
                       f"{len(probe.semantic)} landmark tags, {kb} KB HTML",
                       details)


def detect_platform(home_html: str, headers) -> str:
    hay = home_html.lower()
    if "x-shopify" in " ".join(f"{k}: {v}" for k, v in headers.items()).lower() \
            or "cdn.shopify.com" in hay or "shopify" in hay:
        return "Shopify"
    if "magento" in hay:
        return "Magento"
    if "woocommerce" in hay:
        return "WooCommerce"
    if "bigcommerce" in hay or "bigcommerce" in " ".join(headers.values()).lower():
        return "BigCommerce"
    if "squarespace" in hay:
        return "Squarespace"
    return "unknown"
