"""The quality scale declaration: quality_scale.yaml lists every rule honestly and manifest.json claims no more than it supports."""
import json
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
PACKAGE = os.path.join(ROOT, "custom_components", "decky_home_assistant")

# The rule names on https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/ (checked 2026-11-02).
# When the page changes, update this table and quality_scale.yaml together.
TIERS = {
    "bronze": ["action-setup", "appropriate-polling", "brands", "common-modules", "config-flow", "config-flow-test-coverage",
               "dependency-transparency", "docs-actions", "docs-conditions", "docs-high-level-description",
               "docs-installation-instructions", "docs-removal-instructions", "docs-triggers", "entity-event-setup",
               "entity-unique-id", "has-entity-name", "runtime-data", "test-before-configure", "test-before-setup",
               "unique-config-entry"],
    "silver": ["action-exceptions", "config-entry-unloading", "docs-configuration-parameters", "docs-installation-parameters",
               "entity-unavailable", "integration-owner", "log-when-unavailable", "parallel-updates", "reauthentication-flow",
               "test-coverage"],
    "gold": ["devices", "diagnostics", "discovery", "discovery-update-info", "docs-data-update", "docs-examples",
             "docs-known-limitations", "docs-supported-devices", "docs-supported-functions", "docs-troubleshooting",
             "docs-use-cases", "dynamic-devices", "entity-category", "entity-device-class", "entity-disabled-by-default",
             "entity-translations", "exception-translations", "icon-translations", "reconfiguration-flow", "repair-issues",
             "stale-devices"],
    "platinum": ["async-dependency", "inject-websession", "strict-typing"],
}
ORDER = ["bronze", "silver", "gold", "platinum"]
ALL_RULES = {rule: tier for tier, rules in TIERS.items() for rule in rules}
STATUSES = {"done", "todo", "exempt"}

# README headings the documentation rules that are marked done rely on
DOCS_SECTIONS = {
    "docs-installation-instructions": "## Install", "docs-removal-instructions": "## Uninstall",
    "docs-installation-parameters": "## Setup parameters", "docs-configuration-parameters": "## Options",
    "docs-troubleshooting": "## Troubleshooting", "docs-supported-functions": "## What you get",
    "docs-actions": "Actions (Developer tools",
}


def load_rules(path=os.path.join(PACKAGE, "quality_scale.yaml")):
    """The file uses a small, regular subset of YAML (checked below): `rule: status` or a block with status and comment."""
    rules, current, seen_rules_key = {}, None, False
    header = []
    with open(path) as f:
        for number, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                if not seen_rules_key:
                    header.append(line)
                continue
            if line == "rules:":
                seen_rules_key = True
                continue
            rule = re.fullmatch(r"  ([a-z0-9-]+):(?: (done|todo|exempt))?", line)
            field = re.fullmatch(r"    (status|comment): (.+)", line)
            if rule and seen_rules_key:
                current = rule.group(1)
                assert current not in rules, f"line {number}: {current} twice"
                rules[current] = {"status": rule.group(2)} if rule.group(2) else {}
            elif field and current:
                rules[current][field.group(1)] = field.group(2)
            else:
                raise AssertionError(f"line {number}: not in the expected shape: {line!r}")
    return rules, "\n".join(header)


def problems(rules):
    """Everything wrong with a rules table: missing or unknown rules, bad statuses, exemptions without a reason."""
    found = [f"{r}: no entry" for r in sorted(set(ALL_RULES) - set(rules))]
    found += [f"{r}: not a rule" for r in sorted(set(rules) - set(ALL_RULES))]
    for rule, entry in rules.items():
        if entry.get("status") not in STATUSES:
            found.append(f"{rule}: status {entry.get('status')!r}")
        elif entry["status"] == "exempt" and len(entry.get("comment", "")) <= 20:
            found.append(f"{rule}: exempt without a real reason")
    return found


def achieved_tier(rules):
    reached = None
    for tier in ORDER:
        if any(rules[r]["status"] == "todo" for r in TIERS[tier]):
            break
        reached = tier
    return reached


class QualityScaleFileTests(unittest.TestCase):
    def setUp(self):
        self.rules, self.header = load_rules()

    def test_the_rule_table_has_the_documented_counts(self):
        self.assertEqual({tier: len(rules) for tier, rules in TIERS.items()}, {"bronze": 20, "silver": 10, "gold": 21, "platinum": 3})
        self.assertEqual(len(ALL_RULES), 54)

    def test_the_file_has_no_problems(self):
        self.assertEqual(problems(self.rules), [])

    def test_the_checker_catches_each_kind_of_problem(self):
        rules = {r: {"status": "done"} for r in ALL_RULES}
        self.assertEqual(problems(rules), [])
        missing = dict(rules)
        del missing["config-flow"]
        self.assertEqual(problems(missing), ["config-flow: no entry"])
        self.assertEqual(problems({**rules, "config-flo": {"status": "done"}}), ["config-flo: not a rule"])
        self.assertEqual(problems({**rules, "brands": {"status": "maybe"}}), ["brands: status 'maybe'"])
        self.assertEqual(problems({**rules, "brands": {"status": "exempt", "comment": "no"}}), ["brands: exempt without a real reason"])
        self.assertEqual(problems({**rules, "brands": {"status": "exempt"}}), ["brands: exempt without a real reason"])

    def test_the_manifest_declares_exactly_the_tier_the_file_supports(self):
        with open(os.path.join(PACKAGE, "manifest.json")) as f:
            declared = json.load(f)["quality_scale"]
        self.assertEqual(declared, achieved_tier(self.rules))
        for tier in ORDER[: ORDER.index(declared) + 1]:
            self.assertEqual([r for r in TIERS[tier] if self.rules[r]["status"] == "todo"], [], tier)

    def test_the_file_records_when_the_rule_list_was_checked(self):
        self.assertRegex(self.header, r"Checked 20\d\d-\d\d-\d\d")

    def test_documentation_rules_marked_done_still_have_their_section_in_the_readme(self):
        with open(os.path.join(ROOT, "README.md")) as f:
            readme = f.read()
        for rule, marker in DOCS_SECTIONS.items():
            if self.rules[rule]["status"] == "done":
                self.assertIn(marker, readme, f"{rule} is done but README.md has no {marker!r}")

    def test_a_todo_in_a_declared_tier_would_lower_the_achieved_tier(self):
        rules = {r: dict(entry) for r, entry in self.rules.items()}
        rules["parallel-updates"]["status"] = "todo"
        self.assertEqual(achieved_tier(rules), "bronze")
        rules["config-flow"]["status"] = "todo"
        self.assertIsNone(achieved_tier(rules))


if __name__ == "__main__":
    unittest.main()
