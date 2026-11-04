import os
import subprocess
import sys
import tempfile
import unittest

from scripts import release_notes

SAMPLE = "# Changelog\n\n## 0.5.0 (unreleased)\n\nNew.\n\n### Changed\n- a\n\n## 0.4.2\n\nOld.\n\n## 0.4.1\n\nOldest.\n"


class SectionTests(unittest.TestCase):
    def test_finds_a_middle_section_and_stops_at_the_next_heading(self):
        self.assertEqual(release_notes.section(SAMPLE, "v0.4.2"), "Old.")

    def test_keeps_sub_headings_and_allows_a_title_suffix(self):
        self.assertEqual(release_notes.section(SAMPLE, "0.5.0"), "New.\n\n### Changed\n- a")

    def test_last_section_runs_to_the_end(self):
        self.assertEqual(release_notes.section(SAMPLE, "0.4.1"), "Oldest.")

    def test_missing_version_and_prefix_versions_are_not_found(self):
        self.assertIsNone(release_notes.section(SAMPLE, "0.4.3"))
        self.assertIsNone(release_notes.section(SAMPLE, "0.4"))


class CommandLineTests(unittest.TestCase):
    def run_script(self, *args):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return subprocess.run([sys.executable, os.path.join(root, "scripts", "release_notes.py"), *args],
                              capture_output=True, text=True)

    def test_prints_the_real_changelog_section(self):
        result = self.run_script("v0.4.2")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Plugin update ready", result.stdout)

    def test_exits_non_zero_for_an_unknown_version(self):
        result = self.run_script("v9.9.9")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no section", result.stderr)

    def test_reads_another_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(SAMPLE)
        try:
            self.assertEqual(self.run_script("0.4.1", f.name).stdout.strip(), "Oldest.")
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
