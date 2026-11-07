import os
import sys
import unittest

from . import ROOT

sys.path.insert(0, os.path.join(ROOT, "scripts"))
import check_public  # noqa: E402


class PublicTreeTests(unittest.TestCase):
    def test_no_personal_or_network_specific_data_in_the_tree(self):
        # With a local .public-check.local this also checks the maintainer's own identifiers
        self.assertEqual(check_public.scan(), [])

    def test_the_checker_catches_what_it_should(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.txt")
            with open(path, "w") as f:
                # Built from pieces so this file does not contain the very data the checker looks for
                bad_mail, bad_mac, bad_ip = "someone" + "@example.org", ":".join(["90", "82", "c3", "00", "00", "01"]), ".".join(["192", "168", "50", "7"])
                f.write(f"mail me at {bad_mail}\nmac {bad_mac}\nhost {bad_ip}\nfine 192.168.1.5 aa:bb:cc:dd:ee:ff noreply@anthropic.com\n")
            old = check_public.ROOT
            check_public.ROOT = d
            try:
                found = check_public.scan(["x.txt"])
            finally:
                check_public.ROOT = old
        self.assertEqual([x.split(": ", 1)[1] for x in found], ["email address", "MAC address", "private IP address " + bad_ip])

    def test_a_retired_project_name_is_flagged_except_in_the_changelog(self):
        import tempfile
        old = "dec" + "ky"
        names = [old + "_home_" + "assistant", old + "_" + "ha", old.capitalize() + " Home " + "Assistant"]
        with tempfile.TemporaryDirectory() as d:
            for name in ("x.md", "CHANGELOG.md"):
                with open(os.path.join(d, name), "w") as f:
                    f.write("\n".join(f"was {n}" for n in names) + f"\nfine: {old}_have and {old}-ha\n")
            saved = check_public.ROOT
            check_public.ROOT = d
            try:
                found = check_public.scan(["x.md", "CHANGELOG.md"])
            finally:
                check_public.ROOT = saved
        self.assertEqual(found, [f"x.md:{i}: retired project name" for i in (1, 2, 3)])

    def test_the_checker_script_does_not_contain_the_names_it_looks_for(self):
        with open(os.path.join(ROOT, "scripts", "check_public.py"), encoding="utf-8") as f:
            text = f.read()
        self.assertEqual([p.pattern for p in check_public.RETIRED if p.search(text)], [])
