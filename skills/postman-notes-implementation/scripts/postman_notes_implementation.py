#!/usr/bin/env python3
"""
Implement Postman notes into SOAtest suite Requirements and Notes.

Behavior:
- Reads collection overview notes from Postman collection info.description.
- Reads folder overview notes from Postman folder descriptions.
- Reads request descriptions from Postman collection items.
- Groups request docs by parent folder path.
- Writes collection overview into the top/global SOAtest Test Suite notes.
- Writes folder overview notes into the corresponding SOAtest Scenario/Test Suite notes.
- Skips request-level notes for suites that have folder overview notes.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
REQUEST_BEGIN_MARKER = "[[POSTMAN_REQUEST_DOCS_BEGIN]]"
REQUEST_END_MARKER = "[[POSTMAN_REQUEST_DOCS_END]]"
COLLECTION_BEGIN_MARKER = "[[POSTMAN_COLLECTION_OVERVIEW_BEGIN]]"
COLLECTION_END_MARKER = "[[POSTMAN_COLLECTION_OVERVIEW_END]]"
FOLDER_BEGIN_MARKER = "[[POSTMAN_FOLDER_OVERVIEW_BEGIN]]"
FOLDER_END_MARKER = "[[POSTMAN_FOLDER_OVERVIEW_END]]"
MISSING_TRACKING_ID_RE = re.compile(
    r"requirementsAndTracking\] field identified by \[([^\]]+)\] could not be found"
)


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def convert_postman_variable_syntax(text: str) -> str:
    if not text:
        return ""
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", str(text))


def description_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        content = value.get("content")
        if content is None:
            return ""
        return str(content).strip()
    return ""


def extract_collection_overview(collection: Dict[str, Any]) -> str:
    info = collection.get("info") if isinstance(collection.get("info"), dict) else {}
    info_description = description_to_text(info.get("description"))
    collection_description = description_to_text(collection.get("description"))
    text = info_description or collection_description
    return convert_postman_variable_syntax(text)


@dataclass(frozen=True)
class RequestDocTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    doc_text: str


@dataclass(frozen=True)
class FolderDocTarget:
    folder_path: Tuple[str, ...]
    folder_name: str
    doc_text: str


class SoaVirtClient:
    def __init__(self, base_url: str, username: Optional[str], password: Optional[str], timeout_sec: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = timeout_sec
        self.auth_header = None
        if username is not None and password is not None:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            self.auth_header = f"Basic {token}"

    def _request(
        self,
        method: str,
        path: str,
        query: Optional[Dict[str, str]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.auth_header:
            headers["Authorization"] = self.auth_header

        req = urllib.request.Request(url=url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload.strip() else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} - {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc

    def get_children(self, parent_id: Optional[str] = None, resource_type: Optional[str] = None) -> List[Dict[str, Any]]:
        query: Dict[str, str] = {}
        if parent_id:
            query["id"] = parent_id
        if resource_type:
            query["type"] = resource_type
        result = self._request("GET", "/children", query=query)
        children = result.get("children", [])
        return children if isinstance(children, list) else []

    def descendants_assets(self, resource_id: str) -> Dict[str, Any]:
        return self._request("GET", "/descendants/assets", query={"id": resource_id})

    def get_suite(self, suite_id: str) -> Dict[str, Any]:
        return self._request("GET", "/suites/testSuites", query={"id": suite_id})

    def update_suite_notes(
        self,
        suite_id: str,
        notes: str,
        requirements_and_tracking: Optional[List[Dict[str, Any]]] = None,
        suite_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        resolved_name = str(suite_name or "")
        if not resolved_name:
            suite = self.get_suite(suite_id)
            resolved_name = str(suite.get("name", ""))

        body: Dict[str, Any] = {
            "requirementsAndNotes": {
                "notes": notes,
            }
        }
        if resolved_name:
            body["name"] = resolved_name
        if requirements_and_tracking is not None:
            body["requirementsAndNotes"]["requirementsAndTracking"] = requirements_and_tracking
        return self._request("PUT", "/suites/testSuites", query={"id": suite_id}, body=body)


def resolve_target_tst_id(client: SoaVirtClient, parent_id: str, test_file_name_without_ext: str) -> str:
    target_name = f"{sanitize_filename(test_file_name_without_ext)}.tst"
    for child in client.get_children(parent_id=parent_id):
        if str(child.get("name", "")).lower() == target_name.lower():
            resource_id = str(child.get("id", ""))
            if resource_id:
                return resource_id
    raise RuntimeError(f"Target .tst not found: {parent_id}/{target_name}")


def walk_postman_items_with_docs(
    items: Any,
    folder_path: Tuple[str, ...],
    request_targets: List[RequestDocTarget],
    folder_targets: List[FolderDocTarget],
) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get("name", "Unnamed"))

        nested = item.get("item")
        if isinstance(nested, list):
            current_folder_path = folder_path + (item_name,)
            folder_desc = description_to_text(item.get("description"))
            if folder_desc:
                folder_targets.append(
                    FolderDocTarget(
                        folder_path=current_folder_path,
                        folder_name=item_name,
                        doc_text=convert_postman_variable_syntax(folder_desc),
                    )
                )
            walk_postman_items_with_docs(
                items=nested,
                folder_path=current_folder_path,
                request_targets=request_targets,
                folder_targets=folder_targets,
            )

        request_obj = item.get("request")
        if isinstance(request_obj, dict):
            request_desc = description_to_text(request_obj.get("description"))
            item_desc = description_to_text(item.get("description"))
            raw_doc = request_desc or item_desc
            if raw_doc:
                request_targets.append(
                    RequestDocTarget(
                        folder_path=folder_path,
                        request_name=item_name,
                        doc_text=convert_postman_variable_syntax(raw_doc),
                    )
                )


def build_suite_and_request_lookup(
    descendants_payload: Dict[str, Any],
) -> Tuple[Dict[Tuple[str, ...], str], Dict[Tuple[Tuple[str, ...], str], str], Dict[str, List[str]]]:
    suite_lookup: Dict[Tuple[str, ...], str] = {}
    request_lookup: Dict[Tuple[Tuple[str, ...], str], str] = {}
    request_by_name: Dict[str, List[str]] = defaultdict(list)

    def walk_nodes(nodes: Sequence[Dict[str, Any]], folder_path: Tuple[str, ...]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue

            node_type = str(node.get("type", ""))
            node_name = str(node.get("name", ""))
            node_id = str(node.get("id", ""))

            if node_type == "testSuite":
                current_path = folder_path + (node_name,)
                if node_id:
                    suite_lookup[current_path] = node_id
                children = node.get("children")
                if isinstance(children, list):
                    walk_nodes(children, current_path)
                continue

            if node_type == "restClient":
                if node_id and node_name:
                    request_lookup[(folder_path, node_name)] = node_id
                    request_by_name[node_name].append(node_id)
                children = node.get("children")
                if isinstance(children, list):
                    walk_nodes(children, folder_path)
                continue

            children = node.get("children")
            if isinstance(children, list):
                walk_nodes(children, folder_path)

    root_children = descendants_payload.get("children")
    if not isinstance(root_children, list):
        return suite_lookup, request_lookup, request_by_name

    for top in root_children:
        if not isinstance(top, dict):
            continue
        if str(top.get("type", "")) != "testSuite":
            continue

        top_id = str(top.get("id", ""))
        if top_id:
            suite_lookup[()] = top_id

        children = top.get("children")
        if isinstance(children, list):
            # IMPORTANT: Do not include the top suite name in the lookup key path.
            # Postman folder paths are rooted at the suite's children.
            walk_nodes(children, ())

    return suite_lookup, request_lookup, request_by_name


def collect_descendant_ids(descendants_payload: Dict[str, Any]) -> Set[str]:
    ids: Set[str] = set()

    def walk(nodes: Sequence[Dict[str, Any]]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id", "")).strip()
            if node_id:
                ids.add(node_id)
            children = node.get("children")
            if isinstance(children, list):
                walk(children)

    root_children = descendants_payload.get("children")
    if isinstance(root_children, list):
        walk(root_children)
    return ids


def sanitize_tracking_entries(
    existing_tracking: Sequence[Dict[str, Any]],
    valid_ids: Set[str],
) -> Tuple[List[Dict[str, Any]], int]:
    sanitized: List[Dict[str, Any]] = []
    dropped = 0

    for entry in existing_tracking:
        if not isinstance(entry, dict):
            dropped += 1
            continue
        entry_id = str(entry.get("id", "")).strip()
        if not entry_id or (valid_ids and entry_id not in valid_ids):
            dropped += 1
            continue

        copied = dict(entry)
        if copied.get("comment") is None:
            copied["comment"] = ""
        requirements = copied.get("requirements")
        copied["requirements"] = requirements if isinstance(requirements, list) else []
        sanitized.append(copied)

    return sanitized, dropped


def extract_missing_tracking_id(error_text: str) -> Optional[str]:
    match = MISSING_TRACKING_ID_RE.search(error_text or "")
    if not match:
        return None
    return match.group(1).strip() or None


def strip_generated_block(existing_notes: str, begin_marker: str, end_marker: str) -> str:
    pattern = re.compile(re.escape(begin_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
    cleaned = pattern.sub("", existing_notes)
    return cleaned.strip()


def build_request_generated_block(requests: List[RequestDocTarget]) -> str:
    parts = [str(target.doc_text or "").strip() for target in requests]
    parts = [part for part in parts if part]
    return "\n\n".join(parts).strip()


def build_collection_generated_block(collection_overview_text: str) -> str:
    return str(collection_overview_text or "").strip()


def build_folder_generated_block(folder_name: str, folder_overview_text: str) -> str:
    _ = folder_name
    return str(folder_overview_text or "").strip()


def filter_request_docs_for_overview_suites(
    suite_id_to_targets: Dict[str, List[RequestDocTarget]],
    suite_ids_with_folder_overview: Set[str],
) -> Tuple[Dict[str, List[RequestDocTarget]], int]:
    filtered: Dict[str, List[RequestDocTarget]] = {}
    skipped = 0
    for suite_id, targets in suite_id_to_targets.items():
        if suite_id in suite_ids_with_folder_overview:
            skipped += len(targets)
            continue
        filtered[suite_id] = list(targets)
    return filtered, skipped


def merge_notes(existing_notes: str, generated_block: str, begin_marker: str, end_marker: str) -> str:
    _ = begin_marker
    _ = end_marker
    generated = str(generated_block or "").strip()
    if not generated:
        return str(existing_notes or "").strip()
    return generated


def apply_request_comments_to_tracking(
    existing_tracking: Sequence[Dict[str, Any]],
    comment_entries: Sequence[Tuple[str, str]],
) -> Tuple[List[Dict[str, Any]], int]:
    updated: List[Dict[str, Any]] = []
    id_to_index: Dict[str, int] = {}

    for entry in existing_tracking:
        if not isinstance(entry, dict):
            continue
        copied = dict(entry)
        updated.append(copied)
        entry_id = str(copied.get("id", ""))
        if entry_id:
            id_to_index[entry_id] = len(updated) - 1

    changes = 0
    for entry_id, doc_text in comment_entries:
        generated = str(doc_text or "").strip()
        if not generated:
            continue
        if entry_id in id_to_index:
            idx = id_to_index[entry_id]
            entry = dict(updated[idx])
            existing_comment = str(entry.get("comment", ""))
            if generated != existing_comment.strip():
                entry["comment"] = generated
                updated[idx] = entry
                changes += 1
            continue

        updated.append({"id": entry_id, "comment": generated, "requirements": []})
        id_to_index[entry_id] = len(updated) - 1
        changes += 1

    return updated, changes


def apply_request_comments_by_name(
    existing_tracking: Sequence[Dict[str, Any]],
    comments_by_request_name: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], int]:
    updated: List[Dict[str, Any]] = []
    changes = 0

    for entry in existing_tracking:
        if not isinstance(entry, dict):
            continue
        copied = dict(entry)
        entry_id = str(copied.get("id", "")).strip()
        request_name = entry_id.rsplit("/", 1)[-1] if entry_id else ""
        generated = str(comments_by_request_name.get(request_name, "")).strip()
        if generated:
            existing_comment = str(copied.get("comment", ""))
            if generated != existing_comment.strip():
                copied["comment"] = generated
                changes += 1
        updated.append(copied)

    return updated, changes


def apply_request_docs(
    client: SoaVirtClient,
    tst_id: str,
    request_targets: Sequence[RequestDocTarget],
    folder_targets: Sequence[FolderDocTarget],
    collection_overview_text: str,
) -> Tuple[int, int, int, bool, int, List[str]]:
    descendants = client.descendants_assets(tst_id)
    suite_lookup, request_lookup, request_by_name = build_suite_and_request_lookup(descendants)
    top_suite_id = suite_lookup.get(())
    valid_descendant_ids = collect_descendant_ids(descendants)

    suites_updated = 0
    docs_applied = 0
    comments_applied = 0
    collection_overview_applied = False
    folder_overviews_applied = 0
    warnings: List[str] = []

    # Resolve request node ids first so we can apply long-form docs into the
    # actual parent suite notes regardless of folder-name mismatches.
    suite_id_to_targets: Dict[str, List[RequestDocTarget]] = defaultdict(list)
    request_comment_entries: List[Tuple[str, str]] = []

    suite_rest_names: Dict[str, set[str]] = defaultdict(set)
    suite_request_ids: Dict[Tuple[str, str], str] = {}
    for request_name, ids in request_by_name.items():
        for node_id in ids:
            parent_suite_id = node_id.rsplit("/", 1)[0]
            suite_rest_names[parent_suite_id].add(request_name)
            suite_request_ids.setdefault((parent_suite_id, request_name), node_id)

    grouped_by_folder: Dict[Tuple[str, ...], List[RequestDocTarget]] = defaultdict(list)
    for target in request_targets:
        grouped_by_folder[target.folder_path].append(target)

    for folder_path, targets in grouped_by_folder.items():
        group_names = {t.request_name for t in targets}
        matching_suites = [
            suite_id for suite_id, names in suite_rest_names.items() if group_names.issubset(names)
        ]
        group_suite_id = matching_suites[0] if len(matching_suites) == 1 else None

        for target in targets:
            node_id = request_lookup.get((folder_path, target.request_name))
            if not node_id and group_suite_id is not None:
                node_id = suite_request_ids.get((group_suite_id, target.request_name))
            if not node_id:
                candidates = request_by_name.get(target.request_name, [])
                if group_suite_id is not None:
                    filtered = [cid for cid in candidates if cid.startswith(group_suite_id + "/")]
                    if len(filtered) == 1:
                        node_id = filtered[0]
                if not node_id and len(candidates) == 1:
                    node_id = candidates[0]

            if not node_id:
                warnings.append(
                    "Could not resolve SOAtest REST client node for request docs/comment: "
                    f"{'/'.join(folder_path) if folder_path else '(root)'}/{target.request_name}"
                )
                continue

            if folder_path and request_lookup.get((folder_path, target.request_name)) is None:
                warnings.append(
                    "Folder path mismatch for request; applied by best-effort lookup: "
                    f"{'/'.join(folder_path)} -> {target.request_name}"
                )

            parent_suite_id = node_id.rsplit("/", 1)[0]
            suite_id_to_targets[parent_suite_id].append(target)
            request_comment_entries.append((node_id, target.doc_text))

    top_suite_notes_changed = False
    top_suite_tracking_changed = False
    top_suite_notes = ""
    top_suite_name = ""
    top_suite_tracking: List[Dict[str, Any]] = []

    if top_suite_id:
        valid_descendant_ids.add(top_suite_id)
        try:
            suite = client.get_suite(top_suite_id)
            top_suite_name = str(suite.get("name", ""))
            rn = suite.get("requirementsAndNotes") if isinstance(suite.get("requirementsAndNotes"), dict) else {}
            top_suite_notes = str(rn.get("notes", ""))
            for begin_marker, end_marker in (
                (COLLECTION_BEGIN_MARKER, COLLECTION_END_MARKER),
                (FOLDER_BEGIN_MARKER, FOLDER_END_MARKER),
                (REQUEST_BEGIN_MARKER, REQUEST_END_MARKER),
            ):
                top_suite_notes = strip_generated_block(top_suite_notes, begin_marker, end_marker)
            tracking = rn.get("requirementsAndTracking") if isinstance(rn, dict) else None
            top_suite_tracking_raw = tracking if isinstance(tracking, list) else []
            top_suite_tracking, dropped_tracking = sanitize_tracking_entries(
                top_suite_tracking_raw,
                valid_ids=valid_descendant_ids,
            )
            if dropped_tracking > 0:
                warnings.append(
                    f"Dropped {dropped_tracking} stale requirements tracking entries from top suite before notes update."
                )
        except RuntimeError as exc:
            warnings.append(f"Failed to read top/global SOAtest suite: {exc}")
            top_suite_id = None

    if collection_overview_text:
        if not top_suite_id:
            warnings.append("Could not resolve top/global SOAtest suite for collection overview notes.")
        else:
            try:
                overview_block = build_collection_generated_block(collection_overview_text)
                merged = overview_block
                if merged != top_suite_notes.strip():
                    top_suite_notes = merged
                    top_suite_notes_changed = True
                collection_overview_applied = True
            except RuntimeError as exc:
                warnings.append(f"Failed to apply collection overview notes: {exc}")

    suite_ids_with_folder_overview: Set[str] = set()
    for folder_target in folder_targets:
        suite_id = suite_lookup.get(folder_target.folder_path)
        if not suite_id:
            warnings.append(
                "Could not resolve SOAtest Test Suite for folder overview note: "
                f"{'/'.join(folder_target.folder_path) if folder_target.folder_path else '(root)'}"
            )
            continue

        folder_block = build_folder_generated_block(
            folder_name=folder_target.folder_name,
            folder_overview_text=folder_target.doc_text,
        )

        if suite_id == top_suite_id:
            merged = folder_block
            if merged != top_suite_notes.strip():
                top_suite_notes = merged
                top_suite_notes_changed = True
            suite_ids_with_folder_overview.add(suite_id)
            folder_overviews_applied += 1
            continue

        try:
            suite = client.get_suite(suite_id)
            suite_name = str(suite.get("name", ""))
            rn = suite.get("requirementsAndNotes") if isinstance(suite.get("requirementsAndNotes"), dict) else {}
            existing_notes = str(rn.get("notes", ""))
            for begin_marker, end_marker in (
                (COLLECTION_BEGIN_MARKER, COLLECTION_END_MARKER),
                (FOLDER_BEGIN_MARKER, FOLDER_END_MARKER),
                (REQUEST_BEGIN_MARKER, REQUEST_END_MARKER),
            ):
                existing_notes = strip_generated_block(existing_notes, begin_marker, end_marker)
            merged = folder_block
            if merged != existing_notes.strip():
                client.update_suite_notes(suite_id, merged, None, suite_name=suite_name)
                suites_updated += 1
            suite_ids_with_folder_overview.add(suite_id)
            folder_overviews_applied += 1
        except RuntimeError as exc:
            warnings.append(f"Failed to apply folder overview for suite {suite_id}: {exc}")

    filtered_suite_id_to_targets, skipped_request_docs = filter_request_docs_for_overview_suites(
        suite_id_to_targets,
        suite_ids_with_folder_overview=suite_ids_with_folder_overview,
    )
    if skipped_request_docs > 0:
        warnings.append(
            "Skipped request-level notes for suites with folder overview notes; request comments were still applied: "
            f"{skipped_request_docs} request note entries."
        )

    if top_suite_id and request_comment_entries:
        updated_tracking, comment_changes = apply_request_comments_to_tracking(
            top_suite_tracking,
            request_comment_entries,
        )
        if comment_changes > 0:
            top_suite_tracking = updated_tracking
            top_suite_tracking_changed = True
            comments_applied = comment_changes
    elif request_comment_entries and not top_suite_id:
        warnings.append("Could not apply request-level comments because top suite tracking could not be resolved.")

    # Apply request docs into suite notes (per actual suite id).
    for suite_id, targets in filtered_suite_id_to_targets.items():
        generated_block = build_request_generated_block(targets)
        if suite_id == top_suite_id:
            merged = generated_block
            if top_suite_notes.strip() and generated_block:
                merged = f"{top_suite_notes.strip()}\n\n{generated_block}".strip()
            if merged != top_suite_notes.strip():
                top_suite_notes = merged
                top_suite_notes_changed = True
            docs_applied += len(targets)
            continue

        try:
            suite = client.get_suite(suite_id)
            suite_name = str(suite.get("name", ""))
            rn = suite.get("requirementsAndNotes") if isinstance(suite.get("requirementsAndNotes"), dict) else {}
            existing_notes = str(rn.get("notes", ""))
            for begin_marker, end_marker in (
                (COLLECTION_BEGIN_MARKER, COLLECTION_END_MARKER),
                (FOLDER_BEGIN_MARKER, FOLDER_END_MARKER),
                (REQUEST_BEGIN_MARKER, REQUEST_END_MARKER),
            ):
                existing_notes = strip_generated_block(existing_notes, begin_marker, end_marker)
            merged = generated_block
            if merged != existing_notes.strip():
                client.update_suite_notes(suite_id, merged, None, suite_name=suite_name)
                suites_updated += 1
            docs_applied += len(targets)
        except RuntimeError as exc:
            warnings.append(f"Failed to apply docs for suite {suite_id}: {exc}")

    if top_suite_id and (top_suite_notes_changed or top_suite_tracking_changed):
        tracking_for_update = list(top_suite_tracking)
        updated_top_suite = False
        for _ in range(4):
            try:
                client.update_suite_notes(top_suite_id, top_suite_notes, tracking_for_update, suite_name=top_suite_name)
                suites_updated += 1
                updated_top_suite = True
                break
            except RuntimeError as exc:
                err_text = str(exc)
                missing_id = extract_missing_tracking_id(err_text)
                if not missing_id:
                    warnings.append(f"Failed to apply top suite notes: {exc}")
                    break

                filtered_tracking = [
                    entry
                    for entry in tracking_for_update
                    if str(entry.get("id", "")).strip() != missing_id
                ]
                if len(filtered_tracking) == len(tracking_for_update):
                    warnings.append(f"Failed to apply top suite notes: {exc}")
                    break

                tracking_for_update = filtered_tracking
                warnings.append(
                    f"Removed stale requirements tracking id and retried top suite notes update: {missing_id}"
                )

        if updated_top_suite:
            top_suite_tracking = tracking_for_update

    return docs_applied, comments_applied, suites_updated, collection_overview_applied, folder_overviews_applied, warnings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Postman collection/request notes into SOAtest Test Suite Requirements and Notes "
            "for an existing generated .tst."
        )
    )
    parser.add_argument("postman_collection", type=Path, help="Path to exported Postman Collection JSON.")
    parser.add_argument(
        "--api-base",
        default="http://localhost:9080/soavirt/api/v6",
        help="SOAVirt API base URL (default: http://localhost:9080/soavirt/api/v6).",
    )
    parser.add_argument(
        "--parent-id",
        default="/TestAssets",
        help="SOAVirt parent folder id where target .tst exists (default: /TestAssets).",
    )
    parser.add_argument(
        "--target-test-file-name",
        default=None,
        help="Optional target .tst test file name without extension. Defaults to collection name.",
    )
    parser.add_argument("--username", default=None, help="Optional SOAVirt basic-auth username.")
    parser.add_argument("--password", default=None, help="Optional SOAVirt basic-auth password.")
    parser.add_argument("--timeout-sec", type=int, default=60, help="HTTP timeout seconds (default: 60).")
    parser.add_argument("--orchestrator-internal", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not args.orchestrator_internal:
        print(
            "ERROR: This script is an internal conversion phase. Run the full orchestrator: "
            "skills/postman-to-soatest/scripts/postman_to_soatest.py",
            file=sys.stderr,
        )
        return 2

    if not args.postman_collection.exists():
        print(f"ERROR: Input file does not exist: {args.postman_collection}", file=sys.stderr)
        return 1

    try:
        collection = json.loads(args.postman_collection.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: Input is not valid JSON: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: Failed to read input file: {exc}", file=sys.stderr)
        return 1

    if not isinstance(collection, dict):
        print("ERROR: Postman collection root must be a JSON object.", file=sys.stderr)
        return 1

    info = collection.get("info") if isinstance(collection.get("info"), dict) else {}
    collection_name = str(info.get("name", "Postman Collection"))
    target_name = args.target_test_file_name or collection_name
    collection_overview_text = extract_collection_overview(collection)

    request_targets: List[RequestDocTarget] = []
    folder_targets: List[FolderDocTarget] = []
    walk_postman_items_with_docs(collection.get("item"), (), request_targets, folder_targets)

    client = SoaVirtClient(
        base_url=args.api_base,
        username=args.username,
        password=args.password,
        timeout_sec=args.timeout_sec,
    )

    try:
        tst_id = resolve_target_tst_id(client, parent_id=args.parent_id, test_file_name_without_ext=target_name)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        docs_applied, comments_applied, suites_updated, collection_overview_applied, folder_overviews_applied, warnings = apply_request_docs(
            client=client,
            tst_id=tst_id,
            request_targets=request_targets,
            folder_targets=folder_targets,
            collection_overview_text=collection_overview_text,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(f"Target .tst: {tst_id}")
    print(f"Collection overview discovered: {1 if collection_overview_text else 0}")
    print(f"Collection overview applied: {1 if collection_overview_applied else 0}")
    print(f"Folder overviews discovered: {len(folder_targets)}")
    print(f"Folder overviews applied: {folder_overviews_applied}")
    print(f"Requests with docs discovered: {len(request_targets)}")
    print(f"Request docs applied: {docs_applied}")
    print(f"Request comments applied: {comments_applied}")
    print(f"Suites updated: {suites_updated}")
    print(f"Warnings: {len(warnings)}")

    if warnings:
        print("Warning details:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
