import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "postman_request_body_implementation.py"
spec = importlib.util.spec_from_file_location("postman_request_body_impl", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class BinaryBodySpecTests(unittest.TestCase):
    def test_build_request_body_spec_detects_file_mode_and_preserves_source(self):
        request_obj = {
            "method": "POST",
            "header": [{"key": "Content-Type", "value": "application/pdf"}],
            "body": {
                "mode": "file",
                "file": {"src": "{{BINARY_FILE_PATH}}"},
            },
        }

        spec_obj = mod.build_request_body_spec(request_obj, form_data_file_mode=mod.FORMDATA_FILE_MODE_VARIABLE)

        self.assertEqual("file", spec_obj.mode)
        self.assertEqual("application/pdf", spec_obj.content_type)
        self.assertTrue(spec_obj.requires_manual_setup)
        self.assertTrue(spec_obj.file_hints)
        self.assertEqual("binary-body-file", spec_obj.file_hints[0]["hintType"])
        self.assertEqual("${BINARY_FILE_PATH}", spec_obj.file_hints[0]["sourcePath"])

    def test_canonical_mode_maps_file_and_binary(self):
        self.assertEqual("binary-file", mod.canonical_mode("file"))
        self.assertEqual("binary-file", mod.canonical_mode("binary"))


class _FakeClient:
    def __init__(self):
        self.get_calls = 0
        self.update_calls = 0

    def descendants_assets(self, _resource_id):
        return {
            "children": [
                {
                    "type": "testSuite",
                    "name": "Test Suite",
                    "children": [
                        {
                            "type": "restClient",
                            "name": "BinaryReq",
                            "id": "/TestAssets/Binary.tst/Test Suite/BinaryReq",
                        }
                    ],
                }
            ]
        }

    def get_rest_client(self, _rest_client_id):
        self.get_calls += 1
        return {}

    def update_rest_client(self, _rest_client_id, _body):
        self.update_calls += 1
        return {}


class BinaryApplyTests(unittest.TestCase):
    def test_apply_request_bodies_marks_manual_and_skips_update(self):
        client = _FakeClient()
        target = mod.RequestBodyTarget(
            folder_path=(),
            request_name="BinaryReq",
            request_method="POST",
            body_spec=mod.RequestBodySpec(
                mode="file",
                text="",
                content_type="application/octet-stream",
                warnings=["binary warning"],
                file_hints=[
                    {
                        "hintType": "binary-body-file",
                        "fieldName": "(request-body)",
                        "variableName": "",
                        "sourcePath": "${BINARY_FILE_PATH}",
                    }
                ],
                requires_manual_setup=True,
                manual_setup_reason="manual setup required",
            ),
        )

        mode_counts, update_failures, warnings, request_report, file_hints = mod.apply_request_bodies(
            client=client,
            tst_id="/TestAssets/Binary.tst",
            request_targets=[target],
        )

        self.assertEqual(1, mode_counts["binary-file"])
        self.assertEqual(0, update_failures)
        self.assertEqual(0, client.get_calls)
        self.assertEqual(0, client.update_calls)
        self.assertEqual("manual-setup-required", request_report[0]["status"])
        self.assertIn("BinaryReq: binary warning", warnings)
        self.assertEqual("binary-body-file", file_hints[0]["hintType"])


if __name__ == "__main__":
    unittest.main()
