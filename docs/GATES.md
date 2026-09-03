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

The tier is a floor, not a ceiling. A branch named `proto/*` or `internal/*` whose change set
touches `paths.source` is evaluated at **production** tier: the branch name is an assertion about
risk, and the diff is the only thing in the repository that can contradict it. The change is not
rejected for it — a spike stays a spike right up to the moment it carries shipping source — it is
simply judged by the full set. `--tier` may raise the tier the branch implies and may never lower
it, for the same reason.

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
- **Declared is not the same as real.** A name that is declared must also resolve, offline: Python
  with `importlib.util.find_spec` in the interpreter running the gate, JS/TS against the contents of
  `node_modules`. A name that resolves to nothing fails, and so does one that resolves only to a
  directory holding no importable module — an empty directory named after the package used to make a
  hallucinated import read as a local one, and Python's implicit namespace packages made the same
  trick work for an installed one. A local directory is only a local module if it actually contains
  one.
- The gate never asks a package index and never opens a socket. "Exists" means "resolves in the
  environment the gate is running in". When it cannot see that environment — the project ships a
  `.venv/` the gate is not running inside, or there is no `node_modules` — the existence half is
  reported as `import existence: not_applicable` **with the reason**, and the declaration check
  still applies. It is never a silent pass. Run the gate with the project's own interpreter, and
  install its packages in the CI job, or this half of G4 cannot tell you anything.

## G5 handoff

- A changed file under `paths.handoffs` ending `.md` must exist.
- Every `handoff.required_fields` entry must be present and not a placeholder.
- If the `Spec:` field names a spec, that spec file must exist.
- `Reviewer:` is meant to be filled by a human. `loop.py` treats "only Reviewer missing" as the
  terminal state `ready_for_human_review`.

## G6 integrity

- Fails when the change set touches `agentic.toml`, anything under `.agentic/`, or a CI definition
  that already invoked the runner, unless the branch is production tier **and** the change carries a
  valid maintenance declaration. The runtime artefacts those tools write,
  `.agentic/last-report.json`, `.agentic/runs/`, `.agentic/evals/result.json` and compiled Python,
  are exempt — but only where they are regular files. A symlink or gitlink at one of those paths is
  a write-through, not an artefact, and is protected.
- A declaration is valid when all three hold: the file is a spec **this change references**, so G0
  validated it; it satisfies G0 on its own content, so a one-line `specs/permit.md` authorises
  nothing; and the `Framework maintenance: yes` line is **introduced or altered in this diff**, so a
  cosmetic edit to a spec that already carried it is not a renewal. It is read from the git index at
  commit stage and from the proposed tree in CI, and resolved before the first gate runs, so nothing
  `tests.command` or `evals.command` executes can write itself a permission mid-run.
- Deletions, renames away from a protected path, and file type changes all count. Moving
  `.agentic/gate.py` elsewhere is as effective as editing it, and `--diff-filter=ACMR` with rename
  detection reports only the destination. A deletion still counts when the tree is otherwise clean
  and the run falls back to a whole-tree audit.
- A CI definition that ran the gate on the base ref is protected in full. See the trade-off note
  below: a version bump in that one file is framework maintenance and has to be declared. One that
  never ran the gate is not protected at all.
- Fails when the base ref resolves to the candidate's own tip on any branch other than the project's
  base branch. `--base HEAD` empties the diff, which would otherwise be a skip flag by another name.
- Raises the tier to production when the change set touches `paths.source` on a prototype- or
  internal-tier branch, and says so in its evidence. The tier is an assertion by whoever named the
  branch; this is the only thing in the repository that can contradict it. Renaming a branch to
  `docs/whatever` used to drop G0, G3 and G5 silently; now it drops nothing. The change is judged at
  production tier rather than refused, so the tier system still means something for work that really
  is a spike.
- Fails when `--tier` is passed a tier below the one the branch implies. The lower value is ignored
  and the branch's tier stands, so a lowered `--tier` cannot reduce what runs.
- Fails when no base ref resolves: with no change set, none of the above can be checked.
- The change set it reads is the working tree **and** the committed diff against the base, so a CI
  step that restores the runner from the base ref does not hide the edit it protects against.
- **At `--stage ci` the policy itself is read from the merge base** — the commit where the branch
  diverged, not the current tip of the base branch, so the answer does not move if the target branch
  advances mid-run. A pull request that relaxes a
  rule is judged by the rule it replaces rather than by its replacement. The exception is the same
  authorising channel as above: when the change declares `Framework maintenance: yes` in the
  G0-valid spec it references, with the declaration in this diff, the candidate's own `agentic.toml`
  is used, so a policy change can still be exercised by the change proposing it. That question is
  answered while the base policy is in force, so the candidate cannot steer it with its own
  `[paths]` or `[spec]` keys. Every report carries a `policy:` line saying which was used.
  This lives in `resolve_policy()` in the runner and not in the workflow on purpose: on a
  `pull_request` event the CI definition comes from the branch, so a restore written in YAML is a
  rule the candidate carries, while the runner CI executes is unpacked from the base ref.
- Separately, `verdict()` derives the overall pass or fail from the recorded gate statuses, and
  `enforce_verdict()` re-derives it wherever a report is produced or consumed. A report that lists a
  failure cannot carry a pass verdict, and neither can one whose results do not cover the gates it
  says are required, because `all([])` is `True`.

**What G6 is, honestly.** Locally, in the hook and in `loop.py`, it is a tripwire: a change that
edits the runner can edit G6 out of the runner in the same commit. Nothing that runs from inside a
branch can defend itself against edits to itself. The boundary that holds is the CI step that
unpacks the base ref's `.agentic/` outside the working tree and runs that copy against the tree, so
the judge is never the branch's own copy. It is deliberately not restored *into* the tree: the
branch's own tests have to be able to import the branch's runner, or no change to the runner could
ever be tested by the change that makes it.

**What even that does not reach**, established by adversarial review of this change and left open
deliberately:

- **The CI definition comes from the branch.** On a `pull_request` event GitHub runs the workflow
  file in the PR head, so a PR that edits or deletes it decides whether any of this executes. G6
  fails such a PR, and running the trusted runner from outside the tree keeps `sys.path[0]` clean so
  that an added `.agentic/` module named after a standard library one cannot be imported by the
  judge. But what makes the gate binding is a base branch ruleset requiring the gate job **by its
  job id**, with **Require review from Code Owners** enabled and CODEOWNERS covering
  `.github/workflows/`, `.agentic/` and `agentic.toml`. A skipped job counts as a successful
  required check. Without that configuration, the gate is advice. A deterministic trust root needs
  the workflow to live outside the repository, as an organisation-required workflow.
- **A CI definition that calls the runner indirectly**, through a wrapper script, a make target or a
  reusable workflow, contains no literal `gate.py` and is not recognised as part of the judge.
- **The first landing of a control cannot be judged by it.** The base ref's runner has no G6 until
  this change is merged, so the bootstrap review is human.
- **`tests.command` and `evals.command` execute the branch's code**, by design: they are your test
  suite and your eval runner. Restoring the policy does not sandbox them. A branch that ships a
  test file which always passes is the S5 case, and is a code review problem, not a gate problem.
- **Push events are not restored.** The restore is guarded on pull request events; a direct push to
  a protected branch is outside the model, which is what branch protection is for.
- **The policy restore is a CI property, not a local one.** At `--stage local` and in the hook the
  gates read the branch's own `agentic.toml`, because that is the copy the author is working on.
  Locally the protection is only that G6 fails an undeclared edit to it. An earlier version of this
  document claimed the restore already happened in CI when it did not; it now does, and it is
  described in "G6 integrity" above rather than promised here.
- **A CI definition that runs the gate is protected in full**, not just its lines that name
  `gate.py`. The revision the workflow trusts, the checkout depth, an enclosing `if:` and an
  injected `PYTHONPATH` all decide what the judge is, and none of them mentions the runner. The
  practical cost is that an action or Python version bump in that one file is framework maintenance
  and has to be declared. A workflow that never ran the gate is not protected at all.
- **`Framework maintenance: yes` is self-issued.** It is a declaration in a file the same change
  adds, so it stops drift, not a determined author. Its value is that the claim is in the diff, in
  a spec, where a human reviewing the pull request has to read it.

## What the gates deliberately do not do

- They do not measure coverage percentage, lint, or type-check. Put those in `tests.command`.
- They do not read PR descriptions. Everything they check is in the repository, so it works the
  same locally, in a hook, and in any CI.
- They do not have a skip flag.
