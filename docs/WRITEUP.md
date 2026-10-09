# Write-up: limitations and next steps

## Design in one paragraph
The bot is a small retrieval-augmented pipeline with **deterministic checks around the LLM**. A polite crawler
reads only the 7 given URLs. Extraction keeps the page's structure: tables become self-describing rows, and
schema.org prices become explicit "starting-from" facts. Chunks carry their page and section names so the three
near-identical package pages can't be confused. A router turns follow-ups and non-English questions into a
standalone English search query. Hybrid retrieval (BM25 + local embeddings) feeds numbered passages to the answer
model, which must cite them. Code then checks that **every price in the answer exists in the cited passages**,
attaches the URLs itself, and falls back to an honest "not on our pages + free quote" message when grounding fails.

## Known limitations
- **Number check, not claim check.** The guardrail proves every figure came from the cited text, but not that it
  is attached to the right item. Example: the bot summarised add-ons as "$2,600–$7,200", a range assembled from
  two real figures. Scope mix-ups (e.g. a worldwide range presented as a country price) are prevented by
  prompt rules and model choice, not by code.
- **Small knowledge base by design.** Comparisons beyond the 5 clinic rows on the cost-comparison page (Thailand,
  Croatia, Turkey, Mexico) aren't possible. The page's "Load more" rows come from an `/ajax/` endpoint, which
  robots.txt disallows, so they're excluded.
- **Source data quirks are passed through.** For example, the price list has one row with no name, and the
  comparison page's average and range figures change between crawls. The bot reports what the page says at the
  last refresh (see `/api/admin/sources` for the timestamp).
- **Ephemeral storage on the free host.** SQLite logs (unanswered questions, leads) reset when Render redeploys or
  restarts. Each row is also written to the service logs.
- **Sessions are in memory.** Follow-up context is lost after a restart or after 2 hours of inactivity.
- **Latency** is about 3–6 s (two LLM calls). The free Render instance may also cold-start after inactivity.
- **The intent router can misfire** on ambiguous messages. It defaults to a normal grounded answer, which is the
  safe direction.
- **No streaming.** The UI shows a "searching" indicator until the full answer arrives.

## What I would do with another week
1. **Claim-level verification:** a second, cheap LLM pass (or NLI model) that checks each sentence against its
   cited passage, plus structured price records (item, option, price, currency, scope) so price answers can be
   checked against a record, not just a number.
2. **Evaluation harness in CI:** a labelled set of ~100 questions (including hidden-style variants and
   adversarial prompts) scored for groundedness, citation accuracy and refusal correctness on every prompt or
   model change.
3. **Change-aware refresh:** a scheduled crawl that diffs the extracted prices and alerts on changes, and
   re-embeds only the pages that changed.
4. **Durable storage:** Postgres (or Render disk / Supabase) for leads and the content-gap log, with a small
   admin page and CSV export, and leads forwarded to PlacidWay's CRM or email.
5. **Better multilingual retrieval:** a multilingual embedding model (e.g. `multilingual-e5-small`) so the user's
   original wording also helps retrieval, plus localised UI strings.
6. **UX:** streamed answers, clickable follow-up suggestions, and a stricter rate limit / abuse protection on the
   public endpoint.
