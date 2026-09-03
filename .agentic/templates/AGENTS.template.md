# AGENTS.md

Rules for any coding agent working in this repository. Single source of truth; `CLAUDE.md` and
`GEMINI.md` import it. Start with ten lines. Add a rule every time the agent does something it
should not do again. Stay under the `context.max_lines` limit in `agentic.toml`.

## Stack

- <language and version>
- <framework, runtime, package manager>
- <where config lives, where secrets come from (never from the repo)>

## Conventions

- <file and module layout>
- <naming, formatting, linting command>
- <how tests are organised and named>
- Specs are `specs/SPEC-NNNN-slug.md`. Handoffs are `handoffs/HANDOFF-NNNN.md`.

## Hard rules

- Do not add a skip, force or override flag to any gate. If a gate should not apply, change the tier.
- Do not weaken a gate to make a change pass. Fix the change.
- Do not commit secrets, `.env` files, or private keys.
- Do not import a package that is not stdlib, local, or declared in a manifest.
- Do not claim work is done without running `python .agentic/gate.py` and pasting the result.
- <project-specific prohibitions: tables you must not touch, APIs you must not call, etc.>

## Workflow

1. Read the spec named in the branch or task. If there is none, write one from the template before code.
2. Write or update a test that fails for the right reason. Then write the code.
3. Run `python .agentic/gate.py`. Fix every FAIL. Re-run until `ALL GATES PASS` or only G5 Reviewer remains.
4. Create or update `handoffs/HANDOFF-<spec>.md`. Fill every field except `Reviewer`.
5. Stop and report: what changed, what the gates say, what you did not verify.
