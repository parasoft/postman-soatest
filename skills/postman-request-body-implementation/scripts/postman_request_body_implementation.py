#!/usr/bin/env python3
"""
Implement Postman request bodies into an existing SOAtest .tst.

Supported body modes:
- none
- raw
- form-data
- x-www-form-urlencoded
- graphql
- file (Postman binary body)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
SUPPORTED_MODES = {"none", "raw", "formdata", "urlencoded", "graphql", "file", "binary"}
SUPPORTED_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
FORMDATA_FILE_MODE_VARIABLE = "variable"
FORMDATA_FILE_MODE_PLACEHOLDER = "placeholder"


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def convert_postman_variable_syntax(text: str) -> str:
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", str(text))


def convert_node_variables(node: Any) -> Any:
    if isinstance(node, str):
        return convert_postman_variable_syntax(node)
    if isinstance(node, list):
        return [convert_node_variables(item) for item in node]
    if isinstance(node, dict):
        return {str(key): convert_node_variables(value) for key, value in node.items()}
    return node


def is_enabled(entry: Dict[str, Any]) -> bool:
    return not bool(entry.get("disabled", False))


def extract_header_content_type(request_obj: Dict[str, Any]) -> str:
    headers = request_obj.get("header")
    if not isinstance(headers, list):
        return ""
    for header in headers:
        if not isinstance(header, dict):
            continue
        if not is_enabled(header):
            continue
        key = str(header.get("key", "")).strip().lower()
        if key == "content-type":
            value = str(header.get("value", "")).strip()
            return convert_postman_variable_syntax(value)
    return ""


def language_to_content_type(language: str) -> str:
    mapping = {
        "json": "application/json",
        "javascript": "application/javascript",
        "xml": "application/xml",
        "html": "text/html",
        "text": "text/plain",
    }
    return mapping.get(language.lower(), "")


@dataclass(frozen=True)
class RequestBodySpec:
    mode: str
    text: str
    content_type: str
    warnings: List[str] = field(default_factory=list)
    file_hints: List[Dict[str, str]] = field(default_factory=list)
    requires_manual_setup: bool = False
    manual_setup_reason: str = ""


@dataclass(frozen=True)
class RequestBodyTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    request_method: str
    body_spec: RequestBodySpec


def build_none_body_spec(request_obj: Dict[str, Any]) -> RequestBodySpec:
    header_content_type = extract_header_content_type(request_obj)
    return RequestBodySpec(mode="none", text="", content_type=header_content_type)


def harden_json_text(text: str) -> Tuple[str, List[str]]:
    warnings: List[str] = []
    updated = str(text)

    without_trailing_commas = TRAILING_COMMA_RE.sub(r"\1", updated)
    if without_trailing_commas != updated:
        warnings.append("Removed trailing comma(s) from raw JSON body.")
        updated = without_trailing_commas

    # Keep unquoted ${variable} tokens unquoted for numeric/object fields.
    # Replace tokens with a neutral numeric literal only for linting.
    lint_candidate = re.sub(r"\$\{[^}]+\}", "0", updated)
    try:
        json.loads(lint_candidate)
    except json.JSONDecodeError as exc:
        warnings.append(
            f"JSON linter warning: raw body may still be invalid ({exc.msg} at line {exc.lineno}, col {exc.colno})."
        )

    return updated, warnings


def build_raw_body_spec(request_obj: Dict[str, Any], body_obj: Dict[str, Any]) -> RequestBodySpec:
    raw_text = str(body_obj.get("raw", ""))
    converted_text = convert_postman_variable_syntax(raw_text)
    warnings: List[str] = []

    header_content_type = extract_header_content_type(request_obj)
    content_type = header_content_type

    options = body_obj.get("options")
    if not content_type and isinstance(options, dict):
        raw_options = options.get("raw")
        if isinstance(raw_options, dict):
            language = str(raw_options.get("language", "")).strip()
            content_type = language_to_content_type(language)

    if not content_type:
        content_type = "application/json"

    if "json" in content_type.lower():
        converted_text, json_warnings = harden_json_text(converted_text)
        warnings.extend(json_warnings)

    return RequestBodySpec(mode="raw", text=converted_text, content_type=content_type, warnings=warnings)


def build_urlencoded_body_spec(request_obj: Dict[str, Any], body_obj: Dict[str, Any]) -> RequestBodySpec:
    pairs: List[Tuple[str, str]] = []
    entries = body_obj.get("urlencoded")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not is_enabled(entry):
                continue
            key = convert_postman_variable_syntax(str(entry.get("key", "")))
            value = convert_postman_variable_syntax(str(entry.get("value", "")))
            pairs.append((key, value))

    encoded = "&".join(
        f"{urllib.parse.quote_plus(key)}={urllib.parse.quote_plus(value)}" for key, value in pairs
    )
    content_type = extract_header_content_type(request_obj) or "application/x-www-form-urlencoded"
    return RequestBodySpec(mode="urlencoded", text=encoded, content_type=content_type)


def build_file_path_variable_name(field_name: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]+", " ", str(field_name)).strip()
    if not cleaned:
        return "filePath"
    parts = cleaned.split()
    first = parts[0].lower()
    rest = "".join(part.capitalize() for part in parts[1:])
    candidate = first + rest
    if not candidate.endswith("FilePath"):
        candidate += "FilePath"
    if candidate and candidate[0].isdigit():
        candidate = "file" + candidate
    return candidate


def build_formdata_body_spec(
    request_obj: Dict[str, Any],
    body_obj: Dict[str, Any],
    form_data_file_mode: str,
) -> RequestBodySpec:
    warnings: List[str] = []
    file_hints: List[Dict[str, str]] = []
    boundary = "----PostmanToSOAtestBoundary"
    parts: List[str] = []

    entries = body_obj.get("formdata")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if not is_enabled(entry):
                continue

            raw_key = str(entry.get("key", ""))
            key = convert_postman_variable_syntax(raw_key)
            field_type = str(entry.get("type", "text")).strip().lower()

            if field_type == "file":
                src = entry.get("src")
                src_display = ""
                if isinstance(src, list):
                    src_display = str(src[0]) if src else ""
                elif isinstance(src, str):
                    src_display = src
                filename = os.path.basename(src_display) if src_display else (key or "file")
                if form_data_file_mode == FORMDATA_FILE_MODE_VARIABLE:
                    variable_name = build_file_path_variable_name(raw_key or key)
                    payload_value = "${" + variable_name + "}"
                    file_hints.append(
                        {
                            "hintType": "form-data-file",
                            "fieldName": key,
                            "variableName": variable_name,
                            "sourcePath": src_display,
                        }
                    )
                    warnings.append(
                        f"form-data file field '{key}' mapped to variable '${{{variable_name}}}'. "
                        f"Set this variable to a runtime file path."
                    )
                else:
                    payload_value = f"<<FILE:{src_display}>>"
                    warnings.append(
                        f"form-data file field '{key}' converted to placeholder text because binary upload "
                        "is not represented exactly in this phase."
                    )
                parts.append(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n"
                    f"{payload_value}\r\n"
                )
                continue

            value = convert_postman_variable_syntax(str(entry.get("value", "")))
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            )

    parts.append(f"--{boundary}--\r\n")
    text = "".join(parts)
    content_type = extract_header_content_type(request_obj) or f"multipart/form-data; boundary={boundary}"
    return RequestBodySpec(
        mode="formdata",
        text=text,
        content_type=content_type,
        warnings=warnings,
        file_hints=file_hints,
    )


def build_graphql_body_spec(request_obj: Dict[str, Any], body_obj: Dict[str, Any]) -> RequestBodySpec:
    warnings: List[str] = []
    graphql_obj = body_obj.get("graphql")

    query = ""
    variables: Any = {}

    if isinstance(graphql_obj, dict):
        query = convert_postman_variable_syntax(str(graphql_obj.get("query", "")))
        raw_variables = graphql_obj.get("variables")
        if isinstance(raw_variables, str):
            converted = convert_postman_variable_syntax(raw_variables)
            stripped = converted.strip()
            if stripped:
                try:
                    variables = convert_node_variables(json.loads(stripped))
                except json.JSONDecodeError:
                    variables = converted
                    warnings.append(
                        "graphql variables were not valid JSON; preserved as string in payload."
                    )
            else:
                variables = {}
        elif isinstance(raw_variables, (dict, list)):
            variables = convert_node_variables(raw_variables)
        elif raw_variables is None:
            variables = {}
        else:
            variables = convert_postman_variable_syntax(str(raw_variables))
            warnings.append("graphql variables were converted using string representation.")
    else:
        query = convert_postman_variable_syntax(str(body_obj.get("raw", "")))
        warnings.append(
            "graphql body did not include graphql.query/variables object; raw content was preserved as query."
        )

    payload_obj = {"query": query, "variables": variables}
    text = json.dumps(payload_obj, indent=2, ensure_ascii=False)
    content_type = extract_header_content_type(request_obj) or "application/json"
    return RequestBodySpec(mode="graphql", text=text, content_type=content_type, warnings=warnings)


def extract_postman_file_source(file_obj: Any) -> str:
    if not isinstance(file_obj, dict):
        return ""

    src = file_obj.get("src")
    if isinstance(src, list):
        for candidate in src:
            value = str(candidate).strip()
            if value:
                return convert_postman_variable_syntax(value)
        return ""

    if isinstance(src, str):
        return convert_postman_variable_syntax(src.strip())

    return ""


def build_binary_body_spec(request_obj: Dict[str, Any], body_obj: Dict[str, Any]) -> RequestBodySpec:
    source_path = extract_postman_file_source(body_obj.get("file"))
    if not source_path:
        source_path = extract_postman_file_source(body_obj.get("binary"))
    if not source_path and isinstance(body_obj.get("binary"), str):
        source_path = convert_postman_variable_syntax(str(body_obj.get("binary", "")).strip())

    content_type = extract_header_content_type(request_obj) or "application/octet-stream"

    limitation = (
        "SOAVirt REST API does not expose REST Client payload file-attachment/table row fields for binary request "
        "bodies (only payload.input.literal.text is available). Manual SOAtest setup is required."
    )

    warnings = [f"binary/file request body detected (source: {source_path or '(not exported by Postman)'}).", limitation]
    file_hints = [
        {
            "hintType": "binary-body-file",
            "fieldName": "(request-body)",
            "variableName": "",
            "sourcePath": source_path,
        }
    ]

    return RequestBodySpec(
        mode="file",
        text="",
        content_type=content_type,
        warnings=warnings,
        file_hints=file_hints,
        requires_manual_setup=True,
        manual_setup_reason=limitation,
    )


def build_request_body_spec(request_obj: Dict[str, Any], form_data_file_mode: str) -> RequestBodySpec:
    body_obj = request_obj.get("body")
    if not isinstance(body_obj, dict):
        return build_none_body_spec(request_obj)

    mode = str(body_obj.get("mode", "none")).strip().lower()
    if mode in {"", "none"}:
        return build_none_body_spec(request_obj)
    if mode == "raw":
        return build_raw_body_spec(request_obj, body_obj)
    if mode == "urlencoded":
        return build_urlencoded_body_spec(request_obj, body_obj)
    if mode == "formdata":
        return build_formdata_body_spec(request_obj, body_obj, form_data_file_mode=form_data_file_mode)
    if mode == "graphql":
        return build_graphql_body_spec(request_obj, body_obj)
    if mode in {"file", "binary"}:
        return build_binary_body_spec(request_obj, body_obj)

    warnings = [f"Unsupported Postman body mode '{mode}'. Preserved as empty body."]
    return RequestBodySpec(mode=mode, text="", content_type=extract_header_content_type(request_obj), warnings=warnings)


def walk_postman_requests_with_bodies(
    items: Any,
    folder_path: Tuple[str, ...],
    targets: List[RequestBodyTarget],
    form_data_file_mode: str,
) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get("name", "Unnamed"))
        nested = item.get("item")
        if isinstance(nested, list):
            walk_postman_requests_with_bodies(
                items=nested,
                folder_path=folder_path + (item_name,),
                targets=targets,
                form_data_file_mode=form_data_file_mode,
            )

        request_obj = item.get("request")
        if isinstance(request_obj, dict):
            body_spec = build_request_body_spec(request_obj, form_data_file_mode=form_data_file_mode)
            request_method = str(request_obj.get("method", "GET")).upper()
            targets.append(
                RequestBodyTarget(
                    folder_path=folder_path,
                    request_name=item_name,
                    request_method=request_method,
                    body_spec=body_spec,
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


def build_rest_client_update_payload(
    current: Dict[str, Any],
    body_spec: RequestBodySpec,
    request_method: str,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        key: value
        for key, value in current.items()
        if key not in {"id", "url", "relationships", "referenced"}
    }

    method_upper = str(request_method or "GET").upper()
    method_type = method_upper if method_upper in SUPPORTED_HTTP_METHODS else "CUSTOM"

    header = payload.setdefault("header", {})
    if not isinstance(header, dict):
        header = {}
        payload["header"] = header
    method_obj = header.get("method")
    if not isinstance(method_obj, dict):
        method_obj = {}
    method_obj["methodType"] = method_type
    method_obj["value"] = method_upper
    header["method"] = method_obj

    resource_method = payload.setdefault("resourceMethod", {})
    if not isinstance(resource_method, dict):
        resource_method = {}
        payload["resourceMethod"] = resource_method
    resource_method["httpMethod"] = method_upper

    rest_payload = payload.setdefault("payload", {})
    rest_payload["inputMode"] = "literal"
    rest_payload["input"] = {"literal": {"text": body_spec.text}}

    if body_spec.content_type:
        rest_payload["contentType"] = body_spec.content_type
    elif "contentType" not in rest_payload:
        rest_payload["contentType"] = "application/json"

    return payload


def canonical_mode(mode: str) -> str:
    value = mode.lower().strip()
    if value in {"", "none"}:
        return "none"
    if value == "raw":
        return "raw"
    if value == "formdata":
        return "form-data"
    if value == "urlencoded":
        return "x-www-form-urlencoded"
    if value == "graphql":
        return "graphql"
    if value in {"file", "binary"}:
        return "binary-file"
    return "unsupported"


def apply_request_bodies(
    client: SoaVirtClient,
    tst_id: str,
    request_targets: Sequence[RequestBodyTarget],
) -> Tuple[Dict[str, int], int, List[str], List[Dict[str, Any]], List[Dict[str, Any]]]:
    descendants = client.descendants_assets(tst_id)
    lookup = build_rest_client_lookup(descendants)

    mode_counts: Dict[str, int] = {
        "none": 0,
        "raw": 0,
        "form-data": 0,
        "x-www-form-urlencoded": 0,
        "graphql": 0,
        "binary-file": 0,
        "unsupported": 0,
    }
    update_failures = 0
    warnings: List[str] = []
    request_report: List[Dict[str, Any]] = []
    file_hints: List[Dict[str, Any]] = []

    for target in request_targets:
        mode_label = canonical_mode(target.body_spec.mode)
        if mode_label not in mode_counts:
            mode_label = "unsupported"

        row: Dict[str, Any] = {
            "requestName": target.request_name,
            "folderHierarchy": list(target.folder_path),
            "detectedBodyMode": target.body_spec.mode,
            "canonicalBodyMode": mode_label,
            "applied": False,
            "status": "pending",
            "warnings": list(target.body_spec.warnings),
        }
        for local_warning in target.body_spec.warnings:
            warnings.append(f"{target.request_name}: {local_warning}")
        for hint in target.body_spec.file_hints:
            file_hints.append(
                {
                    "folderHierarchy": list(target.folder_path),
                    "requestName": target.request_name,
                    "hintType": hint.get("hintType", ""),
                    "fieldName": hint.get("fieldName", ""),
                    "variableName": hint.get("variableName", ""),
                    "sourcePath": hint.get("sourcePath", ""),
                }
            )

        key = (target.folder_path, target.request_name)
        ids = lookup.get(key)
        if not ids:
            mapping_warning = (
                "Could not map request to SOAtest REST Client: "
                f"folder={'/'.join(target.folder_path) if target.folder_path else '(root)'} name={target.request_name}"
            )
            warnings.append(mapping_warning)
            row["warnings"].append(mapping_warning)
            row["status"] = "rest-client-not-found"
            request_report.append(row)
            continue

        rest_client_id = ids.pop(0)
        if not rest_client_id:
            id_warning = f"Mapped REST Client id was empty for request '{target.request_name}'"
            warnings.append(id_warning)
            row["warnings"].append(id_warning)
            row["status"] = "rest-client-id-empty"
            request_report.append(row)
            continue

        if target.body_spec.requires_manual_setup:
            mode_counts[mode_label] += 1
            row["status"] = "manual-setup-required"
            row["manualSetupRequired"] = True
            row["manualSetupReason"] = target.body_spec.manual_setup_reason
            request_report.append(row)
            continue

        try:
            current = client.get_rest_client(rest_client_id)
            updated = build_rest_client_update_payload(
                current=current,
                body_spec=target.body_spec,
                request_method=target.request_method,
            )
            client.update_rest_client(rest_client_id, updated)
        except RuntimeError as exc:
            update_failures += 1
            update_warning = f"Body update failed for request '{target.request_name}' (mode={mode_label}): {exc}"
            warnings.append(update_warning)
            row["warnings"].append(update_warning)
            row["status"] = "update-failed"
            request_report.append(row)
            continue

        mode_counts[mode_label] += 1
        row["applied"] = True
        row["status"] = "updated"
        request_report.append(row)

    return mode_counts, update_failures, warnings, request_report, file_hints


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Postman request bodies to SOAtest REST Client payloads for an existing generated .tst "
            "(supports none, raw, form-data, x-www-form-urlencoded, graphql; detects Postman file/binary mode)."
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
    parser.add_argument(
        "--form-data-file-mode",
        choices=[FORMDATA_FILE_MODE_VARIABLE, FORMDATA_FILE_MODE_PLACEHOLDER],
        default=FORMDATA_FILE_MODE_VARIABLE,
        help=(
            "How form-data file fields are represented. "
            "'variable' maps to ${...} file-path variables (default). "
            "'placeholder' keeps legacy <<FILE:...>> markers."
        ),
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
    if "item" not in collection:
        print(
            "ERROR: Input does not look like an exported Postman Collection (missing root 'item').",
            file=sys.stderr,
        )
        return 1

    info = collection.get("info") if isinstance(collection.get("info"), dict) else {}
    collection_name = str(info.get("name", "Postman Collection"))
    target_name = args.target_test_file_name or collection_name

    request_targets: List[RequestBodyTarget] = []
    walk_postman_requests_with_bodies(
        items=collection.get("item"),
        folder_path=(),
        targets=request_targets,
        form_data_file_mode=args.form_data_file_mode,
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
        mode_counts, update_failures, warnings, request_report, file_hints = apply_request_bodies(
            client=client,
            tst_id=tst_id,
            request_targets=request_targets,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(f"Target .tst: {tst_id}")
    print(f"Requests evaluated: {len(request_targets)}")
    print(f"Body mode none applied: {mode_counts['none']}")
    print(f"Body mode raw applied: {mode_counts['raw']}")
    print(f"Body mode form-data applied: {mode_counts['form-data']}")
    print(f"Body mode x-www-form-urlencoded applied: {mode_counts['x-www-form-urlencoded']}")
    print(f"Body mode graphql applied: {mode_counts['graphql']}")
    print(f"Body mode binary-file detected (manual setup required): {mode_counts['binary-file']}")
    print(f"Body mode unsupported preserved with warnings: {mode_counts['unsupported']}")
    print(f"Form-data file handling mode: {args.form_data_file_mode}")
    print(f"Body update failures: {update_failures}")
    if file_hints:
        print("File/body setup hints:")
        for hint in file_hints:
            folder_display = "/".join(hint.get("folderHierarchy", [])) or "(root)"
            hint_type = str(hint.get("hintType", ""))
            variable_name = str(hint.get("variableName", ""))
            variable_ref = "${" + variable_name + "}" if variable_name else "${filePath}"
            source_path = str(hint.get("sourcePath", "")) or "(not exported by Postman)"
            if hint_type == "binary-body-file":
                print(
                    f"- {folder_display} / {hint.get('requestName', '')}: "
                    f"binary request body source={source_path}; configure payload file attachment manually in SOAtest."
                )
            else:
                print(
                    f"- {folder_display} / {hint.get('requestName', '')}: "
                    f"field '{hint.get('fieldName', '')}' -> {variable_ref} "
                    f"(source: {source_path})"
                )

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
