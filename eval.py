"""Score extract.py's output against gold/*.json.

Usage:
    python eval.py --label baseline            # score, using cached predictions if present
    python eval.py --label baseline --refresh  # force fresh calls to extract.py

Caches every raw prediction + its cost/latency telemetry under results/<label>/, so repeated
scoring runs are free. Only --refresh re-calls the model. This is deliberately the same
`python extract.py <file>` entry point graders will run - not a reimplementation of its logic.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).parent
GOLD_DIR = ROOT / "gold"
REPORTS_DIR = ROOT / "reports"
RESULTS_DIR = ROOT / "results"

NAME_MATCH_THRESHOLD = 65

# Paid-tier pricing, USD per token (ai.google.dev/gemini-api/docs/pricing, checked 2026-10-22).
# Actual calls in this project ran on the free tier (cost_usd will be 0 from the API); this is
# used only to project a realistic per-report cost for a production volume discussion.
PRICING = {
    "gemini-3.5-flash-lite": {"input": 0.30e-6, "output": 2.50e-6},
    "gemini-3.8-flash": {"input": 0.75e-6, "output": 3.75e-6},
}


def normalize_name(s: str) -> str:
    s = s.lower().strip()
    s = s.replace("ae", "e").replace("oe", "e")  # British -> American spelling
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def acronym(normalized_name: str) -> str:
    return "".join(w[0] for w in normalized_name.split() if w)


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    score = fuzz.WRatio(na, nb)
    # Lab reports mix acronyms and full names for the same test (FBS vs Fasting Blood
    # Sugar, TSH vs Thyroid Stimulating Hormone) - plain fuzzy ratio under-scores these.
    # Fuzzy (not exact) comparison against the derived acronym, since medical acronyms don't
    # always decompose cleanly from the full name's tokens (PPBS vs "Postprandial Blood
    # Sugar" derives "pbs" - PPBS traditionally comes from "Post Prandial", two words).
    compact_a, compact_b = na.replace(" ", ""), nb.replace(" ", "")
    acronym_score = max(fuzz.ratio(compact_a, acronym(nb)), fuzz.ratio(compact_b, acronym(na)))
    if acronym_score >= 80:
        score = max(score, 92)
    return score


def normalize_text(v) -> str:
    if v is None:
        return ""
    s = str(v).lower().strip()
    return re.sub(r"\s+", "", s)


def normalize_numeric(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def values_match(gold_val, pred_val) -> bool:
    gold_num, pred_num = normalize_numeric(gold_val), normalize_numeric(pred_val)
    if gold_num is not None and pred_num is not None:
        return abs(gold_num - pred_num) <= max(0.01, abs(gold_num) * 0.01)
    return normalize_text(gold_val) == normalize_text(pred_val)


def fields_match(gold_val, pred_val) -> bool:
    return normalize_text(gold_val) == normalize_text(pred_val)


def match_rows(gold_tests: list[dict], pred_tests: list[dict]):
    """Greedy best-first bipartite match on fuzzy test_name similarity."""
    pairs = []
    for gi, g in enumerate(gold_tests):
        for pi, p in enumerate(pred_tests):
            score = name_similarity(g.get("test_name", ""), p.get("test_name", ""))
            if score >= NAME_MATCH_THRESHOLD:
                pairs.append((score, gi, pi))
    pairs.sort(reverse=True)
    used_gold, used_pred, matches = set(), set(), []
    for score, gi, pi in pairs:
        if gi in used_gold or pi in used_pred:
            continue
        used_gold.add(gi)
        used_pred.add(pi)
        matches.append((gi, pi, score))
    return matches, used_gold, used_pred


def score_report(gold_tests: list[dict], pred_tests: list[dict], failed: bool = False) -> dict:
    matches, used_gold, used_pred = match_rows(gold_tests, pred_tests)
    n_gold, n_pred = len(gold_tests), len(pred_tests)
    value_ok = unit_ok = range_ok = fully_correct = 0
    for gi, pi, _ in matches:
        g, p = gold_tests[gi], pred_tests[pi]
        v = values_match(g.get("value"), p.get("value"))
        u = fields_match(g.get("unit"), p.get("unit"))
        r = fields_match(g.get("reference_range"), p.get("reference_range"))
        value_ok += v
        unit_ok += u
        range_ok += r
        fully_correct += v and u and r
    recall = len(used_gold) / n_gold if n_gold else 1.0
    # n_pred==0 is ambiguous on its own (a genuinely empty report vs. a crashed call both
    # produce zero predictions) - `failed` disambiguates so a crash isn't scored as 100%
    # precision just because there was nothing to be wrong about.
    precision = 0.0 if failed else (len(used_pred) / n_pred if n_pred else 1.0)
    row_f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    # Headline: a row only "counts" if it's matched AND the value is right - partial credit for
    # unit/range is shown separately but shouldn't inflate the number a clinician would trust.
    value_recall = sum(values_match(gold_tests[gi].get("value"), pred_tests[pi].get("value"))
                        for gi, pi, _ in matches) / n_gold if n_gold else 1.0
    return {
        "n_gold": n_gold, "n_pred": n_pred, "n_matched": len(matches),
        "recall": recall, "precision": precision, "row_f1": row_f1,
        "value_recall": value_recall,
        "value_acc": value_ok / len(matches) if matches else None,
        "unit_acc": unit_ok / len(matches) if matches else None,
        "range_acc": range_ok / len(matches) if matches else None,
        "fully_correct_rows": fully_correct,
    }


def run_extract(report_path: Path, label: str, refresh: bool) -> tuple[dict, dict, str | None]:
    """Return (prediction, usage, error). Caches under results/<label>/."""
    out_dir = RESULTS_DIR / label
    out_dir.mkdir(parents=True, exist_ok=True)
    pred_path = out_dir / f"{report_path.stem}.json"
    usage_path = out_dir / f"{report_path.stem}.usage.json"
    err_path = out_dir / f"{report_path.stem}.error.txt"

    if not refresh and pred_path.exists():
        prediction = json.loads(pred_path.read_text())
        usage = json.loads(usage_path.read_text()) if usage_path.exists() else {}
        return prediction, usage, None

    err_path.unlink(missing_ok=True)
    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "extract.py", str(report_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=200,
    )
    wall_s = time.monotonic() - start

    usage = {}
    for line in proc.stderr.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                usage = json.loads(line)
            except json.JSONDecodeError:
                pass
    usage.setdefault("wall_s", round(wall_s, 3))

    if proc.returncode != 0:
        err_path.write_text(proc.stderr or proc.stdout)
        return {"tests": []}, usage, (proc.stderr or proc.stdout).strip()

    try:
        prediction = json.loads(proc.stdout)
    except json.JSONDecodeError:
        err_path.write_text(f"non-JSON stdout:\n{proc.stdout}\n\nstderr:\n{proc.stderr}")
        return {"tests": []}, usage, "non-JSON stdout"

    pred_path.write_text(json.dumps(prediction, indent=2))
    usage_path.write_text(json.dumps(usage, indent=2))
    return prediction, usage, None


def cost_usd(usage: dict) -> float | None:
    model = usage.get("model")
    pricing = PRICING.get(model)
    if not pricing or usage.get("prompt_tokens") is None:
        return None
    return (usage.get("prompt_tokens", 0) * pricing["input"]
            + (usage.get("completion_tokens") or 0) * pricing["output"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="baseline", help="cache/result subdirectory name")
    ap.add_argument("--refresh", action="store_true", help="re-run extract.py instead of using cache")
    args = ap.parse_args()

    gold_files = sorted(GOLD_DIR.glob("*.json"))
    rows = []
    for gold_path in gold_files:
        stem = gold_path.stem
        report_path = next((p for p in REPORTS_DIR.glob(f"{stem}.*")), None)
        if report_path is None:
            print(f"WARNING: no report file for {stem}", file=sys.stderr)
            continue
        gold = json.loads(gold_path.read_text())["tests"]
        prediction, usage, error = run_extract(report_path, args.label, args.refresh)
        pred_tests = prediction.get("tests", [])
        metrics = score_report(gold, pred_tests, failed=bool(error))
        metrics.update({"report": stem, "error": error, "usage": usage, "cost_usd": cost_usd(usage)})
        rows.append(metrics)

    print(f"\n=== eval: label={args.label} ===")
    header = f"{'report':12s} {'gold':>4s} {'pred':>4s} {'recall':>7s} {'precis':>7s} {'val_rec':>7s} {'val_acc':>7s} {'unit_acc':>8s} {'range_acc':>9s} {'latency_s':>9s} {'cost_usd':>9s}"
    print(header)
    print("-" * len(header))
    for m in rows:
        latency = m["usage"].get("latency_s", m["usage"].get("wall_s"))
        cost = m["cost_usd"]
        print(f"{m['report']:12s} {m['n_gold']:>4d} {m['n_pred']:>4d} "
              f"{m['recall']*100:>6.0f}% {m['precision']*100:>6.0f}% {m['value_recall']*100:>6.0f}% "
              f"{(m['value_acc'] or 0)*100:>6.0f}% {(m['unit_acc'] or 0)*100:>7.0f}% {(m['range_acc'] or 0)*100:>8.0f}% "
              f"{latency or 0:>9.1f} {cost if cost is not None else 0:>9.5f}"
              + ("  [ERROR]" if m["error"] else ""))

    n = len(rows)
    totals = {k: sum(m[k] for m in rows) / n for k in
              ("recall", "precision", "row_f1", "value_recall")}
    matched_rows = [m for m in rows if m["n_matched"] > 0]
    for k in ("value_acc", "unit_acc", "range_acc"):
        vals = [m[k] for m in matched_rows if m[k] is not None]
        totals[k] = sum(vals) / len(vals) if vals else None
    total_cost = sum(m["cost_usd"] for m in rows if m["cost_usd"] is not None)
    latencies = [m["usage"].get("latency_s", m["usage"].get("wall_s")) for m in rows]
    latencies = [l for l in latencies if l is not None]

    print("-" * len(header))
    print(f"MEAN         recall={totals['recall']*100:.0f}%  precision={totals['precision']*100:.0f}%  "
          f"row_f1={totals['row_f1']*100:.0f}%  value_recall={totals['value_recall']*100:.0f}%  "
          f"value_acc={(totals['value_acc'] or 0)*100:.0f}%  unit_acc={(totals['unit_acc'] or 0)*100:.0f}%  "
          f"range_acc={(totals['range_acc'] or 0)*100:.0f}%")
    print(f"TOTAL cost (8 reports, paid-tier projection): ${total_cost:.5f}   "
          f"mean latency: {sum(latencies)/len(latencies):.1f}s" if latencies else "")

    summary_path = RESULTS_DIR / f"{args.label}_summary.json"
    RESULTS_DIR.mkdir(exist_ok=True)
    summary_path.write_text(json.dumps({"label": args.label, "rows": rows, "mean": totals,
                                         "total_cost_usd": total_cost}, indent=2))
    print(f"\nWrote {summary_path}")


if __name__ == "__main__":
    main()
