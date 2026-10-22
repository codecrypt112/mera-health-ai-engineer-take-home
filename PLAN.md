# PLAN

## Breakdown

1. **Measure before touching anything.** Build `eval.py` against the 8 gold reports before
   changing a line of extraction logic. A "better" extractor is only a claim until there's a
   number before and a number after, from the same scorer.
2. **Diagnose from the documents, not just the code.** Read every one of the 12 source
   reports by eye (not just the gold JSON) before deciding what's broken — gold labels show
   *that* something is wrong, not *why*, and the 4 ungoaled reports only have the documents.
3. **Fix the smallest set of changes that addresses the top 3 diagnosed failures.** Resist
   adding anything the eval can't justify.
4. **Re-measure the same way, plus one change I expect might not pay for itself**, and report
   it honestly either way.
5. Write up cost/latency, the diagnosis, and the three discussion questions last, from
   evidence gathered above — not from a prior opinion.

## What I'll look at first

- `extract.py` end to end — the prompt, the image-loading/rasterization step, and the
  JSON-cleaning step, since bugs in any of these three stages would show up as different
  symptoms in the eval.
- Every PDF/JPG in `reports/`, opened directly, especially the ones without gold labels —
  I can't score them, but I can read them.
- Whether the starter kit's assumptions (one page, numeric values, a reference range always
  exists) actually hold across 12 real documents from "hundreds of labs in wildly different
  formats," per the brief's own framing.

## What I expect to find

- The PDF→image step likely only renders a single page, which would silently drop data from
  any multi-page report.
- Lab values aren't all numbers — urinalysis and similar qualitative panels report text
  ("Negative", colour, cell-count ranges) — and forcing everything through a numeric parser
  probably destroys some of them.
- A prompt that pushes hard for "be thorough, don't miss any test" with no instruction for
  "this isn't a lab report" or "I don't actually know this range" likely encourages the model
  to fabricate rows or reference ranges rather than abstain.
- `GEMINI_API_KEY` isn't available in this environment, so I'll run everything through
  OpenRouter instead (model choice documented in the README) and measure *that* as the
  baseline, before changing any extraction behavior — consistent with the starter kit's own
  instruction to measure a substituted provider before changing behavior.

It's fine if some of this turns out wrong once the eval actually runs — that's what step 1 is
for. This plan isn't getting edited after it's committed.
