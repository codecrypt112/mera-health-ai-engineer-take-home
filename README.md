# Lab report extraction: submission

**Setup differs from the starter kit:** no `GEMINI_API_KEY` was available in the environment
this was built in. `extract.py` still calls Gemini's native REST API (same endpoint, same
code shape), but uses `GEMINI_API_KEY` against model `gemini-3.5-flash-lite` instead of the
default `gemini-3.8-flash`, which returned persistent `503 overloaded` errors when tested.
Same tier ("flash-lite"), same provider, no new Python dependencies in `extract.py` itself.
`eval.py` additionally uses `rapidfuzz` (already present in this environment; add to a fresh
venv with `pip install rapidfuzz` if missing).

## Run it

```bash
export GEMINI_API_KEY=...                        # see prerequisites at the bottom
python3 extract.py reports/report_01.pdf         # entry point, unchanged contract
python3 eval.py --label baseline                 # scores against gold/, caches under results/
python3 eval.py --label final                    # re-run after any extract.py change
python3 eval.py --label final --refresh          # force fresh API calls instead of cache
```
`eval.py` is one command, reuses cached predictions under `results/<label>/` so repeat scoring
is free, and only calls the model again with `--refresh`. `results/baseline*` and
`results/final*` in this repo are the actual runs behind every number below.

## 2. Eval design

`eval.py` runs the real `python extract.py <file>` subprocess per gold report (not a
reimplementation of its logic), then fuzzy-matches predicted rows to gold rows by test name
(RapidFuzz + an acronym-aware bonus, since reports mix "FBS" and "Fasting Blood Sugar" for the
same test) and scores, per report: **recall** (gold rows found), **precision** (predicted rows
that aren't hallucinated extras), and, conditional on a matched row, **value/unit/range
accuracy**. A row only counts toward `value_recall` if its value is actually right - a test
that's "found" with a wrong number is worse than not found, since a doctor acts on the number.
I report the breakdown, not one blended score, because a single number hides *which* kind of
error got better - exactly what "would your number move if the pipeline got worse in a way
that matters" is asking. The 4 ungoaled reports (04, 07, 10, 12) have no gold file; I ran
`extract.py` on each and compared the output to the source document by eye (see decision log).

## 3. Diagnosis: three failure categories (evidence from the 8 gold reports)

| # | Category | Evidence | Root cause |
|---|---|---|---|
| 1 | **Multi-page truncation** | `report_11.pdf` is 2 pages. Baseline recall **56%** (30/54 rows) - exactly page 1's content (Haemogram+Lipid+Liver). Page 2 (Renal, Thyroid, Glycaemic, Vitamins, Urinalysis) never reached the model. | `load_image()` called `pdftoppm -f 1 -l 1`, hardcoded to page 1 regardless of document length. |
| 2 | **Qualitative/range values destroyed** | `report_06.pdf` (urine routine): baseline `value_recall` **17%**. "Dark Yellow"→`null`, "Negative"→`null`, "2-3"→`2.5` (the model averaged the range into a number). | Prompt said "value must be a number" (contradicts `schema.json`'s `string\|number\|null`), and `to_number()` force-cast every value through `float()`, returning `None` on failure. |
| 3 | **Fabricated reference ranges** | `report_03.pdf`: gold has `reference_range: null` for T3, T4, Glycated Hb (genuinely not printed on the source). Baseline invented specific-looking ranges for all 3 ("80 - 200", "4.5 - 12.0", "< 5.7"). | Prompt explicitly said: "If a reference range is missing from the report, fill in the standard reference range for that test." |

I also checked a 4th hypothesis - that the "be thorough" instruction would make the model
fabricate results for a document with no actual lab values (`report_12.jpg` is a prescription
note, not a report). It didn't: baseline correctly returned `{"tests": []}`. I'm not claiming
this as a 4th failure or adding an unverified guardrail for it - no evidence it's broken.

## 4. Fix + before/after (8 gold reports, same model both runs)

| Metric | Baseline | Final | |
|---|--:|--:|---|
| Recall | 92% | **100%** | all rows found |
| Precision | 97% | **100%** | no hallucinated extra rows |
| Value recall | 82% | **99%** | the number a doctor would actually read |
| Value accuracy (matched rows) | 89% | **99%** | |
| Unit accuracy | 100% | 100% | never a problem |
| Range accuracy | 90% | 95% | see note below |

Per-report, the three diagnosed cases: `report_11` recall 56%→**100%**; `report_06`
value_recall 17%→**100%**; `report_03` range_acc 57%→**100%** (ranges now `null` to match
gold, not fabricated). Full per-report table in `results/baseline_summary.json` /
`results/final_summary.json`.

**Range accuracy note:** the 95% isn't 100% because of `report_06` only (58%), and I checked
it by hand - the model includes the unit inside the range string as printed ("0 - 5 /hpf")
*in addition to* the separate `unit` field; gold strips the unit out of the range. No
information lost, just a formatting difference from this gold labeler's convention. I did not
change the prompt to chase this - it would be fitting one labeler's style, not a general fix.

## 5. Cost and latency

| | Baseline | Final |
|---|--:|--:|
| Mean cost/report (paid-tier projection*) | $0.0025 | $0.0028 |
| Mean latency/report | 33.8s | 14.9s |
| Total for 8 reports | $0.0198 | $0.0226 |

\*Actual calls ran on Gemini's free tier (`$0` billed). Projected cost uses published paid
pricing for `gemini-3.5-flash-lite` ($0.30 / $2.50 per 1M input/output tokens, checked
2026-10-22) applied to the real token counts in `results/*/report_*.usage.json`. The final
version costs slightly more per report (multi-page reports send more image tokens) but is
**over 2x faster** - the baseline's higher latency was dominated by one slow outlier call, not
a systematic difference. **At 10,000 reports/day:** ~$28/day at paid-tier pricing, trivial
infra cost; the real constraint at that volume is request concurrency (Gemini free-tier rate
limits would not hold up - paid tier and parallel workers would be required), not per-token
cost.

## 6. What I tried that didn't help

**Self-consistency (3x sampling + majority vote).** The only value mismatch left in the final
eval is `report_01`'s Platelet Count (gold `304000`, predicted `340000`). I hypothesized this
was a stochastic misread and tested 3 extra calls at temperature 0.7 to vote across. All 3
returned the identical value (`340000`) - zero change, for 3x the cost. I then rendered the
source PDF at 300 DPI and read it directly: **it prints "340000."** The gold label is the one
that's wrong (digit transposition), not the model - at temperature 0, temperature 0.7×3, and
DPI 60 or 150 alike. Self-consistency can't fix an error that isn't stochastic noise; this
model is already deterministic and correct on machine-printed numeric tables, so voting bought
nothing. I did not add self-consistency to `extract.py`. Full writeup:
`results/experiment_self_consistency/README.md`. I did **not** edit the gold file to fix my
own score - flagging it here instead.

## Three questions

**Would retrieval (RAG) help here?** Not for the core extraction step - there's no corpus to
retrieve from; it's single-document, single-call extraction, not open-ended Q&A. The one place
retrieval-style lookup could plausibly help is validating/normalizing reference ranges or test
names against a maintained lab-test ontology (e.g. LOINC) as a cross-check after extraction,
not as part of generation. That's a structured lookup with a fixed answer, not RAG's strength
(synthesizing from retrieved passages), so I'd call it low-value for the actual bottleneck.

**Would fine-tuning help here?** No, not at this data scale or problem shape. 12 reports (8
labeled) is nowhere near enough to fine-tune reliably, and the three diagnosed failures were
all fixed by a prompt/pipeline change, not a capability gap the base model lacked. More
fundamentally: the brief describes "hundreds of labs in wildly different formats" - fine-tuning
on today's template mix risks overfitting to formats you've seen and generalizing worse to new
lab templates than a well-prompted frontier multimodal model does zero-shot. It would only be
worth revisiting after collecting thousands of labeled reports across many lab templates, and
even then the per-report economics here (~$0.003/report) don't obviously justify the training
and maintenance overhead versus iterating on the prompt and validation layer.

**Would a multi-step agent help here, or is that complexity unearned?** Unearned for this
pipeline as it stands. All three diagnosed failures were fixed by changing what goes into a
single call (all pages) and what the prompt asks for (don't invent values/ranges) - not by
giving the model tools, multiple turns, or a planning loop. An agent adds latency, cost, and
non-determinism with no evidence the remaining errors are things a multi-step loop would catch
that a single well-specified call doesn't. The one place I'd consider a *second* deterministic
step (not an "agent") is a cheap post-hoc sanity check - e.g. flag rows where a numeric value
falls wildly outside any plausible physiological range - but that's pipeline shape, not agency.

## Decision log

| What happened | What I did |
|---|---|
| No `GEMINI_API_KEY`; tried OpenRouter free-tier instead | Found free vision models unreliable on a harder image; user then supplied a working Gemini key, so reverted to native Gemini (see below) |
| User pasted 2 live API keys directly into the conversation | Flagged both immediately as now-exposed secrets; never wrote either to a committed file, logged, or echoed them; told user to rotate before sharing transcripts |
| User asked for "sheaf Laplacian"/topological-memory techniques for novelty | Pushed back with the brief's own anti-over-engineering criteria; user agreed to stay restrained |
| Suspected OpenRouter models were fabricating `report_08.jpg`'s content (returning `report_06`'s data) | Verified independently via PIL/PyMuPDF decode + git blob hashes - turned out to be **my own** mislabeling of gold files 06/08 in my first pass, not a bug in the kit or a model hallucination. Corrected the record before building the eval on it. |
| Same type of error recurred for the 4 ungoaled reports (04/07/10/12) | Re-verified each file individually (not in one large batch) before writing the diagnosis. `report_12.jpg`, not `report_04.pdf`, is the prescription note. |
| Eval flagged `report_01`'s Platelet Count as wrong (89% value accuracy) | Rendered the source at 300 DPI and read it directly - the gold label has the digit transposition, not the model. Did not edit gold data to raise my own score. |
| `report_03`'s eval showed 75% recall (looked like a real miss) | Checked by hand: `FBS`/`PPBS` weren't fuzzy-matching their full-name equivalents. Fixed the eval's acronym handling (not the extractor) since this was a measurement gap, not an extraction bug. |
| Whether to add an "empty result" guardrail for non-lab documents | Tested on the actual prescription note (`report_12.jpg`) first - baseline already handles it correctly. Did not add unverified prompt complexity for an unconfirmed failure mode. |
| Code review flagged the acronym bonus as exact-match only, missing cases like `PPBS` against "Postprandial Blood Sugar" | Verified independently with `rapidfuzz`, confirmed the gap, and switched to a fuzzy (80%+) acronym comparison instead of exact equality. Also fixed a precision formula that scored a total extraction failure as 100% precision, and two defensive gaps in `extract.py` (bool/int coercion, silent parse failures). |

---
Prerequisites unchanged from the starter kit (Python 3.10+, Poppler/`pdftoppm` on `PATH`,
`GEMINI_API_KEY` via your own secret manager - never commit it). Setup error codes (401/403
key or access, 404 model, 429 quota, 503 overloaded - retry or swap `GEMINI_MODEL`) also
unchanged; see git history for the original kit's full walkthrough.
