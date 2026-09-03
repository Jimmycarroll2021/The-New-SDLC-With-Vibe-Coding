# SPEC-0001: Deterministic gates for the agentic-engineering SDLC

Risk tier: production
Owner: Jimmy Carroll
Status: accepted

## Intent

Make the agentic-engineering row of the SDLC enforceable rather than aspirational: spec and rule
file in, model produces a multi-file diff, tests and evals both run, a CI gate passes or fails on
evidence, and a failure is routed back to the agent automatically. Any repository, any LLM.

## Acceptance criteria

1. A single command, `python .agentic/gate.py`, runs the gates that apply to the current branch's
   risk tier and exits non-zero on any failure. Covered by `tests/test_gate.py`.
2. Risk tier is derived from the branch name via `agentic.toml` and cannot be overridden by a skip
   flag; only `--tier` (which can raise or lower it, visibly in the report) exists.
3. G0 fails when no spec is referenced, when required sections are missing, when placeholders
   remain, or when the spec's tier is lower than the branch tier.
4. G1 fails when the rule file is missing, over the line limit, missing a required section, untracked,
   or contains a secret.
5. G2 fails when source changes without a test change, or when the test command exits non-zero.
6. G3 is not applicable unless the change touches the configured AI surface; when it applies it fails
   on pass rate, case count, missing dimensions, missing rubric, or a stub target.
7. G4 fails on any added line matching a secret pattern (unless marked `agentic:allow`), on secret-
   bearing file types, and on any Python or JS/TS import that is not stdlib, local or declared.
8. G5 fails when no handoff record is in the change set or a required field is a placeholder.
9. The pre-commit hook runs only the cheap gates on the staged diff; CI runs everything.
10. `loop.py` re-invokes a configurable agent command with the gate report until the gates pass or
    only the Reviewer field remains, and records every iteration.
11. Nothing in `.agentic/` imports outside the Python standard library.

## Out of scope

- Running or hosting a model. The framework never calls an LLM itself.
- Tool-specific hooks (Claude Code settings, Cursor rules). Provided as optional adapters only.
- Coverage thresholds, linting, type checking. Add them to `tests.command` if wanted.
- Locating the CapEx/OpEx crossover. Not the framework's problem.

## Risk tier

Production: this is the thing that decides whether other production changes ship. A silent false
pass here lets an unverified change through everywhere the framework is installed.

## Verification

- Criteria 1 to 9 and 11: `tests/test_gate.py` builds temporary git repositories and asserts each
  failure mode and the corresponding pass.
- Criterion 10: `loop.py` is exercised with a fake agent command in the test suite; a real run
  requires a real agent CLI and is verified by hand.
- The framework passes its own gates on `main`: `python .agentic/gate.py` reports ALL GATES PASS.
- Human check: read `docs/GATES.md` against `gate.py` and confirm every documented check exists.
