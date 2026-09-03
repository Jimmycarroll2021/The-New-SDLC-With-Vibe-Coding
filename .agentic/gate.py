#!/usr/bin/env python3
"""
agentic-gates: deterministic gates for the agentic-engineering SDLC.

Every gate returns pass / fail / not_applicable with evidence. There is no
advisory output and no skip flag. Which gates apply is decided by the risk
tier in agentic.toml (derived from the branch name), not by whoever runs it.

Python 3.11+, standard library only. LLM-agnostic: nothing here calls a model.

Usage:
  python .agentic/gate.py                      # local: working tree vs base branch
  python .agentic/gate.py --stage commit       # pre-commit: staged diff, cheap gates
  python .agentic/gate.py --stage ci --base origin/main   # policy read from the base ref
  python .agentic/gate.py --tier production    # raise the tier; it can never be lowered
  python .agentic/gate.py --json               # machine-readable report on stdout
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

GATE_NAMES = {
    "G0": "spec: exists, complete, tier matches branch",
    "G1": "context: rule file present, bounded, versioned",
    "G2": "tests: touched alongside source, and passing",
    "G3": "evals: AI surface scored against a rubric",
    "G4": "review: no secrets, no hallucinated dependencies",
    "G5": "handoff: agent run is recorded",
    "G6": "integrity: the change does not set the rules it is judged by",
}
TIER_RANK = {"prototype": 0, "internal": 1, "production": 2}

# G6. The policy and the runner must not be edited by the change they judge. Hard-coded, with
# no key in agentic.toml: a check that detects edits to agentic.toml cannot live in agentic.toml.
PROTECTED_FILES = ("agentic.toml",)
PROTECTED_DIRS = (".agentic/",)
# A CI definition that invokes the runner decides whether the gates run at all, so it is part
# of the judge. Only the ones that already ran it on the base ref count, so an unrelated new
# workflow is not swept up.
PROTECTED_CI = [".github/workflows/**", ".gitlab-ci.yml", ".circleci/config.yml",
                "azure-pipelines.yml", "Jenkinsfile"]
PROTECTED_RUNTIME = [".agentic/last-report.json", ".agentic/runs/**", ".agentic/evals/result.json",
                     "**/__pycache__/**", "*.pyc"]
# The field a spec carries to authorise a deliberate change to the policy or the runner.
DECLARATION_FIELD = "Framework maintenance"
PLACEHOLDER = re.compile(r"^\s*(<[^>]*>|TBD|TODO|\.\.\.|n/?a)?\s*$", re.I)

# import name -> distribution name, for packages whose two names differ
DEFAULT_IMPORT_ALIASES = {
    "yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python", "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "attr": "attrs",
    "dateutil": "python-dateutil", "jwt": "pyjwt", "Crypto": "pycryptodome",
    "git": "gitpython", "docx": "python-docx", "pptx": "python-pptx", "fitz": "pymupdf",
    "win32com": "pywin32", "pythoncom": "pywin32", "pywintypes": "pywin32",
    "serial": "pyserial", "OpenSSL": "pyopenssl", "psycopg2": "psycopg2-binary",
    "pkg_resources": "setuptools",
}
SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".tox", "dist", "build"}
DEFAULT_SECRET_PATTERNS = [
    r"AKIA[0-9A-Z]{16}",
    r"\bghp_[A-Za-z0-9]{36}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{60,}",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}",
    r"\bsk-[A-Za-z0-9]{20,}\b",
    r"\bAIza[0-9A-Za-z_-]{35}\b",
    r"-----BEGIN (RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----",
    r"(?i)AccountKey=[A-Za-z0-9+/=]{40,}",
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*['\"][^'\"\s]{12,}['\"]",
    r"(?i)Authorization:\s*(Bearer|Basic)\s+[A-Za-z0-9\-._~+/]{20,}=*",
    r"(?i)\b(password|pwd)=[^;\s'\"]{6,};",
]
SECRET_FILE_GLOBS = ["*.pem", "*.key", "*.p12", "*.pfx", ".env", ".env.*", "!.env.example"]
NODE_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "crypto", "dgram", "dns", "events", "fs",
    "http", "http2", "https", "module", "net", "os", "path", "perf_hooks", "process",
    "querystring", "readline", "stream", "string_decoder", "timers", "tls", "tty", "url",
    "util", "v8", "vm", "worker_threads", "zlib", "test",
}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".7z",
              ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".otf", ".pyc",
              ".xlsx", ".xlsm", ".docx", ".pptx", ".bin", ".jar", ".class", ".mp4", ".mp3"}


# ----------------------------------------------------------------------------- results

@dataclass
class GateResult:
    gate: str
    name: str
    status: str                 # pass | fail | not_applicable | error
    evidence: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("pass", "not_applicable")


def _pass(gate: str, evidence: list[str], reason: str = "") -> GateResult:
    return GateResult(gate, GATE_NAMES[gate], "pass", evidence, reason)


def _fail(gate: str, reason: str, evidence: list[str] | None = None) -> GateResult:
    return GateResult(gate, GATE_NAMES[gate], "fail", evidence or [], reason)


def _na(gate: str, reason: str) -> GateResult:
    return GateResult(gate, GATE_NAMES[gate], "not_applicable", [], reason)


# ----------------------------------------------------------------------------- repo

class Repo:
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args: str, check: bool = False) -> str:
        # core.quotePath=false: git C-quotes any path with a non-ASCII byte in line-oriented output,
        # so specs/SPEC-0007-n.md comes back as "specs/SPEC-0007-\303\261.md" and matches nothing.
        # The G6 collectors that read NUL-delimited output are unaffected either way; the readers
        # that cannot use -z (ls-files -s, the diff parsers) depend on this.
        p = subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=self.root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if check and p.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
        return p.stdout if p.returncode == 0 else ""

    def ci_env_applies(self) -> bool:
        """CI branch variables describe the CI workspace repo only. A gate run against any other
        repo (the test fixtures, a nested checkout) must read git, not the runner's environment."""
        for var in ("GITHUB_WORKSPACE", "BUILD_SOURCESDIRECTORY"):
            ws = os.environ.get(var)
            if ws:
                try:
                    return Path(ws).resolve() == self.root.resolve()
                except OSError:
                    return False
        return True

    def branch(self) -> str:
        if self.ci_env_applies():
            for var in ("GITHUB_HEAD_REF", "SYSTEM_PULLREQUEST_SOURCEBRANCH", "GITHUB_REF_NAME",
                        "BUILD_SOURCEBRANCHNAME"):
                if os.environ.get(var):
                    return os.environ[var].removeprefix("refs/heads/")
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip() or "HEAD"

    def merge_base(self, base: str | None) -> str | None:
        if not base:
            return None
        mb = self.git("merge-base", base, "HEAD").strip()
        return mb or None

    def changed_files(self, base: str | None, staged: bool, ci: bool = False) -> tuple[list[str], str]:
        """Returns (files, mode). mode explains how the set was derived, for the report.

        In CI the candidate is the proposed tree, so the set is the committed diff and nothing
        else. Including the working tree there let an untracked spec or handoff - a file that is
        not part of what is being proposed, and that no reviewer will ever see - satisfy G0 and
        G5."""
        if staged:
            out = self.git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
            return sorted(f for f in out.splitlines() if f.strip()), "staged"
        mb = self.merge_base(base)
        mode = "no base resolvable: whole tree"
        if mb:
            rng = f"{mb}..HEAD" if ci else mb
            out = self.git("diff", "--name-only", "--diff-filter=ACMR", rng)
            names = set(out.splitlines())
            if not ci:
                names |= set(self.git("ls-files", "--others", "--exclude-standard").splitlines())
            files = sorted(f for f in names if f.strip())
            head = self.git("rev-parse", "HEAD").strip()
            if files or mb != head:
                return files, (f"committed diff {mb[:10]}..HEAD ({base})" if ci
                               else f"diff vs merge-base {mb[:10]} ({base})")
            mode = f"clean tree on {base} itself: whole-tree audit"
        files = set(self.git("ls-files").splitlines())
        if not ci:
            files |= set(self.git("ls-files", "--others", "--exclude-standard").splitlines())
        return sorted(f for f in files if f.strip()), mode

    def added_lines(self, base: str | None, staged: bool, files: list[str],
                    ci: bool = False) -> dict[str, list[tuple[int, str]]]:
        """file -> [(line_no, text)] of ADDED lines. Falls back to full content when no diff base."""
        args: list[str] | None = None
        if staged:
            args = ["diff", "--cached", "-U0", "--diff-filter=ACMR"]
        else:
            mb = self.merge_base(base)
            if mb:
                # in CI the committed range, for the same reason the change set uses it
                args = ["diff", "-U0", "--diff-filter=ACMR", f"{mb}..HEAD" if ci else mb]
        result: dict[str, list[tuple[int, str]]] = {}
        if args:
            cur, ln = None, 0
            for raw in self.git(*args).splitlines():
                if raw.startswith("+++ "):
                    cur = raw[4:].removeprefix("b/").strip()
                    cur = None if cur == "/dev/null" else cur
                    continue
                if raw.startswith("@@"):
                    m = re.search(r"\+(\d+)", raw)
                    ln = int(m.group(1)) if m else 1
                    continue
                if cur and raw.startswith("+") and not raw.startswith("+++"):
                    result.setdefault(cur, []).append((ln, raw[1:]))
                    ln += 1
                elif cur and not raw.startswith("-") and not raw.startswith("\\"):
                    ln += 1
        # files not covered by the diff (untracked, or no base at all): scan whole content
        for f in files:
            if f in result:
                continue
            text = read_text(self.root / f)
            if text is not None:
                result[f] = list(enumerate(text.splitlines(), start=1))
        return result

    def committed_changed_files(self, base: str | None) -> list[str]:
        """Commit-to-commit view, so a CI step that runs a trusted copy of the runner still sees
        what the branch proposed to do to it. NUL-delimited, because core.quotePath escapes
        unusual names, and rename detection off, so a rename shows as an add and a delete."""
        mb = self.merge_base(base)
        if not mb:
            return []
        out = self.git("diff", "--no-renames", "-z", "--name-only", "--diff-filter=ACMT",
                       f"{mb}..HEAD")
        return sorted(f for f in out.split("\0") if f.strip())

    def vanished_files(self, base: str | None, staged: bool) -> list[str]:
        """Paths that leave a change: deletions, the source side of a rename away, and type
        changes such as a file becoming a symlink. Removing the runner, or moving it out of
        .agentic/, is as hostile as editing it, and --diff-filter=ACMR reports neither."""
        args = ["diff", "--no-renames", "-z", "--name-only", "--diff-filter=DT"]
        if staged:
            outs = [self.git(*args, "--cached")]
        else:
            mb = self.merge_base(base)
            if not mb:
                return []
            outs = [self.git(*args, mb), self.git(*args, f"{mb}..HEAD")]
        return sorted({f for o in outs for f in o.split("\0") if f.strip()})

    def show(self, ref: str, path: str) -> str:
        return self.git("show", f"{ref}:{path}")

    def tracked_at(self, ref: str) -> set[str]:
        """Every path present in a committed tree, so a gate can tell the proposed tree from the
        checkout it happens to be running in."""
        out = self.git("ls-tree", "-r", "--name-only", ref)
        return {f.strip() for f in out.splitlines() if f.strip()}

    def show_blob(self, ref: str, path: str) -> str | None:
        """The blob, or None when the path does not exist at that revision. `show` cannot tell an
        empty file from a missing one, and at CI stage that difference decides whether a candidate
        may fall back to the working tree."""
        p = subprocess.run(["git", "-c", "core.quotePath=false", "cat-file", "-e", f"{ref}:{path}"],
                           cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        return self.git("show", f"{ref}:{path}") if p.returncode == 0 else None

    def is_tracked(self, rel: str) -> bool:
        return bool(self.git("ls-files", "--error-unmatch", rel).strip())

    def commit_messages(self, base: str | None) -> str:
        mb = self.merge_base(base)
        return self.git("log", "--format=%B", f"{mb}..HEAD") if mb else self.git("log", "--format=%B", "-n", "20")


def read_text(p: Path) -> str | None:
    if not p.is_file() or p.suffix.lower() in BINARY_EXT:
        return None
    try:
        head = p.read_bytes()[:8192]
        if b"\x00" in head:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def match_any(path: str, globs: list[str]) -> bool:
    p = path.replace("\\", "/")
    for g in globs:
        if fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p, g.removeprefix("**/")):
            return True
        if g.endswith("/**") and (p == g[:-3] or p.startswith(g[:-3] + "/")):
            return True
    return False


def is_protected(path: str, regular: bool = True) -> bool:
    """True for the policy and the runner, false for the artefacts they write at run time.
    Case-folded: on a case-insensitive filesystem Agentic.toml is the same file.

    The run-time exemption is for regular files and directories only. A symlink or a gitlink at an
    exempt path is not an artefact, it is a write-through: `.agentic/last-report.json` pointing at
    `../agentic.toml` turns the runner's own report write into an overwrite of the policy."""
    p = path.replace("\\", "/").lower()
    if regular and match_any(p, PROTECTED_RUNTIME):
        return False
    return (p in PROTECTED_FILES or p.startswith(PROTECTED_DIRS)
            or p in tuple(d.rstrip("/") for d in PROTECTED_DIRS))


def norm_eol(text: str) -> str:
    """git stores LF; a working tree on Windows may hold CRLF. Compare content, not line endings."""
    return text.replace("\r\n", "\n")


# ----------------------------------------------------------------------------- context

@dataclass
class Ctx:
    repo: Repo
    cfg: dict
    stage: str
    base: str | None
    tier: str
    branch: str
    changed: list[str]
    changed_mode: str
    branch_tier: str = "production"
    committed: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    merge_base: str | None = None
    tier_override_problem: str | None = None
    _added: dict | None = None
    _integrity: "Integrity | None" = None

    def cfg_get(self, *keys, default=None):
        node = self.cfg
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def added(self) -> dict[str, list[tuple[int, str]]]:
        if self._added is None:
            self._added = self.repo.added_lines(self.base, self.stage == "commit", self.changed,
                                                ci=self.stage == "ci")
        return self._added

    @property
    def integrity(self) -> Integrity:
        """What G6 judges, resolved once. run() forces it before any gate executes: see
        resolve_integrity for why that ordering is the whole fix."""
        if self._integrity is None:
            self._integrity = resolve_integrity(self)
        return self._integrity


def detect_tier(cfg: dict, branch: str) -> str:
    patterns = cfg.get("tiers", {}).get("branch_patterns", {})
    for tier, globs in patterns.items():
        if any(fnmatch.fnmatch(branch, g) for g in globs):
            return tier
    return cfg.get("tiers", {}).get("default", "production")


def resolve_tier(cfg: dict, branch: str, requested: str | None) -> tuple[str, str | None]:
    """--tier may raise the tier the branch implies. It may never lower it: the tier decides
    which gates apply, so lowering it from the command line is a skip flag by another name."""
    tier = detect_tier(cfg, branch)
    if tier not in TIER_RANK:
        raise SystemExit(f"unknown tier '{tier}' in agentic.toml. Known: {list(TIER_RANK)}")
    if not requested:
        return tier, None
    if requested not in TIER_RANK:
        raise SystemExit(f"unknown tier '{requested}'. Known: {list(TIER_RANK)}")
    if TIER_RANK[requested] < TIER_RANK[tier]:
        return tier, (f"--tier {requested} is below the tier '{tier}' that branch '{branch}' implies. "
                      f"The tier decides which gates apply, so lowering it is a skip flag. The value is "
                      f"ignored and the branch's tier stands: move the work to a {requested} branch "
                      "instead.")
    return requested, None


def headings(text: str) -> list[str]:
    return [m.group(1).strip().lower() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M)]


def field_value(text: str, name: str) -> str | None:
    """Finds `Name: value` or `**Name:** value` or `- Name: value` (first match)."""
    m = re.search(rf"^\W*{re.escape(name)}\W*:[ 	*_]*(.*?)\s*$", text, re.M | re.I)
    return m.group(1).strip() if m else None


def run_cmd(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def tail(text: str, n: int = 25) -> list[str]:
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-n:]


# ----------------------------------------------------------------------------- gates

def referenced_spec_ids(ctx: Ctx) -> set[str]:
    """A spec is referenced by the branch name, a commit message since the base, the path of a
    changed file under paths.specs, or the Spec: field of a changed handoff."""
    spec_dir = ctx.cfg_get("paths", "specs", default="specs").rstrip("/") + "/"
    hdir = ctx.cfg_get("paths", "handoffs", default="handoffs").rstrip("/") + "/"
    id_re = re.compile(r"SPEC-\d{3,}")
    ids = set(id_re.findall(ctx.branch)) | set(id_re.findall(ctx.repo.commit_messages(ctx.base)))
    for f in ctx.changed:
        rel = f.replace("\\", "/")
        if rel.startswith(spec_dir):
            ids |= set(id_re.findall(f))
        elif rel.startswith(hdir) and f.endswith(".md"):
            ids |= set(id_re.findall(read_text(ctx.repo.root / f) or ""))   # the handoff's Spec: field
    return ids


def spec_problems(ctx: Ctx, rel: str, text: str) -> tuple[list[str], str | None]:
    """G0's checks on one spec, as a function of its text, so that G6 can hold the spec claiming to
    authorise a change to the runner to exactly the bar G0 holds every other spec to."""
    required = [s.lower() for s in ctx.cfg_get("spec", "required_sections", default=[])]
    problems = []
    hs = headings(text)
    missing = [s for s in required if not any(h == s or h.startswith(s) for h in hs)]
    if missing:
        problems.append(f"{rel}: missing sections {missing}")
    tier_val = (field_value(text, "Risk tier") or "").lower().strip("* ")
    tier_word = next((t for t in TIER_RANK if t in tier_val), None)
    if tier_word is None:
        problems.append(f"{rel}: 'Risk tier:' must be one of {list(TIER_RANK)}, got {tier_val!r}")
    elif TIER_RANK[tier_word] < TIER_RANK.get(ctx.tier, 2):
        problems.append(f"{rel}: declares tier '{tier_word}' but branch '{ctx.branch}' is '{ctx.tier}'. "
                        f"Move the work to a {tier_word} branch or raise the spec's tier.")
    placeholders = re.findall(r"<[a-z][^>\n]{2,60}>", text)
    if placeholders:
        problems.append(f"{rel}: unfilled placeholders {placeholders[:4]}")
    return problems, tier_word


def gate_g0_spec(ctx: Ctx) -> GateResult:
    spec_dir = ctx.cfg_get("paths", "specs", default="specs")
    ids = referenced_spec_ids(ctx)
    if not ids:
        return _fail("G0", "no spec referenced",
                     [f"reference a SPEC-NNNN in the branch name, a commit message, a changed handoff's Spec: field, or add/modify a file under {spec_dir}/",
                      f"template: .agentic/templates/SPEC.md"])
    evidence, problems = [], []
    # in CI the proposed tree is the candidate: an untracked spec is not part of what is being
    # proposed, and no reviewer of the pull request will ever see it
    in_tree = ctx.repo.tracked_at("HEAD") if ctx.stage == "ci" else None
    for sid in sorted(ids):
        rels = [p.relative_to(ctx.repo.root).as_posix()
                for p in (ctx.repo.root / spec_dir).glob(f"**/{sid}*.md")]
        if in_tree is not None:
            rels = [r for r in rels if r in in_tree]
        if not rels:
            problems.append(f"{sid}: referenced but no file {spec_dir}/**/{sid}*.md")
            continue
        for rel in rels:
            probs, tier_word = spec_problems(ctx, rel, candidate_text(ctx, rel))
            problems.extend(probs)
            if not probs:
                evidence.append(f"{rel}: sections ok, tier '{tier_word}' >= branch tier '{ctx.tier}'")
    if problems:
        return _fail("G0", "spec incomplete or mismatched", problems)
    return _pass("G0", evidence)


def gate_g1_context(ctx: Ctx) -> GateResult:
    rel = ctx.cfg_get("context", "rule_file", default="AGENTS.md")
    required = [s.lower() for s in ctx.cfg_get("context", "required_sections", default=[])]
    max_lines = int(ctx.cfg_get("context", "max_lines", default=200))
    p = ctx.repo.root / rel
    text = read_text(p)
    if text is None:
        return _fail("G1", f"{rel} not found", ["the agent has no rules to load; create it from the template in this repo"])
    problems, evidence = [], []
    n = len(text.splitlines())
    if n > max_lines:
        problems.append(f"{rel} is {n} lines, limit {max_lines}: static context is paid on every turn. Move detail into skills or docs.")
    else:
        evidence.append(f"{rel}: {n} lines (limit {max_lines})")
    hs = headings(text)
    missing = [s for s in required if not any(h == s or h.startswith(s) for h in hs)]
    if missing:
        problems.append(f"{rel}: missing sections {missing}")
    else:
        evidence.append(f"{rel}: sections {required} present")
    if not ctx.repo.is_tracked(rel):
        problems.append(f"{rel} is not tracked by git: rule files are versioned like code")
    else:
        evidence.append(f"{rel}: tracked")
    hits = scan_secrets(ctx, {rel: list(enumerate(text.splitlines(), 1))})
    if hits:
        problems.extend(hits)
    if problems:
        return _fail("G1", "rule file fails the context checks", problems)
    return _pass("G1", evidence)


def gate_g2_tests(ctx: Ctx) -> GateResult:
    src_globs = ctx.cfg_get("paths", "source", default=[])
    tst_globs = ctx.cfg_get("paths", "tests", default=[])
    src = [f for f in ctx.changed if match_any(f, src_globs) and not match_any(f, tst_globs)]
    tst = [f for f in ctx.changed if match_any(f, tst_globs)]
    evidence = [f"source files changed: {len(src)}", f"test files changed: {len(tst)}"]
    if ctx.cfg_get("tests", "require_test_touch", default=True) and src and not tst:
        return _fail("G2", "source changed without any test change",
                     [f"changed: {f}" for f in src[:10]] + ["tests are the contract with the agent; a change that ships source without touching a test is unverified"])
    if ctx.stage == "commit" and not ctx.cfg_get("tests", "run_on_commit", default=False):
        return _pass("G2", evidence + ["test run deferred to ci/local stage (tests.run_on_commit = false)"])
    cmd = ctx.cfg_get("tests", "command")
    if not cmd:
        return _fail("G2", "tests.command not configured in agentic.toml")
    code, out = run_cmd(cmd, ctx.repo.root, int(ctx.cfg_get("tests", "timeout_seconds", default=900)))
    if code != 0:
        return _fail("G2", f"test command exited {code}: {cmd}", tail(out))
    return _pass("G2", evidence + [f"ran: {cmd}"] + tail(out, 5))


def gate_g3_evals(ctx: Ctx) -> GateResult:
    ai_globs = ctx.cfg_get("paths", "ai_surface", default=[])
    ai = [f for f in ctx.changed if match_any(f, ai_globs)]
    if not ai:
        return _na("G3", "no change touches the configured AI surface (paths.ai_surface)")
    cmd = ctx.cfg_get("evals", "command")
    result_file = ctx.cfg_get("evals", "result_file", default=".agentic/evals/result.json")
    min_rate = float(ctx.cfg_get("evals", "min_pass_rate", default=0.9))
    min_cases = int(ctx.cfg_get("evals", "min_cases", default=5))
    dims = ctx.cfg_get("evals", "required_dimensions", default=[])
    allow_stub = bool(ctx.cfg_get("evals", "allow_stub_target", default=False))
    if not cmd:
        return _fail("G3", "AI surface changed but evals.command is not configured",
                     [f"changed: {f}" for f in ai[:10]])
    code, out = run_cmd(cmd, ctx.repo.root, int(ctx.cfg_get("evals", "timeout_seconds", default=1800)))
    if code != 0:
        return _fail("G3", f"eval command exited {code}: {cmd}", tail(out))
    rp = ctx.repo.root / result_file
    if not rp.is_file():
        return _fail("G3", f"eval runner produced no {result_file}", tail(out))
    try:
        res = json.loads(rp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _fail("G3", f"{result_file} is not valid JSON: {e}")
    problems, evidence = [], [f"AI surface changed: {', '.join(ai[:6])}"]
    cases = int(res.get("cases", 0))
    if cases < min_cases:
        problems.append(f"{cases} eval cases, minimum {min_cases}: a demo proves it can succeed once")
    overall = res.get("overall_pass_rate")
    # bool is a subclass of int, and NaN compares false against everything, so `true` and `NaN`
    # both used to walk straight past `overall < min_rate` and be reported as a pass
    if not isinstance(overall, (int, float)) or isinstance(overall, bool) \
            or overall != overall or overall in (float("inf"), float("-inf")):
        problems.append("result has no numeric overall_pass_rate")
    elif not 0.0 <= overall <= 1.0:
        problems.append(f"overall_pass_rate {overall} is not a rate between 0 and 1")
    elif overall < min_rate:
        problems.append(f"overall_pass_rate {overall:.3f} < {min_rate:.2f}")
    else:
        evidence.append(f"overall_pass_rate {overall:.3f} >= {min_rate:.2f} over {cases} cases")
    got = res.get("dimensions", {}) or {}
    for d in dims:
        if d not in got:
            problems.append(f"dimension '{d}' not scored")
            continue
        if not str(got[d].get("rubric", "")).strip():
            problems.append(f"dimension '{d}' has no rubric: an eval without a rubric measures nothing")
        if "pass_rate" not in got[d]:
            problems.append(f"dimension '{d}' has no pass_rate")
    target = str(res.get("target", ""))
    if target.startswith("builtin-stub") and not allow_stub:
        problems.append("eval target is the built-in stub: nothing real was evaluated (evals.allow_stub_target = false)")
    evidence.append(f"target: {target or 'unspecified'}")
    if problems:
        return _fail("G3", "evals below bar or under-specified", problems)
    return _pass("G3", evidence)


def scan_secrets(ctx: Ctx, added: dict[str, list[tuple[int, str]]]) -> list[str]:
    patterns = [re.compile(p) for p in DEFAULT_SECRET_PATTERNS + list(ctx.cfg_get("review", "extra_secret_patterns", default=[]))]
    exclude = ctx.cfg_get("review", "exclude", default=[])
    hits = []
    for f, lines in added.items():
        if match_any(f, exclude):
            continue
        for ln, text in lines:
            if "agentic:allow" in text:
                continue
            for pat in patterns:
                if pat.search(text):
                    hits.append(f"{f}:{ln}: matches secret pattern /{pat.pattern[:40]}/")
                    break
    return hits


def declared_python_deps(root: Path, aliases: dict[str, str]) -> tuple[set[str], list[str]]:
    names, manifests = set(), []

    def norm(s: str) -> str:
        return re.sub(r"[-_.]+", "_", s.strip().lower())

    def req_name(line: str) -> str | None:
        raw = line.strip()
        if raw.startswith("#"):
            # a commented-out requirement declares nothing. The #egg= fragment belongs to a URL,
            # and a URL never starts the line with a comment marker, so this cannot lose a real
            # declaration - while without it, any installed package could be "declared" by a
            # comment naming it.
            return None
        egg = re.search(r"#egg=([A-Za-z0-9._-]+)", raw)     # a VCS or URL requirement names itself
        if egg:
            return norm(egg.group(1))
        line = raw.split("#", 1)[0].strip()
        if line.startswith(("-e ", "--editable ")):          # an editable install is a declaration
            line = line.split(" ", 1)[1].strip()
            return norm(Path(line).name) if line and not line.startswith("-") else None
        if not line or line.startswith(("-", "git+", "http")):
            return None
        return norm(re.split(r"[\s\[<>=!~;@]", line, 1)[0])

    for req in root.glob("requirements*.txt"):
        manifests.append(req.name)
        for line in (read_text(req) or "").splitlines():
            n = req_name(line)
            if n:
                names.add(n)
    pp = root / "pyproject.toml"
    if pp.is_file():
        manifests.append("pyproject.toml")
        try:
            data = tomllib.loads(pp.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        deps = list(data.get("project", {}).get("dependencies", []))
        for group in data.get("project", {}).get("optional-dependencies", {}).values():
            deps += list(group)
        for group in data.get("dependency-groups", {}).values():
            deps += [d for d in group if isinstance(d, str)]
        poetry = data.get("tool", {}).get("poetry", {})
        deps += list(poetry.get("dependencies", {}).keys())
        for grp in poetry.get("group", {}).values():
            deps += list(grp.get("dependencies", {}).keys())
        for d in deps:
            n = req_name(d)
            if n:
                names.add(n)
    return names, manifests


def local_python_modules(root: Path) -> set[str]:
    mods = set()
    skip = SKIP_DIRS
    for p in root.rglob("*"):
        try:    # relative: an ancestor of the repository named "build" must not blank the scan
            rel_parts = p.relative_to(root).parts
        except ValueError:
            continue
        if any(part in skip for part in rel_parts):
            continue
        if p.suffix == ".py":
            mods.add(p.stem)
            # only a real package contributes its directory name. Without this, any directory
            # anywhere holding one .py file makes an import of that directory's name look local,
            # which is the empty-directory bypass with a file dropped in it.
            if (p.parent / "__init__.py").is_file():
                mods.add(p.parent.name)
        elif p.is_dir() and (p / "__init__.py").exists():
            mods.add(p.name)
    for p in root.iterdir():
        if p.is_dir() and not p.name.startswith(".") and dir_has_importable(p):
            mods.add(p.name)
        elif p.is_file():   # a compiled extension at the root imports by its pre-ABI name
            for s in importlib.machinery.EXTENSION_SUFFIXES:
                if p.name.endswith(s):
                    mods.add(p.name[: -len(s)])
                    break
    return mods


def dir_has_importable(d: Path, depth: int = 6, budget: list[int] | None = None) -> bool:
    """A directory is an importable package only if it actually holds a module, at some depth.

    An empty directory named after a package is the stress test's bypass: it makes a hallucinated
    import read as a local module, and Python's implicit namespace packages make the same trick work
    for an installed one. The search recurses because a real namespace package holds no module of its
    own either: site-packages/google/ is empty until google/cloud/storage/__init__.py, two levels
    down, and stopping at one level would fail every google-cloud-* import."""
    try:
        if (d / "__init__.py").is_file():
            return True
        if budget is None:
            budget = [400]
        subdirs = []
        for c in d.iterdir():
            if c.is_file() and (c.suffix in (".py", ".pyd", ".so")
                                or any(c.name.endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES)):
                return True
            # only __pycache__ is pruned inside a package: `build`, `dist` and `env` are perfectly
            # ordinary submodule names, and pruning them by name rejects real packages
            if c.is_dir() and c.name not in ("__pycache__", ".git"):
                subdirs.append(c)
        for c in subdirs:
            if depth <= 0 or budget[0] <= 0:
                break
            budget[0] -= 1
            if dir_has_importable(c, depth - 1, budget):
                return True
    except OSError:
        return False
    return False


def python_env_note(ctx: Ctx, change_set: list[str]) -> tuple[str | None, str | None]:
    """(why the existence check cannot run, why a virtualenv marker was not honoured).

    Offline by construction: the gate never asks a package index, so "exists" means "resolves in the
    environment the gate runs in". When the project ships its own virtualenv and the gate is not
    running inside it, that environment is not observable, and the existence half is reported as not
    applicable with the reason rather than passing silently.

    The escape is deliberately not something a change can grant itself. A marker this change brings
    with it is ignored, and so is one with no library directory behind it: either would be a skip
    flag for half of G4, written in the diff being judged. `mkdir .venv && touch .venv/pyvenv.cfg`
    is the obvious attempt and it does not work."""
    root = ctx.repo.root
    changed = {f.replace("\\", "/") for f in set(ctx.changed) | set(change_set)}
    try:    # any directory carrying pyvenv.cfg, not only .venv/venv/env
        envs = sorted(d for d in root.iterdir() if d.is_dir() and (d / "pyvenv.cfg").is_file())
    except OSError:
        envs = []
    for d in envs:      # every candidate first: an active environment beats a foreign one that
        try:            # happens to sort earlier, or the check is skipped while running inside it
            if Path(sys.prefix).samefile(d):
                return None, None
        except OSError:
            try:
                if Path(sys.prefix).resolve() == d.resolve():
                    return None, None
            except OSError:
                pass
    for d in envs:
        name = d.name
        if any(p == name or p.startswith(name + "/") for p in changed):
            return None, (f"./{name} is part of this change, so it does not disable the existence "
                          "check: an environment a change brings with it is a skip flag")
        if not any((d / sub).is_dir() for sub in ("lib", "Lib", "lib64", "site-packages")):
            return None, (f"./{name} has a pyvenv.cfg but no library directory, so it is not an "
                          "environment and does not disable the existence check")
        return (f"the project's packages are installed in ./{name}, which is not the interpreter "
                f"running the gate ({sys.prefix}). Run the gate with ./{name} and declared "
                "imports are checked for existence as well as declaration."), None
    return None, None


SCANNED_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def python_module_exists(top: str, dotted: str = "") -> str | None:
    """None when the import resolves to real code, otherwise why it does not.

    find_spec asks the interpreter's own finders for the top-level name. It does not execute the
    module and it does not touch the network. The remaining dotted parts are then walked on the
    filesystem rather than imported, because importing `a.b` runs `a/__init__.py`, and a gate may
    not run the code it is judging. Where the walk cannot see the package on disk it says nothing:
    a false pass is better than failing a real import the gate simply cannot observe."""
    try:
        spec = importlib.util.find_spec(top)
    except Exception:
        # any finder on sys.meta_path may raise anything at all: a gate that crashes is worse than
        # one that reports what it saw
        return "is declared but does not resolve in this environment"
    if spec is None:
        return "is declared but is not installed in this environment"
    locs = [Path(p) for p in (spec.submodule_search_locations or [])]
    if spec.origin in (None, "namespace") and not any(dir_has_importable(p) for p in locs):
        return ("is declared but resolves only to a directory holding no importable module: "
                f"{[str(p) for p in locs[:2]]}")
    parts = dotted.split(".")[1:] if dotted else []
    leaf = top if spec.submodule_search_locations is None else None
    for i, part in enumerate(parts):
        here = [d for d in locs if d.is_dir()]
        if not here:
            # empty because the previous component is a module rather than a package is an
            # observation, not a blind spot: a .py file holds no submodules. The exception is a
            # module that publishes one itself, as os.py does for os.path, and sys.modules is
            # where that is visible without importing anything.
            child = ".".join([top] + parts[:i + 1])
            if leaf and child not in sys.modules:
                return (f"is declared, but '{leaf}' is a module and not a package, so "
                        f"'{child}' cannot exist")
            return None                     # not observable on disk: do not guess
        nxt, found = [], False
        for d in here:
            if (d / part).is_dir():
                nxt.append(d / part)
                found = True
            elif (d / f"{part}.py").is_file() or any((d / (part + s)).is_file()
                                                     for s in importlib.machinery.EXTENSION_SUFFIXES):
                found = True
        if not found:
            return f"is declared, but '{'.'.join([top] + parts[:i + 1])}' does not exist in it"
        # a component that matched only as a file is where the walk has to stop next turn
        leaf = None if nxt else ".".join([top] + parts[:i + 1])
        locs = nxt
    return None


def installed_import_names() -> dict[str, list[str]]:
    """Top-level import name -> distributions that provide it, from installed metadata only."""
    try:
        return importlib.metadata.packages_distributions()
    except Exception:
        return {}


def check_python_imports(ctx: Ctx, files: list[str]) -> tuple[list[str], list[str]]:
    aliases = dict(DEFAULT_IMPORT_ALIASES)
    aliases.update(ctx.cfg_get("review", "import_aliases", default={}))
    declared, manifests = declared_python_deps(ctx.repo.root, aliases)
    # an import name is also declared when an installed distribution owns it: google-cloud-storage
    # in requirements.txt provides the `google` namespace, and no alias table can list them all
    for top, dists in installed_import_names().items():
        if any(re.sub(r"[-_.]+", "_", d.lower()) in declared for d in dists):
            declared.add(re.sub(r"[-_.]+", "_", top.lower()))
    local = local_python_modules(ctx.repo.root)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    env_note, refused = ctx.integrity.python_env
    notes = [f"import existence: not_applicable - {env_note}"] if env_note else []
    if refused:
        notes.append(f"import existence: applied - {refused}")
    problems = []
    for f in files:
        text = ctx.integrity.texts.get(f)
        if not text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError as e:
            problems.append(f"{f}: does not parse ({e.msg} line {e.lineno})")
            continue
        for node in ast.walk(tree):
            mods: list[tuple[str, int]] = []
            if isinstance(node, ast.Import):
                mods = [(a.name, node.lineno) for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [(node.module, node.lineno)]
            for name, ln in mods:
                top = name.split(".")[0]
                cands = {top.lower(), re.sub(r"[-_.]+", "_", top.lower())}
                if top in stdlib or top in local:
                    continue
                declared_here = bool(cands & declared) or any(
                    key in aliases and re.sub(r"[-_.]+", "_", aliases[key].lower()) in declared
                    for key in (top, name))
                if not declared_here:
                    where = (f"not declared in {manifests}" if manifests
                             else "and no requirements*.txt or pyproject.toml exists to declare it")
                    problems.append(f"{f}:{ln}: import '{top}' is not stdlib, not local, {where}")
                    continue
                # Declared is not the same as real. A name in requirements.txt that resolves to
                # nothing, or to an empty directory, is still a hallucinated dependency.
                why = None if env_note else python_module_exists(top, name)
                if why:
                    problems.append(f"{f}:{ln}: import '{top}' {why}")
    return problems, notes


def js_exports_targets(node, out: list[str] | None = None) -> list[str]:
    """Every relative target named anywhere in a package.json `exports` value.

    `exports` nests arbitrarily - condition maps, subpath maps, and arrays of fallbacks - so the
    targets are collected rather than resolved against Node's own algorithm."""
    out = [] if out is None else out
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, list):
        for v in node:
            js_exports_targets(v, out)
    elif isinstance(node, dict):
        for v in node.values():
            js_exports_targets(v, out)
    return out


def js_target_is_file(pkg_dir: Path, target: str) -> bool:
    rel = target[2:] if target.startswith("./") else target.lstrip("/")
    if not rel:
        return False
    t = pkg_dir.joinpath(*rel.split("/"))
    return (t.is_file() or (t / "index.js").is_file()
            or any(t.with_name(t.name + s).is_file() for s in (".js", ".json", ".node", ".mjs", ".cjs")))


def js_entry_ok(modules_dir: Path, name: str) -> bool:
    """Whether node_modules/<name> holds something Node could actually load. Filesystem only: no
    registry lookup and no `npm ls`."""
    d = modules_dir
    for part in name.split("/"):
        d = d / part
    if not d.is_dir():
        return False
    pj = d / "package.json"
    if pj.is_file():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = {}
        exports = data.get("exports")
        if exports is not None:
            # `exports` takes precedence over `main` for a bare specifier, so if it names only
            # files that are absent, Node loads nothing and neither does the gate.
            targets = [t for t in js_exports_targets(exports) if t.startswith(".")]
            if any(js_target_is_file(d, t) for t in targets if "*" not in t):
                return True
            if any("*" in t for t in targets) or not targets:
                return True     # a subpath pattern needs Node's own matcher: a walker that cannot
                                # resolve it does not get to call a real package hollow
            return False
        main = data.get("main")
        if isinstance(main, str) and main:
            t = d / main
            if t.is_file() or (t / "index.js").is_file() or any(
                    (d / (main + s)).is_file() for s in (".js", ".json", ".node", ".mjs", ".cjs")):
                return True
        for idx in ("index.js", "index.mjs", "index.cjs", "index.json", "index.node"):
            if (d / idx).is_file():
                return True
        return False
    for pat in ("*.js", "*.mjs", "*.cjs", "*.json", "*.node", "*.ts"):
        if any(d.glob(pat)):
            return True
    return False


def node_modules_dirs(root: Path, rel_file: str) -> list[Path]:
    """Every node_modules from the importing file's directory up to the repository root, which is
    how Node resolves and how a workspace or monorepo is laid out."""
    out, d = [], (root / rel_file).parent
    while True:
        if (d / "node_modules").is_dir():
            out.append(d / "node_modules")
        if d == root or root not in d.parents:
            break
        d = d.parent
    return out


def js_module_exists(root: Path, rel_file: str, name: str) -> str | None:
    dirs = node_modules_dirs(root, rel_file)
    if any(js_entry_ok(nm, name) for nm in dirs):
        return None
    if any((nm / Path(*name.split("/"))).is_dir() for nm in dirs):
        return "is declared but its node_modules entry has nothing Node could load"
    return "is declared in package.json but is not installed in node_modules"


def check_js_imports(ctx: Ctx, files: list[str]) -> tuple[list[str], list[str]]:
    pkg = ctx.repo.root / "package.json"
    declared: set[str] = set()
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                declared |= set((data.get(k) or {}).keys())
        except json.JSONDecodeError:
            pass
    has_modules = any(node_modules_dirs(ctx.repo.root, f) for f in files)
    env_note = None if has_modules else (
        "node_modules is not present, so the gate cannot tell an installed package from a "
        "hallucinated one. Install the project's packages before the gate runs.")
    ignore_prefixes = tuple(ctx.cfg_get("review", "js_alias_prefixes", default=["@/", "~/", "#"]))
    spec_re = re.compile(r"""(?:\bimport\s+(?:[^'"]*?\s+from\s+)?|\brequire\s*\(\s*|\bimport\s*\(\s*)['"]([^'"]+)['"]""")
    problems = []
    for f in files:
        text = ctx.integrity.texts.get(f)
        if not text:
            continue
        for m in spec_re.finditer(text):
            spec = m.group(1)
            if spec.startswith((".", "/")) or spec.startswith(ignore_prefixes):
                continue
            if spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS:
                continue
            parts = spec.split("/")
            name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
            ln = text.count("\n", 0, m.start()) + 1
            if name not in declared:
                where = "package.json" if pkg.is_file() else "no package.json found"
                problems.append(f"{f}:{ln}: import '{name}' not declared in {where}")
                continue
            why = None if env_note else js_module_exists(ctx.repo.root, f, name)
            if why:
                problems.append(f"{f}:{ln}: import '{name}' {why}")
    return problems, ([f"import existence: not_applicable - {env_note}"] if env_note else [])


def gate_g4_review(ctx: Ctx) -> GateResult:
    problems, evidence, notes = [], [], []
    hits = scan_secrets(ctx, ctx.added)
    problems.extend(hits)
    for f in ctx.changed:
        base = f.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(base, g) for g in SECRET_FILE_GLOBS if not g.startswith("!")) \
                and not any(fnmatch.fnmatch(base, g[1:]) for g in SECRET_FILE_GLOBS if g.startswith("!")):
            problems.append(f"{f}: secret-bearing file type in the change set")
    evidence.append(f"secret scan: {sum(len(v) for v in ctx.added.values())} added lines across {len(ctx.added)} files, {len(hits)} hits")
    if ctx.cfg_get("review", "check_hallucinated_imports", default=True):
        py = [f for f in ctx.changed if f.endswith(".py")]
        js = [f for f in ctx.changed if f.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))]
        p1, n1 = check_python_imports(ctx, py) if py else ([], [])
        p2, n2 = check_js_imports(ctx, js) if js else ([], [])
        problems.extend(p1 + p2)
        evidence.append(f"import check: {len(py)} python, {len(js)} js/ts files, {len(p1) + len(p2)} unresolved")
        notes = n1 + n2
        evidence.extend(notes)
    if problems:
        # the notes say whether the existence half ran at all, which is half the reading of a
        # failure, so they travel with it rather than being dropped
        return _fail("G4", "review findings", problems + notes)
    return _pass("G4", evidence)


def gate_g5_handoff(ctx: Ctx) -> GateResult:
    hdir = ctx.cfg_get("paths", "handoffs", default="handoffs")
    required = ctx.cfg_get("handoff", "required_fields",
                           default=["Spec", "Agent", "Model", "Verified", "Not verified", "Reviewer"])
    files = [f for f in ctx.changed if f.replace("\\", "/").startswith(hdir.rstrip("/") + "/") and f.endswith(".md")]
    if not files:
        return _fail("G5", "no handoff record in the change set",
                     [f"add {hdir}/HANDOFF-<spec-id>.md from .agentic/templates/HANDOFF.md",
                      "a change with no record of what the agent did and what was verified is un-reviewable"])
    problems, evidence = [], []
    spec_dir = ctx.cfg_get("paths", "specs", default="specs")
    for f in files:
        # the candidate snapshot, taken before G2 ran the branch's own test command: a test that
        # fills in 'Verified: TBD' mid-run would otherwise complete a handoff that HEAD still
        # carries as a placeholder
        text = ctx.integrity.texts.get(f) or ""
        for name in required:
            val = field_value(text, name)
            if val is None or PLACEHOLDER.match(val):
                problems.append(f"{f}: field '{name}' missing or placeholder")
        sid = field_value(text, "Spec") or ""
        m = re.search(r"SPEC-\d{3,}", sid)
        if m and not list((ctx.repo.root / spec_dir).glob(f"**/{m.group(0)}*.md")):
            problems.append(f"{f}: Spec '{m.group(0)}' has no file under {spec_dir}/")
        if not problems or all(f not in p for p in problems):
            evidence.append(f"{f}: fields {required} present")
    if problems:
        return _fail("G5", "handoff record incomplete", problems)
    return _pass("G5", evidence)


def candidate_text(ctx: Ctx, rel: str) -> str:
    """The candidate's content for a path, from the most immutable source the stage offers.

    At commit stage that is the index: the hook judges what is about to be committed, not whatever
    the working tree happens to say when it looks. In CI it is the proposed tree, which nothing the
    branch runs can rewrite. Locally the working tree IS the candidate, and the protection there is
    that this is read before any gate executes."""
    if ctx.stage == "commit":
        return ctx.repo.git("show", f":{rel}")
    if ctx.stage == "ci":
        # no fall-through to the working tree: in CI the proposed tree is the candidate, and an
        # uncommitted file is not part of what is being proposed
        return ctx.repo.show_blob("HEAD", rel) or ""
    return read_text(ctx.repo.root / rel) or ""


def ci_files_running_the_gate(ctx: Ctx, files: list[str]) -> list[str]:
    """CI definitions that invoked the runner on the base ref and are not identical here.

    The whole file counts, not only the lines that name gate.py. The revision the workflow picks as
    the base, the depth it checks out, the directory it unpacks the trusted runner into, an
    enclosing `if:`, an injected PYTHONPATH, a `continue-on-error: true` and a bare `exit 0` before
    the call all decide what the judge is and whether it runs, and not one of them mentions
    gate.py. A definition that never ran the gate is an ordinary CI file and is not protected."""
    ref = ctx.merge_base or "HEAD"
    out = []
    for f in files:
        if not match_any(f, PROTECTED_CI):
            continue
        before = ctx.repo.show(ref, f)
        if "gate.py" not in before:
            continue                    # it did not run the gate, so this is an ordinary CI change
        if norm_eol(before) != norm_eol(candidate_text(ctx, f)):
            out.append(f)
    return out


def non_regular_paths(ctx: Ctx) -> set[str]:
    """Paths the candidate records as something other than a regular file or directory: symlinks
    (mode 120000) and gitlinks (160000) in the index, and symlinks in the working tree."""
    out = set()
    for line in ctx.repo.git("ls-files", "-s").splitlines():
        mode, _, rest = line.partition(" ")
        if mode in ("120000", "160000"):
            out.add(rest.split("\t", 1)[-1].strip().replace("\\", "/"))
    for f in ctx.changed:
        try:
            if (ctx.repo.root / f).is_symlink():
                out.add(f.replace("\\", "/"))
        except OSError:
            pass
    return out


def declaration_diff_paths(ctx: Ctx) -> set[str]:
    """Paths where THIS change introduces or rewrites the declaration line itself.

    Touching a file is not declaring anything. Without this, a blank line added to a maintenance
    spec that landed a year ago hands today's change a standing permission."""
    # the VALUE on the added line, not merely the field name: field_value reads the first
    # occurrence in the file, so a change that adds any line naming the field - a fenced example,
    # a "no" further down - used to renew an affirmative declaration written above it long ago
    added = re.compile(rf"^\+\W*{re.escape(DECLARATION_FIELD)}\W*:[ \t*_]*(.*?)\s*$", re.I)
    diffs = []
    if ctx.stage == "commit":
        diffs.append(ctx.repo.git("diff", "--cached", "-U0", "--diff-filter=ACMR"))
    elif ctx.merge_base:
        if ctx.stage != "ci":   # in CI only the committed diff counts, for the same reason
            diffs.append(ctx.repo.git("diff", "-U0", "--diff-filter=ACMR", ctx.merge_base))
        diffs.append(ctx.repo.git("diff", "-U0", "--diff-filter=ACMR", f"{ctx.merge_base}..HEAD"))
    out, cur = set(), None
    for raw in (line for d in diffs for line in d.splitlines()):
        if raw.startswith("+++ "):
            cur = raw[4:].removeprefix("b/").strip()
            cur = None if cur == "/dev/null" else cur
        elif cur:
            m = added.match(raw)
            if m and m.group(1).lower().strip("* ") in ("yes", "true"):
                out.add(cur)
    return out


def resolve_declaration(ctx: Ctx) -> tuple[str | None, list[str]]:
    """The spec THIS change relies on to authorise an edit to the policy or the runner.

    Three conditions, each of them necessary. The file has to be the spec the change references, so
    that G0 validated the same document the human is being asked to read. It has to satisfy G0 on
    its own content, so that a one-line permit file authorises nothing. And the declaration itself
    has to appear in this change's diff, so that an old yes in a file the change happens to touch
    is not a standing permission."""
    spec_dir = ctx.cfg_get("paths", "specs", default="specs").rstrip("/") + "/"
    ids = referenced_spec_ids(ctx)
    in_diff = declaration_diff_paths(ctx)
    untracked = {f.strip().replace("\\", "/")
                 for f in ctx.repo.git("ls-files", "--others", "--exclude-standard").splitlines()
                 if f.strip()} if ctx.stage == "local" else set()
    # an untracked spec is a real declaration only in a local run, where the working tree IS the
    # candidate. At commit stage the index is the candidate and at CI stage the proposed tree is
    notes: list[str] = []
    changed = {f.replace("\\", "/") for f in list(ctx.changed) + list(ctx.committed)}
    for rel in sorted(changed):
        if not rel.startswith(spec_dir) or not rel.endswith(".md"):
            continue
        text = candidate_text(ctx, rel)
        val = (field_value(text, DECLARATION_FIELD) or "").lower().strip("* ")
        if val not in ("yes", "true"):      # a boolean, not a sentence to interpret
            continue
        sid = re.search(r"SPEC-\d{3,}", rel)
        if not sid or sid.group(0) not in ids:
            notes.append(f"{rel}: declares framework maintenance, but this change does not reference it as "
                         "its spec, so G0 never validated it. The authorising file has to be the spec the "
                         "change is working to.")
            continue
        probs, _ = spec_problems(ctx, rel, text)
        if probs:
            notes.append(f"{rel}: declares framework maintenance but does not satisfy G0 itself: {probs[:2]}")
            continue
        if rel not in in_diff and rel not in untracked:
            notes.append(f"{rel}: carries a '{DECLARATION_FIELD}' declaration that this change neither adds "
                         "nor alters. Editing the file around an older declaration does not renew it.")
            continue
        return rel, notes
    return None, notes


def base_is_the_project_base(ctx: Ctx) -> bool:
    """Whether a base ref that resolves to HEAD is the honest case: a clean tree sitting on the
    project's own base branch. Anywhere else a base equal to the candidate's tip was chosen by the
    candidate, and an empty diff is a skip flag by another name."""
    configured = str(ctx.cfg_get("project", "base_branch", default="main"))
    names = {"main", "master", "origin/main", "origin/master"}
    if ctx.stage != "ci":       # locally the configured value is the author's own; in CI it is the
        names |= {configured, configured.removeprefix("origin/"), f"origin/{configured}"}
    return ctx.branch in names and (ctx.base or "") in names


@dataclass
class Integrity:
    """What G6 judges, resolved from the candidate snapshot BEFORE any gate runs.

    G2 and G3 execute the branch's own test and eval commands. Anything G6 read from the live
    filesystem after them would be state the candidate has had the opportunity to write: a
    discovered test that appends a maintenance declaration to a spec during G2 would authorise the
    very runner edit G6 exists to catch. So the change set, the protected paths and the authorising
    declaration are resolved up front, from the index at commit stage and from the proposed tree
    in CI."""
    change_set: list[str] = field(default_factory=list)
    protected: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    declared: str | None = None
    tier_floor: str | None = None
    python_env: tuple[str | None, str | None] = (None, None)
    texts: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


def resolve_integrity(ctx: Ctx) -> Integrity:
    out = Integrity()
    if ctx.stage == "commit":
        out.change_set = sorted(set(ctx.changed) | set(ctx.vanished))
    elif ctx.merge_base is None:
        out.problems.append("no base ref resolvable: with no change set, neither the policy, the runner "
                            "nor the tier can be checked against the diff. Fetch the base branch, or pass --base.")
    elif "whole-tree audit" in ctx.changed_mode:
        head = ctx.repo.git("rev-parse", "HEAD").strip()
        if base_is_the_project_base(ctx):
            out.evidence.append(f"clean tree on {ctx.base} itself: the policy, runner and tier checks have "
                                "nothing to diff, so only deletions are visible")
        else:
            out.problems.append(f"base '{ctx.base}' resolves to this branch's own tip {head[:10]}, so the diff "
                                "is empty and the policy, runner and tier checks can see nothing. Diff against "
                                f"'{ctx.cfg_get('project', 'base_branch', default='main')}' instead: a base the "
                                "change picks for itself is a skip flag.")
        # a deletion never reaches --diff-filter=ACMR, so it is not covered by that empty diff
        out.change_set = sorted(set(ctx.vanished))
    else:
        out.change_set = sorted(set(ctx.changed) | set(ctx.committed) | set(ctx.vanished))
    out.source = [f for f in out.change_set
                  if match_any(f, ctx.cfg_get("paths", "source", default=[]))]
    # The tier floor. A branch name is an assertion about risk, and the diff is the only thing in the
    # repository that can contradict it: renaming a branch to docs/whatever used to drop G0, G3 and
    # G5 silently. Refusing such a change outright would delete the tier system rather than enforce
    # it, so a change carrying production source is judged AT production tier instead. A spike is
    # still a spike, right up to the moment it carries shipping source.
    if out.source and TIER_RANK.get(ctx.tier, 2) < TIER_RANK["production"]:
        out.tier_floor = ctx.tier
        ctx.tier = "production"
    non_regular = non_regular_paths(ctx)
    out.protected = sorted(set(f for f in out.change_set
                               if is_protected(f, regular=f.replace("\\", "/") not in non_regular))
                           | set(ci_files_running_the_gate(ctx, out.change_set)))
    if out.protected:
        out.declared, notes = resolve_declaration(ctx)
        out.evidence.extend(notes)
    # resolved here, with everything else, because G2 runs the branch's own test command: a test
    # that creates a virtualenv marker mid-run would otherwise turn every G4 existence failure into
    # not_applicable, which is the snapshot bug in a different gate
    out.python_env = python_env_note(ctx, out.change_set)
    # and the content G4 scans, for the same reason again: G2 runs the branch's own test command,
    # so a test that rewrites an offending source file between the commit and the read would turn
    # a committed hallucinated import into a G4 pass while HEAD still carries it
    hdir = ctx.cfg_get("paths", "handoffs", default="handoffs").rstrip("/") + "/"
    for rel in ctx.changed:
        if rel.endswith(SCANNED_SUFFIXES) or (rel.replace("\\", "/").startswith(hdir)
                                              and rel.endswith(".md")):
            out.texts[rel] = candidate_text(ctx, rel)
    return out


def gate_g6_integrity(ctx: Ctx) -> GateResult:
    """The change may not set the rules it is judged by, nor understate its own risk tier.

    Locally this is a tripwire: a change that edits the runner can edit this gate out of it in
    the same breath. The boundary that holds is CI, which runs the base ref's copy of the runner
    from outside the working tree, so the judge is never the branch's own copy."""
    intg = ctx.integrity
    problems, evidence = list(intg.problems), list(intg.evidence)
    if ctx.tier_override_problem:
        problems.append(ctx.tier_override_problem)
    if intg.protected:
        # the BRANCH's tier, not the effective one: neither the source floor nor a raised --tier may
        # turn a spike branch into a place where the runner can be edited
        if ctx.branch_tier != "production":
            problems.append(f"{intg.protected[:6]}: the policy and the runner may not be changed from a "
                            f"'{ctx.branch_tier}' branch. Framework maintenance is production work.")
        elif intg.declared is None:
            problems.append(f"{intg.protected[:6]}: this change edits the policy, the runner or the CI "
                            "definition that judges it. If it is deliberate framework maintenance, say so "
                            f"with a '{DECLARATION_FIELD}: yes' field in the spec this change references, "
                            "added or altered in this diff, so that a human reviews it.")
        else:
            evidence.append(f"declared framework maintenance in {intg.declared}: {intg.protected[:6]}")
    if intg.tier_floor:
        evidence.append(f"branch '{ctx.branch}' names tier '{intg.tier_floor}', but the change carries "
                        f"production source: {intg.source[:6]}. A tier that does not run G0, G3 or G5 may "
                        "not decide that source ships, so this run is judged at production tier.")
    evidence.append(f"change set {len(intg.change_set)} files: {len(intg.protected)} policy or runner, "
                    f"{len(intg.source)} source; branch '{ctx.branch}' is tier '{ctx.tier}'")
    if problems:
        # the evidence carries why a candidate declaration was rejected, which is the actionable
        # half of the failure, so it is reported alongside the problems rather than dropped
        return _fail("G6", "the change alters what judges it", problems + evidence)
    return _pass("G6", evidence)


GATES = {"G0": gate_g0_spec, "G1": gate_g1_context, "G2": gate_g2_tests,
         "G3": gate_g3_evals, "G4": gate_g4_review, "G5": gate_g5_handoff,
         "G6": gate_g6_integrity}


# ----------------------------------------------------------------------------- runner

def load_config(root: Path) -> dict:
    p = root / "agentic.toml"
    if not p.is_file():
        raise SystemExit(f"agentic.toml not found in {root}. Copy it from the framework and edit [paths].")
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"agentic.toml does not parse: {e}") from None


def candidate_config(repo: Repo, root: Path, stage: str) -> dict:
    """The candidate's own policy, read from the tree it is proposing rather than from disk.

    In CI the proposed tree is HEAD and an uncommitted agentic.toml is not part of what is being
    proposed. Reading it from the working tree meant that, once the committed diff had declared
    framework maintenance, a dirty checkout chose the gate list - `[tiers.required] production = []`
    on disk collapsed the run to G6 alone."""
    if stage != "ci":
        return load_config(root)
    text = repo.show_blob("HEAD", "agentic.toml") or ""
    if not text.strip():
        return load_config(root)    # nothing committed to judge: the bootstrap case
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise SystemExit(f"agentic.toml on HEAD does not parse: {e}") from None


def verdict(results) -> bool:
    """The overall result is a function of the recorded gate statuses and of nothing else."""
    return all((r["status"] if isinstance(r, dict) else r.status) in ("pass", "not_applicable")
               for r in results)


def enforce_verdict(rep: dict) -> dict:
    """Re-derive the verdict wherever a report is produced or consumed. A report that records a
    failure can never carry a pass, however the value in it was computed. This is what a runner
    edited to say ok = True runs into."""
    ok = verdict(rep.get("results", []))
    got = [r.get("gate") for r in rep.get("results", [])]
    required = list(rep.get("required_gates", []))
    if "G6" not in got:
        # G6 runs at every tier and every stage, so a report without it did not come from this
        # runner. Dropping the failing result AND the required_gates list would otherwise leave a
        # report that re-derives as a pass, because an empty required list skips the check below.
        rep["integrity_error"] = ("the report records no G6 result, and G6 runs at every tier and "
                                  "every stage: this did not come from an intact runner")
        ok = False
    elif required and sorted(got) != sorted(set(required)):
        rep["integrity_error"] = (f"the report does not cover the gates it says are required: "
                                  f"required {sorted(set(required))}, recorded {sorted(got)}. "
                                  "all([]) is True, so an empty result set is not a pass")
        ok = False
    elif rep.get("ok") and not ok:
        rep["integrity_error"] = ("the runner reported a pass while the report records a failure: "
                                  ".agentic/gate.py has been modified or is corrupt")
    rep["ok"] = ok
    return rep


def find_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / "agentic.toml").is_file():
            return p
    return start


def resolve_base(repo: Repo, cfg: dict, stage: str, base: str | None) -> str | None:
    if base is not None or stage == "commit":
        return base
    env = os.environ if repo.ci_env_applies() else {}
    # In CI the fallback is the conventional name, never project.base_branch: that key is in the
    # file the change may be editing, so pointing it at the branch's own tip would empty the diff.
    fallback = "main" if stage == "ci" else cfg.get("project", {}).get("base_branch", "main")
    base = (env.get("GITHUB_BASE_REF") and f"origin/{env['GITHUB_BASE_REF']}") \
        or (env.get("SYSTEM_PULLREQUEST_TARGETBRANCH") and
            "origin/" + env["SYSTEM_PULLREQUEST_TARGETBRANCH"].removeprefix("refs/heads/")) \
        or fallback
    if base and not repo.git("rev-parse", "--verify", "--quiet", base).strip():
        for alt in (f"origin/{base}", base.removeprefix("origin/")):
            if repo.git("rev-parse", "--verify", "--quiet", alt).strip():
                return alt
        return None
    return base


def build_ctx(repo: Repo, cfg: dict, stage: str, base: str | None, tier_override: str | None) -> Ctx:
    branch = repo.branch()
    tier, tier_problem = resolve_tier(cfg, branch, tier_override)
    changed, mode = repo.changed_files(base, stage == "commit", ci=stage == "ci")
    return Ctx(repo, cfg, stage, base, tier, branch, changed, mode,
               branch_tier=detect_tier(cfg, branch),
               committed=repo.committed_changed_files(base), tier_override_problem=tier_problem,
               vanished=repo.vanished_files(base, stage == "commit"),
               merge_base=repo.merge_base(base))


def base_policy(ctx: Ctx) -> tuple[dict | None, str]:
    """agentic.toml as it exists on the base ref, so a relaxation can be judged by the policy it
    replaces rather than by the one replacing it."""
    if not ctx.merge_base:
        return None, "no base ref resolvable"
    text = ctx.repo.show(ctx.merge_base, "agentic.toml")
    if not text.strip():
        return None, f"no agentic.toml on {ctx.merge_base[:10]}"
    try:
        return tomllib.loads(text), f"base ref {ctx.merge_base[:10]}"
    except tomllib.TOMLDecodeError as e:
        return None, f"agentic.toml on {ctx.merge_base[:10]} does not parse: {e}"


def resolve_policy(repo: Repo, cfg: dict, stage: str, base: str | None,
                   tier_override: str | None) -> tuple[Ctx, str]:
    """Which agentic.toml the gates are judged by, and the context built from it.

    Locally it is the candidate's own policy, so a policy change can be exercised by the change
    proposing it. In CI it is the base ref's policy, so a pull request that relaxes a rule is judged
    by the rule it replaces - unless the change declares framework maintenance in the spec it
    references, which is the same authorisation channel G6 resolves from the immutable snapshot. That
    question is asked while the base policy is in force, so the candidate cannot steer the answer
    with its own [paths] or [spec] keys.

    This lives in the runner and not in the workflow on purpose. On a pull request event the CI
    definition comes from the branch, so a restore written in YAML is a rule the candidate carries;
    the runner CI executes is unpacked from the base ref and is not."""
    ctx = build_ctx(repo, cfg, stage, base, tier_override)
    if stage != "ci":
        return ctx, "candidate"
    cfg_base, why = base_policy(ctx)
    if cfg_base is None:
        return ctx, f"candidate: {why}"
    trial = build_ctx(repo, cfg_base, stage, base, tier_override)
    # the whole of G6 under the base policy, not the declaration alone: a candidate that rewrites
    # [tiers.branch_patterns] so its own branch reads as production would otherwise hand itself the
    # policy its declaration was rejected under
    if trial.integrity.declared and gate_g6_integrity(trial).status == "pass":
        # the branch's tier under the policy in force, not under the one this change proposes.
        # Recomputing it from the candidate rejected exactly the maintenance that moves a branch
        # pattern: the base policy had already approved the edit as production work.
        ctx.branch_tier = trial.branch_tier
        return ctx, f"candidate: framework maintenance declared in {trial.integrity.declared}"
    return trial, why


def run(root: Path, stage: str, base: str | None, tier_override: str | None) -> dict:
    repo = Repo(root)
    cfg = candidate_config(repo, root, stage)
    base = resolve_base(repo, cfg, stage, base)
    ctx, policy = resolve_policy(repo, cfg, stage, base, tier_override)
    _ = ctx.integrity   # BEFORE any gate runs. G2 and G3 execute the branch's own commands, so what
                        # authorises a change to the judge may not be read from state it can write.
    _ = ctx.added       # same reason: the secret scan diffs the working tree at local stage
    default = [g for g in GATES if g != "G6"]
    required = list(ctx.cfg.get("tiers", {}).get("required", {}).get(ctx.tier, default))
    if stage == "commit":
        commit_gates = ctx.cfg.get("stages", {}).get("commit", ["G1", "G4"])
        required = [g for g in required if g in commit_gates]
    required = [g for g in required if g != "G6"] + ["G6"]   # unconditional: no tier, no stage, no key
    results = [GATES[g](ctx) for g in GATES if g in required]
    ok = verdict(results)
    return enforce_verdict({
        "ok": ok, "stage": stage, "branch": ctx.branch, "tier": ctx.tier, "base": base,
        "policy": policy, "changed_files": len(ctx.changed), "changed_mode": ctx.changed_mode,
        "required_gates": required,
        "results": [asdict(r) for r in results],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })


def print_report(rep: dict, stream=sys.stdout) -> None:
    w = stream.write
    w(f"\nagentic-gates  stage={rep['stage']}  branch={rep['branch']}  tier={rep['tier']}  "
      f"changed={rep['changed_files']} ({rep['changed_mode']})\n")
    w(f"policy:   {rep.get('policy', 'candidate')}\n")
    w(f"required: {' '.join(rep['required_gates']) or '(none for this tier)'}\n\n")
    for r in rep["results"]:
        mark = {"pass": "PASS", "fail": "FAIL", "not_applicable": "N/A ", "error": "ERR "}[r["status"]]
        w(f"  {mark}  {r['gate']}  {r['name']}\n")
        if r["reason"]:
            w(f"         {r['reason']}\n")
        for e in r["evidence"][:12]:
            w(f"         - {e}\n")
        if len(r["evidence"]) > 12:
            w(f"         - ... {len(r['evidence']) - 12} more\n")
    if rep.get("integrity_error"):
        w(f"\n  INTEGRITY  {rep['integrity_error']}\n")
    w("\n" + ("ALL GATES PASS" if rep["ok"] else "GATE FAILURE: the change is not ready") + "\n\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic gates for the agentic-engineering SDLC.")
    ap.add_argument("--stage", choices=["local", "commit", "ci"], default="local")
    ap.add_argument("--base", help="base ref to diff against (default: project.base_branch, or CI target branch)")
    ap.add_argument("--tier", choices=list(TIER_RANK),
                    help="raise the tier above the one the branch implies; it cannot be lowered")
    ap.add_argument("--root", help="repo root (default: nearest directory containing agentic.toml)")
    ap.add_argument("--json", action="store_true", help="print the JSON report instead of the table")
    a = ap.parse_args(argv)
    root = Path(a.root).resolve() if a.root else find_root(Path.cwd().resolve())
    rep = enforce_verdict(run(root, a.stage, a.base, a.tier))
    out_dir = root / ".agentic"
    report = out_dir / "last-report.json"
    try:
        # Create or replace the entry; never write through it. A symlink at either path is a
        # write-through into whatever it points at, and the runner writes this file with the
        # policy already loaded, so it would not notice overwriting agentic.toml with report JSON.
        if out_dir.is_symlink() or report.is_symlink():
            raise OSError(f"{report} or its directory is a symlink: refusing to write the report")
        out_dir.mkdir(exist_ok=True)
        # the temporary file is as much of a write-through as the report itself: a symlink left at a
        # predictable .tmp path would be followed before os.replace ever runs
        tmp = out_dir / f".last-report.{os.getpid()}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rep, indent=2))
            os.replace(tmp, report)     # inside the guard: a failed rename leaks the temporary
        except BaseException:           # file just as surely as a failed write does
            tmp.unlink(missing_ok=True)
            raise
    except OSError:
        pass
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print_report(rep)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
