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


# Bots grouped by what a block actually costs a store. Opting out of model
# training is a legitimate choice and barely matters for shopping; blocking the
# fetchers that act for a live shopper, or the indexes that answer engines
# search, makes the store disappear from agent-led purchases.
USER_FETCHERS = ["ChatGPT-User", "Claude-User", "Perplexity-User"]
SEARCH_INDEXERS = ["OAI-SearchBot", "Claude-SearchBot", "PerplexityBot", "Amazonbot"]
TRAINING_CRAWLERS = ["GPTBot", "ClaudeBot", "anthropic-ai", "Google-Extended",
                     "Applebot-Extended", "Meta-ExternalAgent", "CCBot", "Bytespider"]

ROBOTS_PENALTY = {"user": 25, "search": 15, "training": 3}
ROBOTS_PENALTY_CAP = {"user": 70, "search": 45, "training": 10}
SITEMAP_POINTS = 10


def _bot_tier(bot: str) -> str:
    if bot in USER_FETCHERS:
        return "user"
    if bot in SEARCH_INDEXERS:
        return "search"
    return "training"


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
    for bot in dict.fromkeys(AI_USER_AGENTS + USER_FETCHERS + SEARCH_INDEXERS + TRAINING_CRAWLERS):
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

    by_tier = {"user": [], "search": [], "training": []}
    for bot in blocked:
        by_tier[_bot_tier(bot)].append(bot)

    details = [f"{len(sitemaps)} sitemap(s) declared"]
    if sitemaps:
        details.append("First sitemap: " + sitemaps[0])
    if by_tier["user"]:
        details.append("Blocked shopper-driven fetchers (costly): " + ", ".join(by_tier["user"]))
    if by_tier["search"]:
        details.append("Blocked answer-engine indexers (costly): " + ", ".join(by_tier["search"]))
    if by_tier["training"]:
        details.append("Blocked model-training crawlers (minor - a legitimate opt-out): "
                       + ", ".join(by_tier["training"]))
    if allowed:
        details.append("Explicitly allowed AI agents: " + ", ".join(allowed))

    if star_full_block:
        return CheckResult("robots.txt", "fail", 0,
                           "robots.txt blocks ALL crawlers (Disallow: / under User-agent: *)",
                           details)

    score = 100.0 - SITEMAP_POINTS + (SITEMAP_POINTS if sitemaps else 0)
    for tier, bots in by_tier.items():
        score -= min(ROBOTS_PENALTY[tier] * len(bots), ROBOTS_PENALTY_CAP[tier])
    score = max(score, 0.0)

    costly = by_tier["user"] + by_tier["search"]
    if costly:
        status = "fail" if by_tier["user"] else "warn"
        summary = f"{len(costly)} shopping/answer agent(s) blocked: {', '.join(costly[:4])}"
    elif by_tier["training"]:
        status = "pass"
        summary = (f"Shopping agents allowed; {len(by_tier['training'])} training crawler(s) "
                   "opted out (fine for commerce)")
    else:
        status = "pass"
        summary = "No AI shopping agents blocked in robots.txt"
    if not sitemaps:
        summary += "; no sitemap declared"
        if status == "pass":
            status = "warn" if score < 90 else "pass"
    return CheckResult("robots.txt", status, score, summary, details)


# --------------------------------------------------------------------------
# llms.txt
# --------------------------------------------------------------------------

def _looks_like_html(text: str) -> bool:
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head" in head


def _llms_shape(text: str) -> tuple[int, list[str]]:
    """Score the file's shape out of 40: a title, a short summary, described links."""
    lines = [l.rstrip() for l in text.splitlines()]
    notes = []
    pts = 0
    title = next((l for l in lines if l.strip()), "")
    if title.startswith("# "):
        pts += 10
        notes.append(f"title line: {title[:60]}")
    else:
        notes.append("first line is not a markdown '# ' title")
    body = [l for l in lines[1:] if l.strip()]
    summary = next((l for l in body if not l.startswith(("#", "-", "*"))), "")
    if summary and len(summary) >= 30:
        pts += 10
        notes.append("has a plain-language summary near the top")
    else:
        notes.append("no summary sentence under the title")
    links = re.findall(r"^\s*[-*]\s*\[[^\]]+\]\([^)]+\)(.*)$", text, re.M)
    described = [l for l in links if re.match(r"\s*[:\-\u2013\u2014]\s*\S", l)]
    if links:
        pts += 10
        notes.append(f"{len(links)} markdown link(s)")
    if links and len(described) >= max(1, len(links) // 2):
        pts += 10
        notes.append(f"{len(described)} link(s) carry a description")
    elif links:
        notes.append("links have no descriptions - agents can't tell which to follow")
    else:
        notes.append("no markdown links to key pages")
    return pts, notes


def _homepage_links_llms(home_html: str) -> bool:
    return bool(re.search(r"""<(?:a|link)\b[^>]*href=["'][^"']*llms(?:-full)?\.txt""", home_html or "", re.I))


def check_llms_txt(session, base_url, timeout, home_html: str = ""):
    resp = _get(session, base_url + "/llms.txt", timeout)
    ok = (resp is not None and resp.status_code == 200 and len(resp.text.strip()) > 20
          and not _looks_like_html(resp.text))
    if not ok:
        extra = []
        if resp is not None and resp.status_code == 200 and _looks_like_html(resp.text):
            extra.append("/llms.txt answers 200 with an HTML page (a catch-all route), not a text file")
        return CheckResult(
            "llms.txt", "fail", 0,
            "No llms.txt - agents get no curated map of the store",
            extra + ["llms.txt is the emerging convention for telling LLM agents what a site "
                     "offers and where things live. Cheap to add, increasingly expected."])
    shape_pts, notes = _llms_shape(resp.text)
    linked = _homepage_links_llms(home_html)
    score = 40 + shape_pts + (20 if linked else 0)
    details = [resp.url] + notes + [
        "homepage links to it" if linked else
        "homepage does not link to it (no <a> or <link> pointing at llms.txt) - agents only find it by guessing"]
    status = "pass" if score >= 80 else "warn"
    lines = len(resp.text.strip().splitlines())
    return CheckResult("llms.txt", status, score,
                       f"llms.txt present ({lines} lines); shape {shape_pts}/40, "
                       f"{'linked' if linked else 'not linked'} from homepage",
                       details)


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
        # The /cart/{id}:1 deep link is the classic checkout handoff: it builds
        # a session-scoped cart by design. We probe with GET only, never POST,
        # and never complete checkout - nothing touches store state any other
        # shopper can see.
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


# --------------------------------------------------------------------------
# agent reachability (does the store serve AI agents' own user agents?)
# --------------------------------------------------------------------------

AGENT_PROBE_UAS = {
    "Claude-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; "
                   "Claude-User/1.0; +Claude-User@anthropic.com)",
    "ChatGPT-User": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                    "ChatGPT-User/1.0; +https://openai.com/bot",
    "OAI-SearchBot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; "
                     "OAI-SearchBot/1.0; +https://openai.com/searchbot",
}

CHALLENGE_MARKERS = [
    "cf-chl", "challenge-platform", "<title>just a moment", "attention required! | cloudflare",
    "_incapsula_resource", "px-captcha", "perimeterx", "captcha-delivery.com", "datadome",
    "verify you are human", "are you a robot", "checking your browser",
]


def looks_like_challenge(text: str) -> bool:
    hay = (text or "")[:20000].lower()
    return any(m in hay for m in CHALLENGE_MARKERS)


def _visible_text_len(html: str) -> int:
    probe = _StructureProbe()
    try:
        probe.feed(html or "")
    except Exception:
        return 0
    return probe.text_len


def classify_agent_response(resp, baseline_text_len: int) -> str:
    """ok | blocked | challenge | empty | error, judged against the browser baseline."""
    if resp is None:
        return "error"
    if resp.status_code in (401, 403, 406, 429, 451, 503):
        return "challenge" if looks_like_challenge(resp.text) else "blocked"
    if resp.status_code >= 400:
        return "blocked"
    if looks_like_challenge(resp.text):
        return "challenge"
    tl = _visible_text_len(resp.text)
    if baseline_text_len > 500 and tl < 0.2 * baseline_text_len:
        return "empty"
    return "ok"


def check_agent_access(base_url, baseline_html, timeout, product_url=None, fetch=None):
    """Fetch pages as the agents themselves identify, and compare with a browser."""
    fetch = fetch or (lambda url, ua: requests.get(url, headers={"User-Agent": ua, "Accept": "text/html,*/*"},
                                                   timeout=timeout, allow_redirects=True))
    base_len = _visible_text_len(baseline_html)
    urls = [base_url] + ([product_url] if product_url else [])
    outcomes = {}
    observed = []
    for name, ua in AGENT_PROBE_UAS.items():
        for url in urls:
            try:
                resp = fetch(url, ua)
            except requests.RequestException:
                resp = None
            verdict = classify_agent_response(resp, base_len)
            outcomes[(name, url)] = verdict
            if resp is not None:
                observed.append(resp)
    total = len(outcomes)
    ok = sum(1 for v in outcomes.values() if v == "ok")
    bad = {k: v for k, v in outcomes.items() if v != "ok"}
    details = [f"{ok}/{total} agent fetches served the real page "
               f"({', '.join(AGENT_PROBE_UAS)} on {len(urls)} page(s))"]
    for (name, url), v in bad.items():
        details.append(f"{name} -> {url}: {v}")
    score = 100.0 * ok / total if total else 0.0
    if ok == total:
        return CheckResult("agent access", "pass", score,
                           "Store serves AI agents' own fetchers the same page as a browser", details), observed
    blocked_agents = sorted({name for (name, _), v in bad.items()})
    status = "fail" if score < 50 else "warn"
    return CheckResult("agent access", status, score,
                       f"Store turns away or degrades {', '.join(blocked_agents)} "
                       f"({total - ok}/{total} fetches not served)", details), observed


# --------------------------------------------------------------------------
# honest status codes
# --------------------------------------------------------------------------

def check_status_codes(session, base_url, timeout, observed=None, token=None):
    import secrets as _secrets
    token = token or _secrets.token_hex(6)
    probe_url = f"{base_url}/agent-ready-missing-{token}"
    details = []
    score = 0.0
    resp = _get(session, probe_url, timeout)
    if resp is None:
        details.append("missing-page probe got no response")
        missing_ok = False
    elif resp.status_code in (404, 410):
        missing_ok = True
        score += 60
        details.append(f"a made-up URL returns HTTP {resp.status_code} (honest)")
    else:
        missing_ok = False
        where = resp.url.rstrip("/")
        redirected_home = where in (base_url.rstrip("/"),) or urlparse(where).path in ("", "/")
        details.append(f"a made-up URL returns HTTP {resp.status_code}"
                       + (" after redirecting to the homepage" if redirected_home and resp.url != probe_url else "")
                       + " - a soft 404: agents can't tell a missing product from a real page")

    silent, unhinted = 0, 0
    for r in observed or []:
        if r.status_code == 200 and looks_like_challenge(r.text):
            silent += 1
        elif r.status_code == 429 and not r.headers.get("Retry-After"):
            unhinted += 1
    if silent:
        details.append(f"{silent} response(s) were a bot challenge served as HTTP 200 - agents read it as the page")
    if unhinted:
        details.append(f"{unhinted} rate-limit response(s) (429) without Retry-After")
    if not silent and not unhinted:
        score += 40
        details.append("no silent challenge pages or unhinted rate limits seen")
    elif not silent:
        score += 20

    status = "pass" if score >= 90 else "warn" if score >= 40 else "fail"
    summary = ("Errors and limits are reported with honest HTTP codes" if status == "pass" else
               "Status codes mislead agents: " + ("soft 404s" if not missing_ok else "") +
               (", " if not missing_ok and (silent or unhinted) else "") +
               ("hidden bot challenges" if silent else "") +
               ("rate limits without Retry-After" if unhinted and not silent else ""))
    return CheckResult("status codes", status, score, summary, details)


# --------------------------------------------------------------------------
# product facts in server-rendered HTML (non-Shopify / headless storefronts)
# --------------------------------------------------------------------------

PRODUCT_PATH = re.compile(r"""href=["']((?:https?://[^"']+)?/(?:[a-z-]{2,5}/)?(?:products?|p|item|shop)/[^"'#?]+)["']""", re.I)
PRICE_TEXT = re.compile(r"(?:[$\u20b9\u20ac\u00a3]|rs\.?|inr|usd|eur|gbp)\s?\d[\d,]*(?:\.\d{1,2})?|\d[\d,]*(?:\.\d{1,2})?\s?(?:\u20ac|eur|usd|inr)", re.I)


class _TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def visible_text(html: str) -> str:
    p = _TextOnly()
    try:
        p.feed(html or "")
    except Exception:
        pass
    return " ".join(p.parts)


def find_product_url(base_url: str, home_html: str) -> str | None:
    host = urlparse(base_url).netloc
    for m in PRODUCT_PATH.finditer(home_html or ""):
        url = urljoin(base_url + "/", m.group(1))
        if urlparse(url).netloc.endswith(host.replace("www.", "")) and not url.rstrip("/").endswith(("/products", "/shop", "/p")):
            return url
    return None


def _product_facts(html: str) -> tuple[str | None, str | None]:
    """Name and price as the page declares them (JSON-LD, then meta tags, then <h1>)."""
    name = price = None
    for m in re.finditer(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        nodes = nodes + [n for d in nodes if isinstance(d, dict) for n in (d.get("@graph") or [])]
        for n in nodes:
            if not isinstance(n, dict):
                continue
            t = n.get("@type")
            t = t if isinstance(t, list) else [t]
            if "Product" in t or "ProductGroup" in t:
                name = name or n.get("name")
                offers = n.get("offers")
                offers = offers[0] if isinstance(offers, list) and offers else offers
                if isinstance(offers, dict):
                    price = price or offers.get("price") or offers.get("lowPrice")
    if not name:
        m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
        name = m.group(1) if m else None
    if not price:
        m = re.search(r'<meta[^>]+(?:property|itemprop)=["\'](?:product:price:amount|og:price:amount|price)["\'][^>]+content=["\']([\d.,]+)', html, re.I)
        price = m.group(1) if m else None
    if not name:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
        name = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else None
    return (str(name).strip() if name else None), (str(price).strip() if price else None)


def _price_in_text(price: str | None, text: str) -> bool:
    if price:
        try:
            val = float(str(price).replace(",", ""))
            forms = {f"{val:,.2f}", f"{val:.2f}", f"{val:,.0f}", f"{val:.0f}"} if val == int(val) else {f"{val:,.2f}", f"{val:.2f}"}
            if any(f in text for f in forms):
                return True
        except ValueError:
            if str(price) in text:
                return True
    return False


def check_server_rendered(session, base_url, home_html, timeout, platform, products=None):
    if platform == "Shopify" and products:
        return CheckResult("server-rendered facts", "skip", 0,
                           "Skipped: Shopify storefront with an open catalog feed", [])
    product_url = find_product_url(base_url, home_html)
    if not product_url:
        return CheckResult("server-rendered facts", "skip", 0,
                           "Skipped: no product page link found on the homepage", [])
    resp = _get(session, product_url, timeout)
    if resp is None or resp.status_code != 200:
        return CheckResult("server-rendered facts", "skip", 0,
                           f"Skipped: product page did not load ({product_url})", [])
    html = resp.text
    text = visible_text(html)
    name, price = _product_facts(html)
    name_visible = bool(name) and name.lower()[:40] in text.lower()
    price_visible = _price_in_text(price, text) if price else bool(PRICE_TEXT.search(text))
    details = [f"sampled product page: {product_url}",
               f"declared name: {name or 'none found'}; declared price: {price or 'none found'}",
               f"name in raw page text: {'yes' if name_visible else 'no'}",
               f"price in raw page text: {'yes' if price_visible else 'no'}"]
    score = (50 if name_visible else 0) + (50 if price_visible else 0)
    if not name_visible and name:
        score += 15
    if not price_visible and price:
        score += 15
    status = "pass" if score == 100 else "warn" if score >= 50 else "fail"
    summary = ("Product name and price are in the HTML the server sends - no JavaScript needed"
               if status == "pass" else
               "Key product facts only appear after JavaScript runs - many agents never see them")
    return CheckResult("server-rendered facts", status, score, summary, details)
