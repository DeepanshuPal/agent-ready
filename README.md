# agent-ready

Audit any ecommerce store for AI shopping-agent readiness, from the command line.

Shopping agents (ChatGPT, Claude, Perplexity, and the checkout copilots built on
top of them) do not browse stores the way humans do. They read `robots.txt`,
look for machine-readable catalogs, parse schema.org markup, and need a
deterministic way to hand a cart back to a human for payment. Most stores were
never built for that. This tool measures how ready a store actually is.

## What it checks

| check | weight | what it looks at |
|---|---|---|
| `products.json` | 20% | Is there a structured, machine-readable catalog? For Shopify stores: coverage of descriptions, images, prices, SKUs, availability flags across products and variants. |
| structured data | 16% | schema.org JSON-LD on the homepage and a sampled product page (`Product` / `ProductGroup`, price, organization identity). |
| agent access | 14% | Fetches the homepage and a sampled product page as ChatGPT-User, Claude-User and OAI-SearchBot identify themselves, and compares each with a normal browser fetch. Flags blocks (403 and friends), bot-challenge pages, and near-empty shells. Robots rules say what a store intends; this shows what its CDN actually does. |
| `robots.txt` | 12% | Blocked AI bots, weighted by what the block costs: shopper-driven fetchers (ChatGPT-User, Claude-User, Perplexity-User) cost the most, answer-engine indexers (OAI-SearchBot, Claude-SearchBot, PerplexityBot, Amazonbot) cost less, and model-training crawlers (GPTBot, ClaudeBot, Google-Extended, CCBot, ...) barely count - opting out of training is a fair choice for a store. Declared sitemaps earn 10 points. |
| checkout handoff | 12% | Can an agent read cart state (`/cart.js`) and deep-link a variant into a cart (`/cart/{variant}:1`) without scraping? Probed with GET deep links in a throwaway session - the probe creates a session-scoped cart (that session cart IS the handoff mechanism) but never POSTs, never completes checkout, and never touches store state any shopper can see. |
| page structure | 10% | Homepage parseability: single `<h1>`, image alt-text coverage, semantic landmarks, text-to-markup ratio. |
| `llms.txt` | 7% | Whether `/llms.txt` exists as a real text file (a catch-all HTML page doesn't count), whether it has a title, a one-line summary and described links, and whether the homepage links to it (`<a>` or `<link>`) so agents don't have to guess. |
| status codes | 5% | One GET to a made-up URL: a missing page should say 404, not 200 or a redirect home. Also flags bot challenges served as HTTP 200 and 429s without `Retry-After`. |
| server-rendered facts | 4% | For non-Shopify or headless storefronts: are the product name and price in the HTML the server sends, or only after JavaScript runs? Skipped on Shopify stores with an open catalog feed. |

Each check scores 0-100; the total is the weighted sum, graded A-F. A skipped check drops out and the other weights scale up to fill its share.

## Why this matters

The stores that win agent-driven demand will be the ones agents can actually
use: a clean product feed beats a beautiful hero video when the buyer is an
LLM. This is the audit I run before touching a store's agent-readiness work -
the output is the prioritized fix list, not the score.

## Install and run

```bash
git clone https://github.com/DeepanshuPal/agent-ready.git
cd agent-ready
pip install -r requirements.txt

python3 agent_ready.py https://www.allbirds.com
python3 agent_ready.py https://www.gymshark.com --json
```

Requires Python 3.9+ and `requests`. No other dependencies.

## Bulk mode

Audit a whole list of stores and get one ranked report:

```bash
# stores.txt: one store URL per line (# comments and blank lines ok)
python3 agent_ready.py --bulk stores.txt --out audits/run-1/results --workers 5
```

Stores are audited in parallel; one broken or unreachable store is recorded
as an error row and does not kill the batch. Output is a CSV (one row per
store: rank, score, grade, per-check scores, top fix) plus a JSON report with
the same rows and batch aggregates (median/mean score, audited vs failed
counts). This is how the [50-store run](audits/2026-09-10-dtc-50/) was produced.

## Sample output

Against `gymshark.com` (full output in [`examples/`](examples/)):

```
agent-ready report: https://www.gymshark.com
platform: Shopify   score: 31/100 (F)   audited in 2.2s
------------------------------------------------------------------------
[PASS] robots.txt         100/100  No AI shopping agents blocked in robots.txt
[FAIL] llms.txt             0/100  No llms.txt - agents get no curated map of the store
[FAIL] products.json        0/100  products.json not available (HTTP 403)
[WARN] structured data     45/100  Some schema.org markup, but no Product type found on the sampled product page
[FAIL] checkout handoff     0/100  Checkout handoff is limited - agents may not be able to complete purchases
[FAIL] page structure      45/100  Homepage parseability: 33% alt coverage, 6 landmark tags, 1658 KB HTML
------------------------------------------------------------------------
top fixes, in order of leverage:
  1. Expose a machine-readable catalog (Shopify products.json, a Google Merchant feed, or a /products API).
  2. Add schema.org Product JSON-LD (name, price, currency, availability) to every product page.
  3. Publish /llms.txt describing the store, catalog endpoints and policies in plain markdown.
```

And `allbirds.com`, for contrast - a store that is genuinely close to agent-ready:

```
agent-ready report: https://www.allbirds.com
platform: Shopify   score: 96/100 (A)
[PASS] robots.txt         100/100  No AI shopping agents blocked in robots.txt
[PASS] llms.txt           100/100  llms.txt present (87 lines of agent-facing docs)
[PASS] products.json      100/100  Machine-readable catalog exposed: 250 products, 100% with descriptions, 100% of variants priced
[PASS] structured data     90/100  Product pages use schema.org ProductGroup (newer type; older agent parsers looking for Product may miss it)
[PASS] checkout handoff   100/100  Agent can read cart state and hand off to checkout via deep links
[PASS] page structure      90/100  Homepage parseability: 62% alt coverage, 5 landmark tags, 655 KB HTML
```

Interesting spread in practice: Allbirds and tentree both publish `llms.txt`
already; Gymshark 403s its own `products.json` for scripted clients, which
means most shopping agents can't see its catalog at all.

## Real runs

- [50 DTC stores, 2026-09-10](audits/2026-09-10-dtc-50/) - full ranked
  results, headline numbers, and the findings worth reading (including one
  store whose llms.txt tries to prompt-inject AI readers).

## Roadmap

- Google Merchant Center feed linting (when a store has no products.json)
- ~~bulk mode: audit a list of stores, emit one CSV/JSON report~~ shipped 2026-09-12
- UCP/ACP checkout-protocol probes
- non-Shopify platform depth (Magento, WooCommerce, custom headless)

## License

MIT
