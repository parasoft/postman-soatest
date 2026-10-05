#!/usr/bin/env python3
"""
Implement Postman query parameters into an existing SOAtest .tst.

Scope:
- Read request.url.query key/value pairs from Postman requests.
- Apply query key/value pairs to matching SOAtest REST Client URLs.
- Ignore Postman query parameter descriptions.
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def convert_postman_variable_syntax(text: str) -> str:
    if text is None:
        return ""
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", str(text))


@dataclass(frozen=True)
class QueryParam:
    key: str
    value: Optional[str]


@dataclass(frozen=True)
class RequestQueryTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    has_query_field: bool
    query_params: Tuple[QueryParam, ...]


def extract_query_params(request_obj: Dict[str, Any]) -> Tuple[bool, Tuple[QueryParam, ...]]:
    url = request_obj.get("url")
    if not isinstance(url, dict):
        return False, ()

    if "query" not in url:
        return False, ()

    raw_query = url.get("query")
    if not isinstance(raw_query, list):
        return True, ()

    params: List[QueryParam] = []
    for entry in raw_query:
        if not isinstance(entry, dict):
            continue
        if bool(entry.get("disabled", False)):
            continue

        key = convert_postman_variable_syntax(entry.get("key", ""))
        if not key:
            continue

        value: Optional[str]
        if "value" in entry and entry.get("value") is not None:
            value = convert_postman_variable_syntax(entry.get("value"))
        else:
            value = None

        params.append(QueryParam(key=key, value=value))

    return True, tuple(params)


def walk_postman_requests_with_query(
    items: Any,
    folder_path: Tuple[str, ...],
    targets: List[RequestQueryTarget],
) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get("name", "Unnamed"))
        nested = item.get("item")
        if isinstance(nested, list):
            walk_postman_requests_with_query(
                items=nested,
                folder_path=folder_path + (item_name,),
                targets=targets,
            )

        request_obj = item.get("request")
        if isinstance(request_obj, dict):
            has_query_field, params = extract_query_params(request_obj)
            targets.append(
                RequestQueryTarget(
                    folder_path=folder_path,
                    request_name=item_name,
                    has_query_field=has_query_field,
                    query_params=params,
                )
            )


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

    def get_rest_client(self, rest_client_id: str) -> Dict[str, Any]:
        return self._request("GET", "/tools/restClients", query={"id": rest_client_id})

    def update_rest_client(self, rest_client_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", "/tools/restClients", query={"id": rest_client_id}, body=body)


def resolve_target_tst_id(client: SoaVirtClient, parent_id: str, test_file_name_without_ext: str) -> str:
    target_name = f"{sanitize_filename(test_file_name_without_ext)}.tst"
    for child in client.get_children(parent_id=parent_id):
        if str(child.get("name", "")).lower() == target_name.lower():
            resource_id = str(child.get("id", ""))
            if resource_id:
                return resource_id
    raise RuntimeError(f"Target .tst not found: {parent_id}/{target_name}")


def build_rest_client_lookup(descendants_payload: Dict[str, Any]) -> Dict[Tuple[Tuple[str, ...], str], List[str]]:
    lookup: Dict[Tuple[Tuple[str, ...], str], List[str]] = {}

    def walk_nodes(nodes: Sequence[Dict[str, Any]], folder_path: Tuple[str, ...]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = str(node.get("type", ""))
            node_name = str(node.get("name", ""))

            if node_type == "testSuite":
                children = node.get("children")
                if isinstance(children, list):
                    walk_nodes(children, folder_path + (node_name,))
                continue

            if node_type == "restClient":
                key = (folder_path, node_name)
                lookup.setdefault(key, []).append(str(node.get("id", "")))

    root_children = descendants_payload.get("children")
    if not isinstance(root_children, list):
        return lookup

    for top in root_children:
        if not isinstance(top, dict):
            continue
        if str(top.get("type", "")) != "testSuite":
            continue
        children = top.get("children")
        if isinstance(children, list):
            walk_nodes(children, ())

    return lookup


def get_current_rest_url(rest_client: Dict[str, Any]) -> str:
    resource = rest_client.get("resource")
    if isinstance(resource, dict):
        literal = resource.get("literalText")
        if isinstance(literal, dict):
            fixed = literal.get("fixed")
            if isinstance(fixed, str):
                return fixed

    url = rest_client.get("url")
    if isinstance(url, str):
        return url

    return ""


def build_query_string(query_params: Sequence[QueryParam]) -> str:
    parts: List[str] = []
    for param in query_params:
        if param.value is None:
            parts.append(param.key)
        else:
            parts.append(f"{param.key}={param.value}")
    return "&".join(parts)


def merge_query_into_url(current_url: str, query_params: Sequence[QueryParam]) -> str:
    base = current_url.split("?", 1)[0]
    query_text = build_query_string(query_params)
    if query_text:
        return f"{base}?{query_text}"
    return base


def build_rest_client_update_payload(current: Dict[str, Any], new_url: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in ("dataSource", "header", "httpOptions", "misc", "name", "payload", "resource"):
        if key in current:
            payload[key] = current[key]

    resource = payload.setdefault("resource", {})
    resource["type"] = "literalText"
    literal = resource.setdefault("literalText", {})
    literal["fixed"] = new_url

    return payload


def apply_query_params(
    client: SoaVirtClient,
    tst_id: str,
    request_targets: Sequence[RequestQueryTarget],
) -> Tuple[int, int, int, int, List[str], int]:
    descendants = client.descendants_assets(tst_id)
    lookup = build_rest_client_lookup(descendants)

    requests_with_query_field = 0
    updated_count = 0
    unchanged_count = 0
    missing_mapping = 0
    warnings: List[str] = []

    for target in request_targets:
        if not target.has_query_field:
            continue

        requests_with_query_field += 1
        key = (target.folder_path, target.request_name)
        ids = lookup.get(key)
        if not ids:
            missing_mapping += 1
            warnings.append(
                "Could not map request to SOAtest REST Client: "
                f"folder={'/'.join(target.folder_path) if target.folder_path else '(root)'} name={target.request_name}"
            )
            continue

        rest_client_id = ids.pop(0)
        if not rest_client_id:
            missing_mapping += 1
            warnings.append(f"Mapped REST Client id was empty for request '{target.request_name}'")
            continue

        try:
            current = client.get_rest_client(rest_client_id)
            current_url = get_current_rest_url(current)
            new_url = merge_query_into_url(current_url, target.query_params)
            if new_url == current_url:
                unchanged_count += 1
                continue

            updated = build_rest_client_update_payload(current, new_url)
            client.update_rest_client(rest_client_id, updated)
            updated_count += 1
        except RuntimeError as exc:
            warnings.append(f"Query parameter update failed for request '{target.request_name}': {exc}")

    failures = len([w for w in warnings if w.startswith("Query parameter update failed")])
    return requests_with_query_field, updated_count, unchanged_count, missing_mapping, warnings[:], failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Postman request.url.query key/value pairs into SOAtest REST Client query parameters "
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

    request_targets: List[RequestQueryTarget] = []
    walk_postman_requests_with_query(
        items=collection.get("item"),
        folder_path=(),
        targets=request_targets,
    )

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
        requests_with_query_field, updated_count, unchanged_count, missing_mapping, warnings, failures = apply_query_params(
            client=client,
            tst_id=tst_id,
            request_targets=request_targets,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(f"Target .tst: {tst_id}")
    print(f"Requests evaluated: {len(request_targets)}")
    print(f"Requests with request.url.query: {requests_with_query_field}")
    print(f"REST Clients updated with query parameters: {updated_count}")
    print(f"REST Clients already matching query parameters: {unchanged_count}")
    print(f"Request-to-REST mapping misses: {missing_mapping}")
    print(f"Update failures: {failures}")

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

