---
name: opportunity-finder
description: Pull new open government contract opportunities from HigherGov for temporary kitchen, laundry, shower, restroom, sleeper, basecamp, and power/water rentals, scored by match %, without repeating ones already reported. Use when asked to find, pull, search, or list contracts or opportunities.
---

# Opportunity Finder

Run from the `gov-con` folder:

```
python higherGov/opportunity-finder/find_opportunities.py [--min-match 0|25|50|100] [--days N] [--dry-run] [--include-seen]
```

- Reads `HIGHER_GOV_API_KEY` from `gov-con/.env.local`. Never ask the user for it.
- First run (or `--days N`): full open-opportunity backfill. It covers every posting within `--lookback` days (default 120): each target NAICS newest-first, plus a keyword scan of every posted day. It takes about an hour, so run it in the background. Later runs only scan what HigherGov captured since the last run, which includes new postings and amendments.
- Keeps only opportunities whose response deadline is **today or later**.
- The keyword query, NAICS list, and exclusions are in the script. The HigherGov API has no keyword parameter, so matching happens locally.
- **No duplicates:** every reported opportunity's `opp_key` (stable across amendments) goes into `data/seen_opportunities.json`, and later runs skip it. `--dry-run` skips this save; `--include-seen` lists seen items again.

## Match %

| Signal | Points |
|---|---|
| Service phrase in title | 50 |
| Service phrase in description | 25 |
| Rent/Lease/Hire in title | 25 |
| NAICS in target list | 25 |
| Each additional distinct phrase | +10 (max 25) |

Every match must contain **one of the keyword phrases** (a NAICS code alone never qualifies; it only adds points) and a rental term. It must not have an excluded or never-match term in its **title**. Never-match terms (default `Solar`) also reject a contract when one appears within 30 characters before a keyword, as in "solar powered light tower rental". Change the rules in the dashboard's Search settings, then click "Re-check saved contracts" or run with `--rescore` to drop saved contracts that no longer match. Descriptions are not checked for excluded terms because SAM boilerplate says "Acquisition" and "purchase order". Tiers: >0%, ≥25%, ≥50%, ≥100%.

## Output

The report goes to `reports/opportunities_<stamp>.md`, grouped by tier, with a matching `.tsv`. The terminal prints a tab-separated "Found these contracts." table: Match %, Source, Title, Solicitation #, Agency, NAICS, PSC, Set-aside, Type, Posted, Response Deadline, Place of Performance, Link, HigherGov, Contact. To go deeper on one result, hand it to `contract-breakdown`.

## Bid check and proposal draft

In the dashboard, open a contract's **Docs** panel, then click **Run bid check** or **Draft proposal**. `proposal.py` does the work:

1. **Bid pack:** `drafts/<solicitation #>/bid-pack.md` holds the HigherGov record, every attachment's summary and full text, past awards by the same agency and NAICS, and award benchmarks for the same NAICS and PSC.
2. **Bid check:** `bid-check.json` gives a verdict (Bid / Maybe / No bid), a 0–100 score, fit, red flags, competition, price signal, must-submit items, gaps, and the deadline.
3. **Draft:** `proposal-draft.md` follows the structure of `company/proposal-template.txt` and uses facts only from `company/profile.md`, the bid pack, and the kit's `UNIT-PRICELIST.md`. Every price is marked `[CONFIRM PRICE]`, and every unknown becomes a `[PLACEHOLDER]` listed under "Before you submit".

The AI runs in this order: `ANTHROPIC_API_KEY` (paid Claude API), then the Claude Code CLI (the user's Claude subscription, found automatically in the VS Code extension folder or set with `CLAUDE_CODE_PATH`), then `GEMINI_API_KEY` (free tier). `AI_PROVIDER=claude|claude-code|gemini` in `.env.local` forces one. In chat, the `bid-check-and-draft` skill does the same work and writes the same files. Keep `company/profile.md` current; the drafts use only facts written there.
