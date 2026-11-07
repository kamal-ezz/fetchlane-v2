import unittest

from scripts.smoke_binary import check_impersonation_targets


class ImpersonationSmokeTests(unittest.TestCase):
    def test_available_native_backend_passes(self):
        check_impersonation_targets(
            "Client OS Source\nEdge - curl_cffi (unavailable)\nChrome-131 Windows-10 curl_cffi\n")

    def test_missing_or_unavailable_backend_fails(self):
        for output in ("", "Chrome - curl_cffi (unavailable)", "Client OS Source"):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                check_impersonation_targets(output)
