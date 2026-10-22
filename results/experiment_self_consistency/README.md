# Experiment: self-consistency (majority vote) - didn't help

**Hypothesis:** the one remaining value mismatch in the final eval (report_01's Platelet
Count: gold=304000, predicted=340000) is an OCR-style misread. Sampling 3x at temperature
0.7 and taking a majority vote should catch stochastic misreads that a single greedy
(temperature=0) call might make.

**Test:** 3 extra calls to gemini-3.5-flash-lite on report_01.pdf at temperature=0.7
(report_01_sample0/1/2.json).

**Result:** all 3 samples returned the identical value (340000). Zero change from the
single-sample temperature=0 baseline. Cost: ~3x for one report (~$0.0026 -> ~$0.0078
projected paid-tier), zero accuracy gain.

**Why:** rendered the source PDF at 300 DPI and read it directly (see decision log in
README.md) - the document actually prints "340000". The gold label (304000) is the error,
not the model's output. There was no misread for self-consistency to catch; the model was
right at temperature 0, temperature 0.7 x3, and DPI 60 and DPI 150 alike.

**Conclusion:** self-consistency triples cost for zero benefit here, because this model is
already deterministic and accurate on machine-printed numeric tables - its few remaining
errors (on this report set) aren't sampling noise, so voting across samples of a
deterministic process can't improve it. Did not add self-consistency to extract.py.
