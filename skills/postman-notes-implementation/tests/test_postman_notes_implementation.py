import importlib.util
import sys
from pathlib import Path
import unittest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "postman_notes_implementation.py"
spec = importlib.util.spec_from_file_location("postman_notes_impl", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class NotesMappingTests(unittest.TestCase):
    def test_walk_extracts_folder_overview_and_request_docs(self):
        items = [
            {
                "name": "BrowseAndCheckout",
                "description": "hello {{folderVar}}",
                "item": [
                    {
                        "name": "BrowseCategories",
                        "request": {"description": "fetch {{requestVar}}"},
                    }
                ],
            }
        ]

        request_targets = []
        folder_targets = []
        mod.walk_postman_items_with_docs(items, (), request_targets, folder_targets)

        self.assertEqual(1, len(folder_targets))
        self.assertEqual(("BrowseAndCheckout",), folder_targets[0].folder_path)
        self.assertEqual("BrowseAndCheckout", folder_targets[0].folder_name)
        self.assertEqual("hello ${folderVar}", folder_targets[0].doc_text)

        self.assertEqual(1, len(request_targets))
        self.assertEqual(("BrowseAndCheckout",), request_targets[0].folder_path)
        self.assertEqual("BrowseCategories", request_targets[0].request_name)
        self.assertEqual("fetch ${requestVar}", request_targets[0].doc_text)

    def test_filter_request_docs_skips_suites_with_folder_overview(self):
        suite_id_to_targets = {
            "/suite/A": [mod.RequestDocTarget(folder_path=("A",), request_name="Req1", doc_text="one")],
            "/suite/B": [mod.RequestDocTarget(folder_path=("B",), request_name="Req2", doc_text="two")],
        }
        filtered, skipped = mod.filter_request_docs_for_overview_suites(
            suite_id_to_targets,
            suite_ids_with_folder_overview={"/suite/A"},
        )

        self.assertEqual(1, skipped)
        self.assertEqual({"/suite/B"}, set(filtered.keys()))
        self.assertEqual("Req2", filtered["/suite/B"][0].request_name)

    def test_apply_request_comments_to_tracking_updates_existing_and_adds_missing(self):
        existing = [
            {"id": "/suite/Test 1", "comment": "", "requirements": []},
            {"id": "/suite/Test 2", "comment": "old", "requirements": []},
        ]
        comments = [
            ("/suite/Test 1", "new one"),
            ("/suite/Test 3", "third"),
        ]

        updated, changes = mod.apply_request_comments_to_tracking(existing, comments)

        self.assertEqual(2, changes)
        by_id = {entry["id"]: entry for entry in updated}
        self.assertEqual("new one", by_id["/suite/Test 1"]["comment"])
        self.assertEqual("old", by_id["/suite/Test 2"]["comment"])
        self.assertEqual("third", by_id["/suite/Test 3"]["comment"])

    def test_note_blocks_are_plain_postman_text_only(self):
        folder_block = mod.build_folder_generated_block("BrowseAndCheckout", "hello from postman")
        self.assertEqual("hello from postman", folder_block)

        collection_block = mod.build_collection_generated_block("hello from collection")
        self.assertEqual("hello from collection", collection_block)

        request_block = mod.build_request_generated_block([
            mod.RequestDocTarget(folder_path=("A",), request_name="Req1", doc_text="first"),
            mod.RequestDocTarget(folder_path=("A",), request_name="Req2", doc_text="second"),
        ])
        self.assertEqual("first\n\nsecond", request_block)


if __name__ == "__main__":
    unittest.main()
