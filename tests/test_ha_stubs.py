import unittest


class HaStubTests(unittest.TestCase):
    def test_integration_imports_without_home_assistant(self):
        import custom_components.piston as integ

        self.assertTrue(hasattr(integ, "async_setup_entry"))
        self.assertEqual(integ.DOMAIN if hasattr(integ, "DOMAIN") else "piston", "piston")
