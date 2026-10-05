import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "postman_http_options_implementation.py"
spec = importlib.util.spec_from_file_location("postman_http_options_impl", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class ParsingTests(unittest.TestCase):
    def test_normalize_http_version(self):
        self.assertEqual("auto", mod.normalize_http_version("Auto"))
        self.assertEqual("http1", mod.normalize_http_version("HTTP/1.x"))
        self.assertEqual("http2", mod.normalize_http_version("HTTP/2"))
        self.assertIsNone(mod.normalize_http_version("HTTP/3"))

    def test_parse_protocol_profile_behavior(self):
        warnings = []
        spec = mod.parse_protocol_profile_behavior(
            {
                "followRedirects": False,
                "requestVersion": "HTTP/2",
            },
            warnings,
            context_label="request 'A'",
        )
        self.assertEqual(False, spec.follow_redirects)
        self.assertEqual("http2", spec.http_version)
        self.assertEqual([], warnings)


class InheritanceTests(unittest.TestCase):
    def test_walk_inherits_and_overrides(self):
        warnings = []
        targets = []
        collection_root = mod.HttpOptionSpec(follow_redirects=True, http_version=None)
        items = [
            {
                "name": "Folder",
                "protocolProfileBehavior": {"requestVersion": "HTTP/2"},
                "item": [
                    {
                        "name": "Req1",
                        "request": {"method": "GET", "url": "http://example"},
                    },
                    {
                        "name": "Req2",
                        "request": {
                            "method": "GET",
                            "url": "http://example",
                            "protocolProfileBehavior": {"followRedirects": False},
                        },
                    },
                ],
            }
        ]

        mod.walk_postman_requests_with_http_options(
            items=items,
            folder_path=(),
            parent_effective=collection_root,
            targets=targets,
            warnings=warnings,
        )

        self.assertEqual(2, len(targets))
        self.assertEqual(("Folder",), targets[0].folder_path)
        self.assertEqual("Req1", targets[0].request_name)
        self.assertEqual("http2", targets[0].effective_options.http_version)
        self.assertEqual(True, targets[0].effective_options.follow_redirects)

        self.assertEqual("Req2", targets[1].request_name)
        self.assertEqual("http2", targets[1].effective_options.http_version)
        self.assertEqual(False, targets[1].effective_options.follow_redirects)
        self.assertEqual([], warnings)


class PayloadTests(unittest.TestCase):
    def test_build_payload_downgrades_http2_to_http1_transport_and_sets_redirect(self):
        current = {
            "name": "Req",
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "generalSettings": {"followHttpRedirects": True},
                    },
                }
            },
        }
        effective = mod.HttpOptionSpec(follow_redirects=False, http_version="http2")
        payload, desired_type = mod.build_rest_client_update_payload(current, effective)

        self.assertEqual("http10", desired_type)
        self.assertEqual("http10", payload["httpOptions"]["transport"]["type"])
        self.assertEqual(
            False,
            payload["httpOptions"]["transport"]["http10"]["generalSettings"]["followHttpRedirects"],
        )


if __name__ == "__main__":
    unittest.main()

