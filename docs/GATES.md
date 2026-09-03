# The gates

Each gate answers one question with `pass`, `fail` or `not_applicable`, and shows its evidence.
None of them consults a model. Which gates apply is decided by risk tier in `agentic.toml`.

| Gate | Question | Paper reference |
|---|---|---|
| G0 spec | Is there a complete spec, and does its tier match the branch? | "Formal specs, architecture docs, memory files"; "make the boundary explicit: which branches" |
| G1 context | Is the rule file present, bounded, sectioned and versioned? | "Start with ten lines"; "static context is expensive"; "treat AGENTS.md as code" |
| G2 tests | Were tests touched with the source, and do they pass? | "Tests verify the deterministic parts"; "write the tests before generating the code" |
| G3 evals | Is the AI surface scored against a rubric above the bar? | "Evals verify the parts that are not deterministic"; "set the bar at the eval, not the demo" |
| G4 review | Any secrets or hallucinated dependencies in the diff? | "Check imports for real packages"; the hook that blocks a hard-coded password |
| G5 handoff | Is there a record of what the agent did and what was verified? | "Traces of every agent run"; "clear handoff protocols govern the boundary" |

## Tier matrix (default)

| Tier | Branch patterns | Gates |
|---|---|---|
| prototype | `proto/*`, `spike/*`, `sandbox/*`, `scratch/*` | G4 |
| internal | `internal/*`, `tooling/*`, `docs/*` | G1 G2 G4 |
| production | `main`, `release/*`, `feature/*`, `fix/*`, everything else | G0 G1 G2 G3 G4 G5 |

Secrets are checked at every tier. A spike that leaks a key is still a leak.

## G0 spec

- A spec is referenced when `SPEC-NNNN` appears in the branch name, in a commit message since the
  base, or in the path of a changed file under `paths.specs`.
- Each referenced spec must exist, contain every `spec.required_sections` heading, have a
  `Risk tier:` line naming a known tier, and contain no `<placeholder>` text.
- The spec's tier must be at least the branch's tier. A `prototype` spec on a `feature/*` branch
  fails: either move the work to a prototype branch or raise the spec.

## G1 context

- `context.rule_file` exists, is tracked by git, has at most `context.max_lines` lines, and has
  every `context.required_sections` heading.
- It is also scanned for secrets, because rule files get pasted into every prompt.

## G2 tests

- If any changed file matches `paths.source` and none matches `paths.tests`, fail.
  A source change without a test change is the paper's definition of vibe coding.
- Otherwise run `tests.command`; non-zero exit fails with the last lines of output.
- At `--stage commit` the run is deferred unless `tests.run_on_commit = true`.

## G3 evals

- Not applicable unless a changed file matches `paths.ai_surface`.
- Runs `evals.command`, reads `evals.result_file`, and checks: case count, overall pass rate,
  every required dimension present with a `pass_rate` and a non-empty `rubric`, and that the
  target is not the built-in stub (unless explicitly allowed, which only this framework repo does).
- The contract is in `.agentic/evals/README.md`. Any runner that honours it works.

## G4 review

- Secret scan over added lines (staged diff, or diff against the base; whole file when a file is
  untracked or there is no base). Built-in patterns cover AWS, GitHub, Slack, OpenAI-style,
  Google API, private keys, Azure account keys, generic `key = "..."` assignments, Authorization
  headers and connection-string passwords. `review.extra_secret_patterns` extends them.
  A line containing `agentic:allow` is exempt, visibly.
- Secret-bearing file types (`.pem`, `.key`, `.p12`, `.pfx`, `.env`, `.env.*` except `.env.example`) fail.
- Python imports are parsed with `ast`; JS/TS with a regex over `import`/`require`. Anything that is
  not stdlib, not a local module, and not declared in `requirements*.txt`, `pyproject.toml` or
  `package.json` fails. `review.import_aliases` maps import names to distribution names.

## G5 handoff

- A changed file under `paths.handoffs` ending `.md` must exist.
- Every `handoff.required_fields` entry must be present and not a placeholder.
- If the `Spec:` field names a spec, that spec file must exist.
- `Reviewer:` is meant to be filled by a human. `loop.py` treats "only Reviewer missing" as the
  terminal state `ready_for_human_review`.

## What the gates deliberately do not do

- They do not measure coverage percentage, lint, or type-check. Put those in `tests.command`.
- They do not read PR descriptions. Everything they check is in the repository, so it works the
  same locally, in a hook, and in any CI.
- They do not have a skip flag.
