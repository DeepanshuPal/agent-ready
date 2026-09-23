# DTC-50 re-audit, 2026-09-24

Same 50 stores as `2026-09-10-dtc-50`, re-run after adding five checks:
agent access (AI user agents vs a browser baseline), tiered robots rules and
sitemap, status codes (soft-404 and bot challenges), llms.txt linkage and
shape, and server-rendered product facts for stores without a product feed.

Baseline ("before") was re-run the same day from the previous commit, so the
deltas below show scoring changes, not site changes.

| | before | after |
|---|---|---|
| mean | 92.1 | 91.2 |
| median | 95 | 93 |
| grades | A45 / B4 / F1 | A46 / B3 / F1 |

- Most stores moved -1 to -3. The cause: llms.txt files that exist but aren't linked from the site or are missing a basic outline. 46 stores score 60 on llms.txt.
- skippi.in 81 -> 91 (B -> A). Its robots.txt blocks ChatGPT-User. That's still a fail, but it's now weighed against the other tiers instead of counting as a total block.
- fixmycurls.com 89 -> 95. It opts out of training crawlers only, which no longer costs points.
- vaaree.com 18 -> 20 (still F). It serves a reCAPTCHA "Checking your browser" page with HTTP 200 to every request, including a browser user agent and a made-up product URL. Status codes score 0.
- Agent access was 100 for all 50 stores. Server-rendered facts were skipped for all 50 (all Shopify with a product feed).
- loom.fr 82 -> 83 (B). allbirds 96 -> 94 (A).
