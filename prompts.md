# Prompts

## Baseline (unmodified, as given in the starter kit)

```
You are an expert at reading medical lab reports.

Look at this lab report and extract every test result from it.

Return JSON in exactly this format:
{"tests": [{"test_name": "...", "value": 0.0, "unit": "...", "reference_range": "...", "status": "normal"}]}

Rules:
- Use the standard medical name for each test.
- value must be a number.
- If a reference range is missing from the report, fill in the standard reference range for that test.
- status is "normal", "high" or "low", based on the value and the reference range.
- Be thorough. Do not miss any tests.
```

I didn't write this one - it's the starter kit's prompt, used verbatim to measure the
baseline. Two of its five rules are the root cause of two of the three diagnosed failure
modes (see README.md): "value must be a number" destroys qualitative results, and "fill in
the standard reference range" fabricates clinical data the source document doesn't contain.

## Final (fixed)

```
You are an expert at reading medical lab reports.

Look at this lab report and extract every test result from it. If more than one page
image is given, they are pages of the same report - extract tests from every page.

Return JSON in exactly this format:
{"tests": [{"test_name": "...", "value": 0.0, "unit": "...", "reference_range": "...", "status": "normal"}]}

Rules:
- Use the standard medical name for each test.
- value is the result exactly as printed: a plain number as a number, otherwise the exact
  text (e.g. a range like "2-3", a qualitative result like "Negative", a description like
  "Dark Yellow"). Never collapse a range into a single average number.
- reference_range is exactly as printed on the report. If the report does not print one for
  a test, use null - do not invent a "standard" range; different labs and methods use
  different ranges, and a wrong one is misleading.
- status is "normal", "high" or "low" based on the value and the reference range, or null if
  the value is not comparable to a range (e.g. a qualitative or descriptive result).
- Be thorough. Do not miss any tests.
```

Line-by-line rationale for every change:

- **"If more than one page image is given..."** - added because the pipeline now sends every
  page of a multi-page PDF in one request (previously only page 1 was ever rendered). Without
  this sentence the model has no instruction for what multiple images in one turn mean.
- **"value is the result exactly as printed..."** - replaces "value must be a number." The
  old wording was actively wrong against `schema.json` (which allows `string | number | null`)
  and, worse, was causing the model itself to invent an averaged number for ranges like "2-3"
  (observed as `2.5` in the baseline output on `report_06`) instead of reporting what's on the
  page. Telling it to preserve the literal text removes the incentive to fabricate a number.
- **"reference_range is exactly as printed... do not invent a 'standard' range"** - replaces
  "fill in the standard reference range for that test." The old instruction directly caused 3
  fabricated ranges on `report_03` (T3, T4, Glycated Hb) where the source prints none and the
  gold label correctly has `null`. "Standard" ranges also vary by lab, method, age and sex
  (several source reports print this caveat verbatim), so a single invented number is actively
  misleading, not just imprecise.
- **"...or null if the value is not comparable to a range"** - minor clarity addition so the
  model doesn't force a `"normal"/"high"/"low"` label onto a color or qualitative result where
  that judgment doesn't make sense. `status` isn't schema-required or gold-scored, so this is
  low-stakes, but it's a one-line fix for something the baseline was doing a little oddly.
- **"Be thorough. Do not miss any tests."** - kept unchanged. Still the right instruction; the
  problem was never recall on tests the model could see, it was fabrication and page coverage.
