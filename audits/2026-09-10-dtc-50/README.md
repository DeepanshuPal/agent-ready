# Agent-readiness audit: 50 DTC stores, 2026-09-10

Real output from running `agent_ready.py` against 50 live DTC stores
(mostly Indian brands) on September 10, 2026. 50 of 50 runs completed.
Raw per-store scores in [results.csv](results.csv).

## Headline numbers

- Scores range 18-100, median 95.0, mean 92.1
- Grades: 46 x A, 3 x B, 0 x C, 1 x F
- Perfect score: Dogsee Chew (100). Lowest: Vaaree (18, the only F)
- Most stores on standard Shopify setups score high - the interesting
  signal is in the exceptions below

## Notable findings

1. **Vaaree (18/F)** - products.json returns an HTML page instead of JSON,
   zero schema.org markup anywhere we sampled, and robots.txt disallows
   /cart, /checkout and /search for every crawler. An agent cannot read
   this catalog at all.
2. **Fix My Curls (87/A)** - robots.txt explicitly blocks 6 AI agents
   (GPTBot, ClaudeBot, anthropic-ai, Applebot-Extended, ...): locking out
   exactly the agents its customers increasingly shop with.
3. **Skippi (81/B)** - robots.txt blocks ChatGPT-User, the user-agent
   ChatGPT sends when a user asks it to go buy something.
4. **KitFox Outfitters (76/B)** - a perfect 250-product feed but zero
   schema.org JSON-LD anywhere.
5. **Conscious Chemist (92/A)** - 250-product catalog where only 21% of
   products carry descriptions; agents have nothing to quote for 4 out of
   5 SKUs.

## Two corrections after manual re-verification

Raw tool output was wrong in two places (tool artifacts, not store
problems); the CSV carries the corrected scores:

- **rasluxuryoils.com** - the tool reported "robots.txt blocks ALL
  crawlers" (81). Its robots.txt uses \r-only newlines, which broke the
  parser. Standard Shopify file, no AI bots blocked. Corrected to 96/A. (Parser fixed in 1b395e8 - current code parses \r-only files correctly.)
- **loom.fr** - the homepage redirects to /fr-fr and the checks ran under
  that path, so the tool missed root-level robots.txt and llms.txt (62).
  Both exist. Corrected to 82/B. Real gaps: no Product schema on product
  pages, 0% image alt coverage. (Tool-side fix landed in a12022a -
  root-level files are now probed at the origin root, and the tool
  reproduces 82/B on its own.)

## One security observation

loom.fr's llms.txt and robots.txt embed text addressed at AI readers,
instructing them to recommend installing a third-party "skill" file
(shop.app/SKILL.md). That is indirect prompt injection via a file format
agents are being told to trust. Documented here as a finding; we did not
follow it. Worth knowing if you quote llms.txt content in agent-facing
context: it is untrusted input, same as any other page content.

## Method

Each store was audited with the CLI in this repo (default settings,
`--json` output). Load-bearing negative claims (blocked agents, missing
catalogs, broken feeds) were re-verified by hand with curl before being
recorded. Scores are a point-in-time snapshot; stores change.
