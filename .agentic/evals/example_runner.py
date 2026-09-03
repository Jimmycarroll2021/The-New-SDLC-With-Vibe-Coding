#!/usr/bin/env python3
"""
Reference eval runner for G3. Model-free by default, so the contract can be exercised anywhere.

  AGENTIC_EVAL_TARGET  command that answers a case  (stdin: case JSON, stdout: {"output","tools_used","steps"})
  AGENTIC_EVAL_JUDGE   command that scores quality  (stdin: {"input","output"}, stdout: {"score": 0..1})
  AGENTIC_EVALSET      path to a JSONL evalset      (default: .agentic/evals/evalset.example.jsonl)

Writes .agentic/evals/result.json in the shape documented in README.md.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def run_json(cmd: str, payload: dict, timeout: int = 300) -> dict:
    p = subprocess.run(cmd, shell=True, input=json.dumps(payload), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd!r} exited {p.returncode}: {p.stderr.strip()[:400]}")
    return json.loads(p.stdout)


def stub_target(case: dict) -> dict:
    """The built-in stand-in. It reads the input and echoes the clause after the colon."""
    text = case["input"].split(":", 1)[-1].strip().lower()
    return {"output": text, "tools_used": ["read_input"], "steps": 1}


def proxy_judge(inp: str, out: str) -> float:
    words = [w.strip(".,;:").lower() for w in inp.split(":", 1)[-1].split()]
    keep = [w for w in words if len(w) > 3]
    hit = sum(1 for w in keep if w in out.lower())
    length_ok = 3 <= len(out.split()) <= 200
    return (hit / len(keep) if keep else 1.0) * (1.0 if length_ok else 0.5)


def main() -> int:
    evalset = Path(os.environ.get("AGENTIC_EVALSET", HERE / "evalset.example.jsonl"))
    target_cmd = os.environ.get("AGENTIC_EVAL_TARGET")
    judge_cmd = os.environ.get("AGENTIC_EVAL_JUDGE")
    cases = [json.loads(l) for l in evalset.read_text(encoding="utf-8").splitlines() if l.strip()]

    dims = {
        "task_success": {"pass": 0, "rubric": "output contains every expected_contains string (case-insensitive)"},
        "tool_use": {"pass": 0, "rubric": "expected_tools is a subset of tools_used"},
        "trajectory": {"pass": 0, "rubric": "steps <= max_steps"},
        "hallucination": {"pass": 0, "rubric": "no forbidden string appears in output"},
        "response_quality": {"pass": 0, "rubric": ("judge score >= 0.7 via AGENTIC_EVAL_JUDGE" if judge_cmd else
                                                  "deterministic proxy: >= 70% of content words echoed, sane length (set AGENTIC_EVAL_JUDGE for an LM judge)")},
    }
    per_case = []
    all_pass = 0
    for c in cases:
        try:
            r = run_json(target_cmd, c) if target_cmd else stub_target(c)
        except Exception as e:  # a crashed target is a failed case, not a crashed eval
            r = {"output": f"<target error: {e}>", "tools_used": [], "steps": 10 ** 6}
        out = str(r.get("output", ""))
        ok = {
            "task_success": all(s.lower() in out.lower() for s in c.get("expected_contains", [])),
            "tool_use": set(c.get("expected_tools", [])) <= set(r.get("tools_used", [])),
            "trajectory": int(r.get("steps", 10 ** 6)) <= int(c.get("max_steps", 10 ** 6)),
            "hallucination": not any(s.lower() in out.lower() for s in c.get("forbidden", [])),
        }
        score = run_json(judge_cmd, {"input": c["input"], "output": out}).get("score", 0.0) if judge_cmd \
            else proxy_judge(c["input"], out)
        ok["response_quality"] = float(score) >= 0.7
        for k, v in ok.items():
            dims[k]["pass"] += int(v)
        all_pass += int(all(ok.values()))
        per_case.append({"id": c.get("id"), **ok, "quality_score": round(float(score), 3)})

    n = len(cases) or 1
    result = {
        "target": (f"command:{target_cmd}" if target_cmd else "builtin-stub (no AGENTIC_EVAL_TARGET set)"),
        "evalset": str(evalset.relative_to(ROOT)) if evalset.is_relative_to(ROOT) else str(evalset),
        "cases": len(cases),
        "overall_pass_rate": round(all_pass / n, 4),
        "dimensions": {k: {"pass_rate": round(v["pass"] / n, 4), "rubric": v["rubric"]} for k, v in dims.items()},
        "per_case": per_case,
    }
    (HERE / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"evals: {len(cases)} cases, overall {result['overall_pass_rate']:.3f}, target={result['target']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
