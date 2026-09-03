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
  python .agentic/gate.py --stage ci --base origin/main
  python .agentic/gate.py --tier production    # raise the tier; it can never be lowered
  python .agentic/gate.py --json               # machine-readable report on stdout
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
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
        p = subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
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

    def changed_files(self, base: str | None, staged: bool) -> tuple[list[str], str]:
        """Returns (files, mode). mode explains how the set was derived, for the report."""
        if staged:
            out = self.git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
            return sorted(f for f in out.splitlines() if f.strip()), "staged"
        mb = self.merge_base(base)
        mode = "no base resolvable: whole tree"
        if mb:
            out = self.git("diff", "--name-only", "--diff-filter=ACMR", mb)
            untracked = self.git("ls-files", "--others", "--exclude-standard")
            files = sorted(f for f in set(out.splitlines()) | set(untracked.splitlines()) if f.strip())
            head = self.git("rev-parse", "HEAD").strip()
            if files or mb != head:
                return files, f"diff vs merge-base {mb[:10]} ({base})"
            mode = f"clean tree on {base} itself: whole-tree audit"
        tracked = self.git("ls-files")
        untracked = self.git("ls-files", "--others", "--exclude-standard")
        files = set(tracked.splitlines()) | set(untracked.splitlines())
        return sorted(f for f in files if f.strip()), mode

    def added_lines(self, base: str | None, staged: bool, files: list[str]) -> dict[str, list[tuple[int, str]]]:
        """file -> [(line_no, text)] of ADDED lines. Falls back to full content when no diff base."""
        args: list[str] | None = None
        if staged:
            args = ["diff", "--cached", "-U0", "--diff-filter=ACMR"]
        else:
            mb = self.merge_base(base)
            if mb:
                args = ["diff", "-U0", "--diff-filter=ACMR", mb]
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


def is_protected(path: str) -> bool:
    """True for the policy and the runner, false for the artefacts they write at run time.
    Case-folded: on a case-insensitive filesystem Agentic.toml is the same file."""
    p = path.replace("\\", "/").lower()
    if match_any(p, PROTECTED_RUNTIME):
        return False
    return (p in PROTECTED_FILES or p.startswith(PROTECTED_DIRS)
            or p in tuple(d.rstrip("/") for d in PROTECTED_DIRS))


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
    committed: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    merge_base: str | None = None
    tier_override_problem: str | None = None
    _added: dict | None = None

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
            self._added = self.repo.added_lines(self.base, self.stage == "commit", self.changed)
        return self._added


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
                      f"The tier decides which gates apply, so lowering it is a skip flag. The run stays "
                      f"'{tier}': move the work to a {requested} branch instead.")
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


def maintenance_spec(ctx: Ctx) -> str | None:
    """The spec THIS change adds or modifies that declares it framework maintenance.

    A spec merged earlier does not authorise a later change: the declaration has to be in the
    diff under review, or it is not a declaration, it is a standing permission."""
    spec_dir = ctx.cfg_get("paths", "specs", default="specs").rstrip("/") + "/"
    changed = {f.replace("\\", "/") for f in list(ctx.changed) + list(ctx.committed)}
    for rel in sorted(changed):
        if not rel.startswith(spec_dir) or not rel.endswith(".md"):
            continue
        val = (field_value(read_text(ctx.repo.root / rel) or "", "Framework maintenance") or "")
        if val.lower().strip("* ") in ("yes", "true"):   # a boolean, not a sentence to interpret
            return rel
    return None


def gate_g0_spec(ctx: Ctx) -> GateResult:
    spec_dir = ctx.cfg_get("paths", "specs", default="specs")
    required = [s.lower() for s in ctx.cfg_get("spec", "required_sections", default=[])]
    ids = referenced_spec_ids(ctx)
    if not ids:
        return _fail("G0", "no spec referenced",
                     [f"reference a SPEC-NNNN in the branch name, a commit message, a changed handoff's Spec: field, or add/modify a file under {spec_dir}/",
                      f"template: .agentic/templates/SPEC.md"])
    evidence, problems = [], []
    for sid in sorted(ids):
        matches = list((ctx.repo.root / spec_dir).glob(f"**/{sid}*.md"))
        if not matches:
            problems.append(f"{sid}: referenced but no file {spec_dir}/**/{sid}*.md")
            continue
        for spec in matches:
            rel = spec.relative_to(ctx.repo.root).as_posix()
            text = read_text(spec) or ""
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
            if not problems or all(rel not in p for p in problems):
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
    if not isinstance(overall, (int, float)):
        problems.append("result has no numeric overall_pass_rate")
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
        line = line.split("#", 1)[0].strip()
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
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", "dist", "build"}
    for p in root.rglob("*"):
        if any(part in skip for part in p.parts):
            continue
        if p.suffix == ".py":
            mods.add(p.stem)
            mods.add(p.parent.name)
        elif p.is_dir() and (p / "__init__.py").exists():
            mods.add(p.name)
    for p in root.iterdir():
        if p.is_dir() and not p.name.startswith("."):
            mods.add(p.name)
    return mods


def check_python_imports(ctx: Ctx, files: list[str]) -> list[str]:
    aliases = dict(DEFAULT_IMPORT_ALIASES)
    aliases.update(ctx.cfg_get("review", "import_aliases", default={}))
    declared, manifests = declared_python_deps(ctx.repo.root, aliases)
    local = local_python_modules(ctx.repo.root)
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    problems = []
    for f in files:
        text = read_text(ctx.repo.root / f)
        if text is None:
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
                if top in aliases and re.sub(r"[-_.]+", "_", aliases[top].lower()) in declared:
                    continue
                if name in aliases and re.sub(r"[-_.]+", "_", aliases[name].lower()) in declared:
                    continue
                if cands & declared:
                    continue
                where = (f"not declared in {manifests}" if manifests
                         else "and no requirements*.txt or pyproject.toml exists to declare it")
                problems.append(f"{f}:{ln}: import '{top}' is not stdlib, not local, {where}")
    return problems


def check_js_imports(ctx: Ctx, files: list[str]) -> list[str]:
    pkg = ctx.repo.root / "package.json"
    declared: set[str] = set()
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                declared |= set((data.get(k) or {}).keys())
        except json.JSONDecodeError:
            pass
    ignore_prefixes = tuple(ctx.cfg_get("review", "js_alias_prefixes", default=["@/", "~/", "#"]))
    spec_re = re.compile(r"""(?:\bimport\s+(?:[^'"]*?\s+from\s+)?|\brequire\s*\(\s*|\bimport\s*\(\s*)['"]([^'"]+)['"]""")
    problems = []
    for f in files:
        text = read_text(ctx.repo.root / f)
        if text is None:
            continue
        for m in spec_re.finditer(text):
            spec = m.group(1)
            if spec.startswith((".", "/")) or spec.startswith(ignore_prefixes):
                continue
            if spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS:
                continue
            parts = spec.split("/")
            name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
            if name not in declared:
                ln = text.count("\n", 0, m.start()) + 1
                where = "package.json" if pkg.is_file() else "no package.json found"
                problems.append(f"{f}:{ln}: import '{name}' not declared in {where}")
    return problems


def gate_g4_review(ctx: Ctx) -> GateResult:
    problems, evidence = [], []
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
        p1 = check_python_imports(ctx, py) if py else []
        p2 = check_js_imports(ctx, js) if js else []
        problems.extend(p1 + p2)
        evidence.append(f"import check: {len(py)} python, {len(js)} js/ts files, {len(p1) + len(p2)} unresolved")
    if problems:
        return _fail("G4", "review findings", problems)
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
        text = read_text(ctx.repo.root / f) or ""
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


def gate_invocation_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if "gate.py" in line]


def ci_files_running_the_gate(ctx: Ctx, files: list[str]) -> list[str]:
    """CI definitions whose invocation of the runner this change alters.

    A definition that invokes the runner decides whether the gates run at all, so changing that
    decision is a change to the judge. Bumping a Python version or an action version in the same
    file is not, and failing those would train people to declare maintenance for everything."""
    ref = ctx.merge_base or "HEAD"
    out = []
    for f in files:
        if not match_any(f, PROTECTED_CI):
            continue
        before = gate_invocation_lines(ctx.repo.show(ref, f))
        if not before:
            continue                    # it did not run the gate, so this is an ordinary CI change
        if before != gate_invocation_lines(read_text(ctx.repo.root / f) or ""):
            out.append(f)
    return out


def gate_g6_integrity(ctx: Ctx) -> GateResult:
    """The change may not set the rules it is judged by, nor understate its own risk tier.

    Locally this is a tripwire: a change that edits the runner can edit this gate out of it in
    the same breath. The boundary that holds is CI, which restores .agentic/ and agentic.toml
    from the base ref before running, so the judge is never the branch's own copy."""
    problems, evidence = [], []
    if ctx.tier_override_problem:
        problems.append(ctx.tier_override_problem)
    if ctx.stage == "commit":
        change_set = sorted(set(ctx.changed) | set(ctx.vanished))
    elif ctx.merge_base is None:
        problems.append("no base ref resolvable: with no change set, neither the policy, the runner "
                        "nor the tier can be checked against the diff. Fetch the base branch, or pass --base.")
        change_set = []
    elif "whole-tree audit" in ctx.changed_mode:
        evidence.append(f"nothing differs from {ctx.base}: the policy, runner and tier checks are vacuous")
        change_set = []
    else:
        change_set = sorted(set(ctx.changed) | set(ctx.committed) | set(ctx.vanished))
    protected = sorted(set(f for f in change_set if is_protected(f))
                       | set(ci_files_running_the_gate(ctx, change_set)))
    if protected:
        declared = maintenance_spec(ctx)
        if ctx.tier != "production":
            problems.append(f"{protected[:6]}: the policy and the runner may not be changed from a "
                            f"'{ctx.tier}' branch. Framework maintenance is production work.")
        elif declared is None:
            problems.append(f"{protected[:6]}: this change edits the policy, the runner or the CI "
                            "definition that judges it. "
                            "If it is deliberate framework maintenance, say so in the spec with a "
                            "'Framework maintenance: yes' field so that a human reviews it. CI restores "
                            "both from the base ref before running the gates.")
        else:
            evidence.append(f"declared framework maintenance in {declared}: {protected[:6]}")
    src = [f for f in change_set if match_any(f, ctx.cfg_get("paths", "source", default=[]))]
    if src and ctx.tier != "production":
        problems.append(f"branch '{ctx.branch}' claims tier '{ctx.tier}', but the change carries production "
                        f"source: {src[:6]}. A tier that does not run G0, G3 or G5 may not ship source. "
                        "Move the work to a production branch.")
    evidence.append(f"change set {len(change_set)} files: {len(protected)} policy or runner, {len(src)} source; "
                    f"branch '{ctx.branch}' is tier '{ctx.tier}'")
    if problems:
        return _fail("G6", "the change alters what judges it", problems)
    return _pass("G6", evidence)


GATES = {"G0": gate_g0_spec, "G1": gate_g1_context, "G2": gate_g2_tests,
         "G3": gate_g3_evals, "G4": gate_g4_review, "G5": gate_g5_handoff,
         "G6": gate_g6_integrity}


# ----------------------------------------------------------------------------- runner

def load_config(root: Path) -> dict:
    p = root / "agentic.toml"
    if not p.is_file():
        raise SystemExit(f"agentic.toml not found in {root}. Copy it from the framework and edit [paths].")
    return tomllib.loads(p.read_text(encoding="utf-8"))


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
    if required and sorted(got) != sorted(set(required)):
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


def run(root: Path, stage: str, base: str | None, tier_override: str | None) -> dict:
    cfg = load_config(root)
    repo = Repo(root)
    branch = repo.branch()
    tier, tier_problem = resolve_tier(cfg, branch, tier_override)
    if base is None and stage != "commit":
        env = os.environ if repo.ci_env_applies() else {}
        base = (env.get("GITHUB_BASE_REF") and f"origin/{env['GITHUB_BASE_REF']}") \
            or (env.get("SYSTEM_PULLREQUEST_TARGETBRANCH") and
                "origin/" + env["SYSTEM_PULLREQUEST_TARGETBRANCH"].removeprefix("refs/heads/")) \
            or cfg.get("project", {}).get("base_branch", "main")
        if base and not repo.git("rev-parse", "--verify", "--quiet", base).strip():
            for alt in (f"origin/{base}", base.removeprefix("origin/")):
                if repo.git("rev-parse", "--verify", "--quiet", alt).strip():
                    base = alt
                    break
            else:
                base = None
    changed, mode = repo.changed_files(base, stage == "commit")
    ctx = Ctx(repo, cfg, stage, base, tier, branch, changed, mode,
              committed=repo.committed_changed_files(base), tier_override_problem=tier_problem,
              vanished=repo.vanished_files(base, stage == "commit"), merge_base=repo.merge_base(base))
    default = [g for g in GATES if g != "G6"]
    required = list(cfg.get("tiers", {}).get("required", {}).get(tier, default))
    if stage == "commit":
        commit_gates = cfg.get("stages", {}).get("commit", ["G1", "G4"])
        required = [g for g in required if g in commit_gates]
    required = [g for g in required if g != "G6"] + ["G6"]   # unconditional: no tier, no stage, no key
    results = [GATES[g](ctx) for g in GATES if g in required]
    ok = verdict(results)
    return enforce_verdict({
        "ok": ok, "stage": stage, "branch": branch, "tier": tier, "base": base,
        "changed_files": len(changed), "changed_mode": mode, "required_gates": required,
        "results": [asdict(r) for r in results],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    })


def print_report(rep: dict, stream=sys.stdout) -> None:
    w = stream.write
    w(f"\nagentic-gates  stage={rep['stage']}  branch={rep['branch']}  tier={rep['tier']}  "
      f"changed={rep['changed_files']} ({rep['changed_mode']})\n")
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
    try:
        out_dir.mkdir(exist_ok=True)
        (out_dir / "last-report.json").write_text(json.dumps(rep, indent=2), encoding="utf-8")
    except OSError:
        pass
    if a.json:
        print(json.dumps(rep, indent=2))
    else:
        print_report(rep)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
