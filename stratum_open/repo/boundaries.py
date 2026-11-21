"""Module rules from `.stratum/policy.yml`. With

    boundaries:
      modules: {api: ["server/api/**"], db: ["server/db/**"]}
      forbid: ["api -> db: Go through the service layer."]

code in `api` must not import `db`. Either side of a rule may be `*`, any
module: `api -> *` keeps `api` to itself, `* -> db` keeps everyone out of
`db`. A file belongs to the first module whose globs match it. Each import a
PR adds to a file in a module with a rule is resolved the way `graph`
resolves it, against the default branch's checkout plus the files the PR
adds, and an import the checkout can't place is let go.

`new_edges: warn` (or `fail`) also flags a module importing one it didn't
import anywhere on the default branch, a low (or high) finding. The default
branch's edges come from the checkout; with no `modules` set, each top
folder is a module (`src/<name>` and the like count two levels). Without a
checkout, or one too big to read whole, there's nothing to compare with, so
it says nothing.

Without a checkout, a forbidden import is resolved against the PR's own
files and, failing that, from its text alone: each path it could name is
matched to the modules, and it counts when all of those that land in a module
land in the same one. That catches `from server.db import x` and `../db/x`,
but a package from outside the repo that shares a module folder's name can
fool it.

Only the line that names the module is checked, and only if the PR adds it:
an import that was already there isn't this PR's doing, and neither is one
the PR only reformats (removed and added back, loading the same file). A
Python `from … import` spread over lines is read one name at a time, so a
name added to the list is checked on its own line, and a name that loads
what the names left in place already did adds nothing. The lines around
the diff come from the checkout, so a list is read whole even when the diff
shows neither its `from` line nor its `)`. Without a checkout (or with one
that doesn't match the diff), only the diff is read: a name added to a list
whose `from` line it doesn't show is missed. One finding per module a file
newly imports, naming the other lines that import it.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Callable, Iterator

from stratum_open.diff import FileDiff
from stratum_open.findings import Finding
from stratum_open.policy import POLICY_PATH, Policy, forbid_rules, matches_any
from stratum_open.repo.graph import (
    JS_EXTENSIONS, MAX_FILES, PY_EXTENSIONS, SKIP_DIRS, Import, Resolver, _minified, _read, code_files, parse_imports,
)
from stratum_open.repo.hotspots import _read_at

RULE = "boundaries/forbidden"
NEW_EDGE_RULE = "boundaries/new-edge"
NESTED_ROOTS = ("src", "lib", "packages", "apps", "services")  # folders whose children are the modules
Modules = tuple[tuple[str, tuple[str, ...]], ...]


def boundary_findings(files: list[FileDiff], policy: Policy, repo_dir: str | None, *, ref: str | None = None) -> list[Finding]:
    """`repo_dir`: the default branch's checkout. `ref`: read that side from
    this commit in `repo_dir` instead of its working tree, for a local review,
    where the working tree is the change itself."""
    settings = policy.boundaries
    rules = forbid_rules(settings)
    new_edges = settings.new_edges != "off" and repo_dir is not None
    modules = settings.modules
    module_of: Callable[[str], str | None] = (lambda path: _module(path, modules)) if modules else _top_module
    if not rules and not new_edges:
        return []
    ruled = {source for source, _, _ in rules}
    checked = [
        f for f in files
        if f.status != "deleted" and not f.binary and (source := module_of(f.path))
        and (new_edges or source in ruled or "*" in ruled)
    ]
    if not checked:
        return []

    present = {f.path for f in files if f.status != "deleted"}
    baseline: set[tuple[str, str]] | None = None
    if repo_dir:
        gone = {f.path for f in files if f.status == "deleted"} | {f.old_path for f in files if f.status == "renamed" and f.old_path}
        checkout = _code_files_at(repo_dir, ref) if ref else code_files(repo_dir)
        resolver = Resolver((set(checkout) - gone) | present)
        # A checkout read only in part, or not at all (a ref git couldn't list), can't say what's new.
        if new_edges and 0 < len(checkout) < MAX_FILES:
            baseline = _edges(repo_dir, checkout, module_of, ref)
    else:
        resolver = Resolver(present, roots={path.partition("/")[0] for path in present if "/" in path})

    bases = _bases(repo_dir, ref, checked) if repo_dir else {}
    findings = []
    for file in checked:
        source = module_of(file.path)
        old_side, new_side = _sides(file, bases.get(file.path))
        guess = not repo_dir
        # What the removed lines loaded: adding back an import of the same file only reformats it.
        removed = {line.old for hunk in file.hunks for line in hunk.lines if line.kind == "-" and line.old is not None}
        before = {what for imp, _ in _changed_imports(file.path, old_side, removed)
                  for _, what, _ in _targets(resolver, file.path, imp, module_of, guess=guess)}
        # The names left in place in a statement the PR adds a name to load what they did,
        # so a new name loading the same file adds nothing; that goes for their own statement only.
        kept_loads: dict[tuple[Import, ...], set[str]] = {}
        by_target: dict[str, list[tuple[int, str, bool]]] = {}
        for imp, kept in _changed_imports(file.path, new_side, {number for number, _ in file.added()}):
            if kept not in kept_loads:
                kept_loads[kept] = {what for one in kept for _, what, _ in _targets(resolver, file.path, one, module_of, guess=guess)}
            for target, what, guessed in _targets(resolver, file.path, imp, module_of, guess=guess):
                if target != source and what not in before and what not in kept_loads[kept]:
                    by_target.setdefault(target, []).append((imp.line, what, guessed))
        for target, hits in by_target.items():
            line, what, guessed = hits[0]
            others = sorted({number for number, _, _ in hits} - {line})
            also = (f" So do lines {', '.join(map(str, others))}." if len(others) > 1
                    else f" So does line {others[0]}." if others else "")
            rule = next(((a, b, why) for a, b, why in rules if a in (source, "*") and b in (target, "*")), None)
            if rule is not None:
                findings.append(_forbidden(file, source, target, rule, line, what, also, guessed, settings.severity))
            elif baseline is not None and (source, target) not in baseline:
                findings.append(_new_edge(file, source, target, line, what, also, settings.new_edges == "fail"))
    return findings


def _forbidden(file: FileDiff, source: str, target: str, rule: tuple[str, str, str], line: int, what: str, also: str,
               guessed: bool, severity: str) -> Finding:
    a, b, why = rule
    return Finding(
        title=f"`{source}` must not import `{target}`",
        detail=(
            f"`{source}` must not import `{target}` (rule `{a} -> {b}` in {POLICY_PATH})." + (f" Why: {why}" if why else "")
            + f" This line imports {what}, which is in `{target}`.{also}"
        ),
        severity=severity,
        category="maintainability",
        path=file.path,
        line_start=line,
        line_end=line,
        suggestion=(
            f"Reach `{target}` through code that `{source}` may depend on, or change the rule in "
            f"{POLICY_PATH} if `{source}` should now use `{target}` directly."
        ),
        source="check",
        rule=RULE,
        confidence=0.6 if guessed else 0.9,
    )


def _new_edge(file: FileDiff, source: str, target: str, line: int, what: str, also: str, fail: bool) -> Finding:
    return Finding(
        title=f"New dependency: `{source}` now imports `{target}`",
        detail=(
            f"Nothing in `{source}` imports `{target}` on the default branch. This line imports {what}, "
            f"which is in `{target}`.{also} A new link between modules is easy to add and hard to take back."
        ),
        severity="high" if fail else "low",
        category="maintainability",
        path=file.path,
        line_start=line,
        line_end=line,
        suggestion=(
            f"If `{source}` should depend on `{target}`, there's nothing to do. If not, reach it through code "
            f"`{source}` already uses, or add `{source} -> {target}` to `boundaries.forbid` in {POLICY_PATH}."
        ),
        source="check",
        rule=NEW_EDGE_RULE,
        confidence=0.9,
        needs_human=fail,  # whether two modules may depend on each other is a maintainer's call
    )


def _changed_imports(path: str, side: dict[int, str], changed: set[int]) -> list[tuple[Import, tuple[Import, ...]]]:
    """The imports in `side` (line number to text) on a `changed` line, with
    their line numbered as in `side`, each with the names its statement keeps
    on unchanged lines. A `from` statement is split into one import for each
    name: a name is changed when its line is, or the line naming the module
    is. Each stretch of consecutive lines is read whole, so a JS import split
    over lines is still seen when its `from '…'` line is changed."""
    if not changed:
        return []
    found: list[tuple[Import, tuple[Import, ...]]] = []
    for start, lines in _stretches(side):
        # A `(` list running past the stretch is closed, so its names are read; a stray `)` after the
        # last line is harmless when none is open.
        for imp in parse_imports(path, "\n".join(lines) + "\n)"):
            first = start + imp.line - 1
            if not imp.names:
                if first in changed:
                    found.append((replace(imp, line=first), ()))
                continue
            split = [replace(imp, line=start + at - 1, names=(name,), name_lines=(start + at - 1,))
                     for name, at in zip(imp.names, imp.name_lines)]
            touched = [one for one in split if first in changed or one.line in changed]
            kept = tuple(one for one in split if one not in touched)
            found += [(one, kept) for one in touched]
    return found


def _bases(repo_dir: str, ref: str | None, files: list[FileDiff]) -> dict[str, str]:
    """Each file's text before the PR, by its new path: from `ref` in
    `repo_dir`, or its working tree. A file the PR adds has none."""
    before = {f.path: f.old_path or f.path for f in files if f.status != "added"}
    if not ref:
        return {path: text for path, old in before.items() if (text := _read(repo_dir, old)) is not None}
    try:
        texts = _read_at(repo_dir, ref, sorted(set(before.values())))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return {}
    return {path: texts[old] for path, old in before.items() if old in texts}


def _sides(file: FileDiff, base: str | None) -> tuple[dict[int, str], dict[int, str]]:
    """The file's old and new lines (number to text). With `base`, its text
    before the PR, both whole: the lines the diff doesn't show are `base`'s,
    so a statement is read whole even when its `from … import (` line or
    its `)` is outside the diff's context. Without it, or when `base`
    doesn't match the diff (the default branch moved on since the PR's
    base), only the lines the diff shows; then a name added to a list whose
    first line isn't among them is missed."""
    shown_old = {line.old: line.text for hunk in file.hunks for line in hunk.lines if line.old is not None}
    shown_new = file.new_side()
    if base is None:
        return shown_old, shown_new
    old = {number: text.removesuffix("\r") for number, text in enumerate(base.split("\n"), 1)}
    if any(old.get(number) != text.removesuffix("\r") for number, text in shown_old.items()):
        return shown_old, shown_new
    # An unchanged line the diff doesn't show moves by what the hunks above it added and removed.
    shifts = sorted((max((l.old for l in h.lines if l.old is not None), default=h.old_start),
                     sum(l.kind == "+" for l in h.lines) - sum(l.kind == "-" for l in h.lines)) for h in file.hunks)
    new = dict(shown_new)
    at, shift = 0, 0
    for number in sorted(set(old) - set(shown_old)):
        while at < len(shifts) and shifts[at][0] < number:
            shift += shifts[at][1]
            at += 1
        if number + shift in new:
            return shown_old, shown_new  # a line that can't be placed: the diff and `base` disagree
        new[number + shift] = old[number]
    return old, new


def _edges(repo_dir: str, files: list[str], module_of: Callable[[str], str | None], ref: str | None = None) -> set[tuple[str, str]]:
    """Every (module, module it imports) on the default branch: its working
    tree, or `ref` in it."""
    resolver = Resolver(files)
    edges: set[tuple[str, str]] = set()
    with ThreadPoolExecutor(max_workers=8) as pool:
        for at in range(0, len(files), 256):
            batch = files[at:at + 256]
            if ref:
                found = _read_at(repo_dir, ref, batch)
                texts = [found.get(path) for path in batch]
            else:
                texts = list(pool.map(lambda p: _read(repo_dir, p), batch))
            for path, text in zip(batch, texts):
                source = module_of(path)
                if source is None or text is None or _minified(text):
                    continue
                for imp in parse_imports(path, text):
                    for target in resolver.resolve(path, imp):
                        if (module := module_of(target)) and module != source:
                            edges.add((source, module))
    return edges


def _targets(resolver: Resolver, importer: str, imp: Import, module_of: Callable[[str], str | None], *,
             guess: bool) -> list[tuple[str, str, bool]]:
    """(module, what's imported, guessed?) for each thing the import loads
    that lands in a module."""
    resolved = resolver.resolve(importer, imp)
    if resolved or not guess:
        return [(module, f"`{path}`", False) for path in resolved if (module := module_of(path))]
    found = []
    named = f"{'.' * imp.level}{imp.module}"
    [own] = resolver.candidates(importer, replace(imp, names=())) or [[]]
    own_landing = {module_of(path) for path in own} - {None}
    for group, name in zip(resolver.candidates(importer, imp), imp.names or ("",)):
        landing = {module_of(path) for path in group} - {None}
        if len(landing) == 1:
            # `from server import db` lands in `db` because of the name, so it's named too.
            what = (named + "." * bool(imp.module) + name) if name and landing != own_landing else named
            found.append((landing.pop(), f"`{what}`", True))
    return found


def _code_files_at(repo_dir: str, ref: str) -> list[str]:
    """`graph.code_files`, at a commit instead of the working tree."""
    try:
        out = subprocess.run(["git", "-C", repo_dir, "ls-tree", "-r", "-z", "--name-only", ref],
                             capture_output=True, timeout=120, check=True).stdout.decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return []
    found = []
    for path in sorted(filter(None, out.split("\0"))):
        folders = path.split("/")[:-1]
        if path.endswith(PY_EXTENSIONS + JS_EXTENSIONS) and not any(f in SKIP_DIRS or f.startswith(".") for f in folders):
            found.append(path)
            if len(found) >= MAX_FILES:
                break
    return found


def _module(path: str, modules: Modules) -> str | None:
    return next((name for name, globs in modules if matches_any(path, globs)), None)


def _top_module(path: str) -> str | None:
    """With no modules set: the top folder, or two levels under `src/` and
    the like. A file at the root is in none."""
    parts = path.split("/")
    if len(parts) < 2:
        return None
    return "/".join(parts[:2]) if parts[0] in NESTED_ROOTS and len(parts) > 2 else parts[0]


def _stretches(lines: dict[int, str]) -> Iterator[tuple[int, list[str]]]:
    """Runs of consecutive line numbers: (first number, their text)."""
    start, run = 0, []
    for number in sorted(lines):
        if run and number != start + len(run):
            yield start, run
            run = []
        if not run:
            start = number
        run.append(lines[number])
    if run:
        yield start, run
