import importlib.util
import io
import sys
import tempfile
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import unittest
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "postman_to_soatest.py"
spec = importlib.util.spec_from_file_location("postman_to_soatest", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class ResolveTestFileNameTests(unittest.TestCase):
    def test_missing_requested_name_fails_with_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            mod.resolve_test_file_name(
                api_base="http://localhost:9080/soavirt/api/v6",
                parent_id="/TestAssets",
                timeout_sec=30,
                auth_header=None,
                requested_name=None,
            )
        self.assertIn("Provide --test-file-name", str(ctx.exception))

    def test_unique_name_returns_as_is(self):
        with mock.patch.object(mod, "soavirt_tst_exists", return_value=False):
            value = mod.resolve_test_file_name(
                api_base="http://localhost:9080/soavirt/api/v6",
                parent_id="/TestAssets",
                timeout_sec=30,
                auth_header=None,
                requested_name="myNewFile",
            )
        self.assertEqual("myNewFile", value)

    def test_collision_auto_suffixes_to_next_available(self):
        with mock.patch.object(mod, "soavirt_tst_exists", side_effect=[True, False]):
            value = mod.resolve_test_file_name(
                api_base="http://localhost:9080/soavirt/api/v6",
                parent_id="/TestAssets",
                timeout_sec=30,
                auth_header=None,
                requested_name="existingName",
            )
        self.assertEqual("existingName_2", value)


class MainPreflightTests(unittest.TestCase):
    def test_missing_environment_argument_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            collection_path = tmp_path / "collection.postman_collection.json"
            workspace_root = tmp_path / "workspace"
            workspace_root.mkdir()
            (workspace_root / "TestAssets").mkdir()
            collection_path.write_text('{"info": {"name": "Demo"}, "item": [{"name": "Req", "request": {"method": "GET", "url": "{{BASE_URL}}/ping"}}]}', encoding="utf-8")

            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                rc = mod.main([
                    str(collection_path),
                    "--test-file-name",
                    "demoOut",
                    "--workspace-root",
                    str(workspace_root),
                ])

            self.assertEqual(1, rc)
            self.assertIn("Postman environment file is required for this conversion", stderr_capture.getvalue())
            self.assertNotIn("=== Step 1", stdout_capture.getvalue())

    def test_missing_output_name_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            collection_path = tmp_path / "collection.postman_collection.json"
            environment_path = tmp_path / "environment.postman_environment.json"
            workspace_root = tmp_path / "workspace"
            workspace_root.mkdir()
            (workspace_root / "TestAssets").mkdir()
            collection_path.write_text('{"info": {"name": "Demo"}, "item": []}', encoding="utf-8")
            environment_path.write_text('{"name": "env", "values": []}', encoding="utf-8")

            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                rc = mod.main([
                    str(collection_path),
                    "--postman-environment",
                    str(environment_path),
                    "--workspace-root",
                    str(workspace_root),
                ])

            self.assertEqual(1, rc)
            self.assertIn("Output .tst name is required", stderr_capture.getvalue())
            self.assertNotIn("=== Step 1", stdout_capture.getvalue())

    def test_missing_workspace_root_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            collection_path = tmp_path / "collection.postman_collection.json"
            environment_path = tmp_path / "environment.postman_environment.json"
            collection_path.write_text('{"info": {"name": "Demo"}, "item": []}', encoding="utf-8")
            environment_path.write_text('{"name": "env", "values": []}', encoding="utf-8")

            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()
            with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                rc = mod.main([
                    str(collection_path),
                    "--postman-environment",
                    str(environment_path),
                    "--test-file-name",
                    "demoOut",
                ])

            self.assertEqual(1, rc)
            self.assertIn("Workspace root is required", stderr_capture.getvalue())
            self.assertNotIn("=== Step 1", stdout_capture.getvalue())

class ServerReachabilityTests(unittest.TestCase):
    def test_unreachable_server_returns_user_friendly_message(self):
        with mock.patch.object(mod.urllib.request, "urlopen", side_effect=urllib.error.URLError("Connection refused")):
            message = mod.check_soavirt_server_reachable(
                api_base="http://localhost:9080/soavirt/api/v6",
                parent_id="/TestAssets",
                auth_header=None,
                timeout_sec=5,
            )

        self.assertIsNotNone(message)
        self.assertIn("Unable to connect to the SOAtest server.", message)
        self.assertIn("local SOAtest/SOAVirt server", message)
        self.assertIn("start the SOAtest server", message)
        self.assertIn("verify the configured server URL", message)
        self.assertIn("--api-base", message)


class MainServerPreflightTests(unittest.TestCase):
    def test_server_unavailable_fails_before_running_steps(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            collection_path = tmp_path / "collection.postman_collection.json"
            environment_path = tmp_path / "environment.postman_environment.json"
            workspace_root = tmp_path / "workspace"
            workspace_root.mkdir()
            (workspace_root / "TestAssets").mkdir()
            collection_path.write_text('{"info": {"name": "Demo"}, "item": []}', encoding="utf-8")
            environment_path.write_text('{"name": "env", "values": []}', encoding="utf-8")

            stdout_capture = io.StringIO()
            stderr_capture = io.StringIO()

            with mock.patch.object(
                mod,
                "check_soavirt_server_reachable",
                return_value=(
                    "Unable to connect to the SOAtest server.\n"
                    "The converter requires the local SOAtest/SOAVirt server to be running before a conversion can begin.\n"
                    "Please: start the SOAtest server (which hosts the SOAVirt REST API), or verify the configured "
                    "server URL with --api-base if it is running on a different host or port.\n"
                    "Configured server URL: http://localhost:9080/soavirt/api/v6"
                ),
            ):
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    rc = mod.main([
                        str(collection_path),
                        "--postman-environment",
                        str(environment_path),
                        "--workspace-root",
                        str(workspace_root),
                        "--test-file-name",
                        "demoOut",
                    ])

            self.assertEqual(1, rc)
            self.assertIn("Unable to connect to the SOAtest server.", stderr_capture.getvalue())
            self.assertNotIn("=== Step 1", stdout_capture.getvalue())


class ValidationGateTests(unittest.TestCase):
    def _required_success_steps(self):
        return [
            {"name": "Step 1: Endpoint Sequence", "status": "SUCCESS", "metrics": {}, "warningCount": 0},
            {"name": "Step 2: Params Implementation", "status": "SUCCESS", "metrics": {}, "warningCount": 0},
            {"name": "Step 3: Variable Implementation", "status": "SUCCESS", "metrics": {}, "warningCount": 0},
            {"name": "Step 4: Authorization Implementation", "status": "SUCCESS", "metrics": {}, "warningCount": 0},
            {"name": "Step 5: Header Implementation", "status": "SUCCESS", "metrics": {}, "warningCount": 0},
            {"name": "Step 6: Request Body Implementation", "status": "SUCCESS", "metrics": {}, "warningCount": 0},
            {"name": "Step 7: Script Implementation", "status": "SUCCESS", "metrics": {"REST clients updated": "1"}, "warningCount": 0},
            {"name": "Step 8: Notes Implementation", "status": "SUCCESS", "metrics": {"Suites updated": "1"}, "warningCount": 0},
            {"name": "Step 9: HTTP Options Implementation", "status": "SUCCESS", "metrics": {}, "warningCount": 0},
        ]

    def _base_expectations(self):
        return {
            "collectionName": "Demo",
            "requestCount": 1,
            "queryKeys": set(),
            "headerNames": set(),
            "bodyModes": {},
            "requestScriptEvents": 0,
            "hasNotes": False,
            "collectionOverviewPresent": False,
            "folderOverviewsWithNotes": 0,
            "requestsWithNotes": 0,
            "authCounts": {},
            "environmentVariableNames": set(),
            "collectionVariableNames": set(),
            "httpOptionsApplicableRequestCount": 0,
            "folderPaths": {("Folder A",)},
        }

    def _write_tst(self, workspace_root: Path, file_name: str, schema_version: str = "03") -> Path:
        target_dir = workspace_root / "TestAssets"
        target_dir.mkdir(parents=True, exist_ok=True)
        tst_path = target_dir / f"{file_name}.tst"
        tst_path.write_text(
            "---\n"
            "parasoftVersion: 2026.1.0\n"
            "productVersion: 10.7.5\n"
            f"schemaVersion: {schema_version}\n"
            "suite:\n"
            "  $type: TestSuite\n"
            "  name: Demo\n"
            "  tests:\n"
            "  - $type: TestSuite\n"
            "    name: Folder A\n"
            "    tests:\n"
            "    - $type: RESTClientToolTest\n"
            "      name: Req 1\n",
            encoding="utf-8",
        )
        return tst_path

    def test_force_schema_version_helper_updates_value(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tst_path = Path(tmp_dir) / "sample.tst"
            tst_path.write_text(
                "---\nparasoftVersion: 2026.1.0\nproductVersion: 10.7.5\nschemaVersion: 03\nsuite: {}\n",
                encoding="utf-8",
            )

            mod.force_schema_version_in_file(tst_path, mod.TARGET_TST_SCHEMA_VERSION)
            updated = tst_path.read_text(encoding="utf-8")
            self.assertIn("schemaVersion: 13", updated)

    def test_malformed_yaml_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir) / "workspace"
            (workspace / "TestAssets").mkdir(parents=True, exist_ok=True)
            tst_path = workspace / "TestAssets" / "demoOut.tst"
            tst_path.write_text(
                "---\nparasoftVersion: 2026.1.0\nproductVersion: 10.7.5\nschemaVersion: 03\nsuite: [\n",
                encoding="utf-8",
            )

            with mock.patch.object(mod, "_load_yaml_document", side_effect=RuntimeError("bad yaml")):
                errors = mod.validate_final_tst_against_expectations(
                    step_reports=self._required_success_steps(),
                    expectations=self._base_expectations(),
                    selected_test_file_name="demoOut",
                    workspace_root=workspace,
                    parent_id="/TestAssets",
                    environment_name="Default Environment",
                    api_base="http://localhost:9080/soavirt/api/v6",
                    auth_header=None,
                    timeout_sec=5,
                )

            self.assertTrue(errors)
            self.assertIn("[YAML Parse Validation]", errors[0])

    def test_missing_required_header_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir) / "workspace"
            self._write_tst(workspace, "demoOut")

            parsed = {"productVersion": "10.7.5", "schemaVersion": "13", "suite": {}}
            with mock.patch.object(mod, "_load_yaml_document", return_value=parsed):
                errors = mod.validate_final_tst_against_expectations(
                    step_reports=self._required_success_steps(),
                    expectations=self._base_expectations(),
                    selected_test_file_name="demoOut",
                    workspace_root=workspace,
                    parent_id="/TestAssets",
                    environment_name="Default Environment",
                    api_base="http://localhost:9080/soavirt/api/v6",
                    auth_header=None,
                    timeout_sec=5,
                )

            self.assertTrue(errors)
            self.assertIn("[Required Header Validation]", errors[0])

    def test_read_back_failure_fails_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir) / "workspace"
            self._write_tst(workspace, "demoOut")

            parsed = {
                "parasoftVersion": "2026.1.0",
                "productVersion": "10.7.5",
                "schemaVersion": "13",
                "suite": {
                    "$type": "TestSuite",
                    "name": "Demo",
                    "tests": [
                        {
                            "$type": "TestSuite",
                            "name": "Folder A",
                            "tests": [{"$type": "RESTClientToolTest", "name": "Req 1"}],
                        }
                    ],
                },
            }

            with mock.patch.object(mod, "_load_yaml_document", return_value=parsed), mock.patch.object(
                mod,
                "_validate_soavirt_read_back",
                return_value=["simulated read-back failure"],
            ):
                errors = mod.validate_final_tst_against_expectations(
                    step_reports=self._required_success_steps(),
                    expectations=self._base_expectations(),
                    selected_test_file_name="demoOut",
                    workspace_root=workspace,
                    parent_id="/TestAssets",
                    environment_name="Default Environment",
                    api_base="http://localhost:9080/soavirt/api/v6",
                    auth_header=None,
                    timeout_sec=5,
                )

            self.assertTrue(errors)
            self.assertIn("[SOAVirt API Read-Back Validation]", errors[0])


    def test_final_validation_enforces_schema_version(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir) / "workspace"
            tst_path = self._write_tst(workspace, "demoOut", schema_version="03")

            parsed = {
                "parasoftVersion": "2026.1.0",
                "productVersion": "10.7.5",
                "schemaVersion": "13",
                "suite": {
                    "$type": "TestSuite",
                    "name": "Demo",
                    "tests": [
                        {
                            "$type": "TestSuite",
                            "name": "Folder A",
                            "tests": [{"$type": "RESTClientToolTest", "name": "Req 1"}],
                        }
                    ],
                },
            }

            with mock.patch.object(mod, "_load_yaml_document", return_value=parsed), mock.patch.object(
                mod,
                "_validate_soavirt_read_back",
                return_value=[],
            ):
                errors = mod.validate_final_tst_against_expectations(
                    step_reports=self._required_success_steps(),
                    expectations=self._base_expectations(),
                    selected_test_file_name="demoOut",
                    workspace_root=workspace,
                    parent_id="/TestAssets",
                    environment_name="Default Environment",
                    api_base="http://localhost:9080/soavirt/api/v6",
                    auth_header=None,
                    timeout_sec=5,
                )

            self.assertEqual([], errors)
            self.assertIn("schemaVersion: 13", tst_path.read_text(encoding="utf-8"))

    def test_missing_expected_rest_clients_fails_structure_validation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir) / "workspace"
            self._write_tst(workspace, "demoOut")

            parsed = {
                "parasoftVersion": "2026.1.0",
                "productVersion": "10.7.5",
                "schemaVersion": "13",
                "suite": {
                    "$type": "TestSuite",
                    "name": "Demo",
                    "tests": [{"$type": "TestSuite", "name": "Folder A", "tests": []}],
                },
            }
            expectations = self._base_expectations()
            expectations["requestCount"] = 2

            with mock.patch.object(mod, "_load_yaml_document", return_value=parsed):
                errors = mod.validate_final_tst_against_expectations(
                    step_reports=self._required_success_steps(),
                    expectations=expectations,
                    selected_test_file_name="demoOut",
                    workspace_root=workspace,
                    parent_id="/TestAssets",
                    environment_name="Default Environment",
                    api_base="http://localhost:9080/soavirt/api/v6",
                    auth_header=None,
                    timeout_sec=5,
                )

            self.assertTrue(errors)
            self.assertIn("[SOAtest Structure Validation]", errors[0])


class CleanupBehaviorTests(unittest.TestCase):
    def test_postflight_failure_cleans_up_run_created_tst(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            workspace_root = tmp_path / "workspace"
            (workspace_root / "TestAssets").mkdir(parents=True, exist_ok=True)
            collection_path = tmp_path / "collection.postman_collection.json"
            environment_path = tmp_path / "environment.postman_environment.json"
            collection_path.write_text('{"info": {"name": "Demo"}, "item": []}', encoding="utf-8")
            environment_path.write_text('{"name": "env", "values": []}', encoding="utf-8")

            def fake_run_step(_cmd, step_name):
                if step_name == "Step 1: Endpoint Sequence":
                    return 0, "Selected test file name: demoOut\nCreated importable SOAtest file via API: /TestAssets/demoOut.tst\n"
                return 0, ""

            with mock.patch.object(mod, "check_soavirt_server_reachable", return_value=None), mock.patch.object(
                mod,
                "validate_soavirt_parent_folder_access",
                return_value=None,
            ), mock.patch.object(mod, "resolve_test_file_name", return_value="demoOut"), mock.patch.object(
                mod,
                "run_step",
                side_effect=fake_run_step,
            ), mock.patch.object(
                mod,
                "validate_final_tst_against_expectations",
                return_value=["postflight failure"],
            ), mock.patch.object(mod, "delete_soavirt_file") as delete_mock:
                rc = mod.main(
                    [
                        str(collection_path),
                        "--postman-environment",
                        str(environment_path),
                        "--workspace-root",
                        str(workspace_root),
                        "--test-file-name",
                        "demoOut",
                    ]
                )

            self.assertEqual(1, rc)
            delete_mock.assert_called_once()
            self.assertIn("/TestAssets/demoOut.tst", str(delete_mock.call_args))


if __name__ == "__main__":
    unittest.main()
