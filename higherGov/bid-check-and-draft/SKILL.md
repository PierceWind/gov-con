---
name: bid-check-and-draft
description: Decide whether Temporary 123 should bid on a government opportunity, then draft the proposal or sources-sought response from its bid pack, company profile, capability statement, and proposal template. Use when someone asks to bid-check, score, or vet an opportunity or a batch of them, asks "should we bid", or asks to write, draft, or generate a proposal, quote, or sources-sought/RFI response for a solicitation number or HigherGov opportunity.
---

# Bid Check and Draft

This is the chat version of the dashboard's **Run bid check** and **Draft proposal** buttons. It writes the same files, so results appear in the dashboard's Bid column and Docs panel.

Work from the `gov-con` folder. Opportunities live in `data/opportunities.json`, keyed by `opp_key`; each entry's `Solicitation #` names its folder in `drafts/`.

## Step 1: Get the bid pack

If `drafts/<solicitation #>/bid-pack.md` is missing or older than the latest amendment, build it:

```
cd higherGov/opportunity-finder
python -c "import proposal; proposal.build_bid_pack('<opp_key>')"
```

It holds the HigherGov record, every attachment's summary and full text, past awards by the same agency, and award benchmarks. Find the `opp_key` by matching the solicitation number or title in `data/opportunities.json`.

## Step 2: Read the company facts

Read `company/profile.md` (the facts to use, with items marked CONFIRM), `company/capability-statement.txt`, and, for pricing, `../template/.agents/skills/site-rebuild-standards/UNIT-PRICELIST.md`. For drafting, also read `company/proposal-template.txt`.

Use only facts from these files and the bid pack. Never invent certifications, past performance, unit counts, prices, names, or dates. Write a `[CONFIRM: ...]` placeholder for anything missing.

## Step 3: Bid check

Write `drafts/<solicitation #>/bid-check.json` with exactly these keys: `verdict` ("Bid", "Maybe", or "No bid"), `score` (0–100), `one_line`, `scope`, `fit` (list), `red_flags` (list), `competition`, `price_signal`, `must_have` (list, each with its source), `gaps` (list), `deadline`, `checked` (ISO timestamp), and `ai` (`"claude-code"`).

Check:
- Scope against the profile's competencies and fleet.
- NAICS, PSC, and GSA SIN coverage.
- The set-aside against our status: SBE and MBE only, with no 8(a), WOSB, SDVOSB, or HUBZone.
- Place of performance and the deadline against today's date.
- Mandatory site visits, bonding, and licenses.
- Past winners, the number of offers they faced, and award prices.

For a batch, process each one, then report a table: solicitation, verdict, score, one-line reason.

## Step 4: Draft

The opportunity type decides the format:

- **Sources sought, RFI, special notice, or presolicitation:** write `sources-sought-response.md`. Include:
  - a cover letter asking for a small-business set-aside;
  - company information (UEI, CAGE, DUNS, GSA MAS and SINs, NAICS, PSC, SBE/MBE);
  - capability mapped to each question in the notice, in its order;
  - 2–3 relevant past performance references;
  - "Before you submit".

  Copy `company/capability-statement.pdf` into the folder as `Temporary 123 Capability Statement.pdf`; it is attached to the response.
- **Solicitation or RFQ:** write `proposal-draft.md`. Mirror the template's sections: cover letter, table of contents, company overview, technical approach (Phases I–IV, compliance, utilities, readiness), equipment specifications, price proposal, past performance, and required documentation. If the solicitation prescribes its own sections, use those instead.
  - Tailor every section to the agency, site, CLINs, and period of performance.
  - Mark every price `[CONFIRM PRICE]`.
  - Pick the 3 most relevant past performance references.
  - Copy the capability statement PDF into the folder too.

End every draft with "## Before you submit": every placeholder, every submission requirement with its source, the submission method, and the deadline.

Go one section at a time with the user only if they ask to. The full interactive, form-by-form process is the `proposal-forms-writer` skill.
