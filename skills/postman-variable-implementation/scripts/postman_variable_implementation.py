#!/usr/bin/env python3
"""
Implement Postman variables into an existing SOAtest .tst.

Scope:
- Discover and classify Postman variables
- Create/update SOAtest environment variables only for external/pre-declared variables
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
from typing import Any, Dict, Iterable, List, Optional, Tuple

VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
VAR_CREATE_RE = re.compile(
    r"pm\.(environment|collectionVariables|variables)\.set\(\s*(['\"`])([^'\"`]+?)\2",
    re.DOTALL,
)

CLASS_EXTERNAL = "external/pre-declared"
CLASS_CREATED = "script-created"
CLASS_BOTH = "both referenced and script-created"


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name).strip().rstrip(". ")
    return value or "postman-collection"


def load_environment_values(environment_path: Path) -> Dict[str, str]:
    try:
        payload = json.loads(environment_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Environment file is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"Failed to read environment file: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("Environment file root must be a JSON object.")

    values = payload.get("values")
    if not isinstance(values, list):
        raise RuntimeError("Environment file is missing a 'values' array.")

    result: Dict[str, str] = {}
    for entry in values:
        if not isinstance(entry, dict):
            continue
        if not bool(entry.get("enabled", True)):
            continue

        key = str(entry.get("key", "")).strip()
        if not key:
            continue

        value = entry.get("value")
        result[key] = "" if value is None else str(value)

    return result


def load_collection_declared_values(collection: Dict[str, Any]) -> Dict[str, str]:
    variables = collection.get("variable")
    if not isinstance(variables, list):
        return {}

    result: Dict[str, str] = {}
    for entry in variables:
        if not isinstance(entry, dict):
            continue
        if bool(entry.get("disabled", False)):
            continue

        key = str(entry.get("key", "")).strip()
        if not key:
            continue

        value = entry.get("value")
        result[key] = "" if value is None else str(value)
    return result


def iter_strings(node: Any, path: str) -> Iterable[Tuple[str, str]]:
    if isinstance(node, str):
        yield path, node
        return
    if isinstance(node, list):
        for idx, child in enumerate(node):
            yield from iter_strings(child, f"{path}[{idx}]")
        return
    if isinstance(node, dict):
        for key, child in node.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from iter_strings(child, child_path)


def find_references(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    for match in VAR_REFERENCE_RE.finditer(text):
        name = match.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            found.append(name)
    return found


def normalize_url(url_obj: Any) -> str:
    if isinstance(url_obj, str):
        return url_obj
    if not isinstance(url_obj, dict):
        return ""

    raw = url_obj.get("raw")
    if isinstance(raw, str):
        return raw

    protocol = str(url_obj.get("protocol", "")).strip()
    host = url_obj.get("host")
    path = url_obj.get("path")

    host_text = ""
    if isinstance(host, list):
        host_text = ".".join(str(part) for part in host if part is not None)
    elif isinstance(host, str):
        host_text = host

    path_text = ""
    if isinstance(path, list):
        parts = [str(part).strip("/") for part in path if str(part).strip("/")]
        path_text = "/" + "/".join(parts) if parts else ""
    elif isinstance(path, str) and path.strip():
        path_text = "/" + path.lstrip("/")

    if protocol and host_text:
        return f"{protocol}://{host_text}{path_text}"
    return f"{host_text}{path_text}".strip()


def add_occurrence(
    bucket: Dict[str, List[Dict[str, Any]]],
    variable_name: str,
    occurrence: Dict[str, Any],
) -> None:
    if not variable_name:
        return
    bucket.setdefault(variable_name, []).append(occurrence)


def build_occurrence(
    variable_name: str,
    source_type: str,
    json_path: str,
    request_name: Optional[str],
    folder_hierarchy: List[str],
    request_order: Optional[int],
    order_key: int,
    creation_scope: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "variableName": variable_name,
        "sourceType": source_type,
        "jsonPath": json_path,
        "requestName": request_name,
        "folderHierarchy": folder_hierarchy,
        "requestOrder": request_order,
        "orderKey": order_key,
        "creationScope": creation_scope,
    }


def scan_text_block_for_references(
    text: str,
    references: Dict[str, List[Dict[str, Any]]],
    source_type: str,
    json_path: str,
    request_name: Optional[str],
    folder_hierarchy: List[str],
    request_order: Optional[int],
    order_key: int,
) -> None:
    for variable_name in find_references(text):
        add_occurrence(
            references,
            variable_name,
            build_occurrence(
                variable_name=variable_name,
                source_type=source_type,
                json_path=json_path,
                request_name=request_name,
                folder_hierarchy=folder_hierarchy,
                request_order=request_order,
                order_key=order_key,
            ),
        )


def scan_node_for_references(
    node: Any,
    references: Dict[str, List[Dict[str, Any]]],
    source_type: str,
    json_path: str,
    request_name: Optional[str],
    folder_hierarchy: List[str],
    request_order: Optional[int],
    order_key: int,
) -> None:
    for path, text in iter_strings(node, json_path):
        scan_text_block_for_references(
            text=text,
            references=references,
            source_type=source_type,
            json_path=path,
            request_name=request_name,
            folder_hierarchy=folder_hierarchy,
            request_order=request_order,
            order_key=order_key,
        )


def script_text_from_event(event: Dict[str, Any]) -> str:
    script = event.get("script")
    if not isinstance(script, dict):
        return ""

    exec_block = script.get("exec")
    if isinstance(exec_block, list):
        return "\n".join(str(line) for line in exec_block)
    if isinstance(exec_block, str):
        return exec_block
    return ""


def scan_script_event(
    event: Dict[str, Any],
    event_json_path: str,
    references: Dict[str, List[Dict[str, Any]]],
    created: Dict[str, List[Dict[str, Any]]],
    request_name: Optional[str],
    folder_hierarchy: List[str],
    request_order: Optional[int],
    order_key: int,
) -> None:
    listen = str(event.get("listen", "script")).strip().lower()
    script_text = script_text_from_event(event)
    if not script_text:
        return

    source_type = f"{listen}-script"
    script_path = f"{event_json_path}.script"

    scan_text_block_for_references(
        text=script_text,
        references=references,
        source_type=source_type,
        json_path=script_path,
        request_name=request_name,
        folder_hierarchy=folder_hierarchy,
        request_order=request_order,
        order_key=order_key,
    )

    seen_create_names: set[Tuple[str, str]] = set()
    for match in VAR_CREATE_RE.finditer(script_text):
        scope = match.group(1)
        variable_name = match.group(3)
        key = (scope, variable_name)
        if key in seen_create_names:
            continue
        seen_create_names.add(key)

        add_occurrence(
            created,
            variable_name,
            build_occurrence(
                variable_name=variable_name,
                source_type=source_type,
                json_path=script_path,
                request_name=request_name,
                folder_hierarchy=folder_hierarchy,
                request_order=request_order,
                order_key=order_key,
                creation_scope=scope,
            ),
        )


def analyze_request(
    item: Dict[str, Any],
    item_path: str,
    request_order: int,
    folder_hierarchy: List[str],
    references: Dict[str, List[Dict[str, Any]]],
    created: Dict[str, List[Dict[str, Any]]],
) -> None:
    request_name = str(item.get("name", f"Request {request_order}"))
    request_obj = item.get("request")
    if not isinstance(request_obj, dict):
        return

    base_order = request_order * 10

    item_events = item.get("event") if isinstance(item.get("event"), list) else []
    for idx, event in enumerate(item_events):
        if isinstance(event, dict):
            scan_script_event(
                event=event,
                event_json_path=f"{item_path}.event[{idx}]",
                references=references,
                created=created,
                request_name=request_name,
                folder_hierarchy=folder_hierarchy,
                request_order=request_order,
                order_key=base_order,
            )

    request_events = request_obj.get("event") if isinstance(request_obj.get("event"), list) else []
    for idx, event in enumerate(request_events):
        if isinstance(event, dict):
            scan_script_event(
                event=event,
                event_json_path=f"{item_path}.request.event[{idx}]",
                references=references,
                created=created,
                request_name=request_name,
                folder_hierarchy=folder_hierarchy,
                request_order=request_order,
                order_key=base_order,
            )

    url_obj = request_obj.get("url")
    if url_obj is not None:
        normalized = normalize_url(url_obj)
        if normalized:
            scan_text_block_for_references(
                text=normalized,
                references=references,
                source_type="request-url",
                json_path=f"{item_path}.request.url",
                request_name=request_name,
                folder_hierarchy=folder_hierarchy,
                request_order=request_order,
                order_key=base_order + 1,
            )
        scan_node_for_references(
            node=url_obj,
            references=references,
            source_type="request-url",
            json_path=f"{item_path}.request.url",
            request_name=request_name,
            folder_hierarchy=folder_hierarchy,
            request_order=request_order,
            order_key=base_order + 1,
        )

    if "body" in request_obj:
        scan_node_for_references(
            node=request_obj.get("body"),
            references=references,
            source_type="request-body",
            json_path=f"{item_path}.request.body",
            request_name=request_name,
            folder_hierarchy=folder_hierarchy,
            request_order=request_order,
            order_key=base_order + 1,
        )

    if "header" in request_obj:
        scan_node_for_references(
            node=request_obj.get("header"),
            references=references,
            source_type="request-header",
            json_path=f"{item_path}.request.header",
            request_name=request_name,
            folder_hierarchy=folder_hierarchy,
            request_order=request_order,
            order_key=base_order + 1,
        )

    if "auth" in request_obj:
        scan_node_for_references(
            node=request_obj.get("auth"),
            references=references,
            source_type="request-auth",
            json_path=f"{item_path}.request.auth",
            request_name=request_name,
            folder_hierarchy=folder_hierarchy,
            request_order=request_order,
            order_key=base_order + 1,
        )


def walk_items(
    items: Any,
    item_path: str,
    folder_hierarchy: List[str],
    state: Dict[str, int],
    references: Dict[str, List[Dict[str, Any]]],
    created: Dict[str, List[Dict[str, Any]]],
) -> None:
    if not isinstance(items, list):
        return

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            continue

        current_item_path = f"{item_path}[{idx}]"
        item_name = str(item.get("name", f"Item {idx + 1}"))

        nested_items = item.get("item")
        if isinstance(nested_items, list):
            walk_items(
                items=nested_items,
                item_path=f"{current_item_path}.item",
                folder_hierarchy=[*folder_hierarchy, item_name],
                state=state,
                references=references,
                created=created,
            )

        if isinstance(item.get("request"), dict):
            state["request_order"] += 1
            analyze_request(
                item=item,
                item_path=current_item_path,
                request_order=state["request_order"],
                folder_hierarchy=folder_hierarchy,
                references=references,
                created=created,
            )


def dedupe_occurrences(occurrences: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[Tuple[Any, ...]] = set()
    unique: List[Dict[str, Any]] = []
    for occ in sorted(
        occurrences,
        key=lambda value: (
            value.get("orderKey", 10**9),
            value.get("requestOrder") if value.get("requestOrder") is not None else 10**9,
            str(value.get("requestName", "")),
            str(value.get("jsonPath", "")),
            str(value.get("sourceType", "")),
            str(value.get("creationScope", "")),
        ),
    ):
        key = (
            occ.get("requestOrder"),
            occ.get("requestName"),
            tuple(occ.get("folderHierarchy", [])),
            occ.get("sourceType"),
            occ.get("jsonPath"),
            occ.get("creationScope"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(occ)
    return unique


def visible_occurrence(occ: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        "sourceType": occ.get("sourceType"),
        "jsonPath": occ.get("jsonPath"),
        "requestName": occ.get("requestName"),
        "folderHierarchy": occ.get("folderHierarchy"),
        "requestOrder": occ.get("requestOrder"),
    }
    creation_scope = occ.get("creationScope")
    if creation_scope:
        result["creationScope"] = creation_scope
    return result


def classify_variable(
    variable_name: str,
    references: Dict[str, List[Dict[str, Any]]],
    created: Dict[str, List[Dict[str, Any]]],
    collection_declared: Dict[str, str],
) -> Tuple[str, bool, str]:
    has_ref = variable_name in references and bool(references[variable_name])
    has_create = variable_name in created and bool(created[variable_name])
    has_collection_declared = variable_name in collection_declared

    if has_ref and has_create:
        return (
            CLASS_BOTH,
            False,
            "Referenced and script-created in collection scripts.",
        )

    if has_create:
        return (
            CLASS_CREATED,
            False,
            "Created dynamically in pre-request or test scripts.",
        )

    if has_ref and has_collection_declared:
        return (
            CLASS_EXTERNAL,
            False,
            "Referenced with {{...}} and declared at collection level.",
        )

    if has_collection_declared:
        return (
            CLASS_EXTERNAL,
            False,
            "Declared at collection level.",
        )

    return (
        CLASS_EXTERNAL,
        True,
        "Referenced with {{...}} but not created by collection scripts.",
    )


def build_report(collection: Dict[str, Any], input_file: Path) -> Dict[str, Any]:
    references: Dict[str, List[Dict[str, Any]]] = {}
    created: Dict[str, List[Dict[str, Any]]] = {}
    collection_declared = load_collection_declared_values(collection)

    collection_events = collection.get("event") if isinstance(collection.get("event"), list) else []
    for idx, event in enumerate(collection_events):
        if not isinstance(event, dict):
            continue

        listen = str(event.get("listen", "script")).strip().lower()
        if listen == "prerequest":
            order_key = 0
        elif listen == "test":
            order_key = 999999
        else:
            order_key = 500000

        scan_script_event(
            event=event,
            event_json_path=f"event[{idx}]",
            references=references,
            created=created,
            request_name=None,
            folder_hierarchy=[],
            request_order=None,
            order_key=order_key,
        )

    if "auth" in collection:
        scan_node_for_references(
            node=collection.get("auth"),
            references=references,
            source_type="collection-auth",
            json_path="auth",
            request_name=None,
            folder_hierarchy=[],
            request_order=None,
            order_key=1,
        )

    state = {"request_order": 0}
    walk_items(
        items=collection.get("item"),
        item_path="item",
        folder_hierarchy=[],
        state=state,
        references=references,
        created=created,
    )

    variable_names = sorted(set(references.keys()) | set(created.keys()) | set(collection_declared.keys()))

    variables: List[Dict[str, Any]] = []
    for variable_name in variable_names:
        ref_entries = dedupe_occurrences(references.get(variable_name, []))
        create_entries = dedupe_occurrences(created.get(variable_name, []))

        classification, manual_setup, manual_reason = classify_variable(
            variable_name=variable_name,
            references=references,
            created=created,
            collection_declared=collection_declared,
        )

        variables.append(
            {
                "variableName": variable_name,
                "classification": classification,
                "likelyRequiresManualSetup": manual_setup,
                "manualSetupReason": manual_reason,
                "declaredAtCollectionLevel": variable_name in collection_declared,
                "referencedCount": len(ref_entries),
                "createdCount": len(create_entries),
                "referencedAt": [visible_occurrence(entry) for entry in ref_entries],
                "createdAt": [visible_occurrence(entry) for entry in create_entries],
            }
        )

    summary = {
        "totalVariables": len(variables),
        "externalPreDeclared": sum(1 for entry in variables if entry["classification"] == CLASS_EXTERNAL),
        "scriptCreated": sum(1 for entry in variables if entry["classification"] == CLASS_CREATED),
        "bothReferencedAndScriptCreated": sum(1 for entry in variables if entry["classification"] == CLASS_BOTH),
        "collectionDeclared": len(collection_declared),
        "totalRequestsScanned": state["request_order"],
    }

    info = collection.get("info") if isinstance(collection.get("info"), dict) else {}
    collection_name = str(info.get("name", "Postman Collection"))

    return {
        "collectionName": collection_name,
        "inputFile": input_file.name,
        "summary": summary,
        "variables": variables,
    }


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

    def create_environment(self, parent_suite_id: str, name: str, variables: List[Dict[str, str]]) -> Dict[str, Any]:
        body = {
            "parent": {"id": parent_suite_id},
            "local": {"name": name, "active": True, "variables": variables},
        }
        return self._request("POST", "/environments", body=body)

    def update_environment(self, environment_id: str, name: str, variables: List[Dict[str, str]]) -> Dict[str, Any]:
        body = {"local": {"name": name, "active": True, "variables": variables}}
        return self._request("PUT", "/environments", query={"id": environment_id}, body=body)


def resolve_target_tst_and_top_suite(
    client: SoaVirtClient,
    parent_id: str,
    test_file_name_without_ext: str,
) -> Tuple[str, str]:
    target_name = f"{sanitize_filename(test_file_name_without_ext)}.tst"
    children = client.get_children(parent_id=parent_id)

    tst_id = ""
    for child in children:
        if str(child.get("name", "")).lower() == target_name.lower():
            tst_id = str(child.get("id", ""))
            break

    if not tst_id:
        raise RuntimeError(
            f"Target .tst not found: {parent_id}/{target_name}. "
            "Create the endpoint sequence first."
        )

    suite_children = client.get_children(parent_id=tst_id, resource_type="testSuite")
    if not suite_children:
        suite_children = client.get_children(parent_id=tst_id)

    top_suite_id = ""
    for child in suite_children:
        if str(child.get("type", "")).lower() in {"testsuite", "testSuite".lower()}:
            top_suite_id = str(child.get("id", ""))
            break
    if not top_suite_id and suite_children:
        top_suite_id = str(suite_children[0].get("id", ""))

    if not top_suite_id:
        raise RuntimeError("Could not resolve top-level Test Suite for target .tst.")

    return tst_id, top_suite_id


def upsert_environment(
    client: SoaVirtClient,
    top_suite_id: str,
    environment_name: str,
    external_variable_names: List[str],
    exported_environment_values: Dict[str, str],
    collection_declared_values: Dict[str, str],
    placeholder: str,
) -> Tuple[str, List[str]]:
    fallback_names: List[str] = []
    variables_payload: List[Dict[str, str]] = []
    for name in external_variable_names:
        value = exported_environment_values.get(name, "")
        if str(value).strip() == "":
            value = collection_declared_values.get(name, "")
        if str(value).strip() == "":
            value = placeholder
            fallback_names.append(name)
        variables_payload.append({"name": name, "value": value})

    existing_envs = client.get_children(parent_id=top_suite_id, resource_type="environment")
    for env in existing_envs:
        if str(env.get("name", "")).lower() == environment_name.lower():
            env_id = str(env.get("id", ""))
            if not env_id:
                continue
            client.update_environment(env_id, environment_name, variables_payload)
            return env_id, fallback_names

    created = client.create_environment(top_suite_id, environment_name, variables_payload)
    created_id = str(created.get("id", ""))
    if not created_id:
        raise RuntimeError("SOAVirt API did not return environment id after create.")
    return created_id, fallback_names


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover Postman variables and implement external/pre-declared ones in existing SOAtest .tst environment."
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
        help="Target .tst test file name without extension. Defaults to collection name.",
    )
    parser.add_argument(
        "--environment-name",
        default="Default Environment",
        help="SOAtest environment name to create or update (default: Default Environment).",
    )
    parser.add_argument(
        "--postman-environment",
        type=Path,
        default=None,
        help=(
            "Optional path to exported Postman environment JSON containing shared/non-empty values. "
            "If omitted, collection-declared values and placeholder fallbacks are used."
        ),
    )
    parser.add_argument(
        "--placeholder",
        default="SET_ME",
        help="Fallback value used when external variables are missing/empty in the environment export (default: SET_ME).",
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

    input_path = args.postman_collection
    if not input_path.exists():
        print(f"ERROR: Input file does not exist: {input_path}", file=sys.stderr)
        return 1
    if args.postman_environment is not None and not args.postman_environment.exists():
        print(f"ERROR: Postman environment file does not exist: {args.postman_environment}", file=sys.stderr)
        return 1

    try:
        collection = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: Input is not valid JSON: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"ERROR: Failed to read input file: {exc}", file=sys.stderr)
        return 1

    if not isinstance(collection, dict):
        print("ERROR: Postman collection root must be a JSON object.", file=sys.stderr)
        return 1

    collection_declared_values = load_collection_declared_values(collection)
    report = build_report(collection, input_file=input_path)

    external_names = [
        entry["variableName"]
        for entry in report.get("variables", [])
        if isinstance(entry, dict) and entry.get("classification") == CLASS_EXTERNAL
    ]

    exported_environment_values: Dict[str, str] = {}
    if args.postman_environment is not None:
        try:
            exported_environment_values = load_environment_values(args.postman_environment)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    info = collection.get("info") if isinstance(collection.get("info"), dict) else {}
    default_name = str(info.get("name", "Postman Collection"))
    target_test_file_name = args.target_test_file_name or default_name

    client = SoaVirtClient(
        base_url=args.api_base,
        username=args.username,
        password=args.password,
        timeout_sec=args.timeout_sec,
    )

    try:
        tst_id, top_suite_id = resolve_target_tst_and_top_suite(
            client=client,
            parent_id=args.parent_id,
            test_file_name_without_ext=target_test_file_name,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    env_id = ""
    fallback_names: List[str] = []
    if external_names:
        try:
            env_id, fallback_names = upsert_environment(
                client=client,
                top_suite_id=top_suite_id,
                environment_name=args.environment_name,
                external_variable_names=external_names,
                exported_environment_values=exported_environment_values,
                collection_declared_values=collection_declared_values,
                placeholder=args.placeholder,
            )
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 3

    summary = report.get("summary", {})
    print(f"Target .tst: {tst_id}")
    print(f"Top suite id: {top_suite_id}")
    if env_id:
        print(f"Environment created/updated: {env_id}")
    else:
        print("Environment update skipped: no external/pre-declared variables were discovered.")
    if args.postman_environment is not None:
        print(f"Environment value source: {args.postman_environment}")
    else:
        print("Environment value source: none")
        print("Note: No Postman environment file was provided.")
    print(f"External/pre-declared variables implemented: {len(external_names)}")
    print(f"Collection-level variables discovered: {len(collection_declared_values)}")
    if fallback_names:
        print(
            f"Fallback applied ({args.placeholder}) for missing/empty values ({len(fallback_names)}): "
            f"{', '.join(sorted(fallback_names))}"
        )
    print(f"Script-created variables discovered (not implemented): {summary.get('scriptCreated', 0)}")
    print(f"Both referenced and script-created discovered (not implemented): {summary.get('bothReferencedAndScriptCreated', 0)}")

    # Backward-compatible summary lines consumed by orchestrator/report surfaces.
    print(f"Variables: implemented {len(external_names)} external/pre-declared variables")
    print(
        "Variables discovered but not implemented: "
        f"{summary.get('scriptCreated', 0)} script-created, "
        f"{summary.get('bothReferencedAndScriptCreated', 0)} both referenced+script-created"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
