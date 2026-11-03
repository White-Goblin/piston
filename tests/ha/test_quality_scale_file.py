"""The declaration parsed by a real YAML parser: it is valid YAML and says what the small test parser says."""
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from test_quality_scale import ALL_RULES, PACKAGE, load_rules  # noqa: E402


def test_the_file_is_valid_yaml_with_the_same_content():
    with open(os.path.join(PACKAGE, "quality_scale.yaml")) as f:
        parsed = yaml.safe_load(f)["rules"]
    simple, _ = load_rules()
    assert set(parsed) == set(ALL_RULES)
    for rule, entry in simple.items():
        value = parsed[rule]
        status = value if isinstance(value, str) else value["status"]
        assert status == entry["status"], rule
        if status == "exempt":
            assert value["comment"] == entry["comment"], rule
