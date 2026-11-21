from stratum_open.diff import parse_diff
from stratum_open.policy import Policy, parse_policy
from stratum_open.repo.boundaries import boundary_findings

POLICY = parse_policy(
    """
boundaries:
  modules:
    api: ["server/api/**", "web/api/**"]
    db: ["server/db/**", "web/db/**"]
    core: ["server/core/**"]
  forbid: ["api -> db"]
"""
)

CHECKOUT = {
    "server/__init__.py": "",
    "server/api/__init__.py": "",
    "server/api/views.py": "import os\n",
    "server/db/__init__.py": "",
    "server/db/models.py": "",
    "server/db/session.py": "",
    "server/core/__init__.py": "",
    "server/core/config.py": "",
    "web/api/client.ts": "",
    "web/db/store.ts": "",
}


def write(root, files):
    for path, text in files.items():
        full = root / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding="utf-8")
    return str(root)


def change(path, *lines, new=False):
    """A diff of `path`: lines starting "+" are added, " " unchanged."""
    kept = sum(1 for line in lines if line.startswith(" "))
    header = [f"diff --git a/{path} b/{path}"]
    header += ["new file mode 100644", "--- /dev/null"] if new else [f"--- a/{path}"]
    header += [f"+++ b/{path}", f"@@ -{1 if kept else 0},{kept} +1,{len(lines)} @@"]
    [parsed] = parse_diff("\n".join(header + list(lines)) + "\n")
    return parsed


def test_forbidden_import_is_a_finding_and_an_allowed_one_is_not(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    diff = change("server/api/views.py", " import os", "+from server.db.models import User", "+from server.core import config")

    [finding] = boundary_findings([diff], POLICY, repo)
    assert (finding.path, finding.line_start, finding.line_end) == ("server/api/views.py", 2, 2)
    assert (finding.severity, finding.category, finding.rule, finding.source) == ("medium", "maintainability", "boundaries/forbidden", "check")
    assert finding.title == "`api` must not import `db`"
    assert finding.detail == (
        "`api` must not import `db` (rule `api -> db` in .stratum/policy.yml). "
        "This line imports `server/db/models.py`, which is in `db`."
    )
    assert finding.confidence == 0.9


def test_a_module_can_be_named_by_its_folder(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    diff = change("server/api/views.py", " import os", "+from server.db.models import User")
    policy = parse_policy('boundaries:\n  modules: {api: ["server/api/"], db: ["server/db/"]}\n  forbid: ["api -> db"]\n')
    [finding] = boundary_findings([diff], policy, repo)
    assert finding.title == "`api` must not import `db`"


def test_relative_and_multiline_imports(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    python = change("server/api/jobs.py", "+import os", "+from ..db import session, models", new=True)
    js = change("web/api/client.ts", " import {", "   get,", "+} from '../db/store'")

    findings = boundary_findings([python, js], POLICY, repo)
    assert [(f.path, f.line_start) for f in findings] == [("server/api/jobs.py", 2), ("web/api/client.ts", 3)]


def test_an_import_continued_with_a_backslash(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    names = change("server/api/jobs.py", "+import os", "+from server import core, \\", "+    db", new=True)
    modules = change("server/api/tasks.py", "+import os, \\", "+    server.db.models", new=True)
    assert [(f.path, f.line_start) for f in boundary_findings([names, modules], POLICY, repo)] == [
        ("server/api/jobs.py", 3), ("server/api/tasks.py", 2)]


def test_a_name_added_to_a_from_import_over_several_lines(tmp_path):
    """Black and isort put a long import's names one to a line: adding one adds
    only its own line, and the finding goes there."""
    repo = write(tmp_path, CHECKOUT)
    head = "diff --git a/server/api/views.py b/server/api/views.py\n--- a/server/api/views.py\n+++ b/server/api/views.py\n"
    cases = {
        "@@ -1,4 +1,5 @@\n from server import (\n     core,\n+    db,\n )\n x = 1\n": [3],
        "@@ -1,2 +1,3 @@\n-from server import core\n+from server import core, \\\n+    db\n x = 1\n": [2],
        "@@ -1,3 +1,3 @@\n from server import core, \\\n-    api\n+    db\n x = 1\n": [2],
        # The module changed, so every name now loads something new.
        "@@ -1,3 +1,3 @@\n-from server.core import (\n+from server.db import (\n     models,\n )\n": [2],
        # The names already there loaded `server.db`, so the new one adds nothing.
        "@@ -1,3 +1,4 @@\n from server.db import (\n     Base,\n+    User,\n )\n": [],
        "@@ -1,3 +1,3 @@\n from server import (\n-    db,\n+    db as database,\n )\n": [],
    }
    for hunk, lines in cases.items():
        files = parse_diff(head + hunk)
        assert [f.line_start for f in boundary_findings(files, POLICY, repo)] == lines, hunk
        assert [f.line_start for f in boundary_findings(files, POLICY, None)] == lines, hunk
    # Without a checkout, the name says what's imported when it, not the module, lands in `db`.
    for module, what in (("server", "`server.db`"), ("..", "`..db`")):
        files = parse_diff(head + f"@@ -1,3 +1,4 @@\n from {module} import (\n     core,\n+    db,\n )\n")
        [finding] = boundary_findings(files, POLICY, None)
        assert f"This line imports {what}, which is in `db`." in finding.detail


LONG_LIST = "from server import (\n    core,\n    a,\n    b,\n    c,\n    d,\n    e,\n    api,\n)\nx = 1\n"
VIEWS = "diff --git a/server/api/views.py b/server/api/views.py\n--- a/server/api/views.py\n+++ b/server/api/views.py\n"
NO_CLOSE = "@@ -1,5 +1,6 @@\n from server import (\n     core,\n+    db,\n     a,\n     b,\n     c,\n"  # `)` not shown
NO_FROM = "@@ -5,5 +5,6 @@\n     c,\n     d,\n     e,\n+    db,\n     api,\n )\n"  # `from … import (` not shown


def test_a_name_added_to_a_long_list_is_read_with_the_rest_of_the_file(tmp_path):
    """The diff's context rarely shows both ends of a list black or isort
    wrote one name to a line; the checkout fills in the lines it doesn't."""
    repo = write(tmp_path, {**CHECKOUT, "server/api/views.py": LONG_LIST})
    assert [f.line_start for f in boundary_findings(parse_diff(VIEWS + NO_CLOSE), POLICY, repo)] == [3]
    assert [f.line_start for f in boundary_findings(parse_diff(VIEWS + NO_FROM), POLICY, repo)] == [8]
    # Moved out of the list into a statement of its own: the removal is seen too, so it's no new import.
    moved = LONG_LIST.replace("    e,\n", "    db,\n    e,\n") + "y = 2\n"
    write(tmp_path, {"server/api/views.py": moved})
    hunks = "@@ -5,4 +5,3 @@\n     c,\n     d,\n-    db,\n     e,\n@@ -12,1 +11,2 @@\n y = 2\n+from server import db\n"
    assert boundary_findings(parse_diff(VIEWS + hunks), POLICY, repo) == []

    # Without a checkout, a list still open where the diff stops is read to its end, but one whose
    # `from` line isn't shown can't be.
    assert [f.line_start for f in boundary_findings(parse_diff(VIEWS + NO_CLOSE), POLICY, None)] == [3]
    assert boundary_findings(parse_diff(VIEWS + NO_FROM), POLICY, None) == []


def test_a_checkout_that_does_not_match_the_diff_is_not_used_to_fill_it_in(tmp_path):
    # The default branch moved on since the PR's base: a line was added at the top.
    repo = write(tmp_path, {**CHECKOUT, "server/api/views.py": "import os\n" + LONG_LIST})
    assert [f.line_start for f in boundary_findings(parse_diff(VIEWS + NO_CLOSE), POLICY, repo)] == [3]
    assert boundary_findings(parse_diff(VIEWS + NO_FROM), POLICY, repo) == []


def test_a_local_review_fills_in_the_file_from_its_base(tmp_path):
    from test_repo_history import Repo

    repo = Repo(tmp_path)
    repo.commit({**CHECKOUT, "server/api/views.py": LONG_LIST}, day=1)
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"server/api/views.py": LONG_LIST.replace("    api,\n", "    db,\n    api,\n")}, day=2)
    assert [f.line_start for f in boundary_findings(parse_diff(VIEWS + NO_FROM), POLICY, repo.path, ref=base)] == [8]


def test_names_kept_in_one_statement_do_not_hide_an_import_in_another(tmp_path):
    repo = write(tmp_path, {**CHECKOUT, "server/api/views.py": "from server.db import (\n    Base,\n)\nx = 1\n"})
    hunk = "@@ -1,4 +1,6 @@\n from server.db import (\n     Base,\n+    User,\n )\n x = 1\n+from server.db import Other\n"
    for checkout in (repo, None):
        # `User` loads what `Base` already did; line 6 is a new statement, so it's checked.
        assert [f.line_start for f in boundary_findings(parse_diff(VIEWS + hunk), POLICY, checkout)] == [6]


def test_imports_the_pr_did_not_add_are_left_alone(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    diff = change("server/api/views.py", " from server.db.models import User", "+x = 1")

    assert boundary_findings([diff], POLICY, repo) == []


def test_imports_of_what_the_checkout_does_not_have(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    added = change("server/db/fresh.py", "+x = 1", new=True)
    diff = change(
        "server/api/views.py", " import os",
        "+from server.db.fresh import x",  # added by this PR: found
        "+from server.db.missing import y",  # nowhere: no guessing when there's a checkout
        "+import db",  # not on any root
    )

    findings = boundary_findings([added, diff], POLICY, repo)
    assert [(f.line_start, f.detail.split("This line imports ")[1]) for f in findings] == [
        (2, "`server/db/fresh.py`, which is in `db`."),
    ]


def test_without_a_checkout_the_import_text_is_used(tmp_path):
    diff = change(
        "server/api/views.py", " import os",
        "+from server.db.models import User",
        "+from db import session",  # as run from server/
        "+import requests",
        "+from server.core import config",
    )
    js = change("web/api/client.ts", "+import { save } from '../db/store'", new=True)

    findings = boundary_findings([diff, js], POLICY, None)
    assert [(f.path, f.line_start, f.confidence) for f in findings] == [("server/api/views.py", 2, 0.6), ("web/api/client.ts", 1, 0.6)]
    assert findings[0].detail.endswith("This line imports `server.db.models`, which is in `db`. So does line 3.")  # one per module


def test_only_files_in_a_module_with_a_rule_are_checked(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    from_db = change("server/db/models.py", "+from server.api import views", new=True)  # no `db -> api` rule
    from_core = change("server/core/config.py", "+from server.db import models", new=True)  # core has no rule
    outside = change("scripts/seed.py", "+from server.db import models", new=True)
    within = change("server/api/views.py", " import os", "+from server import api")

    assert boundary_findings([from_db, from_core, outside, within], POLICY, repo) == []
    assert boundary_findings([within], Policy(), "/no/such/checkout") == []


def policy(extra):
    return parse_policy("""
boundaries:
  modules:
    api: ["server/api/**"]
    db: ["server/db/**"]
    core: ["server/core/**"]
""" + extra)


def test_either_side_of_a_rule_may_be_any_module(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    from_core = change("server/core/config.py", "+from server.db import models", new=True)
    from_db = change("server/db/session.py", "+from server.db import models", new=True)  # a module may import itself
    into_db = policy('  forbid: ["* -> db: Only the repository layer talks to the database."]\n  severity: high\n')
    [finding] = boundary_findings([from_core, from_db], into_db, repo)
    assert (finding.path, finding.severity) == ("server/core/config.py", "high")
    assert finding.detail.startswith("`core` must not import `db` (rule `* -> db` in .stratum/policy.yml). "
                                     "Why: Only the repository layer talks to the database. This line imports")

    from_api = change("server/api/views.py", " import os", "+from server.core import config", "+from server.db import models")
    assert [f.title for f in boundary_findings([from_api], policy('  forbid: ["api -> *"]\n'), repo)] == [
        "`api` must not import `core`", "`api` must not import `db`"]


def test_an_import_the_pr_only_reformats_is_not_new(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    diff = change("server/api/views.py", " import os", "-from server.db.models import User,Team", "+from server.db.models import Team, User")
    assert boundary_findings([diff], POLICY, repo) == []


def test_new_edges_are_measured_against_the_default_branch(tmp_path):
    repo = write(tmp_path, {**CHECKOUT, "server/api/views.py": "from server.core import config\n"})
    diff = change("server/api/views.py", " from server.core import config", "+from server.core.config import X",
                  "+from server.db import models", "+from server.db import session")
    [warn] = boundary_findings([diff], policy("  new_edges: warn\n"), repo)
    assert (warn.rule, warn.severity, warn.line_start, warn.needs_human) == ("boundaries/new-edge", "low", 3, False)
    assert warn.title == "New dependency: `api` now imports `db`" and "So does line 4." in warn.detail  # api -> core was there already
    [fail] = boundary_findings([diff], policy("  new_edges: fail\n"), repo)
    assert (fail.severity, fail.needs_human) == ("high", True)  # a maintainer's call, not the author's to fix
    assert boundary_findings([diff], policy("  new_edges: warn\n"), None) == []  # nothing to compare with


def test_without_modules_each_top_folder_is_one(tmp_path):
    repo = write(tmp_path, {"src/app/__init__.py": "", "src/app/main.py": "", "src/billing/__init__.py": "",
                            "src/billing/pay.py": "", "tools/run.py": "from src.app import main\n"})
    diff = change("src/app/main.py", "+from src.billing import pay", new=True)
    [finding] = boundary_findings([diff], parse_policy("boundaries:\n  new_edges: warn\n"), repo)
    assert finding.title == "New dependency: `src/app` now imports `src/billing`"


def test_rules_can_be_written_three_ways():
    parsed = parse_policy("""
boundaries:
  modules: {api: ["a/**"], db: ["d/**"]}
  forbid:
    - api -> db: Go through the service layer.
    - "* -> db"
    - {from: api, to: "*", reason: Keep api thin.}
  new_edges: off
""")
    assert parsed.errors == ()
    assert parsed.boundaries.forbid == ("api -> db: Go through the service layer.", "* -> db", "api -> *: Keep api thin.")
    assert parsed.boundaries.new_edges == "off"  # unquoted, YAML reads it as false
    assert parse_policy("boundaries:\n  forbid: [{to: db}]\n").errors == (
        'boundaries.forbid entry "to: db" must look like "a -> b", or "a -> b: why".',)
    assert parse_policy("boundaries:\n  forbid: [[api, db]]\n").errors == (
        'boundaries.forbid must be a list of rules like "a -> b" or "a -> b: why".',)


def test_an_import_changed_to_another_module_is_checked(tmp_path):
    repo = write(tmp_path, CHECKOUT)
    diff = change("server/api/views.py", " import os", "-from server import core", "+from server import db")
    [finding] = boundary_findings([diff], POLICY, repo)  # same `from server`, but it now loads db
    assert finding.title == "`api` must not import `db`"


def test_a_local_review_reads_the_baseline_at_its_base(tmp_path):
    from test_repo_history import Repo

    repo = Repo(tmp_path)
    repo.commit({**CHECKOUT, "server/api/views.py": "from server.core import config\n"}, day=1)
    base = repo.git("rev-parse", "HEAD")
    repo.commit({"server/api/views.py": "from server.core import config\nfrom server.db import models\n"}, day=2)
    diff = change("server/api/views.py", " from server.core import config", "+from server.db import models")
    warn = policy("  new_edges: warn\n")
    assert [f.rule for f in boundary_findings([diff], warn, repo.path, ref=base)] == ["boundaries/new-edge"]
    assert boundary_findings([diff], warn, repo.path) == []  # the working tree already has the change, so it looks old


def test_no_baseline_when_git_cannot_list_the_ref(tmp_path):
    from test_repo_history import Repo

    repo = Repo(tmp_path)
    repo.commit({**CHECKOUT, "server/api/views.py": "from server.db import models\n"}, day=1)  # api -> db already
    added = change("server/db/fresh.py", "+x = 1", new=True)
    diff = change("server/api/views.py", " from server.db import models", "+from server.db.fresh import x")
    warn = policy("  new_edges: warn\n")
    assert boundary_findings([added, diff], warn, repo.path, ref="HEAD") == []  # not new: api imported db already
    assert boundary_findings([added, diff], warn, repo.path, ref="no-such-ref") == []  # nothing listed, so nothing to call new
