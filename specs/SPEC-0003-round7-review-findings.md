# SPEC-0003: The seventh review round on the SPEC-0002 fix commits

Risk tier: production
Framework maintenance: yes
Owner: Jimmy Carroll
Status: draft

## Intent

SPEC-0002 landed on `main` as `c4ec387` through six rounds of external review. Its own final fix
range, `9862b98..b2040c5`, was never reviewed by anybody: the handoff says so in its "Not verified"
section. A seventh round put that range, and the whole of the current runner, in front of Codex,
Gemini and Kimi. Between them they raised eighteen findings. Each was reproduced against the live
runner before anything was written, and nine of them turned out to be real. A second Codex pass
over the same material raised twenty more, of which five further defects were reproduced and fixed
here; the rest are recorded as open.

The theme is the one SPEC-0002 already named and did not finish applying. A gate must judge the
change the author is proposing, and nothing else. Three of the nine are the snapshot principle
reappearing in places SPEC-0002 did not reach: G4 reads file content from the working tree *after*
G2 has run the branch's own test command, so a test that rewrites an offending source file turns a
committed hallucinated import into a pass; the secret scan diffs the same mutable tree; and the
candidate's policy is read from disk rather than from the tree it is proposing, so an uncommitted
`agentic.toml` chose the gate list once framework maintenance had been declared.

Two more are checks that answer a weaker question than the one they are named for. The
authorising declaration was renewed by any added line mentioning the field, while the value was
read from the first occurrence in the file — so a fenced `Framework maintenance: no` renewed an
affirmative declaration written above it a year earlier. And `#egg=` was matched before the comment
marker was stripped, so a commented-out requirement declared any already-installed package.

Three are existence checks that report success on something that cannot exist: a dotted import
whose intermediate component is a `.py` file rather than a package directory; a Node package whose
`exports` map names only files that are absent; and, in the other direction, framework maintenance
that moves its own branch pattern was rejected by a tier recomputed from the policy it proposes
rather than the policy in force.

The ninth is a test that could not fail. The symlink guard on the temporary report file was
covered by a test that created the link under the test process's pid and then ran the gate in a
subprocess under a different one, touching neither name.

## Architecture

No new gate and no new configuration key. Nine changes, all inside `.agentic/gate.py` and its
tests, each closing one reproduced defect.

`Integrity` gains a `texts` map. `resolve_integrity` already runs before any gate and already
snapshots the change set, the protected paths, the authorising declaration and the virtualenv
marker; it now also snapshots the content of every file G4 will scan, through the existing
`candidate_text`, which reads the index at commit stage and the proposed tree in CI.
`check_python_imports` and `check_js_imports` read that map instead of the working tree, and `run`
forces `ctx.added` alongside `ctx.integrity` so the secret scan is taken from the same moment.

`candidate_config` is new and small: at CI stage the candidate's policy comes from `HEAD` rather
than from disk, because an uncommitted `agentic.toml` is not part of what is being proposed.

`declaration_diff_paths` captures the value on the added line and requires it to be affirmative, so
the line that satisfies the diff check is the line that declares. `resolve_policy` carries the
base ref's `branch_tier` onto the approved candidate context, so the tier that decides whether the
runner may be edited is the tier in force.

`python_module_exists` tracks whether the component it just matched was a leaf. An empty location
list is then two distinct facts rather than one: a parent that is a module and can hold no child,
which fails, and a package the walk genuinely cannot see on disk, which still declines to judge.
A module that publishes its own submodule, as `os.py` does for `os.path`, is recognised through
`sys.modules` without importing anything.

`js_exports_targets` and `js_target_is_file` collect the relative targets named anywhere in an
`exports` value and check whether one of them is a real file. A subpath pattern containing `*`
needs Node's own matcher, so the walker declines rather than calling a real package hollow.

`req_name` returns nothing for a line that starts with a comment marker. A `#egg=` fragment belongs
to a URL, and a URL never begins the line with `#`, so no real declaration is lost.

The temporary report rename moves inside the guard that deletes the temporary file, and the symlink
test runs the gate in-process so that the pid in the path is the pid that writes it.

The second pass extends the same two ideas rather than adding new ones. G5 reads the handoff from
the snapshot for exactly the reason G4 reads source from it: G5 runs after G2, so a test command
could fill in a `Verified: TBD` that `HEAD` still carries. And the CI change set becomes the
committed range `merge-base..HEAD` rather than a diff against the working tree unioned with
untracked files, because in CI the proposed tree is the candidate — an untracked spec or handoff
satisfied G0 and G5 while being invisible to every reviewer of the pull request. `Repo.tracked_at`
is new and lets G0 ask the same question about the spec files it globs. `added_lines` follows the
change set onto the committed range for the same reason.

`gate_g3_evals` rejects a pass rate that is not a real number in range. `bool` is a subclass of
`int` and `NaN` compares false against everything, so `true` and `NaN` both walked past the numeric
check and then past `overall < min_rate`, and `true` was reported as `1.000`.

## Acceptance criteria

1. `python_module_exists` fails a dotted import whose parent component is a file module, at any
   depth including the top level, and still passes `os.path`, `json.decoder` and
   `collections.abc`.
2. A source file rewritten by the branch's own test command during G2 does not change what G4
   reports; G4 fails on the import committed at `HEAD`.
3. An added line naming the declaration field renews an affirmative declaration only when the added
   line itself carries `yes` or `true`. A declaration the change genuinely adds is still honoured.
4. At CI stage an uncommitted `agentic.toml` does not decide the gate list, including after the
   committed diff has declared framework maintenance.
5. Declared framework maintenance that moves its own branch pattern to a lower tier passes G6,
   judged at the tier the base ref's policy assigns. A prototype branch still may not edit the
   runner.
6. A commented-out requirement line declares nothing, and a genuine `git+https://…#egg=name`
   requirement still declares itself.
7. A Node package whose `exports` names only absent files fails; one whose `exports` names a file
   that is present passes; one whose `exports` is a `*` pattern is not failed.
8. The temporary report file is removed when the rename fails, not only when the write fails.
9. The symlink test exercises the path the runner actually writes, and says so if the host will not
   let it create a symlink at all.
10. Every test added for the above failed against the runner immediately before its fix.
11. A handoff completed by the branch's own test command during G2 does not satisfy G5; the
    committed placeholder still fails.
12. A secret committed at `HEAD` and removed from the working tree by the G2 command is still
    found by the secret scan.
13. In CI an untracked spec does not satisfy G0 and an untracked handoff does not satisfy G5,
    while committed ones still do.
14. G3 fails an `overall_pass_rate` that is boolean, non-finite, or outside 0 to 1, and passes a
    real one.
15. The stress harness still reports all fifteen prior detections, and every scenario blocked
    before this change is still blocked.

## Out of scope

Four findings were reproduced and are deliberately not fixed here, because each needs a design
decision rather than a repair, and a wrong repair fails real repositories:

- A nested `.py` file anywhere in the tree contributes its stem as a top-level local module, so
  `docs/x.py` makes `import x` read as local. Restricting it needs a real notion of import roots
  drawn from `paths.source` and the packaging configuration; a depth heuristic would break `src/`
  layouts.
- A subpath import of a real package (`real-pkg/no-such`) is truncated to the package name and
  never checked. Checking it needs Node's own `exports` resolution algorithm.
- One declared distribution authorises every installed sibling sharing its top-level namespace.
  This is the documented trade-off that lets `google-cloud-storage` cover the `google` namespace;
  narrowing it cannot pass a hallucinated import, only an installed and undeclared one.
- `check_js_imports` reads only the root `package.json`, which is the JavaScript half of the known
  monorepo defect in `declared_python_deps`. Both belong in one change.

The second Codex pass raised nine more that are reproduced or plausible and are deliberately left
for a following change, because each needs a judgement rather than a repair: `base_policy` reads
the merge base rather than the target ref's tip, so a target that tightens its policy after the
fork does not bind the candidate; deletions are excluded from the ordinary change set, so a
delete-only change is invisible to G2 and G3; `added_lines` falls back to whole-file content for a
deletion-only diff and can report an untouched line as added; G3 accepts a stale `result.json` that
the current command did not write; `base_is_the_project_base` hard-codes `main` and `master` at CI
stage, so a project based on `develop` is told its base was self-picked; a report-write failure is
swallowed, so a stale `last-report.json` can outlive a failing run; and the virtualenv change-set
comparison is case-sensitive on a case-insensitive filesystem.

Also out of scope: the duplicate `SPEC-0002` filename on `main`, and any change to `agentic.toml`
`[tiers]` or `[stages]`.

## Risk tier

Production, because the change is to the runner that judges every other change, and because
framework maintenance is production work at every tier.

## Verification

`tests/test_gate.py`, class `Round7`, plus the amended symlink test in `Integrity`. Run
`python -m pytest tests/test_gate.py`, `python .agentic/gate.py`, and
`python C:/Users/j_car/gates-stress/harness.py`.
