# AGENTS.md

Rules for any coding agent working in this repository. This file is the single source of truth;
`CLAUDE.md` and `GEMINI.md` import it. Keep it dense: it is loaded on every turn.

## Stack

- Python 3.11+, standard library only. No third-party imports in `.agentic/`.
- Git. Gates run locally, in the pre-commit hook, and in CI (GitHub Actions or Azure Pipelines).
- Config lives in `agentic.toml`. Policy lives there, not in prose.

## Conventions

- One gate = one function in `.agentic/gate.py` returning `GateResult` with `evidence`.
- Every gate outcome is `pass`, `fail` or `not_applicable`. Never advisory. Never a warning.
- Specs are `specs/SPEC-NNNN-slug.md` from `.agentic/templates/SPEC.md`.
- Handoff records are `handoffs/HANDOFF-NNNN.md` from `.agentic/templates/HANDOFF.md`.
- Tests are `tests/test_*.py` using `unittest`. Fixtures build real temporary git repos.
- en-GB spelling in prose. No em or en dashes.

## Hard rules

- Do not add a skip, force or override flag to any gate. If a gate should not apply, change the tier.
- Do not weaken a gate to make a change pass. Fix the change.
- Do not edit `agentic.toml`, anything under `.agentic/`, or the gate step of a CI file, as part of
  a feature change. G6 fails it, and CI judges with the base ref's runner, not the branch's.
- Framework maintenance is the exception: a production branch, and a spec **in that same change**
  saying `Framework maintenance: yes`. Say it in the spec, not in the code.
- Do not carry `paths.source` on a prototype or internal branch. That tier does not run G0, G3 or G5.
- Do not commit secrets, `.env` files, or private keys. The hook blocks them; do not route around it.
- Do not import a package that is not stdlib, local, or declared in a manifest.
- Do not claim work is done without running `python .agentic/gate.py` and pasting the result.

## Workflow

1. Read the spec named in the branch or task. If there is none, write one from the template before code.
2. Write or update a test that fails for the right reason. Then write the code.
3. Run `python .agentic/gate.py`. Fix every FAIL. Re-run until `ALL GATES PASS` or only G5 Reviewer remains.
4. Create or update `handoffs/HANDOFF-<spec>.md`. Fill every field except `Reviewer`.
5. Stop and report: what changed, what the gates say, what you did not verify.
