# SPEC-0002: Public release and a gated deployment stage

Risk tier: production
Owner: Jimmy Carroll
Status: accepted

## Intent

Make the repository fit for strangers: a public-facing README that says what the tool is and why
in thirty seconds, a licence so people are allowed to use it, and the CD half of the pipeline so
"gates" visibly leads to "deploy" with a human approval between them.

## Architecture

One workflow file, two jobs. `gates` is unchanged. `deploy` declares `needs: gates`, runs only on
pushes to `main`, and binds to the `production` GitHub environment, which holds the required
reviewer. It downloads the gate report artefact the `gates` job uploaded and refuses to proceed if
that report is not `ok`, so the deploy step never runs against a report it did not see. The deploy
step itself is a placeholder echo; the wiring is the deliverable. Trade-off: a placeholder rather
than a real target, because this repository has nothing to deploy and a fake target would be
verified against nothing.

## Acceptance criteria

1. `deploy` is skipped on pull request runs and runs only on `push` to `main`.
2. `deploy` cannot start until `gates` succeeds, and pauses for the `production` reviewer.
3. `deploy` fails if the downloaded gate report has `ok: false`.
4. README opens with badges (CI, Python, licence), a one-paragraph pitch, Figure 1 as an image,
   and a copy-paste quick start. No em or en dashes.
5. `LICENSE` is MIT.
6. `docs/figure-1-spectrum.svg` renders on GitHub with no external fonts or stylesheets.

## Out of scope

- A real deployment target. Users replace the placeholder step.
- Staging or multi-environment promotion. One environment shows the pattern.
- Any change to gate logic.

## Risk tier

Production: `main` is protected and the deploy stage will run on every merge to it; a mistake here
either blocks all merges or deploys without approval.

## Verification

- Criterion 1: the CI run on this PR shows `deploy` skipped.
- Criteria 2 and 3: the first push to `main` after merge shows `deploy` waiting for approval, then
  printing the gate report and the placeholder line after approval. Recorded in HANDOFF-0002.
- Criteria 4 to 6: render on GitHub after merge; dash check `grep -c` on README is zero.
