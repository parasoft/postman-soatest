import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "create_endpoint_sequence.py"
spec = importlib.util.spec_from_file_location("create_endpoint_sequence", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class ResolveTargetFileStemTests(unittest.TestCase):
    def test_skip_collision_check_uses_orchestrator_name_without_api_lookup(self):
        original_find_existing = mod.find_existing_target
        original_prompt = mod.prompt_for_test_file_name
        try:
            def fail_find_existing(*args, **kwargs):
                raise AssertionError("find_existing_target should not be called when skip_collision_check is enabled")

            def fail_prompt(_default_name):
                raise AssertionError("prompt_for_test_file_name should not be called when skip_collision_check is enabled")

            mod.find_existing_target = fail_find_existing
            mod.prompt_for_test_file_name = fail_prompt

            value = mod.resolve_target_file_stem(
                client=object(),
                parent_id="/TestAssets",
                default_name="defaultName",
                test_file_name_override="MyTarget",
                tracker=mod.RuntimeGeneratedTracker(),
                skip_collision_check=True,
            )

            self.assertEqual("MyTarget", value)
        finally:
            mod.find_existing_target = original_find_existing
            mod.prompt_for_test_file_name = original_prompt

    def test_skip_collision_check_requires_resolved_name_from_orchestrator(self):
        with self.assertRaises(RuntimeError) as ctx:
            mod.resolve_target_file_stem(
                client=object(),
                parent_id="/TestAssets",
                default_name="defaultName",
                test_file_name_override=None,
                tracker=mod.RuntimeGeneratedTracker(),
                skip_collision_check=True,
            )

        self.assertIn("skip_collision_check requires --test-file-name", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
