"""The rules table in AGENTS.md stays honest: every rule has a reason and a guard that exists, the docs only name files that exist, the
current-state block carries the released version, and no tracked file points into the local-only planning tree.

When this fails: fix the row or reference it names (a renamed test needs its row updated; a new release needs the version in the
"Current state" block of AGENTS.md updated)."""
import ast
import glob
import json
import os
import re
import subprocess
import unittest

from . import ROOT

NONE = "none (convention)"
PATH_START = (".github/", "scripts/", "tests/", "docs/", "custom_components/", "piston_device/", "src/", "assets/")
PATH_EXT = (".py", ".md", ".json", ".yml", ".yaml", ".ts", ".tsx", ".js", ".toml", ".html", ".svg", ".png")
GENERATED = ("out/", "dist/", "node_modules/", ".venv")  # paths a build or a local setup creates
NO_PLANNING_TREE_IN = {"tests/test_rules_freshness.py"}  # names the forbidden text on purpose
PLANNING_TREE = "sw" + "e/"  # assembled so this file does not contain it


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def table_rows(markdown: str) -> list:
    """(rule, why, guard) for each row of the Rules table: the table whose header starts with Rule | Why | Guard."""
    rows, in_table = [], False
    for line in markdown.split("\n"):
        cells = [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else None
        if cells is None:
            in_table = False
        elif cells[:3] == ["Rule", "Why", "Guard"]:
            in_table = True
        elif in_table and not set("".join(cells)) <= set("-: "):
            rows.append(tuple(cells))
    return rows


def guard_items(cell: str) -> list:
    return [item.strip().strip("`") for item in cell.split(";")]


def resolves(item: str, root: str = ROOT) -> str | None:
    """None if the guard exists, otherwise what is wrong. `path`, `path::Class` or `path::Class::test`; read with ast, never imported."""
    path, _, rest = item.partition("::")
    if not os.path.isfile(os.path.join(root, path)):
        return f"{path} does not exist"
    if not rest:
        return None
    with open(os.path.join(root, path), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    cls, _, test = rest.partition("::")
    node = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls), None)
    if node is None:
        return f"{path} has no class {cls}"
    if test and not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == test for n in node.body):
        return f"{path}::{cls} has no test {test}"
    return None


def path_references(markdown: str) -> list:
    found = []
    for token in re.findall(r"`([^`\n]+)`", markdown):
        if re.search(r"[\s<>*{}~$()\[\]|=:,]|\.\.\.", token) or token.startswith(("/", "-", ".venv")):
            continue
        if token.startswith(PATH_START) and (token.endswith(PATH_EXT) or token.endswith("/")) and not token.startswith(GENERATED):
            found.append(token)
    return found


def state_block(markdown: str) -> list:
    lines = markdown.split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip().lower() == "## current state")
    block = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if line.strip():
            block.append(line)
    return block


def text_files() -> list:
    out = subprocess.check_output(["git", "ls-files", "-z", "-c", "-o", "--exclude-standard"], cwd=ROOT).decode()
    skip = (".png", ".jpg", ".ico", ".gz", ".zip", ".map", ".svg", ".lock")
    return [f for f in out.split("\0") if f and not f.endswith(skip) and os.path.basename(f) != "pnpm-lock.yaml" and os.path.isfile(os.path.join(ROOT, f))]


class RulesTableTests(unittest.TestCase):
    def test_the_table_has_rules_and_every_row_is_complete(self):
        rows = table_rows(read("AGENTS.md"))
        self.assertGreaterEqual(len(rows), 10, "the Rules table in AGENTS.md is missing or unparsable")
        for row in rows:
            self.assertEqual(len(row), 3, f"row needs Rule, Why and Guard: {row}")
            self.assertTrue(all(row), f"empty cell in row: {row[0][:60]}")

    def test_every_guard_exists_or_says_none(self):
        problems = []
        for rule, _why, guard in table_rows(read("AGENTS.md")):
            if guard == NONE:
                continue
            for item in guard_items(guard):
                problem = resolves(item)
                if problem:
                    problems.append(f"{rule[:50]}...: {problem}")
        self.assertEqual(problems, [], f"A guard is a test id (path::Class::test), a script path, or exactly '{NONE}'.")

    def test_the_checks_notice_a_bad_guard(self):
        self.assertIsNone(resolves("tests/test_rules_freshness.py::RulesTableTests::test_every_guard_exists_or_says_none"))
        self.assertIsNone(resolves("scripts/check_public.py"))
        self.assertIn("no class", resolves("tests/test_rules_freshness.py::NoSuchClass"))
        self.assertIn("no test", resolves("tests/test_rules_freshness.py::RulesTableTests::test_nope"))
        self.assertIn("does not exist", resolves("tests/nope.py"))


class DocsFreshnessTests(unittest.TestCase):
    def test_the_docs_only_name_files_that_exist(self):
        problems = []
        for rel in ["AGENTS.md"] + sorted(glob.glob("docs/*.md", root_dir=ROOT)):
            for token in path_references(read(rel)):
                if not os.path.exists(os.path.join(ROOT, token)):
                    problems.append(f"{rel} names {token}")
        self.assertEqual(problems, [], "A doc names a file that no longer exists: update the doc.")

    def test_path_detection(self):
        self.assertEqual(path_references("`scripts/a.py` `out/x.zip` `<name>/x.py` `tests/*.py` `~/homebrew/x.json` `docs/` `plain`"),
                         ["scripts/a.py", "docs/"])

    def test_the_current_state_block_is_short_and_carries_the_released_version(self):
        block = state_block(read("AGENTS.md"))
        self.assertLessEqual(len(block), 5, "keep the Current state block to 5 lines; history belongs elsewhere")
        version = json.loads(read("custom_components/piston/manifest.json"))["version"]
        self.assertIn(version, "\n".join(block), f"update the Current state block of AGENTS.md to version {version}")

    def test_no_tracked_file_points_into_the_local_planning_tree(self):
        offenders = [rel for rel in text_files() if rel not in NO_PLANNING_TREE_IN and PLANNING_TREE in read(rel)]
        self.assertEqual(offenders, [], "The planning tree is local only; do not mention its paths in files that are published.")
