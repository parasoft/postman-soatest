#!/usr/bin/env python3
"""
Create SOAtest .tst endpoint sequence from Postman Collection.

Scope:
- Keep exactly one runtime-generated .tst in the target parent folder per run
- Preserve collection/folder/request order and method/url mapping
- Do not create SOAtest environment variables
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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SUPPORTED_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "TRACE"}
VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
SCHEMA_VERSION_RE = re.compile(r"(?m)^schemaVersion:\s*.+$")
TARGET_SCHEMA_VERSION = "13"


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip().rstrip(". ")
    return value or "postman-collection"


def normalize_tst_file_stem(name: str) -> str:
    sanitized = sanitize_filename(str(name or "").strip())
    if sanitized.lower().endswith(".tst"):
        sanitized = sanitize_filename(sanitized[:-4])
    return sanitized


def normalize_postman_display_name(name: Any, fallback: str) -> str:
    value = str(name) if name is not None else ""
    return value if value != "" else fallback


def prompt_for_test_file_name(default_name: str) -> str:
    while True:
        raw = input(f"Enter .tst test file name [{default_name}]: ").strip()
        candidate = raw or default_name
        sanitized = normalize_tst_file_stem(candidate)
        if not sanitized:
            print("Please enter a valid test file name.", file=sys.stderr)
            continue
        if sanitized != candidate:
            print(f"Using sanitized test file name: {sanitized}")
        return sanitized


def confirm_overwrite(file_name: str, parent_id: str) -> bool:
    answer = input(
        f"Runtime-generated test file exists: {parent_id}/{file_name}. Replace this test file? [y/N]: "
    ).strip().lower()
    return answer in {"y", "yes"}


def convert_postman_variable_syntax(text: str) -> str:
    if not text:
        return text
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", text)


def get_request_url(request_obj: Dict[str, Any]) -> str:
    url = request_obj.get("url")
    if isinstance(url, str):
        return convert_postman_variable_syntax(url)
    if not isinstance(url, dict):
        return ""

    raw = url.get("raw")
    if isinstance(raw, str) and raw.strip():
        return convert_postman_variable_syntax(raw)

    host = url.get("host")
    path = url.get("path")
    protocol = url.get("protocol")

    host_text = ""
    if isinstance(host, list):
        if len(host) == 1:
            host_text = str(host[0])
        elif host:
            host_text = ".".join(str(part) for part in host if part is not None)
    elif isinstance(host, str):
        host_text = host

    path_text = ""
    if isinstance(path, list):
        parts = [str(part).strip("/") for part in path if str(part).strip("/")]
        path_text = "/" + "/".join(parts) if parts else ""
    elif isinstance(path, str) and path.strip():
        path_text = "/" + path.lstrip("/")

    endpoint = ""
    if protocol and host_text and "://" not in host_text:
        endpoint = f"{protocol}://{host_text}{path_text}"
    elif host_text and path_text:
        endpoint = f"{host_text.rstrip('/')}{path_text}"
    else:
        endpoint = host_text or path_text
    return convert_postman_variable_syntax(endpoint)


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

    def create_tst(self, file_name_without_ext: str, parent_id: str) -> Dict[str, Any]:
        body = {"name": file_name_without_ext, "parent": {"id": parent_id}}
        return self._request("POST", "/files/tsts", body=body)

    def create_suite(self, parent_suite_id: str, suite_name: str) -> Dict[str, Any]:
        body = {"parent": {"id": parent_suite_id}, "name": suite_name}
        return self._request("POST", "/suites/testSuites", body=body)

    def rename_suite(self, suite_id: str, suite_name: str) -> Dict[str, Any]:
        body = {"name": suite_name}
        return self._request("PUT", "/suites/testSuites", query={"id": suite_id}, body=body)

    def create_rest_client(self, parent_suite_id: str, request_name: str, method: str, endpoint: str) -> Dict[str, Any]:
        method_upper = method.upper()
        method_type = method_upper if method_upper in SUPPORTED_HTTP_METHODS else "CUSTOM"

        body = {
            "parent": {"id": parent_suite_id},
            "name": request_name,
            "header": {"method": {"methodType": method_type, "value": method_upper}},
            "resource": {"type": "literalText", "literalText": {"fixed": endpoint}},
            "httpOptions": {
                "transport": {
                    "type": "http10",
                    "http10": {
                        "generalSettings": {"followHttpRedirects": True},
                        "security": {},
                        "httpHeaders": {"type": "literal", "literal": ""},
                    },
                }
            },
            "payload": {
                "inputMode": "literal",
                "input": {"literal": {"text": ""}},
                "contentType": "application/json",
            },
        }
        return self._request("POST", "/tools/restClients", body=body)

    def delete_file(self, file_id: str, recursive: bool = True) -> Dict[str, Any]:
        query: Dict[str, str] = {"id": file_id}
        if recursive:
            query["recursive"] = "true"
        return self._request("DELETE", "/files", query=query)


class RuntimeGeneratedTracker:
    def __init__(self) -> None:
        self._generated_tst_ids: set[str] = set()

    def remember_generated_tst(self, file_id: str) -> None:
        normalized = file_id.strip()
        if normalized:
            self._generated_tst_ids.add(normalized)

    def is_runtime_generated(self, file_id: str) -> bool:
        normalized = file_id.strip()
        return bool(normalized) and normalized in self._generated_tst_ids


def delete_runtime_generated_tst_files_in_parent(
    client: SoaVirtClient, parent_id: str, tracker: RuntimeGeneratedTracker
) -> None:
    existing = client.get_children(parent_id=parent_id)
    for child in existing:
        child_name = str(child.get("name", ""))
        child_id = str(child.get("id", ""))
        if child_name.lower().endswith(".tst") and child_id and tracker.is_runtime_generated(child_id):
            client.delete_file(file_id=child_id, recursive=True)


def find_existing_target(client: SoaVirtClient, parent_id: str, file_name: str) -> Optional[Dict[str, Any]]:
    for child in client.get_children(parent_id=parent_id):
        child_name = str(child.get("name", ""))
        if child_name.lower() == file_name.lower():
            return child if isinstance(child, dict) else None
    return None


def resolve_target_file_stem(
    client: SoaVirtClient,
    parent_id: str,
    default_name: str,
    test_file_name_override: Optional[str],
    tracker: RuntimeGeneratedTracker,
    skip_collision_check: bool = False,
) -> str:
    candidate = normalize_tst_file_stem((test_file_name_override or "").strip()) if test_file_name_override else ""

    if skip_collision_check:
        if candidate:
            return candidate
        raise RuntimeError(
            "Internal error: skip_collision_check requires --test-file-name so name resolution occurs only once in the orchestrator."
        )

    while True:
        if not candidate:
            candidate = prompt_for_test_file_name(default_name)

        file_name = f"{candidate}.tst"
        existing = find_existing_target(client, parent_id=parent_id, file_name=file_name)
        if existing is None:
            return candidate

        existing_id = str(existing.get("id", ""))
        if tracker.is_runtime_generated(existing_id):
            if confirm_overwrite(file_name=file_name, parent_id=parent_id):
                delete_runtime_generated_tst_files_in_parent(client, parent_id=parent_id, tracker=tracker)
                return candidate
        else:
            print(
                "Guard rail: existing target was not generated during this runtime and will not be deleted.",
                file=sys.stderr,
            )

        candidate = ""
        print("Please provide a different test file name.")


def create_nodes_from_postman_items(client: SoaVirtClient, items: List[Dict[str, Any]], parent_suite_id: str) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = normalize_postman_display_name(item.get("name"), "Unnamed")
        nested_items = item.get("item")
        request_obj = item.get("request")

        if isinstance(nested_items, list):
            created_suite = client.create_suite(parent_suite_id=parent_suite_id, suite_name=item_name)
            suite_id = str(created_suite.get("id", ""))
            if not suite_id:
                raise RuntimeError(f"SOAVirt API did not return suite id for folder '{item_name}'.")

            renamed_suite = client.rename_suite(suite_id=suite_id, suite_name=item_name)
            suite_id = str(renamed_suite.get("id", "")) or suite_id

            create_nodes_from_postman_items(client, nested_items, suite_id)

        if isinstance(request_obj, dict):
            method = str(request_obj.get("method", "GET")).upper()
            endpoint = get_request_url(request_obj)
            client.create_rest_client(
                parent_suite_id=parent_suite_id,
                request_name=item_name,
                method=method,
                endpoint=endpoint,
            )


def convert_collection(
    collection: Dict[str, Any],
    client: SoaVirtClient,
    parent_id: str,
    test_file_name_override: Optional[str] = None,
    tracker: Optional[RuntimeGeneratedTracker] = None,
    skip_name_collision_check: bool = False,
) -> Tuple[str, str, str]:
    runtime_tracker = tracker or RuntimeGeneratedTracker()
    info = collection.get("info", {}) if isinstance(collection.get("info"), dict) else {}
    collection_display_name = normalize_postman_display_name(info.get("name"), "Postman Collection")
    collection_file_stem = sanitize_filename(collection_display_name)
    file_stem = resolve_target_file_stem(
        client=client,
        parent_id=parent_id,
        default_name=collection_file_stem,
        test_file_name_override=test_file_name_override,
        tracker=runtime_tracker,
        skip_collision_check=skip_name_collision_check,
    )

    # Guard rail: only prune .tst files that were generated by this process runtime.
    delete_runtime_generated_tst_files_in_parent(client, parent_id=parent_id, tracker=runtime_tracker)

    created_tst = client.create_tst(file_name_without_ext=file_stem, parent_id=parent_id)
    tst_id = str(created_tst.get("id", ""))
    if not tst_id:
        raise RuntimeError("SOAVirt API did not return .tst id after creation.")
    runtime_tracker.remember_generated_tst(tst_id)

    children_rel = (
        created_tst.get("relationships", {}).get("childrenRel", [])
        if isinstance(created_tst.get("relationships"), dict)
        else []
    )
    if not isinstance(children_rel, list) or not children_rel:
        raise RuntimeError("SOAVirt API did not return top-level suite relationship for created .tst.")

    top_suite_id = ""
    for child in children_rel:
        if isinstance(child, dict) and child.get("type") == "testSuite":
            top_suite_id = str(child.get("id", ""))
            break
    if not top_suite_id:
        top_suite_id = str(children_rel[0].get("id", "")) if isinstance(children_rel[0], dict) else ""
    if not top_suite_id:
        raise RuntimeError("Could not resolve top-level Test Suite id for created .tst.")

    renamed_top_suite = client.rename_suite(suite_id=top_suite_id, suite_name=collection_display_name)
    top_suite_id = str(renamed_top_suite.get("id", "")) or top_suite_id

    top_items = collection.get("item", [])
    if isinstance(top_items, list):
        create_nodes_from_postman_items(client, top_items, parent_suite_id=top_suite_id)

    return tst_id, top_suite_id, file_stem


def expected_tst_path(workspace_root: Path, parent_id: str, file_stem: str) -> Path:
    relative_parent = parent_id.replace("\\", "/").strip("/")
    parts = [p for p in relative_parent.split("/") if p]
    return workspace_root.joinpath(*parts, f"{file_stem}.tst")


def expected_local_parent_path(workspace_root: Path, parent_id: str) -> Path:
    relative_parent = parent_id.replace("\\", "/").strip("/")
    parts = [p for p in relative_parent.split("/") if p]
    return workspace_root.joinpath(*parts)


def looks_like_soatest_yaml(tst_path: Path) -> bool:
    try:
        text = tst_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False

    if not text.startswith("---"):
        return False

    required_headers = ("parasoftVersion:", "productVersion:", "schemaVersion:", "suite:")
    return all(header in text for header in required_headers)


def force_schema_version(tst_path: Path, target_schema_version: str = TARGET_SCHEMA_VERSION) -> None:
    try:
        text = tst_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to read .tst for schema normalization: {exc}") from exc

    if not SCHEMA_VERSION_RE.search(text):
        raise RuntimeError("schemaVersion header not found while normalizing generated .tst.")

    updated = SCHEMA_VERSION_RE.sub(f"schemaVersion: {target_schema_version}", text, count=1)
    if updated != text:
        try:
            tst_path.write_text(updated, encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"Failed to write .tst after schema normalization: {exc}") from exc


def read_schema_version(tst_path: Path) -> Optional[str]:
    try:
        text = tst_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to read .tst while verifying schemaVersion: {exc}") from exc

    match = SCHEMA_VERSION_RE.search(text)
    if not match:
        return None

    value = match.group(0).split(":", 1)[1].strip()
    return value or None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create SOAtest endpoint sequence .tst from Postman Collection JSON via SOAVirt API."
    )
    parser.add_argument("postman_collection", type=Path, help="Path to exported Postman Collection JSON file.")
    parser.add_argument(
        "--api-base",
        default="http://localhost:9080/soavirt/api/v6",
        help="SOAVirt API base URL (default: http://localhost:9080/soavirt/api/v6).",
    )
    parser.add_argument(
        "--parent-id",
        default="/TestAssets",
        help="SOAVirt parent folder id where the new .tst will be created (default: /TestAssets).",
    )
    parser.add_argument("--username", default=None, help="Optional SOAVirt basic-auth username.")
    parser.add_argument("--password", default=None, help="Optional SOAVirt basic-auth password.")
    parser.add_argument("--timeout-sec", type=int, default=60, help="HTTP timeout seconds (default: 60).")
    parser.add_argument(
        "--test-file-name",
        default=None,
        help="Optional override for the generated .tst test file name (without extension).",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=(
            "Local SOAtest workspace root used for post-create schema/format checks. "
            "Required; no auto-discovery is performed."
        ),
    )
    parser.add_argument("--skip-name-collision-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--orchestrator-internal", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not args.orchestrator_internal:
        print(
            "ERROR: This script is an internal conversion phase. Run the full orchestrator: "
            "skills/postman-to-soatest/scripts/postman_to_soatest.py",
            file=sys.stderr,
        )
        return 2

    if args.workspace_root is None:
        print(
            "ERROR: Workspace root is required for endpoint sequence validation. "
            "Provide --workspace-root \"<soatest-workspace-root>\" and run again.",
            file=sys.stderr,
        )
        return 1
    if not args.workspace_root.exists() or not args.workspace_root.is_dir():
        print(
            f"ERROR: Workspace root does not exist or is not a directory: {args.workspace_root}",
            file=sys.stderr,
        )
        return 1

    local_parent_dir = expected_local_parent_path(args.workspace_root, args.parent_id)
    if not local_parent_dir.exists() or not local_parent_dir.is_dir():
        hint = ""
        normalized_parent_parts = [p for p in args.parent_id.replace("\\", "/").strip("/").split("/") if p]
        if normalized_parent_parts and args.workspace_root.name.lower() == normalized_parent_parts[-1].lower():
            hint = (
                " Workspace root appears to include the parent folder already; "
                "pass the SOAtest workspace root (one level above the parent folder)."
            )
        print(
            "ERROR: Workspace root does not map to the expected local parent folder for generated .tst files. "
            f"Expected local parent path: {local_parent_dir}.{hint}",
            file=sys.stderr,
        )
        return 1

    input_path = args.postman_collection
    if not input_path.exists():
        print(f"ERROR: Input file does not exist: {input_path}", file=sys.stderr)
        return 1

    try:
        collection = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: Input is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(collection, dict):
        print("ERROR: Postman collection root must be a JSON object.", file=sys.stderr)
        return 1

    client = SoaVirtClient(
        base_url=args.api_base,
        username=args.username,
        password=args.password,
        timeout_sec=args.timeout_sec,
    )

    runtime_tracker = RuntimeGeneratedTracker()

    def cleanup_runtime_generated() -> None:
        try:
            delete_runtime_generated_tst_files_in_parent(client, parent_id=args.parent_id, tracker=runtime_tracker)
        except RuntimeError as exc:
            print(f"WARNING: Failed to clean up runtime-generated .tst after failure: {exc}", file=sys.stderr)

    try:
        tst_id, top_suite_id, file_stem = convert_collection(
            collection,
            client=client,
            parent_id=args.parent_id,
            test_file_name_override=args.test_file_name,
            tracker=runtime_tracker,
            skip_name_collision_check=args.skip_name_collision_check,
        )
    except RuntimeError as exc:
        cleanup_runtime_generated()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    local_tst = expected_tst_path(args.workspace_root, args.parent_id, file_stem)
    print(f"Local path (expected): {local_tst}")
    if not local_tst.exists():
        cleanup_runtime_generated()
        print(f"ERROR: Generated .tst not found for endpoint sequence validation: {local_tst}", file=sys.stderr)
        return 3

    try:
        force_schema_version(local_tst, TARGET_SCHEMA_VERSION)
        persisted_schema = read_schema_version(local_tst)
        if persisted_schema != TARGET_SCHEMA_VERSION:
            raise RuntimeError(
                "schemaVersion did not persist after endpoint-sequence enforcement. "
                f"Expected '{TARGET_SCHEMA_VERSION}', found '{persisted_schema}'."
            )
    except RuntimeError as exc:
        cleanup_runtime_generated()
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    if not looks_like_soatest_yaml(local_tst):
        cleanup_runtime_generated()
        print("ERROR: Generated .tst is missing required SOAtest YAML headers after endpoint sequence.", file=sys.stderr)
        return 3

    print(f"Selected test file name: {file_stem}")
    print(f"Created importable SOAtest file via API: {tst_id}")
    print(f"Top suite id: {top_suite_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

