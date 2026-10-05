#!/usr/bin/env python3
"""
Orchestrator: Postman -> SOAtest

Runs skills in sequence:
1) postman-endpoint-sequence
2) postman-params-implementation
3) postman-variable-implementation
4) postman-auth-implementation
4b) soatest-template-fallback (auto-run on auth persistence failures)
5) postman-header-implementation
6) postman-request-body-implementation
7) postman-script-implementation
7b) soatest-template-fallback (auto-run on Difference assertion fallback needs)
8) postman-notes-implementation
9) postman-http-options-implementation
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


DIFFERENCE_ASSERTION_MARKERS: Tuple[str, ...] = (
    "Numeric Difference Assertion",
    "Date Difference Assertion",
    "Date Time Difference Assertion",
    "NumericDifferenceAssertion",
    "DateDifferenceAssertion",
    "DateTimeDifferenceAssertion",
)

POSTMAN_VARIABLE_REFERENCE_RE = re.compile(r"\{\{\s*([^{}\s]+)\s*\}\}")
SCHEMA_VERSION_RE = re.compile(r"(?m)^schemaVersion:\s*.+$")
TARGET_TST_SCHEMA_VERSION = "13"


INTERNAL_PHASE_FLAG = "--orchestrator-internal"


def internal_phase_cmd(cmd: List[str]) -> List[str]:
    return [*cmd, INTERNAL_PHASE_FLAG]


def run_step(cmd: List[str], step_name: str) -> Tuple[int, str]:
    print(f"=== {step_name} ===")
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = proc.stdout or ""
    if output:
        print(output.rstrip())
    if proc.returncode != 0:
        print(f"ERROR: {step_name} failed with exit code {proc.returncode}", file=sys.stderr)
    return proc.returncode, output


def parse_step_metrics(step_output: str) -> Dict[str, str]:
    metrics: Dict[str, str] = {}
    for raw_line in step_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("===") or line.startswith("- "):
            continue
        if line.startswith("Enter .tst test file name"):
            continue
        if line.startswith("Warnings:"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            metrics[key] = value
    return metrics


def parse_step_warning_messages(step_output: str) -> List[str]:
    warnings: List[str] = []
    capture = False
    for raw_line in step_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("Warnings:"):
            capture = True
            continue
        if capture and line.startswith("- "):
            message = line[2:].strip()
            if message:
                warnings.append(message)
            continue
        if capture and not line.startswith("- "):
            capture = False
    return warnings


def build_step_report(step_name: str, cmd: List[str], exit_code: int, output: str) -> Dict[str, Any]:
    warnings = parse_step_warning_messages(output)
    return {
        "name": step_name,
        "status": "SUCCESS" if exit_code == 0 else "FAILED",
        "exitCode": exit_code,
        "command": cmd,
        "metrics": parse_step_metrics(output),
        "warnings": warnings,
        "warningCount": len(warnings),
        "rawOutput": output,
    }


def _int_metric(metrics: Dict[str, Any], key: str, default: int = 0) -> int:
    raw = metrics.get(key, default)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def reconcile_auth_warnings_with_template_fallback(steps: List[Dict[str, Any]]) -> int:
    auth_step: Optional[Dict[str, Any]] = None
    fallback_step: Optional[Dict[str, Any]] = None

    for step in steps:
        name = str(step.get("name", ""))
        if name.startswith("Step 4: Authorization Implementation"):
            auth_step = step
        elif name.startswith("Step 4b: SOAtest Template Fallback"):
            fallback_step = step

    if auth_step is None or fallback_step is None:
        return 0
    if str(fallback_step.get("status", "")) != "SUCCESS":
        return 0

    fallback_metrics = fallback_step.get("metrics", {}) if isinstance(fallback_step.get("metrics"), dict) else {}
    patched_count = _int_metric(fallback_metrics, "REST clients patched by template fallback", 0)
    if patched_count <= 0:
        return 0

    auth_warnings = auth_step.get("warnings", [])
    if not isinstance(auth_warnings, list):
        return 0

    resolved_count = 0
    remaining: List[str] = []
    for warning in auth_warnings:
        text = str(warning)
        if "did not persist as expected" in text:
            resolved_count += 1
        else:
            remaining.append(text)

    if resolved_count == 0:
        return 0

    auth_step["warnings"] = remaining
    auth_step["warningCount"] = len(remaining)

    auth_metrics = auth_step.setdefault("metrics", {})
    if isinstance(auth_metrics, dict):
        auth_metrics["Warnings resolved by template fallback"] = str(resolved_count)

    fallback_metrics = fallback_step.setdefault("metrics", {})
    if isinstance(fallback_metrics, dict):
        fallback_metrics["Auth warnings resolved from Step 4"] = str(resolved_count)

    return resolved_count


def collection_has_difference_assertions(collection_path: Path) -> bool:
    try:
        content = collection_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(marker in content for marker in DIFFERENCE_ASSERTION_MARKERS)


def build_workflow_report(
    *,
    started_utc: str,
    finished_utc: str,
    status: str,
    failed_step: Optional[str],
    collection_path: Path,
    environment_path: Optional[Path],
    api_base: str,
    parent_id: str,
    selected_test_file_name: str,
    steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    steps_succeeded = sum(1 for step in steps if step.get("status") == "SUCCESS")
    steps_failed = sum(1 for step in steps if step.get("status") == "FAILED")
    total_warnings = sum(int(step.get("warningCount", 0)) for step in steps)
    return {
        "schemaVersion": "1.0",
        "run": {
            "startedUtc": started_utc,
            "finishedUtc": finished_utc,
            "status": status,
            "failedStep": failed_step,
        },
        "inputs": {
            "postmanCollection": str(collection_path),
            "postmanEnvironment": str(environment_path) if environment_path is not None else None,
            "apiBase": api_base,
            "parentId": parent_id,
            "selectedTestFileName": selected_test_file_name,
        },
        "summary": {
            "stepsTotal": len(steps),
            "stepsSucceeded": steps_succeeded,
            "stepsFailed": steps_failed,
            "totalWarningMessages": total_warnings,
        },
        "steps": steps,
    }


def print_uniform_report(report: Dict[str, Any]) -> None:
    run = report.get("run", {})
    summary = report.get("summary", {})
    inputs = report.get("inputs", {})
    steps = report.get("steps", [])

    print("=== Conversion Report ===")
    print(f"Status: {run.get('status', 'UNKNOWN')}")
    print(f"Collection: {Path(str(inputs.get('postmanCollection', ''))).name}")
    print(f"Environment: {Path(str(inputs.get('postmanEnvironment'))).name if inputs.get('postmanEnvironment') else 'None'}")
    print(f"Target test file: {inputs.get('selectedTestFileName', '')}")
    print(f"Started UTC: {run.get('startedUtc', '')}")
    print(f"Finished UTC: {run.get('finishedUtc', '')}")
    print(
        "Conversion steps: "
        f"{summary.get('stepsSucceeded', 0)}/{summary.get('stepsTotal', 0)} succeeded, "
        f"{summary.get('stepsFailed', 0)} failed"
    )
    print(f"Total warning messages: {summary.get('totalWarningMessages', 0)}")
    if run.get("failedStep"):
        print(f"Failed conversion step: {run.get('failedStep')}")
    print("--- Conversion Steps ---")
    for step in steps:
        warning_count = int(step.get("warningCount", 0))
        step_line = f"[{step.get('name')}] {step.get('status')} (exitCode={step.get('exitCode')})"
        if warning_count > 0:
            step_line += f", warnings={warning_count}"
        print(step_line)

    warning_rows: List[str] = []
    for step in steps:
        step_name = step.get("name", "Unknown Conversion Step")
        for warning in step.get("warnings", []):
            warning_rows.append(f"[{step_name}] {warning}")
    if warning_rows:
        print("--- Warnings ---")
        for row in warning_rows:
            print(f"- {row}")

    def get_step(prefix: str) -> Optional[Dict[str, Any]]:
        for step in steps:
            if str(step.get("name", "")).startswith(prefix):
                return step
        return None

    variable_step = get_step("Step 3: Variable Implementation")
    auth_step = get_step("Step 4: Authorization Implementation")
    template_fallback_step = get_step("Step 4b: SOAtest Template Fallback")
    script_step = get_step("Step 7: Script Implementation")
    http_options_step = get_step("Step 9: HTTP Options Implementation")

    highlight_rows: List[str] = []
    if variable_step:
        metrics = variable_step.get("metrics", {})
        ext = metrics.get("External/pre-declared variables implemented")
        script_created = metrics.get("Script-created variables discovered (not implemented)")
        both = metrics.get("Both referenced and script-created discovered (not implemented)")

        if ext is not None:
            highlight_rows.append(f"Variables: implemented {ext} external/pre-declared variables")
        if script_created is not None or both is not None:
            highlight_rows.append(
                "Variables discovered but not implemented: "
                f"{script_created or 0} script-created, {both or 0} both referenced+script-created"
            )

        for key, value in metrics.items():
            if key.startswith("Fallback applied"):
                highlight_rows.append(f"Variable fallback: {key}: {value}")
                break

    if auth_step:
        metrics = auth_step.get("metrics", {})
        resolved = metrics.get("Warnings resolved by template fallback")
        if resolved is not None:
            highlight_rows.append(f"Auth warnings resolved by template fallback: {resolved}")

    if template_fallback_step:
        metrics = template_fallback_step.get("metrics", {})
        patched = metrics.get("REST clients patched by template fallback")
        added_digest = metrics.get("Digest profiles added by template fallback")
        added_ntlm = metrics.get("NTLM profiles added by template fallback")
        added_aws = metrics.get("AWS Signature profiles added by template fallback")
        added_oauth2 = metrics.get("OAuth2 profiles added by template fallback")
        added_total = metrics.get("Auth profiles added by template fallback")
        if (
            patched is not None
            or added_digest is not None
            or added_ntlm is not None
            or added_aws is not None
            or added_oauth2 is not None
            or added_total is not None
        ):
            highlight_rows.append(
                "Template fallback: "
                f"REST clients patched = {patched or 0}, "
                f"digest profiles added = {added_digest or 0}, "
                f"ntlm profiles added = {added_ntlm or 0}, "
                f"aws profiles added = {added_aws or 0}, "
                f"oauth2 profiles added = {added_oauth2 or 0}, "
                f"total auth profiles added = {added_total or 0}"
            )

    if script_step:
        metrics = script_step.get("metrics", {})
        native_created = metrics.get("Native tools created")
        rest_updated = metrics.get("REST clients updated")
        if native_created is not None or rest_updated is not None:
            highlight_rows.append(
                "Script step: "
                f"native tools created = {native_created or 0}, REST clients updated = {rest_updated or 0}"
            )

    if http_options_step:
        metrics = http_options_step.get("metrics", {})
        updated = metrics.get("REST Clients updated with HTTP options")
        unchanged = metrics.get("REST Clients already matching HTTP options")
        downgraded = metrics.get("HTTP/2 requests downgraded to HTTP/1 (unsupported)")
        if updated is not None or unchanged is not None or downgraded is not None:
            highlight_rows.append(
                "HTTP options: "
                f"updated = {updated or 0}, unchanged = {unchanged or 0}, HTTP/2 downgraded (unsupported) = {downgraded or 0}"
            )
    if highlight_rows:
        print("--- Highlights ---")
        for row in highlight_rows:
            print(row)


def extract_selected_test_file_name(step_output: str) -> Optional[str]:
    marker = "Selected test file name:"
    for line in step_output.splitlines():
        if line.startswith(marker):
            return line.split(":", 1)[1].strip()
    return None


def extract_created_tst_id(step_output: str) -> Optional[str]:
    marker = "Created importable SOAtest file via API:"
    for line in step_output.splitlines():
        if line.startswith(marker):
            value = line.split(":", 1)[1].strip()
            return value or None
    return None


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def normalize_tst_file_stem(name: str) -> str:
    sanitized = sanitize_filename(str(name or "").strip())
    if sanitized.lower().endswith(".tst"):
        sanitized = sanitize_filename(sanitized[:-4])
    return sanitized


def load_collection_name(collection_path: Path) -> str:
    try:
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return sanitize_filename(collection_path.stem or "Postman Collection")

    if not isinstance(collection, dict):
        return sanitize_filename(collection_path.stem or "Postman Collection")

    info = collection.get("info")
    if isinstance(info, dict):
        return sanitize_filename(str(info.get("name", collection_path.stem or "Postman Collection")))
    return sanitize_filename(collection_path.stem or "Postman Collection")


def build_auth_header(username: Optional[str], password: Optional[str]) -> Optional[str]:
    if username is None or password is None:
        return None
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _server_unavailable_message(api_base: str, reason: Optional[str] = None) -> str:
    message = (
        "Unable to connect to the SOAtest server.\n"
        "The converter requires the local SOAtest/SOAVirt server to be running before a conversion can begin.\n"
        "Please: start the SOAtest server (which hosts the SOAVirt REST API), or verify the configured "
        "server URL with --api-base if it is running on a different host or port.\n"
        f"Configured server URL: {api_base}"
    )
    if reason:
        return f"{message}\nConnection detail: {reason}"
    return message


def check_soavirt_server_reachable(
    api_base: str,
    parent_id: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> Optional[str]:
    query = urllib.parse.urlencode({"id": parent_id})
    url = api_base.rstrip("/") + "/children?" + query
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    req = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec):
            return None
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        return _server_unavailable_message(api_base, reason)


def delete_soavirt_file(
    api_base: str,
    file_id: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> None:
    query = urllib.parse.urlencode({"id": file_id, "recursive": "true"})
    url = api_base.rstrip("/") + "/files?" + query
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    req = urllib.request.Request(url=url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec):
            return
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"DELETE /files failed: HTTP {exc.code} - {detail}") from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        raise RuntimeError(_server_unavailable_message(api_base, reason)) from exc


def validate_soavirt_parent_folder_access(
    api_base: str,
    parent_id: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> Optional[str]:
    try:
        _fetch_children(
            api_base=api_base,
            parent_id=parent_id,
            auth_header=auth_header,
            timeout_sec=timeout_sec,
        )
        return None
    except RuntimeError as exc:
        return (
            f"SOAtest parent folder '{parent_id}' is not accessible through the server API. "
            f"{exc}"
        )


def _fetch_children(
    api_base: str,
    parent_id: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> List[Dict[str, Any]]:
    query = urllib.parse.urlencode({"id": parent_id})
    url = api_base.rstrip("/") + "/children?" + query
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    req = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET /children failed: HTTP {exc.code} - {detail}") from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        raise RuntimeError(_server_unavailable_message(api_base, reason)) from exc

    result = json.loads(payload) if payload.strip() else {}
    children = result.get("children", []) if isinstance(result, dict) else []
    return children if isinstance(children, list) else []


def _fetch_descendants_assets(
    api_base: str,
    resource_id: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> Dict[str, Any]:
    query = urllib.parse.urlencode({"id": resource_id})
    url = api_base.rstrip("/") + "/descendants/assets?" + query
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    req = urllib.request.Request(url=url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GET /descendants/assets failed: HTTP {exc.code} - {detail}") from exc
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        raise RuntimeError(_server_unavailable_message(api_base, reason)) from exc

    result = json.loads(payload) if payload.strip() else {}
    return result if isinstance(result, dict) else {}


def _read_schema_version_value(tst_text: str) -> Optional[str]:
    match = SCHEMA_VERSION_RE.search(tst_text)
    if not match:
        return None
    raw = match.group(0).split(":", 1)[1].strip()
    return raw.strip("'\"") if raw else None


def force_schema_version_in_file(tst_path: Path, target_schema_version: str = TARGET_TST_SCHEMA_VERSION) -> None:
    try:
        text = tst_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to read .tst for schema enforcement: {exc}") from exc

    if not SCHEMA_VERSION_RE.search(text):
        raise RuntimeError("schemaVersion header not found while enforcing target schema.")

    updated = SCHEMA_VERSION_RE.sub(f"schemaVersion: {target_schema_version}", text, count=1)
    try:
        tst_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to write .tst after schema enforcement: {exc}") from exc


def _collect_descendant_suite_paths_and_rest_count(
    descendants_payload: Dict[str, Any],
) -> Tuple[Set[Tuple[str, ...]], int, Set[str]]:
    suite_paths: Set[Tuple[str, ...]] = set()
    top_suite_names: Set[str] = set()
    rest_count = 0

    root_children = descendants_payload.get("children")
    if not isinstance(root_children, list):
        return suite_paths, rest_count, top_suite_names

    def walk_nodes(nodes: List[Dict[str, Any]], folder_path: Tuple[str, ...]) -> None:
        nonlocal rest_count
        for node in nodes:
            if not isinstance(node, dict):
                continue

            node_type = str(node.get("type", ""))
            node_name = str(node.get("name", "")).strip()
            children = node.get("children")

            if node_type == "testSuite":
                next_path = folder_path + ((node_name,) if node_name else tuple())
                if node_name:
                    suite_paths.add(next_path)
                if isinstance(children, list):
                    walk_nodes(children, next_path)
                continue

            if node_type == "restClient":
                rest_count += 1
                if isinstance(children, list):
                    walk_nodes(children, folder_path)
                continue

            if isinstance(children, list):
                walk_nodes(children, folder_path)

    for top in root_children:
        if not isinstance(top, dict):
            continue
        if str(top.get("type", "")) != "testSuite":
            continue

        top_name = str(top.get("name", "")).strip()
        if top_name:
            top_suite_names.add(top_name)
        children = top.get("children")
        if isinstance(children, list):
            walk_nodes(children, tuple())

    return suite_paths, rest_count, top_suite_names


def _validate_soavirt_read_back(
    *,
    api_base: str,
    parent_id: str,
    selected_test_file_name: str,
    expectations: Dict[str, Any],
    auth_header: Optional[str],
    timeout_sec: int,
) -> List[str]:
    errors: List[str] = []

    target = find_existing_tst_target(
        api_base=api_base,
        parent_id=parent_id,
        file_stem=selected_test_file_name,
        auth_header=auth_header,
        timeout_sec=timeout_sec,
    )
    if target is None:
        return ["SOAVirt could not locate the generated .tst during read-back validation."]

    tst_id = str(target.get("id", "")).strip()
    if not tst_id:
        return ["SOAVirt returned a target .tst entry without an id during read-back validation."]

    try:
        descendants = _fetch_descendants_assets(
            api_base=api_base,
            resource_id=tst_id,
            auth_header=auth_header,
            timeout_sec=timeout_sec,
        )
    except RuntimeError as exc:
        return [f"SOAVirt read-back traversal failed: {exc}"]

    suite_paths, rest_client_count, top_suite_names = _collect_descendant_suite_paths_and_rest_count(descendants)

    expected_collection_name = str(expectations.get("collectionName", "")).strip()
    if expected_collection_name and expected_collection_name not in top_suite_names:
        errors.append(
            "SOAVirt read-back missing expected top-level Test Suite name "
            f"'{expected_collection_name}'."
        )

    expected_request_count = int(expectations.get("requestCount", 0))
    if expected_request_count > 0 and rest_client_count < expected_request_count:
        errors.append(
            "SOAVirt read-back REST Client count mismatch: "
            f"expected at least {expected_request_count}, found {rest_client_count}."
        )

    expected_folder_paths = expectations.get("folderPaths", set())
    if isinstance(expected_folder_paths, set) and expected_folder_paths:
        missing_paths = sorted(
            "/".join(path_parts)
            for path_parts in expected_folder_paths
            if path_parts and path_parts not in suite_paths
        )
        if missing_paths:
            errors.append(
                "SOAVirt read-back missing expected folder/suite paths: "
                + ", ".join(missing_paths[:8])
            )

    return errors



def find_existing_tst_target(
    api_base: str,
    parent_id: str,
    file_stem: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> Optional[Dict[str, Any]]:
    target_name = f"{sanitize_filename(file_stem)}.tst".lower()
    for child in _fetch_children(
        api_base=api_base,
        parent_id=parent_id,
        auth_header=auth_header,
        timeout_sec=timeout_sec,
    ):
        if not isinstance(child, dict):
            continue
        if str(child.get("name", "")).lower() == target_name:
            return child
    return None


def soavirt_tst_exists(
    api_base: str,
    parent_id: str,
    file_stem: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> bool:
    existing = find_existing_tst_target(
        api_base=api_base,
        parent_id=parent_id,
        file_stem=file_stem,
        auth_header=auth_header,
        timeout_sec=timeout_sec,
    )
    return existing is not None


def resolve_test_file_name(
    api_base: str,
    parent_id: str,
    timeout_sec: int,
    auth_header: Optional[str],
    requested_name: Optional[str],
) -> str:
    if requested_name is None or not str(requested_name).strip():
        raise RuntimeError(
            "Output .tst name is required. Provide --test-file-name \"<name>\" and run again."
        )

    base_candidate = normalize_tst_file_stem(str(requested_name).strip())
    if not base_candidate:
        raise RuntimeError(
            "Output .tst name is required. Provide --test-file-name \"<name>\" and run again."
        )

    if not soavirt_tst_exists(
        api_base=api_base,
        parent_id=parent_id,
        file_stem=base_candidate,
        auth_header=auth_header,
        timeout_sec=timeout_sec,
    ):
        return base_candidate

    suffix = 2
    while True:
        candidate = normalize_tst_file_stem(f"{base_candidate}_{suffix}")
        if not soavirt_tst_exists(
            api_base=api_base,
            parent_id=parent_id,
            file_stem=candidate,
            auth_header=auth_header,
            timeout_sec=timeout_sec,
        ):
            print(f"Output name collision detected for '{base_candidate}.tst'; using '{candidate}.tst'.")
            return candidate
        suffix += 1


def _extract_auth_type(auth_obj: Any) -> Optional[str]:
    if not isinstance(auth_obj, dict):
        return None
    raw_type = auth_obj.get("type")
    if not isinstance(raw_type, str):
        return None
    auth_type = raw_type.strip().lower()
    return auth_type or None


def _is_disabled(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    return bool(entry.get("disabled"))


def _extract_query_keys(request_obj: Dict[str, Any]) -> Set[str]:
    keys: Set[str] = set()
    url_obj = request_obj.get("url")
    if isinstance(url_obj, dict):
        query = url_obj.get("query")
        if isinstance(query, list):
            for item in query:
                if not isinstance(item, dict) or _is_disabled(item):
                    continue
                key = str(item.get("key", "")).strip()
                if key:
                    keys.add(key)
        raw = url_obj.get("raw")
        if isinstance(raw, str) and raw.strip():
            parsed = urllib.parse.urlparse(raw)
            for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
                if key:
                    keys.add(key)
    elif isinstance(url_obj, str) and url_obj.strip():
        parsed = urllib.parse.urlparse(url_obj)
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if key:
                keys.add(key)
    return keys


def _extract_header_names(request_obj: Dict[str, Any]) -> Set[str]:
    names: Set[str] = set()
    headers = request_obj.get("header")
    if not isinstance(headers, list):
        return names
    for header in headers:
        if not isinstance(header, dict) or _is_disabled(header):
            continue
        key = str(header.get("key", "")).strip()
        if key:
            names.add(key)
    return names


def _extract_request_script_events(item: Dict[str, Any]) -> int:
    total = 0
    events = item.get("event")
    if not isinstance(events, list):
        return total
    for event in events:
        if not isinstance(event, dict):
            continue
        listen = str(event.get("listen", "")).strip().lower()
        if listen not in {"test", "prerequest", "pre-request"}:
            continue
        script = event.get("script")
        if isinstance(script, dict):
            exec_value = script.get("exec")
            if isinstance(exec_value, list) and any(str(line).strip() for line in exec_value):
                total += 1
            elif isinstance(exec_value, str) and exec_value.strip():
                total += 1
        elif isinstance(script, str) and script.strip():
            total += 1
    return total


def _parse_bool_like(value: Any) -> Optional[bool]:
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


def _normalize_http_option_version(value: Any) -> Optional[str]:
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


def _merge_http_option_specs(
    parent: Tuple[Optional[bool], Optional[str]],
    override: Tuple[Optional[bool], Optional[str]],
) -> Tuple[Optional[bool], Optional[str]]:
    follow_redirects = override[0] if override[0] is not None else parent[0]
    http_version = override[1] if override[1] is not None else parent[1]
    return follow_redirects, http_version


def _parse_http_option_overrides(raw: Any) -> Tuple[Optional[bool], Optional[str]]:
    if not isinstance(raw, dict):
        return None, None

    follow_redirects: Optional[bool] = None
    if "followRedirects" in raw:
        follow_redirects = _parse_bool_like(raw.get("followRedirects"))
    elif "disableRedirects" in raw:
        parsed = _parse_bool_like(raw.get("disableRedirects"))
        follow_redirects = None if parsed is None else (not parsed)
    elif "automaticallyFollowRedirects" in raw:
        follow_redirects = _parse_bool_like(raw.get("automaticallyFollowRedirects"))

    http_version: Optional[str] = None
    for key in ("httpVersion", "requestProtocol", "httpProtocolVersion"):
        if key in raw:
            normalized = _normalize_http_option_version(raw.get(key))
            if normalized is not None:
                http_version = normalized
            break

    return follow_redirects, http_version


def _count_http_option_applicable_requests(
    items: Any,
    inherited: Tuple[Optional[bool], Optional[str]],
) -> int:
    if not isinstance(items, list):
        return 0

    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue

        item_effective = _merge_http_option_specs(
            inherited,
            _parse_http_option_overrides(item.get("protocolProfileBehavior")),
        )

        nested = item.get("item")
        if isinstance(nested, list):
            count += _count_http_option_applicable_requests(nested, item_effective)

        request_obj = item.get("request")
        if isinstance(request_obj, dict):
            request_effective = _merge_http_option_specs(
                item_effective,
                _parse_http_option_overrides(request_obj.get("protocolProfileBehavior")),
            )
            if request_effective[0] is not None or request_effective[1] is not None:
                count += 1

    return count


def _extract_body_mode(request_obj: Dict[str, Any]) -> Optional[str]:
    body = request_obj.get("body")
    if not isinstance(body, dict):
        return None
    mode = str(body.get("mode", "")).strip().lower()
    if mode == "file":
        return "binary-file"
    if mode == "formdata":
        return "form-data"
    if mode == "urlencoded":
        return "x-www-form-urlencoded"
    if mode == "raw":
        return "raw"
    if mode == "graphql":
        return "graphql"
    if mode == "none":
        return "none"
    return mode or None


def _extract_collection_variable_names(collection: Dict[str, Any]) -> Set[str]:
    names: Set[str] = set()
    variables = collection.get("variable")
    if not isinstance(variables, list):
        return names
    for entry in variables:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "")).strip()
        if key:
            names.add(key)
    return names


def _collect_postman_variable_references(value: Any, names: Set[str]) -> None:
    if isinstance(value, str):
        for match in POSTMAN_VARIABLE_REFERENCE_RE.finditer(value):
            name = match.group(1).strip()
            if name:
                names.add(name)
        return

    if isinstance(value, dict):
        for nested in value.values():
            _collect_postman_variable_references(nested, names)
        return

    if isinstance(value, list):
        for nested in value:
            _collect_postman_variable_references(nested, names)


def _extract_referenced_variable_names(collection: Dict[str, Any]) -> Set[str]:
    names: Set[str] = set()
    _collect_postman_variable_references(collection, names)
    return names


def _extract_environment_variable_names(environment_path: Optional[Path]) -> Set[str]:
    if environment_path is None:
        return set()
    try:
        payload = json.loads(environment_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict):
        return set()
    values = payload.get("values")
    if not isinstance(values, list):
        return set()
    names: Set[str] = set()
    for entry in values:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "")).strip()
        if key:
            names.add(key)
    return names


def collect_postman_expectations(collection: Dict[str, Any], environment_path: Optional[Path]) -> Dict[str, Any]:
    collection_name = ""
    info = collection.get("info")
    if isinstance(info, dict):
        collection_name = str(info.get("name", "")).strip()

    collection_auth = _extract_auth_type(collection.get("auth"))
    request_count = 0
    query_keys: Set[str] = set()
    header_names: Set[str] = set()
    body_modes: Dict[str, int] = {}
    auth_counts: Dict[str, int] = {}
    request_script_events = 0
    collection_has_notes = bool(str(info.get("description", "")).strip()) if isinstance(info, dict) else False
    requests_with_notes = 0
    folder_overviews_with_notes = 0
    folder_paths: Set[Tuple[str, ...]] = set()

    def walk(items: Any, inherited_auth: Optional[str], folder_path: Tuple[str, ...]) -> None:
        nonlocal request_count, request_script_events, requests_with_notes, folder_overviews_with_notes
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue

            item_auth = inherited_auth
            item_auth_candidate = _extract_auth_type(item.get("auth"))
            if item_auth_candidate and item_auth_candidate != "inherit":
                item_auth = item_auth_candidate

            request_obj = item.get("request")
            if isinstance(request_obj, dict):
                request_count += 1
                request_auth = _extract_auth_type(request_obj.get("auth"))
                effective_auth = item_auth if request_auth in (None, "inherit") else request_auth
                if effective_auth:
                    auth_counts[effective_auth] = auth_counts.get(effective_auth, 0) + 1

                query_keys.update(_extract_query_keys(request_obj))
                header_names.update(_extract_header_names(request_obj))

                body_mode = _extract_body_mode(request_obj)
                if body_mode and body_mode != "none":
                    body_modes[body_mode] = body_modes.get(body_mode, 0) + 1

                request_script_events += _extract_request_script_events(item)

                request_description = request_obj.get("description")
                if isinstance(request_description, dict):
                    request_description = request_description.get("content") or request_description.get("text")
                if isinstance(request_description, str) and request_description.strip():
                    requests_with_notes += 1
                elif isinstance(item.get("description"), str) and str(item.get("description", "")).strip():
                    requests_with_notes += 1

            nested_items = item.get("item")
            if isinstance(nested_items, list):
                folder_description = item.get("description")
                if isinstance(folder_description, dict):
                    folder_description = folder_description.get("content") or folder_description.get("text")
                if isinstance(folder_description, str) and folder_description.strip():
                    folder_overviews_with_notes += 1
                current_folder_name = str(item.get("name", "Unnamed"))
                current_folder_path = folder_path + (current_folder_name,)
                folder_paths.add(current_folder_path)
                walk(nested_items, item_auth, current_folder_path)

    walk(collection.get("item"), collection_auth, tuple())

    collection_variables = _extract_collection_variable_names(collection)
    environment_variables = _extract_environment_variable_names(environment_path)

    collection_http_options = _parse_http_option_overrides(collection.get("protocolProfileBehavior"))
    http_options_applicable_requests = _count_http_option_applicable_requests(
        collection.get("item"),
        collection_http_options,
    )

    supported_auth_types = {"basic", "digest", "ntlm", "awsv4", "apikey", "bearer", "oauth2", "jwt"}
    applicable_auth = {key: count for key, count in auth_counts.items() if key in supported_auth_types and count > 0}

    return {
        "collectionName": collection_name,
        "requestCount": request_count,
        "queryKeys": query_keys,
        "headerNames": header_names,
        "bodyModes": body_modes,
        "requestScriptEvents": request_script_events,
        "hasNotes": collection_has_notes or requests_with_notes > 0 or folder_overviews_with_notes > 0,
        "collectionOverviewPresent": collection_has_notes,
        "folderOverviewsWithNotes": folder_overviews_with_notes,
        "requestsWithNotes": requests_with_notes,
        "authCounts": applicable_auth,
        "environmentVariableNames": environment_variables,
        "collectionVariableNames": collection_variables,
        "httpOptionsApplicableRequestCount": http_options_applicable_requests,
        "folderPaths": folder_paths,
    }


def _get_step_report(steps: List[Dict[str, Any]], prefix: str) -> Optional[Dict[str, Any]]:
    for step in steps:
        name = str(step.get("name", ""))
        if name.startswith(prefix):
            return step
    return None


def _metric_value(step: Optional[Dict[str, Any]], key: str) -> int:
    if step is None:
        return 0
    metrics = step.get("metrics", {})
    if not isinstance(metrics, dict):
        return 0
    return _int_metric(metrics, key, 0)


def expected_tst_path(workspace_root: Path, parent_id: str, file_stem: str) -> Path:
    relative_parent = parent_id.replace("\\", "/").strip("/")
    parts = [p for p in relative_parent.split("/") if p]
    return workspace_root.joinpath(*parts, f"{sanitize_filename(file_stem)}.tst")


def expected_local_parent_path(workspace_root: Path, parent_id: str) -> Path:
    relative_parent = parent_id.replace("\\", "/").strip("/")
    parts = [p for p in relative_parent.split("/") if p]
    return workspace_root.joinpath(*parts)


def validate_final_tst_against_expectations(
    *,
    step_reports: List[Dict[str, Any]],
    expectations: Dict[str, Any],
    selected_test_file_name: str,
    workspace_root: Path,
    parent_id: str,
    environment_name: str,
    api_base: str,
    auth_header: Optional[str],
    timeout_sec: int,
) -> List[str]:
    errors: List[str] = []

    required_steps = [
        "Step 1: Endpoint Sequence",
        "Step 2: Params Implementation",
        "Step 3: Variable Implementation",
        "Step 4: Authorization Implementation",
        "Step 5: Header Implementation",
        "Step 6: Request Body Implementation",
        "Step 7: Script Implementation",
        "Step 8: Notes Implementation",
        "Step 9: HTTP Options Implementation",
    ]

    for prefix in required_steps:
        step = _get_step_report(step_reports, prefix)
        if step is None:
            errors.append(f"Missing required conversion phase: {prefix}")
            continue
        if str(step.get("status", "")) != "SUCCESS":
            errors.append(f"Conversion phase did not complete successfully: {prefix}")

    if errors:
        return errors

    target_path = expected_tst_path(workspace_root, parent_id, selected_test_file_name)
    if not target_path.exists():
        return [f"Final .tst file was not generated at expected path: {target_path}"]

    try:
        force_schema_version_in_file(target_path, TARGET_TST_SCHEMA_VERSION)
        tst_text = target_path.read_text(encoding="utf-8")
        persisted_schema = _read_schema_version_value(tst_text)
    except RuntimeError as exc:
        return [f"[Schema Enforcement] {exc}"]
    except OSError as exc:
        return [f"[Schema Enforcement] Failed to read generated .tst after schema enforcement: {exc}"]

    if persisted_schema != TARGET_TST_SCHEMA_VERSION:
        return [
            "[Schema Enforcement] schemaVersion persistence check failed: "
            f"expected '{TARGET_TST_SCHEMA_VERSION}', found '{persisted_schema}'."
        ]

    params_step = _get_step_report(step_reports, "Step 2: Params Implementation")
    variable_step = _get_step_report(step_reports, "Step 3: Variable Implementation")
    auth_step = _get_step_report(step_reports, "Step 4: Authorization Implementation")
    auth_fallback_step = _get_step_report(step_reports, "Step 4b: SOAtest Template Fallback")
    header_step = _get_step_report(step_reports, "Step 5: Header Implementation")
    body_step = _get_step_report(step_reports, "Step 6: Request Body Implementation")
    binary_fallback_step = _get_step_report(step_reports, "Step 6b: Binary Body Template Fallback")
    script_step = _get_step_report(step_reports, "Step 7: Script Implementation")
    notes_step = _get_step_report(step_reports, "Step 8: Notes Implementation")
    http_options_step = _get_step_report(step_reports, "Step 9: HTTP Options Implementation")

    if _metric_value(params_step, "Request-to-REST mapping misses") > 0:
        errors.append("Params phase reported request-to-REST mapping misses.")
    if _metric_value(params_step, "Update failures") > 0:
        errors.append("Params phase reported update failures.")

    query_keys = expectations.get("queryKeys", set())
    if isinstance(query_keys, set) and query_keys:
        missing_query_keys = sorted(key for key in query_keys if f"{key}=" not in tst_text and key not in tst_text)
        if missing_query_keys:
            errors.append(
                "Query parameter mappings missing from generated .tst for keys: "
                + ", ".join(missing_query_keys[:8])
            )

    expected_environment = bool(expectations.get("environmentVariableNames") or expectations.get("collectionVariableNames"))
    if expected_environment:
        if f"name: {environment_name}" not in tst_text:
            errors.append("Environment mapping missing: expected environment section was not found in .tst.")
        if _metric_value(variable_step, "External/pre-declared variables implemented") <= 0:
            errors.append("Variable phase did not report implemented external/pre-declared variables.")

    auth_counts = expectations.get("authCounts", {})
    if isinstance(auth_counts, dict) and auth_counts:
        if "authentications:" not in tst_text and "Authorization" not in tst_text:
            errors.append("Authentication mapping missing from generated .tst.")

        direct_keys = {
            "basic": "Basic auth applied",
            "digest": "Digest auth applied",
            "ntlm": "NTLM auth applied",
            "awsv4": "AWS Signature auth applied",
            "apikey": "API Key auth mapped",
            "bearer": "Bearer auth mapped",
        }
        for auth_type, metric_key in direct_keys.items():
            expected_count = int(auth_counts.get(auth_type, 0))
            if expected_count > 0 and _metric_value(auth_step, metric_key) <= 0:
                errors.append(f"Auth mapping missing for type '{auth_type}'.")

        fallback_required = int(auth_counts.get("oauth2", 0)) + int(auth_counts.get("jwt", 0))
        if fallback_required > 0:
            if auth_fallback_step is None or str(auth_fallback_step.get("status", "")) != "SUCCESS":
                errors.append("Auth template fallback did not run successfully for OAuth2/JWT mapping.")
            elif (
                _metric_value(auth_fallback_step, "REST clients patched by template fallback")
                + _metric_value(auth_fallback_step, "Requests already matching expected auth")
            ) <= 0:
                errors.append("Auth template fallback ran but did not materialize any OAuth2/JWT auth mappings.")

    header_names = expectations.get("headerNames", set())
    if isinstance(header_names, set) and header_names:
        if _metric_value(header_step, "Request-to-REST mapping misses") > 0:
            errors.append("Header phase reported request-to-REST mapping misses.")
        if _metric_value(header_step, "Update failures") > 0:
            errors.append("Header phase reported update failures.")
        lowered = tst_text.lower()
        missing_headers = sorted(name for name in header_names if name.lower() not in lowered)
        if missing_headers:
            errors.append("Header mappings missing from generated .tst for: " + ", ".join(missing_headers[:8]))

    body_modes = expectations.get("bodyModes", {})
    if isinstance(body_modes, dict) and body_modes:
        mode_metric = {
            "raw": "Body mode raw applied",
            "form-data": "Body mode form-data applied",
            "x-www-form-urlencoded": "Body mode x-www-form-urlencoded applied",
            "graphql": "Body mode graphql applied",
            "binary-file": "Body mode binary-file detected (manual setup required)",
        }
        for mode, count in body_modes.items():
            metric_key = mode_metric.get(mode)
            if metric_key and int(count) > 0 and _metric_value(body_step, metric_key) <= 0:
                errors.append(f"Request body mapping missing for mode '{mode}'.")
        if _metric_value(body_step, "Body update failures") > 0:
            errors.append("Request body phase reported update failures.")

        binary_expected = int(body_modes.get("binary-file", 0))
        if binary_expected > 0:
            if binary_fallback_step is None or str(binary_fallback_step.get("status", "")) != "SUCCESS":
                errors.append("Binary body fallback phase did not run successfully.")
            else:
                if (
                    _metric_value(binary_fallback_step, "Binary literal/file payload blocks patched by template fallback")
                    + _metric_value(binary_fallback_step, "Binary payload blocks already matching template")
                ) < binary_expected:
                    errors.append("Binary body fallback did not patch all expected binary request payload mappings.")
                if _metric_value(binary_fallback_step, "Binary body requests not found in .tst") > 0:
                    errors.append("Binary body fallback reported requests missing in .tst.")
                if _metric_value(binary_fallback_step, "Binary body template entries missing for requests") > 0:
                    errors.append("Binary body fallback reported missing template entries.")

    request_script_events = int(expectations.get("requestScriptEvents", 0))
    if request_script_events > 0:
        processed = _metric_value(script_step, "Request script events processed")
        if processed < request_script_events:
            errors.append(
                f"Script phase processed fewer events than expected ({processed}/{request_script_events})."
            )
        if "Postman Script" not in tst_text and _metric_value(script_step, "REST clients updated") <= 0:
            errors.append("Script phase did not materialize script mappings in generated .tst.")

    if bool(expectations.get("hasNotes")):
        expected_collection_notes = 1 if bool(expectations.get("collectionOverviewPresent")) else 0
        expected_folder_notes = int(expectations.get("folderOverviewsWithNotes", 0))
        expected_request_notes = int(expectations.get("requestsWithNotes", 0))

        collection_applied = _metric_value(notes_step, "Collection overview applied")
        folder_applied = _metric_value(notes_step, "Folder overviews applied")
        request_docs_applied = _metric_value(notes_step, "Request docs applied")
        request_comments_applied = _metric_value(notes_step, "Request comments applied")

        if expected_collection_notes > 0 and collection_applied < expected_collection_notes:
            errors.append("Notes phase did not apply collection overview notes.")
        if expected_folder_notes > 0 and folder_applied < expected_folder_notes:
            errors.append(
                "Notes phase did not apply all folder overview notes "
                f"({folder_applied}/{expected_folder_notes})."
            )
        if expected_request_notes > 0 and (request_docs_applied + request_comments_applied) <= 0:
            errors.append("Notes phase did not apply request-level note mappings.")
        if _metric_value(notes_step, "Suites updated") <= 0:
            errors.append("Notes phase did not report any suite updates.")

    http_options_applicable_count = int(expectations.get("httpOptionsApplicableRequestCount", 0))
    if http_options_applicable_count > 0:
        requests_evaluated = _metric_value(http_options_step, "Requests evaluated")
        if requests_evaluated < http_options_applicable_count:
            errors.append(
                "HTTP options phase did not evaluate all applicable requests "
                f"({requests_evaluated}/{http_options_applicable_count})."
            )
        if _metric_value(http_options_step, "Request-to-REST mapping misses") > 0:
            errors.append("HTTP options phase reported request-to-REST mapping misses.")
        if _metric_value(http_options_step, "Update failures") > 0:
            errors.append("HTTP options phase reported update failures.")

    if errors:
        return [f"[SOAtest Structure Validation] {msg}" for msg in errors]

    api_errors = _validate_soavirt_read_back(
        api_base=api_base,
        parent_id=parent_id,
        selected_test_file_name=selected_test_file_name,
        expectations=expectations,
        auth_header=auth_header,
        timeout_sec=timeout_sec,
    )
    if api_errors:
        return [f"[SOAVirt API Read-Back Validation] {msg}" for msg in api_errors]

    return []


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run full Postman-to-SOAtest workflow: endpoint sequence + query parameter implementation + "
            "variable implementation + authorization implementation + auth persistence template fallback + "
            "header implementation + request body implementation + script implementation + notes implementation + HTTP options implementation."
        )
    )
    parser.add_argument("postman_collection", type=Path, help="Path to exported Postman Collection JSON file.")
    parser.add_argument(
        "--postman-environment",
        type=Path,
        default=None,
        help=(
            "Optional path to exported Postman environment JSON. Required when collection variable references need external values."
        ),
    )
    parser.add_argument(
        "--api-base",
        default="http://localhost:9080/soavirt/api/v6",
        help="SOAtest server API base URL (default: http://localhost:9080/soavirt/api/v6).",
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
        help="Required output .tst file name (with or without .tst extension).",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=None,
        help=(
            "Local SOAtest workspace root for endpoint sequence output and template fallback patching. "
            "Required; no auto-discovery is performed."
        ),
    )
    parser.add_argument(
        "--form-data-file-mode",
        choices=["variable", "placeholder"],
        default="variable",
        help=(
            "How form-data file fields are represented in body conversion. "
            "'variable' maps to ${...} file-path vars (default); "
            "'placeholder' keeps <<FILE:...>> markers."
        ),
    )
    parser.add_argument(
        "--environment-name",
        default="Default Environment",
        help="SOAtest environment name for variable implementation.",
    )
    args = parser.parse_args(argv)


    if not args.test_file_name:
        print(
            "ERROR: Output .tst name is required. Provide --test-file-name \"<name>\" and run again.",
            file=sys.stderr,
        )
        return 1

    if args.workspace_root is None:
        print(
            "ERROR: Workspace root is required. Provide --workspace-root \"<soatest-workspace-root>\" and run again.",
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

    repo_root = Path(__file__).resolve().parents[3]
    started_at = datetime.now(timezone.utc)
    started_utc = started_at.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    selected_test_file_name = normalize_tst_file_stem(args.test_file_name)
    postman_environment_path: Optional[Path] = None
    step_reports: List[Dict[str, Any]] = []
    run_created_tst_id: Optional[str] = None

    def cleanup_failed_run_output() -> None:
        nonlocal run_created_tst_id
        if not run_created_tst_id:
            return
        try:
            delete_soavirt_file(
                api_base=args.api_base,
                file_id=run_created_tst_id,
                auth_header=auth_header,
                timeout_sec=args.timeout_sec,
            )
            print(f"Cleanup: removed incomplete run-created .tst: {run_created_tst_id}")
        except RuntimeError as exc:
            print(
                "WARNING: Failed to clean up run-created .tst after conversion failure: "
                f"{exc}",
                file=sys.stderr,
            )
        finally:
            run_created_tst_id = None

    def finalize_and_return(exit_code: int, failed_step: Optional[str]) -> int:
        if exit_code != 0:
            cleanup_failed_run_output()
        finished_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        workflow_report = build_workflow_report(
            started_utc=started_utc,
            finished_utc=finished_utc,
            status="SUCCESS" if exit_code == 0 else "FAILED",
            failed_step=failed_step,
            collection_path=args.postman_collection,
            environment_path=postman_environment_path,
            api_base=args.api_base,
            parent_id=args.parent_id,
            selected_test_file_name=selected_test_file_name,
            steps=step_reports,
        )
        print_uniform_report(workflow_report)
        if exit_code == 0:
            print("=== Workflow Complete ===")
        else:
            print("=== Workflow Failed ===")
        return exit_code

    def run_and_record(cmd: List[str], step_name: str) -> Tuple[int, str]:
        rc, output = run_step(cmd, step_name)
        step_reports.append(build_step_report(step_name, cmd, rc, output))
        return rc, output

    if not args.postman_collection.exists():
        print(f"ERROR: Input file does not exist: {args.postman_collection}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Input Validation")

    try:
        collection = json.loads(args.postman_collection.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"ERROR: Input is not valid JSON: {exc}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Input Validation")
    except OSError as exc:
        print(f"ERROR: Failed to read input file: {exc}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Input Validation")

    if not isinstance(collection, dict):
        print("ERROR: Postman collection root must be a JSON object.", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Input Validation")

    referenced_variable_names = _extract_referenced_variable_names(collection)

    if args.postman_environment is None:
        if referenced_variable_names:
            sample_names = sorted(referenced_variable_names)
            preview = ", ".join(sample_names[:8])
            if len(sample_names) > 8:
                preview += ", ..."
            print(
                "ERROR: Postman environment file is required for this conversion because "
                "variable references were detected in the collection "
                f"({preview}). Provide --postman-environment \"<environment.json>\" and run again.",
                file=sys.stderr,
            )
            return finalize_and_return(1, "Preflight: Input Validation")
    else:
        if not args.postman_environment.exists():
            print(f"ERROR: Postman environment file does not exist: {args.postman_environment}", file=sys.stderr)
            return finalize_and_return(1, "Preflight: Input Validation")
        postman_environment_path = args.postman_environment

    auth_header = build_auth_header(args.username, args.password)
    server_reachability_error = check_soavirt_server_reachable(
        api_base=args.api_base,
        parent_id=args.parent_id,
        auth_header=auth_header,
        timeout_sec=args.timeout_sec,
    )
    if server_reachability_error:
        print(f"ERROR: {server_reachability_error}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: SOAVirt Server Reachability")

    parent_folder_error = validate_soavirt_parent_folder_access(
        api_base=args.api_base,
        parent_id=args.parent_id,
        auth_header=auth_header,
        timeout_sec=args.timeout_sec,
    )
    if parent_folder_error:
        print(f"ERROR: {parent_folder_error}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: SOAtest Parent Folder Validation")

    expectations = collect_postman_expectations(collection, postman_environment_path)

    try:
        selected_test_file_name = resolve_test_file_name(
            api_base=args.api_base,
            parent_id=args.parent_id,
            timeout_sec=args.timeout_sec,
            auth_header=auth_header,
            requested_name=args.test_file_name,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Target Name Resolution")

    endpoint_script = repo_root / "skills" / "postman-endpoint-sequence" / "scripts" / "create_endpoint_sequence.py"
    params_script = (
        repo_root / "skills" / "postman-params-implementation" / "scripts" / "postman_params_implementation.py"
    )
    variable_script = (
        repo_root / "skills" / "postman-variable-implementation" / "scripts" / "postman_variable_implementation.py"
    )
    auth_script = (
        repo_root / "skills" / "postman-auth-implementation" / "scripts" / "postman_auth_implementation.py"
    )
    template_fallback_script = (
        repo_root / "skills" / "soatest-template-fallback" / "scripts" / "soatest_template_fallback.py"
    )
    header_script = (
        repo_root / "skills" / "postman-header-implementation" / "scripts" / "postman_header_implementation.py"
    )
    body_script = (
        repo_root
        / "skills"
        / "postman-request-body-implementation"
        / "scripts"
        / "postman_request_body_implementation.py"
    )
    script_impl_script = (
        repo_root
        / "skills"
        / "postman-script-implementation"
        / "scripts"
        / "postman_script_implementation.py"
    )
    notes_script = (
        repo_root
        / "skills"
        / "postman-notes-implementation"
        / "scripts"
        / "postman_notes_implementation.py"
    )
    http_options_script = (
        repo_root
        / "skills"
        / "postman-http-options-implementation"
        / "scripts"
        / "postman_http_options_implementation.py"
    )

    if not endpoint_script.exists():
        print(f"ERROR: Missing endpoint sequence script: {endpoint_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not params_script.exists():
        print(f"ERROR: Missing params implementation script: {params_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not variable_script.exists():
        print(f"ERROR: Missing variable implementation script: {variable_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not auth_script.exists():
        print(f"ERROR: Missing auth implementation script: {auth_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not template_fallback_script.exists():
        print(f"ERROR: Missing template fallback script: {template_fallback_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not header_script.exists():
        print(f"ERROR: Missing header implementation script: {header_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not body_script.exists():
        print(f"ERROR: Missing request body implementation script: {body_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not script_impl_script.exists():
        print(f"ERROR: Missing script implementation script: {script_impl_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not notes_script.exists():
        print(f"ERROR: Missing notes implementation script: {notes_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")
    if not http_options_script.exists():
        print(f"ERROR: Missing HTTP options implementation script: {http_options_script}", file=sys.stderr)
        return finalize_and_return(1, "Preflight: Script Discovery")

    endpoint_cmd = [
        sys.executable,
        str(endpoint_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
        "--workspace-root",
        str(args.workspace_root),
    ]
    if args.username is not None:
        endpoint_cmd.extend(["--username", args.username])
    if args.password is not None:
        endpoint_cmd.extend(["--password", args.password])
    endpoint_cmd.extend(["--test-file-name", selected_test_file_name])
    endpoint_cmd.append("--skip-name-collision-check")
    rc, endpoint_output = run_and_record(internal_phase_cmd(endpoint_cmd), "Step 1: Endpoint Sequence")
    if rc != 0:
        return finalize_and_return(rc, "Step 1: Endpoint Sequence")

    run_created_tst_id = extract_created_tst_id(endpoint_output)
    endpoint_selected_name = extract_selected_test_file_name(endpoint_output)
    if endpoint_selected_name:
        selected_test_file_name = endpoint_selected_name

    params_cmd = [
        sys.executable,
        str(params_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
    ]
    if args.username is not None:
        params_cmd.extend(["--username", args.username])
    if args.password is not None:
        params_cmd.extend(["--password", args.password])
    params_cmd.extend(["--target-test-file-name", selected_test_file_name])

    rc, _ = run_and_record(internal_phase_cmd(params_cmd), "Step 2: Params Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 2: Params Implementation")

    variable_cmd = [
        sys.executable,
        str(variable_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
        "--environment-name",
        args.environment_name,
    ]
    if args.username is not None:
        variable_cmd.extend(["--username", args.username])
    if args.password is not None:
        variable_cmd.extend(["--password", args.password])
    if postman_environment_path is not None:
        variable_cmd.extend(["--postman-environment", str(postman_environment_path)])
    variable_cmd.extend(["--target-test-file-name", selected_test_file_name])
    rc, _ = run_and_record(internal_phase_cmd(variable_cmd), "Step 3: Variable Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 3: Variable Implementation")

    auth_cmd = [
        sys.executable,
        str(auth_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--workspace-root",
        str(args.workspace_root),
        "--timeout-sec",
        str(args.timeout_sec),
    ]
    if args.username is not None:
        auth_cmd.extend(["--username", args.username])
    if args.password is not None:
        auth_cmd.extend(["--password", args.password])
    if postman_environment_path is not None:
        auth_cmd.extend(["--postman-environment", str(postman_environment_path)])
    auth_cmd.extend(["--target-test-file-name", selected_test_file_name])

    rc, auth_output = run_and_record(internal_phase_cmd(auth_cmd), "Step 4: Authorization Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 4: Authorization Implementation")

    template_fallback_cmd: Optional[List[str]] = None
    auth_metrics = parse_step_metrics(auth_output)
    auth_failures = _int_metric(auth_metrics, "Auth update failures", 0)
    oauth2_failures = _int_metric(auth_metrics, "OAuth 2.0 requiring template fallback", 0)
    auth_warnings = parse_step_warning_messages(auth_output)
    persistence_warning_count = sum(
        1
        for warning in auth_warnings
        if "did not persist as expected" in warning or "Auth update failed" in warning
    )
    run_template_fallback = auth_failures > 0 or oauth2_failures > 0 or persistence_warning_count > 0

    if run_template_fallback:
        print(
            "Running template fallback due to Step 4 authentication persistence failures "
            f"(failures={auth_failures}, oauth2={oauth2_failures}, warnings={persistence_warning_count})."
        )
        if not template_fallback_script.exists():
            print(f"ERROR: Missing template fallback script: {template_fallback_script}", file=sys.stderr)
            return finalize_and_return(1, "Step 4b: SOAtest Template Fallback")
        template_fallback_cmd = [
            sys.executable,
            str(template_fallback_script),
            str(args.postman_collection),
            "--workspace-root",
            str(args.workspace_root),
            "--parent-id",
            args.parent_id,
            "--target-test-file-name",
            selected_test_file_name,
        ]
        if postman_environment_path is not None:
            template_fallback_cmd.extend(["--postman-environment", str(postman_environment_path)])
        rc, _ = run_and_record(internal_phase_cmd(template_fallback_cmd), "Step 4b: SOAtest Template Fallback")
        if rc != 0:
            return finalize_and_return(rc, "Step 4b: SOAtest Template Fallback")

        resolved_warnings = reconcile_auth_warnings_with_template_fallback(step_reports)
        if resolved_warnings > 0:
            print(
                "Resolved auth persistence warnings via template fallback: "
                f"{resolved_warnings}"
            )

    header_cmd = [
        sys.executable,
        str(header_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
    ]
    if args.username is not None:
        header_cmd.extend(["--username", args.username])
    if args.password is not None:
        header_cmd.extend(["--password", args.password])
    header_cmd.extend(["--target-test-file-name", selected_test_file_name])

    rc, _ = run_and_record(internal_phase_cmd(header_cmd), "Step 5: Header Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 5: Header Implementation")

    body_cmd = [
        sys.executable,
        str(body_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
        "--form-data-file-mode",
        args.form_data_file_mode,
    ]
    if args.username is not None:
        body_cmd.extend(["--username", args.username])
    if args.password is not None:
        body_cmd.extend(["--password", args.password])
    body_cmd.extend(["--target-test-file-name", selected_test_file_name])
    rc, body_output = run_and_record(internal_phase_cmd(body_cmd), "Step 6: Request Body Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 6: Request Body Implementation")

    body_metrics = parse_step_metrics(body_output)
    binary_body_count = _int_metric(
        body_metrics,
        "Body mode binary-file detected (manual setup required)",
        0,
    )
    if binary_body_count > 0:
        if not template_fallback_script.exists():
            print(f"ERROR: Missing template fallback script: {template_fallback_script}", file=sys.stderr)
            return finalize_and_return(1, "Step 6b: Binary Body Template Fallback")

        binary_fallback_cmd = [
            sys.executable,
            str(template_fallback_script),
            str(args.postman_collection),
            "--mode",
            "binary-body",
            "--workspace-root",
            str(args.workspace_root),
            "--parent-id",
            args.parent_id,
            "--target-test-file-name",
            selected_test_file_name,
        ]
        if postman_environment_path is not None:
            binary_fallback_cmd.extend(["--postman-environment", str(postman_environment_path)])
        rc, _ = run_and_record(internal_phase_cmd(binary_fallback_cmd), "Step 6b: Binary Body Template Fallback")
        if rc != 0:
            return finalize_and_return(rc, "Step 6b: Binary Body Template Fallback")

    script_cmd = [
        sys.executable,
        str(script_impl_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
        "--target-test-file-name",
        selected_test_file_name,
        "--environment-name",
        args.environment_name,
    ]
    if args.username is not None:
        script_cmd.extend(["--username", args.username])
    if args.password is not None:
        script_cmd.extend(["--password", args.password])
    rc, _ = run_and_record(internal_phase_cmd(script_cmd), "Step 7: Script Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 7: Script Implementation")

    if collection_has_difference_assertions(args.postman_collection):
        if not template_fallback_script.exists():
            print(f"ERROR: Missing template fallback script: {template_fallback_script}", file=sys.stderr)
            return finalize_and_return(1, "Step 7b: Difference Assertion Template Fallback")
        diff_fallback_cmd = [
            sys.executable,
            str(template_fallback_script),
            str(args.postman_collection),
            "--mode",
            "difference-assertions",
            "--workspace-root",
            str(args.workspace_root),
            "--parent-id",
            args.parent_id,
            "--target-test-file-name",
            selected_test_file_name,
        ]
        rc, _ = run_and_record(internal_phase_cmd(diff_fallback_cmd), "Step 7b: Difference Assertion Template Fallback")
        if rc != 0:
            return finalize_and_return(rc, "Step 7b: Difference Assertion Template Fallback")

    notes_cmd = [
        sys.executable,
        str(notes_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
    ]
    if args.username is not None:
        notes_cmd.extend(["--username", args.username])
    if args.password is not None:
        notes_cmd.extend(["--password", args.password])
    notes_cmd.extend(["--target-test-file-name", selected_test_file_name])

    rc, _ = run_and_record(internal_phase_cmd(notes_cmd), "Step 8: Notes Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 8: Notes Implementation")

    if run_template_fallback and template_fallback_cmd is not None:
        print("Re-applying template fallback after Step 8 to preserve final auth mapping.")
        rc, _ = run_and_record(
            internal_phase_cmd(template_fallback_cmd),
            "Step 8b: SOAtest Template Fallback Finalize",
        )
        if rc != 0:
            return finalize_and_return(rc, "Step 8b: SOAtest Template Fallback Finalize")

    http_options_cmd = [
        sys.executable,
        str(http_options_script),
        str(args.postman_collection),
        "--api-base",
        args.api_base,
        "--parent-id",
        args.parent_id,
        "--timeout-sec",
        str(args.timeout_sec),
        "--target-test-file-name",
        selected_test_file_name,
    ]
    if args.username is not None:
        http_options_cmd.extend(["--username", args.username])
    if args.password is not None:
        http_options_cmd.extend(["--password", args.password])

    rc, _ = run_and_record(internal_phase_cmd(http_options_cmd), "Step 9: HTTP Options Implementation")
    if rc != 0:
        return finalize_and_return(rc, "Step 9: HTTP Options Implementation")

    postflight_errors = validate_final_tst_against_expectations(
        step_reports=step_reports,
        expectations=expectations,
        selected_test_file_name=selected_test_file_name,
        workspace_root=args.workspace_root,
        parent_id=args.parent_id,
        environment_name=args.environment_name,
        api_base=args.api_base,
        auth_header=auth_header,
        timeout_sec=args.timeout_sec,
    )
    if postflight_errors:
        print("ERROR: End-to-end conversion validation failed.", file=sys.stderr)
        for error in postflight_errors:
            print(f"- {error}", file=sys.stderr)
        return finalize_and_return(1, "Postflight: End-to-End Validation")

    return finalize_and_return(0, None)


if __name__ == "__main__":
    raise SystemExit(main())


