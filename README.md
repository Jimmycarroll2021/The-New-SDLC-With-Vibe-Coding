# agentic-gates

Deterministic gates for the agentic-engineering SDLC. Drop it into any repository, point it at
your tests, and the row of the diagram that reads *spec + AGENTS.md → model → code → tests +
evals → CI/CD gate → merge* becomes enforceable rather than aspirational. Failure is routed back
to the agent automatically. Nothing here calls a model; any coding agent works.

```
  PASS  G0  spec: exists, complete, tier matches branch
  PASS  G1  context: rule file present, bounded, versioned
  PASS  G2  tests: touched alongside source, and passing
  N/A   G3  evals: AI surface scored against a rubric
  PASS  G4  review: no secrets, no hallucinated dependencies
  FAIL  G5  handoff: agent run is recorded
         handoff record incomplete
         - handoffs/HANDOFF-0012.md: field 'Reviewer' missing or placeholder

GATE FAILURE: the change is not ready
```

The last line is the point. A gate says pass or fail with evidence. It never says "consider".

## What it enforces

| Gate | Fails when | Source of the rule |
|---|---|---|
| **G0 spec** | no `SPEC-NNNN` referenced, sections missing (Architecture included), placeholders left, or spec tier below branch tier | "formal specs, architecture docs"; "make the prototype/production boundary explicit" |
| **G1 context** | `AGENTS.md` missing, over the line limit, missing a section, untracked, or leaking a secret | "start with ten lines"; "treat AGENTS.md as code" |
| **G2 tests** | source changed without a test change, or the test command fails | "tests verify the deterministic parts"; enforces tests *alongside* code (a diff cannot prove *before*) |
| **G3 evals** | an AI-surface change scores below the bar, has too few cases, no rubric, or a stub target | "evals verify the parts that are not deterministic"; "the bar is the eval, not the demo" |
| **G4 review** | a secret pattern in an added line, a `.env`/`.pem` in the diff, or an import that is not stdlib, local, or declared | "check imports for real packages"; the hook that blocks the hard-coded password |
| **G5 handoff** | no `handoffs/HANDOFF-*.md` in the change, or a required field is a placeholder | "traces of every agent run"; "clear handoff protocols" |

Which gates apply is decided by **risk tier**, derived from the branch name in `agentic.toml`:

| Tier | Default branches | Gates |
|---|---|---|
| prototype | `proto/*` `spike/*` `sandbox/*` | G4 |
| internal | `internal/*` `tooling/*` `docs/*` | G1 G2 G4 |
| production | `main` `release/*` `feature/*` `fix/*` and anything else | all six |

There is no `--skip`. If a gate should not apply, the work is on the wrong branch.

## Install into a repository (five minutes)

```sh
# 1. copy the framework files in (from a clone of this repo)
cp -r agentic-gates/.agentic  your-repo/
cp    agentic-gates/agentic.toml agentic-gates/CLAUDE.md agentic-gates/GEMINI.md  your-repo/
cp    agentic-gates/.agentic/templates/AGENTS.template.md  your-repo/AGENTS.md
mkdir -p your-repo/specs your-repo/handoffs

# 2. edit agentic.toml: [paths] source/tests/ai_surface, [tests].command, and set
#    evals.allow_stub_target = false. Edit AGENTS.md: fill Stack and Conventions.

# 3. install the pre-commit hook (sets core.hooksPath for this clone)
cd your-repo && python .agentic/hooks/install_hooks.py

# 4. add CI: copy .github/workflows/agentic-gates.yml or azure-pipelines.yml, add your
#    dependency install step before "Run gates"

# 5. write the first spec and run
cp .agentic/templates/SPEC.md specs/SPEC-0001-first-change.md
python .agentic/gate.py
```

Python 3.11+ and git are the only requirements.

## Daily use

**Human or agent, same loop:**

```sh
git checkout -b feature/SPEC-0012-export-csv      # tier comes from the branch
#   ... write the spec, write a failing test, write the code ...
python .agentic/gate.py                             # fix every FAIL
#   ... create handoffs/HANDOFF-0012.md, leave Reviewer blank ...
git commit                                          # hook runs G1 + G4 on the staged diff
#   open PR; CI runs everything; reviewer fills Reviewer:
```

**Automated, no human in the loop until the end:**

```sh
python .agentic/loop.py SPEC-0012
```

`loop.py` sends the spec and `AGENTS.md` to the agent named in `[loop].agent_command`, runs the
gates, and if anything fails sends the gate report back to the agent. It stops on
`all_gates_pass`, or on `ready_for_human_review` when the only failure is the blank `Reviewer`
field, or after `max_iterations` with exit code 2. Every prompt, agent log and gate report is
written under `.agentic/runs/<run-id>/` so the trajectory is reviewable.

The agent is any CLI that reads a prompt:

```toml
[loop]
agent_command = "claude -p --permission-mode acceptEdits"   # or
agent_command = "codex exec --full-auto -"                 # or
agent_command = "gemini -p -"                              # or anything: {prompt_file} is substituted if present
```

## LLM-agnostic by construction

- `AGENTS.md` is the single rule file. `CLAUDE.md` and `GEMINI.md` are one line each: `@AGENTS.md`.
  Codex reads `AGENTS.md` natively. Cursor gets a one-paragraph pointer rule. See `adapters/`.
- The gate runner never calls a model. It runs your test command and your eval command and
  reads their exit codes and result files.
- Evals are a **contract, not a runner**: any command that writes the JSON shape in
  `.agentic/evals/README.md` satisfies G3. The shipped `example_runner.py` is a working reference
  that runs with no model at all, and G3 refuses its stub result in a real repo.
- `loop.py` talks to the agent over stdin or a prompt file. Swap the command, keep the loop.

## Layout

```
agentic.toml                     policy: tiers, paths, commands, thresholds
AGENTS.md  CLAUDE.md  GEMINI.md  rules, and two one-line pointers to them
.agentic/
  gate.py                        the six gates and the runner (stdlib only)
  loop.py                        agent -> gates -> route failure back
  templates/                     SPEC.md, HANDOFF.md, AGENTS.template.md, PULL_REQUEST_TEMPLATE.md
  hooks/                         pre-commit + install_hooks.py
  evals/                         contract README, example_runner.py, evalset.example.jsonl
specs/  handoffs/                one file per change, checked by G0 and G5
docs/GATES.md                    what each gate checks and why
adapters/                        optional per-tool conveniences
tests/test_gate.py               26 tests; each builds a real temporary git repo
```

## What it deliberately does not do

- Measure coverage, lint or type-check. Put those in `tests.command`.
- Read PR descriptions. Everything checked is in the repository, so hook, local and CI agree.
- Decide what "correct" means for your AI surface. That is the rubric in your eval set, which is
  the one piece of this the paper says only you can write.

## Provenance

Built from *The New SDLC With Vibe Coding: From ad-hoc prompting to Agentic Engineering*
(Osmani, Saboo, Kartakis, Google, May 2026), specifically its agentic-engineering column, its
harness section, and its "where to start" lists. The spec for this repository is
`specs/SPEC-0001-agentic-gates.md`; it passes its own gates.
