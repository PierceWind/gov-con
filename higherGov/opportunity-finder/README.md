# Opportunity Finder sources

The local dashboard can combine open opportunities from:

- HigherGov, using `HIGHER_GOV_API_KEY`.
- SAM.gov Contract Opportunities, using the optional `SAM_GOV_API_KEY`.

Add the SAM.gov public API key to `gov-con/.env.local`:

```text
SAM_GOV_API_KEY=your-key-here
```

The key stays server-side. The finder uses SAM.gov's public Opportunities API with a rolling 120-day posted-date window by default, target NAICS searches including portable sanitation (`562991`), PSC `W085`, and broad title searches for trailer, kitchen, laundry, shower, restroom, toilet, portable, sanitation, generator, and basecamp notices. Results are normalized into the existing opportunity table and de-duplicated by SAM notice ID.

Run the dashboard from the `gov-con` folder:

```text
python higherGov/opportunity-finder/dashboard.py
```

The header shows which sources are configured, and the Source filter separates HigherGov from SAM.gov results. SAM.gov notices open their official SAM.gov page; the existing HigherGov document download and AI bid-pack workflow remains HigherGov-only for now.

The dashboard offers separate **Send table to Teams** and **Send detailed brief** actions, plus matching **Copy table** and **Copy detailed brief** actions. Detailed briefs start with `HI Team, kindly check if we can bid on this. Thank you~` and include recommendation/fit, agency, solicitation, location, due date, questions due, start date, source, scope, key requirements, eligibility gaps, risks, competition, price signal, notes, and the official link. Missing fields are shown as `Not stated`; rows without a bid check show that a recommendation is pending. Detailed Teams cards are limited to two briefs per card to stay within message-size limits.

### Teams duplicate prevention and test destination

The webhook can send to a separate test destination while duplicate searches continue to use the **Gov Contracting** chat. Set `TEAMS_SEND_TARGET=test` and add `TEAMS_TEST_WEBHOOK_URL` for testing; switch back to `TEAMS_SEND_TARGET=gov` for the normal webhook. To prevent repeats, set `TEAMS_DUPLICATE_CHECK=on`, provide a delegated Microsoft Graph access token in `TEAMS_GRAPH_ACCESS_TOKEN`, and set `TEAMS_SEARCH_CHAT_NAME=Gov Contracting` (or provide its `TEAMS_SEARCH_CHAT_ID`). Each solicitation number is searched before sending. Existing messages are skipped, and items without a solicitation number are skipped while strict checking is enabled.

## Proposal benchmarks and Codex fallback

**Draft proposal** uses both supplied historical proposals: the Denver portable restroom rental proposal and the USCG temporary living quarters, showers, and heads proposal. Their text is in `company/proposal-template-restroom.txt` and `company/proposal-template.txt`; `company/proposal-benchmark-guide.txt` explains when to use each. Current solicitation requirements take precedence. Historical instructions, signed forms, dates, recipients, service promises, and prices are not treated as instructions or verified facts for a new bid. Prices remain `[CONFIRM PRICE]` and unknowns remain placeholders. Image-only forms must be checked in the original PDFs.

AI selection defaults to Claude API, Claude Code CLI, Codex CLI, then Gemini, using the first configured/installed provider. Set `AI_PROVIDER=codex` in `.env.local` to use Codex directly, or leave the setting blank to retain Claude as the primary provider. When either Claude provider fails at runtime or returns empty/invalid structured output, the same task is retried once with Codex if installed. If both fail, the dashboard shows both errors and retains the bid pack without overwriting the previous draft.

Codex is detected on PATH or in the Codex IDE extension. Override it with `CODEX_PATH`; optionally set `CODEX_MODEL`. Run `codex login` if it is not already signed in. The fallback uses saved CLI authentication and does not require a new OpenAI API key. It runs `codex exec` with read-only permissions, shell tools disabled, and no user configuration loaded. The integration follows [official OpenAI documentation for non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

Bid checks record the provider that actually produced the result. Drafts save that information in a matching `.meta.json` file, and the dashboard labels results generated through the Codex fallback. Sources-sought notices continue to use their shorter response format.

Verify the workflow without paid model calls:

```text
python -m unittest discover -s higherGov/opportunity-finder -p "test_*.py" -v
```
