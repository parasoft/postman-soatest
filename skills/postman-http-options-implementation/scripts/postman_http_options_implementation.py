#!/usr/bin/env python3
"""
Apply Postman request-level HTTP options to an existing SOAtest .tst.

Current scope:
- HTTP version mapping (Auto, HTTP/1.x)
- HTTP/2 detection with explicit unsupported warning and HTTP/1 downgrade
- Automatically follow redirects

Notes:
- Postman settings are read from protocolProfileBehavior at collection/folder/request levels.
- Request-level behavior inherits parent settings when request-level values are absent.
- Unsupported values are skipped with warnings.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

TRANSPORT_HTTP10 = "http10"
TRANSPORT_HTTP11 = "http11"
TRANSPORT_HTTP20 = "http20"
VALID_TRANSPORT_TYPES = {TRANSPORT_HTTP10, TRANSPORT_HTTP11, TRANSPORT_HTTP20}


@dataclass(frozen=True)
class HttpOptionSpec:
    follow_redirects: Optional[bool] = None
    http_version: Optional[str] = None  # auto | http1 | http2(detected; downgraded during apply)

    def has_values(self) -> bool:
        return self.follow_redirects is not None or self.http_version is not None


@dataclass(frozen=True)
class RequestHttpOptionsTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    effective_options: HttpOptionSpec


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def parse_bool_like(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "enabled"}:
            return True
        if lowered in {"false", "0", "no", "off", "disabled"}:
            return False
    return None


def normalize_http_version(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if int(value) == 2:
            return "http2"
        if int(value) == 1:
            return "http1"
        return None

    text = str(value).strip().lower()
    if not text:
        return None
    if text in {"auto", "default", "system"}:
        return "auto"
    if text in {"2", "h2"} or "http/2" in text or "http2" in text:
        return "http2"
    if text in {"1", "1.x", "h1"} or "http/1" in text or "http1" in text:
        return "http1"
    return None


def merge_http_options(parent: HttpOptionSpec, override: HttpOptionSpec) -> HttpOptionSpec:
    return HttpOptionSpec(
        follow_redirects=(
            override.follow_redirects if override.follow_redirects is not None else parent.follow_redirects
        ),
        http_version=(override.http_version if override.http_version is not None else parent.http_version),
    )


def parse_protocol_profile_behavior(raw: Any, warnings: List[str], context_label: str) -> HttpOptionSpec:
    if not isinstance(raw, dict):
        return HttpOptionSpec()

    follow_redirects: Optional[bool] = None
    if "followRedirects" in raw:
        parsed = parse_bool_like(raw.get("followRedirects"))
        if parsed is None:
            warnings.append(
                f"Unsupported followRedirects value at {context_label}: {raw.get('followRedirects')}"
            )
        else:
            follow_redirects = parsed
    elif "disableRedirects" in raw:
        parsed = parse_bool_like(raw.get("disableRedirects"))
        if parsed is None:
            warnings.append(
                f"Unsupported disableRedirects value at {context_label}: {raw.get('disableRedirects')}"
            )
        else:
            follow_redirects = not parsed
    elif "automaticallyFollowRedirects" in raw:
        parsed = parse_bool_like(raw.get("automaticallyFollowRedirects"))
        if parsed is None:
            warnings.append(
                "Unsupported automaticallyFollowRedirects value at "
                f"{context_label}: {raw.get('automaticallyFollowRedirects')}"
            )
        else:
            follow_redirects = parsed

    http_version: Optional[str] = None
    version_key = None
    for candidate_key in ("requestVersion", "httpVersion", "protocolVersion"):
        if candidate_key in raw:
            version_key = candidate_key
            break

    if version_key is not None:
        normalized = normalize_http_version(raw.get(version_key))
        if normalized is None:
            warnings.append(
                f"Unsupported {version_key} value at {context_label}: {raw.get(version_key)}"
            )
        else:
            http_version = normalized

    return HttpOptionSpec(follow_redirects=follow_redirects, http_version=http_version)


def walk_postman_requests_with_http_options(
    items: Any,
    folder_path: Tuple[str, ...],
    parent_effective: HttpOptionSpec,
    targets: List[RequestHttpOptionsTarget],
    warnings: List[str],
) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get("name", "Unnamed"))
        item_effective = merge_http_options(
            parent_effective,
            parse_protocol_profile_behavior(
                item.get("protocolProfileBehavior"),
                warnings,
                context_label=f"item '{item_name}'",
            ),
        )

        nested = item.get("item")
        if isinstance(nested, list):
            walk_postman_requests_with_http_options(
                items=nested,
                folder_path=folder_path + (item_name,),
                parent_effective=item_effective,
                targets=targets,
                warnings=warnings,
            )

        request_obj = item.get("request")
        if isinstance(request_obj, dict):
            request_effective = merge_http_options(
                item_effective,
                parse_protocol_profile_behavior(
                    request_obj.get("protocolProfileBehavior"),
                    warnings,
                    context_label=f"request '{item_name}'",
                ),
            )
            if request_effective.has_values():
                targets.append(
                    RequestHttpOptionsTarget(
                        folder_path=folder_path,
                        request_name=item_name,
                        effective_options=request_effective,
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


def normalize_transport_type(raw_type: Any) -> str:
    value = str(raw_type or "").strip().lower()
    if value in VALID_TRANSPORT_TYPES:
        return value
    if value in {"http1", "http1x", "http/1.0", "http/1.1"}:
        return TRANSPORT_HTTP10
    if value in {"http2", "http/2", "h2"}:
        return TRANSPORT_HTTP20
    return TRANSPORT_HTTP10


def get_rest_client_transport(rest_client: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    http_options = rest_client.get("httpOptions")
    if not isinstance(http_options, dict):
        return TRANSPORT_HTTP10, {}

    transport = http_options.get("transport")
    if not isinstance(transport, dict):
        return TRANSPORT_HTTP10, {}

    transport_type = normalize_transport_type(transport.get("type"))
    return transport_type, transport


def get_follow_redirect_for_transport(rest_client: Dict[str, Any], transport_type: str) -> Optional[bool]:
    _, transport = get_rest_client_transport(rest_client)
    transport_obj = transport.get(transport_type)
    if not isinstance(transport_obj, dict):
        return None

    general_settings = transport_obj.get("generalSettings")
    if not isinstance(general_settings, dict):
        return None

    return parse_bool_like(general_settings.get("followHttpRedirects"))


def build_rest_client_update_payload(
    current: Dict[str, Any],
    effective_options: HttpOptionSpec,
) -> Tuple[Dict[str, Any], str]:
    payload: Dict[str, Any] = {}
    for key in ("dataSource", "header", "httpOptions", "misc", "name", "payload", "resource"):
        if key in current:
            payload[key] = copy.deepcopy(current[key])

    http_options = payload.setdefault("httpOptions", {})
    transport = http_options.setdefault("transport", {})

    current_type = normalize_transport_type(transport.get("type"))
    if current_type not in transport or not isinstance(transport.get(current_type), dict):
        transport[current_type] = {}

    selected_version = effective_options.http_version
    if selected_version == "http2":
        selected_version = "http1"
    desired_type = current_type

    if selected_version == "http1":
        desired_type = current_type if current_type in {TRANSPORT_HTTP10, TRANSPORT_HTTP11} else TRANSPORT_HTTP10
        transport["type"] = desired_type

    if desired_type not in transport or not isinstance(transport.get(desired_type), dict):
        template = transport.get(current_type)
        if isinstance(template, dict):
            transport[desired_type] = copy.deepcopy(template)
        else:
            transport[desired_type] = {}

    transport_obj = transport.setdefault(desired_type, {})
    if effective_options.follow_redirects is not None:
        general_settings = transport_obj.setdefault("generalSettings", {})
        general_settings["followHttpRedirects"] = effective_options.follow_redirects

    return payload, desired_type


def needs_update(current: Dict[str, Any], effective_options: HttpOptionSpec) -> bool:
    current_type, _ = get_rest_client_transport(current)
    target_type = current_type
    target_http_version = "http1" if effective_options.http_version == "http2" else effective_options.http_version

    if target_http_version == "http1":
        target_type = current_type if current_type in {TRANSPORT_HTTP10, TRANSPORT_HTTP11} else TRANSPORT_HTTP10
        if current_type not in {TRANSPORT_HTTP10, TRANSPORT_HTTP11}:
            return True

    if effective_options.follow_redirects is not None:
        current_follow = get_follow_redirect_for_transport(current, target_type)
        if current_follow != effective_options.follow_redirects:
            return True

    return False


def persisted_matches(
    persisted: Dict[str, Any],
    expected_transport_type: Optional[str],
    expected_follow_redirects: Optional[bool],
) -> Tuple[bool, str]:
    actual_type, _ = get_rest_client_transport(persisted)

    if expected_transport_type is not None and actual_type != expected_transport_type:
        return False, f"expected transport type {expected_transport_type} but found {actual_type}"

    follow_check_type = expected_transport_type or actual_type
    if expected_follow_redirects is not None:
        actual_follow = get_follow_redirect_for_transport(persisted, follow_check_type)
        if actual_follow != expected_follow_redirects:
            return (
                False,
                "expected followHttpRedirects="
                f"{expected_follow_redirects} but found {actual_follow}",
            )

    return True, ""



def apply_http_options(
    client: SoaVirtClient,
    tst_id: str,
    request_targets: Sequence[RequestHttpOptionsTarget],
) -> Tuple[int, int, int, int, int, int, List[str]]:
    descendants = client.descendants_assets(tst_id)
    lookup = build_rest_client_lookup(descendants)

    updated_count = 0
    unchanged_count = 0
    missing_mapping = 0
    failures = 0
    http2_downgrades = 0
    warnings: List[str] = []

    for target in request_targets:
        key = (target.folder_path, target.request_name)
        ids = lookup.get(key)
        if not ids:
            missing_mapping += 1
            warnings.append(
                "Could not map request to SOAtest REST Client: "
                f"folder={'/'.join(target.folder_path) if target.folder_path else '(root)'} "
                f"name={target.request_name}"
            )
            continue

        rest_client_id = ids.pop(0)
        if not rest_client_id:
            missing_mapping += 1
            warnings.append(f"Mapped REST Client id was empty for request '{target.request_name}'")
            continue

        try:
            current = client.get_rest_client(rest_client_id)
        except RuntimeError as exc:
            failures += 1
            warnings.append(f"HTTP options read failed for request '{target.request_name}': {exc}")
            continue

        effective_options = target.effective_options
        if target.effective_options.http_version == "http2":
            http2_downgrades += 1
            warnings.append(
                "HTTP/2 requested for request "
                f"'{target.request_name}', but HTTP/2 conversion is not supported for REST Client output. "
                "Using HTTP/1 transport. Equivalent HTTP/2 behavior in SOAtest requires Messaging Client "
                "with the Parasoft HTTP/2 Transport Extension."
            )
            effective_options = HttpOptionSpec(
                follow_redirects=target.effective_options.follow_redirects,
                http_version="http1",
            )

        if not needs_update(current, effective_options):
            unchanged_count += 1
            continue

        payload, expected_type = build_rest_client_update_payload(current, effective_options)

        def try_apply(update_payload: Dict[str, Any], expected_transport: Optional[str]) -> Tuple[bool, str]:
            client.update_rest_client(rest_client_id, update_payload)
            persisted = client.get_rest_client(rest_client_id)
            ok, reason = persisted_matches(
                persisted,
                expected_transport,
                effective_options.follow_redirects,
            )
            return ok, reason

        update_error: Optional[str] = None
        try:
            ok, reason = try_apply(payload, expected_type if effective_options.http_version in {"http1"} else None)
            if ok:
                updated_count += 1
                continue
            update_error = reason
        except RuntimeError as exc:
            update_error = str(exc)

        failures += 1
        warnings.append(f"HTTP options update failed for request '{target.request_name}': {update_error}")

    return updated_count, unchanged_count, missing_mapping, failures, http2_downgrades, len(request_targets), warnings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Postman request-level HTTP version and follow-redirect settings into "
            "SOAtest REST Client HTTP options for an existing generated .tst."
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

    warnings: List[str] = []
    root_options = parse_protocol_profile_behavior(
        collection.get("protocolProfileBehavior"),
        warnings,
        context_label="collection root",
    )

    request_targets: List[RequestHttpOptionsTarget] = []
    walk_postman_requests_with_http_options(
        items=collection.get("item"),
        folder_path=(),
        parent_effective=root_options,
        targets=request_targets,
        warnings=warnings,
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
        updated_count, unchanged_count, missing_mapping, failures, http2_downgrades, total_targets, apply_warnings = (
            apply_http_options(client=client, tst_id=tst_id, request_targets=request_targets)
        )
        warnings.extend(apply_warnings)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(f"Target .tst: {tst_id}")
    print(f"Requests evaluated: {total_targets}")
    print(f"Requests with HTTP options settings: {total_targets}")
    print(f"REST Clients updated with HTTP options: {updated_count}")
    print(f"REST Clients already matching HTTP options: {unchanged_count}")
    print(f"Request-to-REST mapping misses: {missing_mapping}")
    print(f"HTTP/2 requests downgraded to HTTP/1 (unsupported): {http2_downgrades}")
    print(f"Update failures: {failures}")

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

