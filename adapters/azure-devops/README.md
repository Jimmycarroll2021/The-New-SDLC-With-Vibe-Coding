# Azure Pipelines adapter (unverified)

`azure-pipelines.yml` is the Azure DevOps equivalent of `.github/workflows/agentic-gates.yml`.
It has **not been executed on a hosted agent**. GitHub Actions is the verified CI path.

Use it only if the repository you install agentic-gates into lives in Azure DevOps. When you
first run it, check three things in the log before trusting a green result:

- the report header says `diff vs merge-base ... (origin/main)` on a PR build, not
  `no base resolvable: whole tree` (if it does, add `git fetch origin main` before the gate step);
- `branch=` shows the source branch, not `merge`;
- `tier=` is correct for that branch name.

Copy it to the repository root as `azure-pipelines.yml` and create the pipeline from the
existing file. Needs an org with hosted parallelism and, for PR triggers on a GitHub-hosted
repo, the Azure Pipelines GitHub App.
