#!/usr/bin/env python3
"""
Implement explicit Postman request headers into an existing SOAtest .tst.

Scope:
- Read request.header entries from Postman requests.
- Apply enabled header key/value pairs to matching SOAtest REST Clients.
- Skip disabled/non-key header entries.
- Convert Postman variable syntax {{var}} -> ${var}.
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


def convert_postman_variable_syntax(text: Any) -> str:
    if text is None:
        return ""
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", str(text))


@dataclass(frozen=True)
class HeaderEntry:
    key: str
    value: str


@dataclass(frozen=True)
class RequestHeaderTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    has_header_field: bool
    headers: Tuple[HeaderEntry, ...]


def extract_headers(request_obj: Dict[str, Any]) -> Tuple[bool, Tuple[HeaderEntry, ...]]:
    raw_headers = request_obj.get("header")
    if raw_headers is None:
        return False, ()
    if not isinstance(raw_headers, list):
        return True, ()

    headers: List[HeaderEntry] = []
    for entry in raw_headers:
        if not isinstance(entry, dict):
            continue
        if bool(entry.get("disabled", False)):
            continue

        key = convert_postman_variable_syntax(entry.get("key", "")).strip()
        if not key:
            continue

        value = convert_postman_variable_syntax(entry.get("value", ""))
        headers.append(HeaderEntry(key=key, value=value))

    return True, tuple(headers)


def walk_postman_requests_with_headers(
    items: Any,
    folder_path: Tuple[str, ...],
    targets: List[RequestHeaderTarget],
) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get("name", "Unnamed"))
        nested = item.get("item")
        if isinstance(nested, list):
            walk_postman_requests_with_headers(
                items=nested,
                folder_path=folder_path + (item_name,),
                targets=targets,
            )

        request_obj = item.get("request")
        if isinstance(request_obj, dict):
            has_header_field, headers = extract_headers(request_obj)
            targets.append(
                RequestHeaderTarget(
                    folder_path=folder_path,
                    request_name=item_name,
                    has_header_field=has_header_field,
                    headers=headers,
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


def headers_to_literal(headers: Sequence[HeaderEntry]) -> str:
    lines: List[str] = []
    for entry in headers:
        if entry.value == "":
            lines.append(f"{entry.key}:")
        else:
            lines.append(f"{entry.key}: {entry.value}")
    return "\n".join(lines)


def _parse_header_literal(literal: str) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for line in str(literal or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            pairs.append((key.strip(), value.strip()))
        else:
            pairs.append((stripped, ""))
    return pairs


def _contains_header(headers: Sequence[HeaderEntry], name: str) -> bool:
    target = name.strip().lower()
    for entry in headers:
        if entry.key.strip().lower() == target:
            return True
    return False


def _get_header_value_from_literal(literal: str, name: str) -> Optional[str]:
    target = name.strip().lower()
    for key, value in _parse_header_literal(literal):
        if key.strip().lower() == target:
            return value.strip()
    return None


def _upsert_header_literal(literal: str, name: str, value: str) -> str:
    target = name.strip().lower()
    pairs = _parse_header_literal(literal)

    updated: List[Tuple[str, str]] = []
    replaced = False
    for key, existing_value in pairs:
        if key.strip().lower() == target:
            if not replaced:
                updated.append((name, value))
                replaced = True
            continue
        updated.append((key, existing_value))

    if not replaced:
        updated.append((name, value))

    lines: List[str] = []
    for key, header_value in updated:
        lines.append(f"{key}:" if header_value == "" else f"{key}: {header_value}")
    return "\n".join(lines)


def get_current_headers_literal(rest_client: Dict[str, Any]) -> str:
    http_options = rest_client.get("httpOptions")
    if not isinstance(http_options, dict):
        return ""

    transport = http_options.get("transport")
    if not isinstance(transport, dict):
        return ""

    transport_type = str(transport.get("type", "http10")).lower()
    if transport_type not in {"http10", "http11"}:
        transport_type = "http10"

    transport_obj = transport.get(transport_type)
    if not isinstance(transport_obj, dict):
        return ""

    http_headers = transport_obj.get("httpHeaders")
    if not isinstance(http_headers, dict):
        return ""

    literal = http_headers.get("literal")
    return literal if isinstance(literal, str) else ""


def build_rest_client_update_payload(current: Dict[str, Any], header_literal: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in ("dataSource", "header", "httpOptions", "misc", "name", "payload", "resource"):
        if key in current:
            payload[key] = current[key]

    http_options = payload.setdefault("httpOptions", {})
    transport = http_options.setdefault("transport", {})
    transport_type = str(transport.get("type", "http10")).lower()
    if transport_type not in {"http10", "http11"}:
        transport_type = "http10"
        transport["type"] = "http10"

    transport_obj = transport.setdefault(transport_type, {})
    transport_obj["httpHeaders"] = {
        "type": "literal",
        "literal": header_literal,
    }
    return payload


def apply_headers(
    client: SoaVirtClient,
    tst_id: str,
    request_targets: Sequence[RequestHeaderTarget],
) -> Tuple[int, int, int, int, List[str], int]:
    descendants = client.descendants_assets(tst_id)
    lookup = build_rest_client_lookup(descendants)

    requests_with_header_field = 0
    updated_count = 0
    unchanged_count = 0
    missing_mapping = 0
    warnings: List[str] = []

    for target in request_targets:
        if not target.has_header_field:
            continue

        requests_with_header_field += 1
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
            new_literal = headers_to_literal(target.headers)
            current_literal = get_current_headers_literal(current)

            if not _contains_header(target.headers, "Authorization"):
                current_auth_value = _get_header_value_from_literal(current_literal, "Authorization")
                if current_auth_value is not None:
                    new_literal = _upsert_header_literal(new_literal, "Authorization", current_auth_value)

            if new_literal == current_literal:
                unchanged_count += 1
                continue

            updated = build_rest_client_update_payload(current, new_literal)
            client.update_rest_client(rest_client_id, updated)
            updated_count += 1
        except RuntimeError as exc:
            warnings.append(f"Header update failed for request '{target.request_name}': {exc}")

    failures = len([w for w in warnings if w.startswith("Header update failed")])
    return requests_with_header_field, updated_count, unchanged_count, missing_mapping, warnings[:], failures


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert explicit Postman request headers into SOAtest REST Client HTTP header configuration "
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

    request_targets: List[RequestHeaderTarget] = []
    walk_postman_requests_with_headers(
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
        requests_with_header_field, updated_count, unchanged_count, missing_mapping, warnings, failures = apply_headers(
            client=client,
            tst_id=tst_id,
            request_targets=request_targets,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(f"Target .tst: {tst_id}")
    print(f"Requests evaluated: {len(request_targets)}")
    print(f"Requests with request.header: {requests_with_header_field}")
    print(f"REST Clients updated with explicit headers: {updated_count}")
    print(f"REST Clients already matching headers: {unchanged_count}")
    print(f"Request-to-REST mapping misses: {missing_mapping}")
    print(f"Update failures: {failures}")

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

