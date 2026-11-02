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
