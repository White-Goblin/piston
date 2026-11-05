"""The repository stays eligible for the HACS default list and a release cannot go out inconsistent."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import check_release  # noqa: E402

# keys hacs.json may carry, from https://www.hacs.xyz/docs/publish/start/
HACS_JSON_KEYS = {"name", "content_in_root", "zip_release", "filename", "hide_default_branch", "country", "homeassistant", "hacs", "persistent_directory"}
MANIFEST_KEYS = ("domain", "name", "version", "documentation", "issue_tracker", "codeowners")


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class HacsDefaultListTests(unittest.TestCase):
    def test_hacs_json_has_a_name_and_only_documented_keys(self):
        hacs = json.loads(read("hacs.json"))
        self.assertTrue(hacs.get("name"))
        self.assertEqual(set(hacs) - HACS_JSON_KEYS, set())

    def test_the_manifest_defines_what_hacs_requires(self):
        manifest = json.loads(read("custom_components", "piston", "manifest.json"))
        for key in MANIFEST_KEYS:
            self.assertTrue(manifest.get(key), key)

    def test_the_hacs_workflow_runs_the_integration_check_without_ignores(self):
        workflow = read(".github", "workflows", "hacs.yml")
        self.assertIn("uses: hacs/action@", workflow)
        self.assertRegex(workflow, r"category:\s*integration")
        self.assertNotRegex(workflow, r"(?m)^\s*ignore:", "the default list requires the HACS Action to pass without ignores")

    def test_the_hacs_action_is_pinned_to_a_commit(self):
        self.assertRegex(read(".github", "workflows", "hacs.yml"), r"hacs/action@[0-9a-f]{40}\b")

    def test_hassfest_runs_on_every_push_and_pull_request(self):
        workflow = read(".github", "workflows", "hassfest.yml")
        self.assertIn("home-assistant/actions/hassfest", workflow)
        self.assertRegex(workflow, r"(?m)^\s*push:")
        self.assertRegex(workflow, r"(?m)^\s*pull_request:")

    def test_both_workflows_also_run_on_a_schedule_so_a_broken_check_is_noticed(self):
        for name in ("hacs.yml", "hassfest.yml"):
            self.assertIn("schedule:", read(".github", "workflows", name), name)

    def test_the_release_workflow_publishes_only_after_the_guards(self):
        workflow = read(".github", "workflows", "release.yml")
        guard_tag, guard_checks, create = (workflow.index(s) for s in ("check_release.py", "HACS validation", "gh release create"))
        self.assertLess(guard_tag, create)
        self.assertLess(guard_checks, create)
        self.assertIn("Hassfest", workflow)
        self.assertIn("actions: read", workflow)

    def test_the_real_home_assistant_suite_runs_in_ci_with_a_coverage_floor_but_does_not_gate_releases(self):
        workflow = read(".github", "workflows", "tests.yml")
        self.assertIn("ha-tests:", workflow)
        self.assertIn("tests/ha/requirements.txt", workflow)
        self.assertRegex(workflow, r"--cov-fail-under=\d+")
        self.assertNotIn("pytest", read(".github", "workflows", "release.yml"))

    def test_the_brand_icon_exists(self):
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "custom_components", "piston", "brand", "icon.png")))

    def test_the_releasing_doc_lists_what_keeps_the_default_listing(self):
        doc = read("docs", "releasing.md")
        for term in ("archive", "without ignores", "check_release.py"):
            self.assertIn(term, doc, term)


class ReleaseConsistencyTests(unittest.TestCase):
    def tree(self, manifest="0.7.0", package="0.7.0", changelog="## 0.7.0\n\nNotes.\n"):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        os.makedirs(os.path.join(root, "custom_components", "piston"))
        with open(os.path.join(root, "custom_components", "piston", "manifest.json"), "w") as f:
            json.dump({"version": manifest}, f)
        with open(os.path.join(root, "package.json"), "w") as f:
            json.dump({"version": package}, f)
        with open(os.path.join(root, "CHANGELOG.md"), "w") as f:
            f.write(changelog)
        return root

    def test_a_consistent_tag_has_no_problems(self):
        self.assertEqual(check_release.problems("v0.7.0", self.tree()), [])

    def test_the_tag_must_match_the_manifest_and_package_versions(self):
        found = check_release.problems("v0.8.0", self.tree())
        self.assertEqual(len(found), 3)           # manifest, package.json and the missing changelog section
        self.assertTrue(any("manifest.json" in line for line in found))
        self.assertTrue(any("package.json" in line for line in found))

    def test_a_missing_changelog_section_is_reported(self):
        self.assertEqual(len(check_release.problems("v0.7.0", self.tree(changelog="## 0.6.0\n\nOld.\n"))), 1)

    def test_a_tag_without_the_v_prefix_is_reported(self):
        self.assertTrue(any("'v'" in line for line in check_release.problems("0.7.0", self.tree())))

    def test_the_real_repository_is_consistent_for_its_own_version(self):
        version = json.loads(read("package.json"))["version"]
        self.assertEqual(check_release.problems(f"v{version}"), [])


if __name__ == "__main__":
    unittest.main()
