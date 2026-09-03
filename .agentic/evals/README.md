# Eval contract (G3)

Tests verify the deterministic parts. Evals verify the parts that are not deterministic: did the
agent take the right trajectory, use the right tools, and produce output that meets the bar.
The gate does not care how you run evals. It cares that the result has this shape.

## What G3 runs

`evals.command` from `agentic.toml`. It must exit 0 and write `evals.result_file`:

```json
{
  "target": "my-support-agent@v12",
  "evalset": ".agentic/evals/evalset.jsonl",
  "cases": 24,
  "overall_pass_rate": 0.958,
  "dimensions": {
    "task_success":     {"pass_rate": 1.00, "rubric": "output contains every expected_contains string"},
    "tool_use":         {"pass_rate": 0.96, "rubric": "expected_tools is a subset of tools_used"},
    "trajectory":       {"pass_rate": 0.92, "rubric": "steps <= max_steps"},
    "hallucination":    {"pass_rate": 1.00, "rubric": "no forbidden string appears in output"},
    "response_quality": {"pass_rate": 0.91, "rubric": "judge score >= 0.7 on clarity and completeness"}
  }
}
```

## What G3 checks

- `cases >= evals.min_cases` (a demo proves it can succeed once)
- `overall_pass_rate >= evals.min_pass_rate`
- every `evals.required_dimensions` entry is present, has a `pass_rate`, and a non-empty `rubric`
  (an eval without a rubric measures nothing)
- `target` does not start with `builtin-stub` unless `evals.allow_stub_target = true`

## The example runner

`example_runner.py` is a working, model-free reference implementation of the contract.

- Cases come from `evalset.example.jsonl`, one JSON object per line:
  `{"id", "input", "expected_contains": [], "forbidden": [], "expected_tools": [], "max_steps": N}`
- The system under test is the command in `AGENTIC_EVAL_TARGET`. It receives the case as JSON on
  stdin and must print `{"output": "...", "tools_used": [...], "steps": N}`. Any language, any model.
- If `AGENTIC_EVAL_TARGET` is unset, a built-in stub answers and the result is tagged `builtin-stub`,
  which G3 rejects in a real repo. That is deliberate.
- `response_quality` uses the command in `AGENTIC_EVAL_JUDGE` if set (stdin: `{"input","output"}`,
  stdout: `{"score": 0..1}`). This is where an LM judge plugs in. Unset, it falls back to a
  deterministic length-and-terms proxy and says so in the rubric.

Replace the runner with your own whenever you like. Keep the result shape.
