# The gates

Each gate answers one question with `pass`, `fail` or `not_applicable`, and shows its evidence.
None of them consults a model. Which gates apply is decided by risk tier in `agentic.toml`.

| Gate | Question | Paper reference |
|---|---|---|
| G0 spec | Is there a complete spec with an architecture section, and does its tier match the branch? | "Formal specs, architecture docs, memory files"; "architecture remains the most human-centric phase" |
| G1 context | Is the rule file present, bounded, sectioned and versioned? | "Start with ten lines"; "static context is expensive"; "treat AGENTS.md as code" |
| G2 tests | Were tests touched alongside the source, and do they pass? | "Tests verify the deterministic parts"; "write the tests before generating the code" (G2 can only verify alongside, see below) |
| G3 evals | Is the AI surface scored against a rubric above the bar? | "Evals verify the parts that are not deterministic"; "set the bar at the eval, not the demo" |
| G4 review | Any secrets or hallucinated dependencies in the diff? | "Check imports for real packages"; the hook that blocks a hard-coded password |
| G5 handoff | Is there a record of what the agent did and what was verified? | "Traces of every agent run"; "clear handoff protocols govern the boundary" |
| G6 integrity | Does the change edit the policy or the runner that judges it, or claim a tier its diff contradicts? | "The gate is the control point"; a control an agent can rewrite is not a control |

## Tier matrix (default)

| Tier | Branch patterns | Gates |
|---|---|---|
| prototype | `proto/*`, `spike/*`, `sandbox/*`, `scratch/*` | G4 |
| internal | `internal/*`, `tooling/*`, `docs/*` | G1 G2 G4 |
| production | `main`, `release/*`, `feature/*`, `fix/*`, everything else | G0 G1 G2 G3 G4 G5 |

Secrets are checked at every tier. A spike that leaks a key is still a leak. G6 runs at every tier
and every stage too, and is deliberately absent from `[tiers.required]`: a gate that detects edits
to `agentic.toml` cannot be switched off by editing `agentic.toml`.

## G0 spec

- A spec is referenced when `SPEC-NNNN` appears in the branch name, in a commit message since the
  base, or in the path of a changed file under `paths.specs`.
- Each referenced spec must exist, contain every `spec.required_sections` heading (by default
  including `Architecture`, because the paper's agentic column names architecture docs alongside
  specs and says the model implements structural decisions rather than making them), have a
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
- This verifies tests changed **alongside** source. It cannot verify they were written **before**:
  a diff has no ordering. "Tests before code" is the instruction in `AGENTS.md`; "tests alongside
  code" is the fact G2 enforces. Do not read the gate as proving more than it does.
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

## G6 integrity

- Fails when the change set touches `agentic.toml`, anything under `.agentic/`, or a CI definition
  that already invoked the runner on the base ref, unless the branch is production tier **and** a
  referenced spec carries a `Framework maintenance: yes` field, exactly that value and nothing else.
  The runtime artefacts those tools write, `.agentic/last-report.json`, `.agentic/runs/`,
  `.agentic/evals/result.json` and compiled Python, are exempt. Deletions count: removing the runner
  is as hostile as editing it.
- Fails when the change set touches `paths.source` and the branch is not production tier. The tier
  is an assertion by whoever named the branch; this is the only thing in the repository that can
  contradict it. Renaming a branch to `docs/whatever` used to drop G0, G3 and G5 silently.
- Fails when `--tier` is passed a tier below the one the branch implies. The lower value is ignored
  and the branch's tier stands, so a lowered `--tier` cannot reduce what runs.
- Fails when no base ref resolves: with no change set, none of the above can be checked.
- The change set it reads is the working tree **and** the committed diff against the base, so a CI
  step that restores the policy and the runner from the base ref does not hide the edit it protects
  against.
- Separately, `verdict()` derives the overall pass or fail from the recorded gate statuses, and
  `enforce_verdict()` re-derives it wherever a report is produced or consumed. A report that lists a
  failure cannot carry a pass verdict.

**What G6 is, honestly.** Locally, in the hook and in `loop.py`, it is a tripwire: a change that
edits the runner can edit G6 out of the runner in the same commit. Nothing that runs from inside a
branch can defend itself against edits to itself. The boundary that holds is the CI step that
restores `.agentic/` and `agentic.toml` from the base ref before running the gates, so the judge is
never the branch's own copy.

**What even that does not reach**, established by adversarial review of this change and left open
deliberately:

- **The CI definition comes from the branch.** On a `pull_request` event GitHub runs the workflow
  file in the PR head, so a PR that edits or deletes it decides whether any of this executes. G6
  fails such a PR, and the restore step removes and re-checks-out `.agentic/` rather than
  overwriting it, so an added `.agentic/<stdlib name>.py` cannot shadow an import in the runner. But
  what makes the gate binding is branch protection with the gate job as a **required status check**,
  plus **CODEOWNERS** on `.github/workflows/`, `.agentic/` and `agentic.toml`. Without those two,
  the gate is advice.
- **`tests.command` and `evals.command` execute the branch's code**, by design: they are your test
  suite and your eval runner. Restoring the policy does not sandbox them. A branch that ships a
  test file which always passes is the S5 case, and is a code review problem, not a gate problem.
- **Push events are not restored.** The restore is guarded on pull request events; a direct push to
  a protected branch is outside the model, which is what branch protection is for.
- **A pull request that relaxes policy is judged by the policy it is replacing**, because CI
  restores `agentic.toml` from the base ref. That is deliberate, and it means a policy relaxation
  must still satisfy the old rules to land. If the old policy is unsatisfiable, the maintainer has
  to land the policy change on its own, which is exactly the conversation that should happen.
- **`Framework maintenance: yes` is self-issued.** It is a declaration in a file the same change
  adds, so it stops drift, not a determined author. Its value is that the claim is in the diff, in
  a spec, where a human reviewing the pull request has to read it.

## What the gates deliberately do not do

- They do not measure coverage percentage, lint, or type-check. Put those in `tests.command`.
- They do not read PR descriptions. Everything they check is in the repository, so it works the
  same locally, in a hook, and in any CI.
- They do not have a skip flag.
