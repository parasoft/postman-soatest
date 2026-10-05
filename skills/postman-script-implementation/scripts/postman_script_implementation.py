#!/usr/bin/env python3
"""
Implement Postman pre-request and test scripts into SOAtest.

Strategy:
- Preserve script phase distinction:
  - prerequest -> pre-request stage (Request Traffic output provider)
  - test -> post-request stage (Response Traffic output provider)
- Prefer native SOAtest tools:
  - JSON Data Bank for pm.*.set(..., json....)
  - JSON Assertor for pm.expect(json....) assertions
  - REST Client valid response codes for pm.response.code assertions
- Never emit raw/minimally-modified Postman pm.* scripts into SOAtest.
- Omit unsupported script patterns instead of generating non-functional script fallbacks.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


SCRIPT_MODE_PREFER_NATIVE = "prefer-native-tools"
SCRIPT_STORAGE_EMBEDDED = "embedded"
SCRIPT_STORAGE_EXTERNAL = "external"
DEFAULT_SCRIPT_LANGUAGE = "JavaScript (OpenJDK Nashorn)"

LISTEN_PRE = "prerequest"
LISTEN_POST = "test"
PHASE_PRE = "pre-request"
PHASE_POST = "post-request"
PHASE_UNKNOWN = "unknown"

VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")

SET_CALL_RE = re.compile(
    r"pm\.(?:environment|collectionVariables|variables)\.set\(\s*(['\"`])(?P<name>[^'\"`]+)\1\s*,\s*(?P<expr>.*?)\)\s*;",
    re.DOTALL,
)
ENV_GET_RE = re.compile(
    r"pm\.(?:environment|collectionVariables|variables)\.get\(\s*(['\"`])(?P<name>[^'\"`]+)\1\s*\)"
)
ITERATION_DATA_GET_RE = re.compile(
    r"pm\.iterationData\.get\(\s*(['\"`])(?P<name>[^'\"`]+)\1\s*\)"
)
ALIAS_ASSIGN_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<alias>[A-Za-z_]\w*)\s*=\s*(?P<expr>[^;]+);",
    re.DOTALL,
)
FILTER_FIND_ALIAS_RE = re.compile(
    r"^(?P<source>[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*)|\[\d+\])*)\.(?P<kind>filter|find)\(",
    re.DOTALL,
)
PM_TEST_NAME_RE = re.compile(
    r"pm\.test\(\s*(['\"])(?P<name>(?:\\.|(?!\1).)*)\1\s*,",
    re.DOTALL,
)

EXPECT_EQUAL_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^;]*?)\s*\)\s*\.to\s*(?P<be>\.\s*be\s*)?"
    r"(?P<not>\.\s*not\s*)?\.\s*(?P<cmp>equal|eql|include|contain|above|below)\s*"
    r"\(\s*(?P<expected>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_AT_LEAST_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*\.\s*be\s*\.\s*at\s*\.\s*least\s*"
    r"\(\s*(?P<expected>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_AT_MOST_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*\.\s*be\s*\.\s*at\s*\.\s*most\s*"
    r"\(\s*(?P<expected>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_WITHIN_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>.*?)\s*\)\s*\.to\s*(?:\.\s*be\s*)?(?P<not>\.\s*not\s*)?"
    r"\.\s*within\s*\(\s*(?P<lower>.*?)\s*,\s*(?P<upper>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_ONEOF_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^;]*?)\s*\)\s*\.to\s*(?:\.\s*be\s*)?(?P<not>\.\s*not\s*)?"
    r"\.\s*oneOf\s*\(\s*\[(?P<values>.*?)\]\s*\)\s*;",
    re.DOTALL,
)

EXPECT_INCLUDE_CHAIN_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>.*?)\s*\)\s*\.to\s*(?P<not>\.\s*not\s*)?"
    r"\.\s*(?P<cmp>include|contain)\s*\(\s*(?P<expected>.*?)\s*\)"
    r"(?P<chain>(?:\s*\.and\s*(?:\.\s*to\s*)?\.\s*(?:include|contain)\s*\(\s*.*?\s*\))+)\s*;",
    re.DOTALL,
)

EXPECT_INCLUDE_CHAIN_PART_RE = re.compile(
    r"\.and\s*(?:\.\s*to\s*)?\.\s*(?P<cmp>include|contain)\s*\(\s*(?P<expected>.*?)\s*\)",
    re.DOTALL,
)

EXPECT_LENGTH_OF_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*(?P<not>\.\s*not\s*)?"
    r"(?:\.\s*have\s*)?\.\s*lengthOf\s*\(\s*(?P<expected>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_PROPERTY_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*(?P<not>\.\s*not\s*)?"
    r"\.\s*have\s*\.\s*property\s*\(\s*(?P<args>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_MATCH_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>.*?)\s*\)\s*\.to\s*(?P<not>\.\s*not\s*)?\.\s*match\s*"
    r"\(\s*(?P<expected>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_EXIST_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*(?P<not>\.\s*not\s*)?(?:\.\s*be\s*)?"
    r"\.\s*(?:exist|exists)\s*(?:\(\s*\))?\s*;",
    re.DOTALL,
)

EXPECT_EMPTY_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*(?P<not>\.\s*not\s*)?(?:\.\s*be\s*)?"
    r"\.\s*empty\s*(?:\(\s*\))?\s*;",
    re.DOTALL,
)

EXPECT_TYPE_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*(?P<be>\.\s*be\s*)?(?P<not>\.\s*not\s*)?"
    r"\.\s*(?:a|an)\s*\(\s*(?P<expected>.*?)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_LITERAL_STATE_RE = re.compile(
    r"pm\.expect\(\s*(?P<actual>[^\n;]*?)\s*\)\s*\.to\s*(?P<not>\.\s*not\s*)?"
    r"(?:\.\s*be\s*)?\.\s*(?P<state>true|false|null)\s*(?:\(\s*\))?\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_CODE_EQUAL_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.code\s*\)\s*\.to\s*(?:\.\s*be\s*)?\.\s*(?:equal|eql)\s*"
    r"\(\s*(?P<code>\d{3})\s*\)\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_CODE_ONEOF_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.code\s*\)\s*\.to\s*\.\s*be\s*\.\s*oneOf\s*"
    r"\(\s*\[(?P<codes>[^\]]+)\]\s*\)\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_CODE_INCLUDED_IN_ARRAY_RE = re.compile(
    r"pm\.expect\(\s*\[(?P<codes>[^\]]+)\]\s*\)\s*\.to\s*(?:\.\s*be\s*)?"
    r"\.\s*(?:include|contain)\s*\(\s*pm\.response\.code\s*\)\s*(?=;|\)|$)",
    re.DOTALL,
)

EXPECT_RESPONSE_CODE_COMPARE_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.code\s*\)\s*\.to\s*(?:\.\s*be\s*)?"
    r"\.\s*(?P<cmp>above|below|greaterThan|lessThan)\s*\(\s*(?P<code>\d{3})\s*\)\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_CODE_WITHIN_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.code\s*\)\s*\.to\s*(?:\.\s*be\s*)?"
    r"\.\s*within\s*\(\s*(?P<lower>\d{3})\s*,\s*(?P<upper>\d{3})\s*\)\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_CODE_AT_LEAST_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.code\s*\)\s*\.to\s*\.\s*be\s*\.\s*at\s*\.\s*least\s*"
    r"\(\s*(?P<code>\d{3})\s*\)\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_CODE_AT_MOST_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.code\s*\)\s*\.to\s*\.\s*be\s*\.\s*at\s*\.\s*most\s*"
    r"\(\s*(?P<code>\d{3})\s*\)\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_HAVE_STATUS_RE = re.compile(
    r"pm\.response\.to\s*\.\s*have\s*\.\s*status\s*\(\s*(?P<code>\d{3})\s*\)\s*(?=;|\)|$)",
    re.DOTALL,
)

EXPECT_RESPONSE_TIME_BELOW_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.responseTime\s*\)\s*\.to\s*(?:\.\s*be\s*)?"
    r"\.\s*(?:below|lessThan)\s*\(\s*(?P<ms>\d+)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_RESPONSE_TIME_AT_MOST_RE = re.compile(
    r"pm\.expect\(\s*pm\.response\.responseTime\s*\)\s*\.to\s*\.\s*be\s*\.\s*at\s*\.\s*most\s*"
    r"\(\s*(?P<ms>\d+)\s*\)\s*;",
    re.DOTALL,
)

EXPECT_ENV_GET_EXIST_RE = re.compile(
    r"pm\.expect\(\s*pm\.(?:environment|collectionVariables|variables)\.get\(\s*(['\"`])(?P<name>[^'\"`]+)\1\s*\)\s*\)\s*"
    r"\.to\s*(?P<not>\.\s*not\s*)?(?:\.\s*be\s*)?\.\s*(?:exist|exists)\s*(?:\(\s*\))?\s*;",
    re.DOTALL,
)

DATE_LITERAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_LITERAL_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ][0-2]\d:[0-5]\d(?::[0-5]\d(?:\.\d{1,9})?)?(?:Z|[+\-][0-2]\d:[0-5]\d)?$"
)
NUMERIC_LITERAL_RE = re.compile(r"^[+\-]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+\-]?\d+)?$")

ADVANCED_SCRIPT_PATTERN_RE = re.compile(
    r"\bif\s*\(|\bfor\s*\(|\bwhile\s*\(|\bswitch\s*\(|\btry\s*\{|\bcatch\s*\(|"
    r"\bthrow\s+new\s+Error\b|=>|\bpm\.sendRequest\s*\(|\bpostman\.setNextRequest\s*\(",
    re.DOTALL,
)

HARD_ADVANCED_SCRIPT_PATTERN_RE = re.compile(
    r"\bfor\s*\(|\bwhile\s*\(|\bswitch\s*\(|\btry\s*\{|\bcatch\s*\(|"
    r"\bthrow\s+new\s+Error\b|\bpm\.sendRequest\s*\(|\bpostman\.setNextRequest\s*\(",
    re.DOTALL,
)

SOFT_CONDITIONAL_PATTERN_RE = re.compile(r"\bif\s*\(", re.DOTALL)


@dataclass(frozen=True)
class ScriptOccurrence:
    phase: str
    listen: str
    scope: str
    json_path: str
    folder_hierarchy: Tuple[str, ...]
    request_name: Optional[str]
    event_index: int
    script_text: str
    line_count: int
    has_executable_code: bool


@dataclass(frozen=True)
class RequestScriptTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    occurrences: Tuple[ScriptOccurrence, ...]


@dataclass(frozen=True)
class DataBankCandidate:
    variable_name: str
    xpath: str


@dataclass(frozen=True)
class AssertorCandidate:
    assertion: Dict[str, Any]


@dataclass
class EventApplyResult:
    created_tool_ids: List[str] = field(default_factory=list)
    updated_rest_client: bool = False
    warnings: List[str] = field(default_factory=list)
    intents: List[str] = field(default_factory=list)


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def convert_postman_variable_syntax(text: str) -> str:
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", str(text))


def rewrite_script_variable_references(text: str, script_variable_names: Sequence[str]) -> str:
    result = str(text)
    for name in script_variable_names:
        # Normalize any typed variable syntax to standard SOAtest variable/column syntax.
        # Example: ${number:itemId} -> ${itemId}
        typed_patterns = (
            "${number:" + name + "}",
            "${string:" + name + "}",
            "${boolean:" + name + "}",
        )
        for pattern in typed_patterns:
            result = result.replace(pattern, "${" + name + "}")
    return result


def rewrite_script_variable_references_in_structure(
    value: Any,
    script_variable_names: Sequence[str],
) -> Any:
    if isinstance(value, str):
        return rewrite_script_variable_references(value, script_variable_names)
    if isinstance(value, list):
        return [rewrite_script_variable_references_in_structure(item, script_variable_names) for item in value]
    if isinstance(value, dict):
        return {
            key: rewrite_script_variable_references_in_structure(item, script_variable_names)
            for key, item in value.items()
        }
    return value


def default_external_script_dir(input_path: Path) -> Path:
    if input_path.suffix.lower() == ".json":
        base_name = input_path.stem
    else:
        base_name = input_path.name
    return input_path.with_name(f"{base_name}.soatest_scripts")


def phase_from_listen(listen: str) -> str:
    normalized = listen.strip().lower()
    if normalized == LISTEN_PRE:
        return PHASE_PRE
    if normalized == LISTEN_POST:
        return PHASE_POST
    return PHASE_UNKNOWN


def script_text_from_event(event: Dict[str, Any]) -> Tuple[str, int]:
    script = event.get("script")
    if not isinstance(script, dict):
        return "", 0
    exec_block = script.get("exec")
    if isinstance(exec_block, list):
        lines = [str(line) for line in exec_block]
        return "\n".join(lines), len(lines)
    if isinstance(exec_block, str):
        text = exec_block
        return text, len(text.splitlines()) if text else 0
    return "", 0


def build_occurrence(
    event: Dict[str, Any],
    event_path: str,
    folder_hierarchy: Tuple[str, ...],
    request_name: Optional[str],
    scope: str,
    event_index: int,
) -> ScriptOccurrence:
    listen = str(event.get("listen", "")).strip().lower()
    phase = phase_from_listen(listen)
    script_text, line_count = script_text_from_event(event)
    return ScriptOccurrence(
        phase=phase,
        listen=listen or "unknown",
        scope=scope,
        json_path=event_path,
        folder_hierarchy=folder_hierarchy,
        request_name=request_name,
        event_index=event_index,
        script_text=script_text,
        line_count=line_count,
        has_executable_code=bool(script_text.strip()),
    )


def scan_events(
    events: Any,
    event_path_prefix: str,
    folder_hierarchy: Tuple[str, ...],
    request_name: Optional[str],
    scope: str,
    out: List[ScriptOccurrence],
) -> None:
    if not isinstance(events, list):
        return
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        out.append(
            build_occurrence(
                event=event,
                event_path=f"{event_path_prefix}[{index}]",
                folder_hierarchy=folder_hierarchy,
                request_name=request_name,
                scope=scope,
                event_index=index,
            )
        )


def walk_items(
    items: Any,
    item_path: str,
    folder_hierarchy: Tuple[str, ...],
    request_occurrences: Dict[Tuple[Tuple[str, ...], str], List[ScriptOccurrence]],
    non_request_occurrences: List[ScriptOccurrence],
) -> None:
    if not isinstance(items, list):
        return

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        current_path = f"{item_path}[{index}]"
        item_name = str(item.get("name", f"Item {index + 1}"))
        request_obj = item.get("request")
        nested_items = item.get("item")
        is_request = isinstance(request_obj, dict)
        is_folder = isinstance(nested_items, list)

        if is_request:
            key = (folder_hierarchy, item_name)
            request_occurrences.setdefault(key, [])
            temp: List[ScriptOccurrence] = []
            scan_events(
                events=item.get("event"),
                event_path_prefix=f"{current_path}.event",
                folder_hierarchy=folder_hierarchy,
                request_name=item_name,
                scope="request",
                out=temp,
            )
            scan_events(
                events=request_obj.get("event"),
                event_path_prefix=f"{current_path}.request.event",
                folder_hierarchy=folder_hierarchy,
                request_name=item_name,
                scope="request",
                out=temp,
            )
            request_occurrences[key].extend(temp)
        elif is_folder:
            folder_path = folder_hierarchy + (item_name,)
            scan_events(
                events=item.get("event"),
                event_path_prefix=f"{current_path}.event",
                folder_hierarchy=folder_path,
                request_name=None,
                scope="folder",
                out=non_request_occurrences,
            )
        else:
            scan_events(
                events=item.get("event"),
                event_path_prefix=f"{current_path}.event",
                folder_hierarchy=folder_hierarchy,
                request_name=item_name,
                scope="item",
                out=non_request_occurrences,
            )

        if is_folder:
            walk_items(
                items=nested_items,
                item_path=f"{current_path}.item",
                folder_hierarchy=folder_hierarchy + (item_name,),
                request_occurrences=request_occurrences,
                non_request_occurrences=non_request_occurrences,
            )


def collect_script_targets(collection: Dict[str, Any]) -> Tuple[List[RequestScriptTarget], List[ScriptOccurrence]]:
    request_occurrences: Dict[Tuple[Tuple[str, ...], str], List[ScriptOccurrence]] = {}
    non_request_occurrences: List[ScriptOccurrence] = []

    scan_events(
        events=collection.get("event"),
        event_path_prefix="event",
        folder_hierarchy=(),
        request_name=None,
        scope="collection",
        out=non_request_occurrences,
    )

    walk_items(
        items=collection.get("item"),
        item_path="item",
        folder_hierarchy=(),
        request_occurrences=request_occurrences,
        non_request_occurrences=non_request_occurrences,
    )

    targets: List[RequestScriptTarget] = []
    for (folder_path, request_name), occurrences in request_occurrences.items():
        targets.append(
            RequestScriptTarget(
                folder_path=folder_path,
                request_name=request_name,
                occurrences=tuple(occurrences),
            )
        )
    return targets, non_request_occurrences


def unwrap_js_wrapper(expr: str) -> str:
    value = expr.strip()
    while True:
        unwrapped: Optional[str] = None
        for fn_name in ("Number", "String", "Boolean", "parseInt", "parseFloat"):
            prefix = fn_name + "("
            if not value.startswith(prefix):
                continue
            open_paren_index = len(fn_name)
            close_paren_index = _find_matching_paren(value, open_paren_index)
            if close_paren_index == len(value) - 1:
                unwrapped = value[open_paren_index + 1 : close_paren_index].strip()
                break
        if unwrapped is None:
            return value
        value = unwrapped


def normalize_js_expr(expr: str) -> str:
    return re.sub(r"\s+", "", expr)


def _find_matching_paren(text: str, open_paren_index: int) -> int:
    depth = 0
    quote: Optional[str] = None
    escape = False

    for index in range(open_paren_index, len(text)):
        ch = text[index]
        if quote is not None:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == quote:
                quote = None
            continue

        if ch in {"'", '"', "`"}:
            quote = ch
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            if depth == 0:
                return index
            continue

    return -1


def extract_alias_json_roots(script_text: str) -> Dict[str, str]:
    alias_to_root: Dict[str, str] = {}

    def resolve_to_json_root(expr: str) -> Optional[str]:
        if expr.startswith("json"):
            return expr
        for known_alias, known_root in alias_to_root.items():
            if expr == known_alias:
                return known_root
            if expr.startswith(known_alias + ".") or expr.startswith(known_alias + "["):
                return known_root + expr[len(known_alias) :]
        return None

    for match in ALIAS_ASSIGN_RE.finditer(script_text):
        alias = match.group("alias")
        expr = normalize_js_expr(unwrap_js_wrapper(match.group("expr").strip()))
        filter_match = FILTER_FIND_ALIAS_RE.match(expr)
        if filter_match:
            source_expr = filter_match.group("source")
            kind = filter_match.group("kind")
            resolved_source = resolve_to_json_root(source_expr)
            if resolved_source:
                # find(...) returns a single element; approximate with first matching item path.
                if kind == "find":
                    alias_to_root[alias] = resolved_source + "[0]"
                else:
                    alias_to_root[alias] = resolved_source
            continue
        resolved = resolve_to_json_root(expr)
        if resolved:
            alias_to_root[alias] = resolved
    return alias_to_root


def extract_filter_alias_expressions(
    script_text: str,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    filter_aliases: Dict[str, str] = {}

    def resolve_source(expr: str) -> str:
        if not alias_json_roots:
            return expr
        for alias, root_expr in alias_json_roots.items():
            if expr == alias:
                return root_expr
            if expr.startswith(alias + ".") or expr.startswith(alias + "["):
                return root_expr + expr[len(alias) :]
        return expr

    for match in ALIAS_ASSIGN_RE.finditer(script_text):
        alias = match.group("alias")
        expr = normalize_js_expr(unwrap_js_wrapper(match.group("expr").strip()))
        filter_match = FILTER_FIND_ALIAS_RE.match(expr)
        if not filter_match or filter_match.group("kind") != "filter":
            continue
        source_expr = filter_match.group("source")
        resolved_source = resolve_source(source_expr)
        filter_aliases[alias] = resolved_source + expr[len(source_expr) :]

    return filter_aliases


def parse_json_expression(expr: str, alias_json_roots: Optional[Dict[str, str]] = None) -> Optional[str]:
    value = normalize_js_expr(unwrap_js_wrapper(expr.strip().rstrip(";")))

    # Ignore basic string transforms when building extraction paths.
    value = re.sub(r"\.(?:toLowerCase|toUpperCase|trim)\(\)$", "", value)
    # Removing the transform may expose a wrapper expression such as String(json.foo).
    value = normalize_js_expr(unwrap_js_wrapper(value))

    if alias_json_roots:
        for alias, root_expr in alias_json_roots.items():
            if value == alias:
                value = root_expr
                break
            if value.startswith(alias + ".") or value.startswith(alias + "["):
                value = root_expr + value[len(alias) :]
                break

    if not value.startswith("json"):
        return None
    value = value[4:]
    if value.startswith("."):
        value = value[1:]
    return value


def _path_to_segments(path: str) -> Optional[List[Tuple[str, int]]]:
    tokens: List[Tuple[str, Any]] = []
    i = 0
    while i < len(path):
        ch = path[i]
        if ch in ". ":
            i += 1
            continue
        if ch == "[":
            end = path.find("]", i + 1)
            if end == -1:
                return None
            idx_text = path[i + 1 : end].strip()
            if not idx_text.isdigit():
                return None
            tokens.append(("idx", int(idx_text)))
            i = end + 1
            continue
        if ch.isalpha() or ch == "_":
            start = i
            i += 1
            while i < len(path) and (path[i].isalnum() or path[i] == "_"):
                i += 1
            tokens.append(("prop", path[start:i]))
            continue
        return None

    segments: List[Tuple[str, int]] = []
    for token_type, token_value in tokens:
        if token_type == "prop":
            segments.append((str(token_value), 1))
        else:
            if not segments:
                return None
            # JSON arrays are represented in SOAtest's XML-converted model
            # using nested "item" nodes under the array property.
            segments.append(("item", int(token_value) + 1))
    return segments


def _segments_to_xpath(segments: Sequence[Tuple[str, int]], value_suffix: Optional[str]) -> str:
    xpath = "/root"
    for name, index in segments:
        xpath += f"/{name}[{index}]"
    if value_suffix:
        xpath += f"/{value_suffix}"
    return xpath


def json_expression_to_xpath(
    expr: str,
    value_suffix: str = "text()",
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    path = parse_json_expression(expr, alias_json_roots=alias_json_roots)
    if not path:
        return None

    segments = _path_to_segments(path)
    if segments is None:
        return None
    if not segments:
        return None

    return _segments_to_xpath(segments, value_suffix)


def build_occurrence_assertion_from_length(
    actual_expr: str,
    operator: str,
    expected_expr: str,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    normalized = normalize_js_expr(unwrap_js_wrapper(actual_expr.strip()))
    if not normalized.endswith(".length"):
        return None

    base_expr = normalized[: -len(".length")]
    path = parse_json_expression(base_expr, alias_json_roots=alias_json_roots)
    if not path:
        return None
    segments = _path_to_segments(path)
    if not segments:
        return None

    # Length in Postman corresponds to counting all array elements.
    base_segments = list(segments)
    if base_segments and base_segments[-1][0] == "item":
        base_segments = base_segments[:-1]
    # Use wildcard item selection so SOAtest counts all array members instead of
    # collapsing to the first indexed element.
    occurrence_xpath = _segments_to_xpath(base_segments, value_suffix=None) + "/item[*]"
    expected_value = normalize_expected_value(expected_expr)

    return AssertorCandidate(
        assertion={
            "type": "occurrenceAssertion",
            "occurrenceAssertion": {
                "name": f"{operator} {occurrence_xpath}",
                "configuration": {
                    "expectedValue": {"type": "fixed", "fixed": expected_value},
                    "operator": operator,
                },
                "selectedElement": {"xpath": occurrence_xpath, "extractionType": "entireElement"},
            },
        }
    )


def build_value_occurrence_assertion_from_filtered_length(
    actual_expr: str,
    operator: str,
    expected_count_expr: str,
    alias_json_roots: Optional[Dict[str, str]] = None,
    filter_alias_expressions: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    normalized = normalize_js_expr(unwrap_js_wrapper(actual_expr.strip()))
    if not normalized.endswith(".length"):
        return None

    core_expr = normalized[: -len(".length")]
    if filter_alias_expressions and core_expr in filter_alias_expressions:
        core_expr = filter_alias_expressions[core_expr]

    filter_match = re.fullmatch(
        r"(?P<source>[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*)|\[\d+\])*)"
        r"\.filter\(\(?(?P<param>[A-Za-z_]\w*)\)?=>(?P<cond>.+)\)",
        core_expr,
        re.DOTALL,
    )
    if not filter_match:
        return None

    source_expr = filter_match.group("source")
    param = filter_match.group("param")
    condition_expr = filter_match.group("cond")

    comparison_match = re.fullmatch(
        r"(?P<left>.+?)(?P<op>===|!==|>=|<=|==|!=|>|<)(?P<right>.+)",
        condition_expr,
        re.DOTALL,
    )
    if not comparison_match:
        return None

    left_expr = normalize_js_expr(unwrap_js_wrapper(comparison_match.group("left")))
    right_expr = comparison_match.group("right")
    condition_operator = comparison_match.group("op")
    if not left_expr.startswith(param):
        return None

    tail = left_expr[len(param) :]
    if tail and not (tail.startswith(".") or tail.startswith("[")):
        return None

    expected_count = normalize_expected_value(expected_count_expr)

    if condition_operator in {"===", "==", "!==", "!="}:
        selected_expr = source_expr + tail
        selected_xpath = json_expression_to_xpath(selected_expr, value_suffix="string()", alias_json_roots=alias_json_roots)
        if not selected_xpath:
            return None

        element_value = normalize_expected_value(right_expr)
        assertion = {
            "type": "valueOccurrenceAssertion",
            "valueOccurrenceAssertion": {
                "name": f"{operator} {selected_xpath}",
                "configuration": {
                    "elementValue": {"type": "fixed", "fixed": element_value},
                    "expectedValue": {"type": "fixed", "fixed": expected_count},
                    "operator": operator,
                },
                "selectedElement": {"xpath": selected_xpath, "extractionType": "entireElement"},
                "options": {"trimContent": True},
            },
        }
        return AssertorCandidate(assertion=assertion)

    source_path = parse_json_expression(source_expr, alias_json_roots=alias_json_roots)
    if not source_path:
        return None
    source_segments = _path_to_segments(source_path)
    if not source_segments:
        return None

    source_base_xpath = _segments_to_xpath(source_segments, value_suffix=None)
    # Use wildcard item selection so the filtered occurrence applies across the
    # full array, not just the first indexed element.
    candidate_xpath = source_base_xpath + "/item[*]"

    if not tail:
        return None

    relative_left_xpath = json_expression_to_xpath("json" + tail, value_suffix=None)
    if not relative_left_xpath or not relative_left_xpath.startswith("/root/"):
        return None
    relative_left_xpath = relative_left_xpath[len("/root/") :]

    right_value = normalize_expected_value(right_expr)
    if NUMERIC_LITERAL_RE.fullmatch(right_value):
        left_operand = f"number({relative_left_xpath})"
        right_operand = right_value
    elif right_value.startswith("${") and right_value.endswith("}"):
        left_operand = f"number({relative_left_xpath})"
        right_operand = f"number({right_value})"
    else:
        parsed_literal = parse_js_string_literal(unwrap_js_wrapper(right_expr.strip()))
        if parsed_literal is None:
            return None
        escaped = convert_postman_variable_syntax(parsed_literal).replace("'", "''")
        left_operand = f"{relative_left_xpath}/string()"
        right_operand = f"'{escaped}'"

    filtered_item_xpath = f"{candidate_xpath}[{left_operand}{condition_operator}{right_operand}]"
    assertion = {
        "type": "occurrenceAssertion",
        "occurrenceAssertion": {
            "name": f"{operator} {filtered_item_xpath}",
            "configuration": {
                "expectedValue": {"type": "fixed", "fixed": expected_count},
                "operator": operator,
            },
            "selectedElement": {"xpath": filtered_item_xpath, "extractionType": "entireElement"},
        },
    }
    return AssertorCandidate(assertion=assertion)


def normalize_expected_value(expected_expr: str) -> str:
    value = expected_expr.strip().rstrip(";")
    value = unwrap_js_wrapper(value)

    for getter_re in (ENV_GET_RE, ITERATION_DATA_GET_RE):
        getter_match = getter_re.fullmatch(value)
        if getter_match:
            return "${" + getter_match.group("name") + "}"

    if (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
        or (value.startswith("`") and value.endswith("`"))
    ):
        inner = value[1:-1]
        return convert_postman_variable_syntax(inner)

    return convert_postman_variable_syntax(value)


def is_unquoted_numeric_literal(expected_expr: str) -> bool:
    value = unwrap_js_wrapper(expected_expr.strip().rstrip(";"))
    if not value:
        return False
    # Quoted literals (for example "0") are treated as strings, not numeric literals.
    if parse_js_string_literal(value) is not None:
        return False
    return bool(NUMERIC_LITERAL_RE.fullmatch(value))


def parse_js_string_literal(value: str) -> Optional[str]:
    text = str(value).strip()
    if len(text) < 2:
        return None
    quote = text[0]
    if quote not in {"'", '"', "`"}:
        return None
    if text[-1] != quote:
        return None
    inner = text[1:-1]
    try:
        return bytes(inner, "utf-8").decode("unicode_escape")
    except Exception:
        return inner


LINE_COMMENT_RE = re.compile(r"//.*?$", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def strip_js_comments(script_text: str) -> str:
    without_block = BLOCK_COMMENT_RE.sub("", script_text)
    return LINE_COMMENT_RE.sub("", without_block)


def parse_simple_set_assignment_value(expr: str) -> Optional[str]:
    value = unwrap_js_wrapper(str(expr).strip().rstrip(";"))
    if not value:
        return None

    string_literal = parse_js_string_literal(value)
    if string_literal is not None:
        return convert_postman_variable_syntax(string_literal)

    if NUMERIC_LITERAL_RE.fullmatch(value):
        return value

    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered

    env_get_match = ENV_GET_RE.fullmatch(value)
    if env_get_match:
        return "${" + env_get_match.group("name") + "}"

    iteration_match = ITERATION_DATA_GET_RE.fullmatch(value)
    if iteration_match:
        return "${" + iteration_match.group("name") + "}"

    return None


def parse_safe_prerequest_value_with_aliases(expr: str, alias_values: Dict[str, str]) -> Optional[str]:
    parsed = parse_simple_set_assignment_value(expr)
    if parsed is not None:
        return parsed

    value = unwrap_js_wrapper(str(expr).strip().rstrip(";"))
    if value in alias_values:
        return alias_values[value]

    return None


def extract_safe_prerequest_set_assignments(script_text: str) -> Optional[Dict[str, str]]:
    cleaned = strip_js_comments(script_text)
    if not cleaned.strip():
        return {}

    aliases: Dict[str, str] = {}
    assignments: Dict[str, str] = {}
    cursor = 0
    text_len = len(cleaned)
    while cursor < text_len:
        while cursor < text_len and cleaned[cursor] in {" ", "\t", "\r", "\n", ";"}:
            cursor += 1
        if cursor >= text_len:
            break

        alias_match = ALIAS_ASSIGN_RE.match(cleaned, cursor)
        if alias_match:
            alias_name = alias_match.group("alias").strip()
            parsed_alias_value = parse_safe_prerequest_value_with_aliases(alias_match.group("expr"), aliases)
            if not alias_name or parsed_alias_value is None:
                return None
            aliases[alias_name] = parsed_alias_value
            cursor = alias_match.end()
            continue

        set_match = SET_CALL_RE.match(cleaned, cursor)
        if set_match:
            variable_name = set_match.group("name").strip()
            parsed_value = parse_safe_prerequest_value_with_aliases(set_match.group("expr"), aliases)
            if not variable_name or parsed_value is None:
                return None
            assignments[variable_name] = parsed_value
            cursor = set_match.end()
            continue

        # Contains executable constructs other than safe alias/set statements.
        return None

    if not assignments:
        # Safe script but no set operations (for example alias declarations only)
        return {}

    return assignments


def split_js_top_level_csv(values_text: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    escape = False
    paren_depth = 0
    bracket_depth = 0
    brace_depth = 0

    for ch in values_text:
        if quote is not None:
            current.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue

        if ch in {"'", '"', "`"}:
            quote = ch
            current.append(ch)
            continue
        if ch == "(":
            paren_depth += 1
            current.append(ch)
            continue
        if ch == ")":
            paren_depth = max(paren_depth - 1, 0)
            current.append(ch)
            continue
        if ch == "[":
            bracket_depth += 1
            current.append(ch)
            continue
        if ch == "]":
            bracket_depth = max(bracket_depth - 1, 0)
            current.append(ch)
            continue
        if ch == "{":
            brace_depth += 1
            current.append(ch)
            continue
        if ch == "}":
            brace_depth = max(brace_depth - 1, 0)
            current.append(ch)
            continue
        if ch == "," and paren_depth == 0 and bracket_depth == 0 and brace_depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue

        current.append(ch)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def infer_range_assertion_type(lower_expr: str, upper_expr: str) -> str:
    normalized_lower = unwrap_js_wrapper(lower_expr.strip().rstrip(";"))
    normalized_upper = unwrap_js_wrapper(upper_expr.strip().rstrip(";"))
    lower_literal = parse_js_string_literal(normalized_lower)
    upper_literal = parse_js_string_literal(normalized_upper)
    if lower_literal is None or upper_literal is None:
        return "numericRangeAssertion"

    lower_text = convert_postman_variable_syntax(lower_literal).strip()
    upper_text = convert_postman_variable_syntax(upper_literal).strip()

    if DATETIME_LITERAL_RE.fullmatch(lower_text) and DATETIME_LITERAL_RE.fullmatch(upper_text):
        return "dateTimeRangeAssertion"
    if DATE_LITERAL_RE.fullmatch(lower_text) and DATE_LITERAL_RE.fullmatch(upper_text):
        return "dateRangeAssertion"
    return "numericRangeAssertion"


def parse_regular_expression_expected(expected_expr: str) -> Optional[Tuple[str, bool]]:
    value = unwrap_js_wrapper(expected_expr.strip().rstrip(";"))
    literal_match = re.fullmatch(r"/(?P<pattern>(?:\\.|[^/])*)/(?P<flags>[a-zA-Z]*)", value, re.DOTALL)
    if literal_match:
        flags = literal_match.group("flags")
        if any(flag not in "dgimsuvy" for flag in flags):
            return None
        pattern = literal_match.group("pattern").replace(r"\/", "/")
        ignore_case = "i" in flags
        return pattern, ignore_case

    ctor_match = re.fullmatch(
        r"new\s+RegExp\(\s*(?P<pattern>(?:'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\"|`[^`\\]*(?:\\.[^`\\]*)*`))"
        r"\s*(?:,\s*(?P<flags>(?:'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\"|`[^`\\]*(?:\\.[^`\\]*)*`))\s*)?\)",
        value,
        re.DOTALL,
    )
    if not ctor_match:
        return None

    pattern_literal = parse_js_string_literal(ctor_match.group("pattern"))
    if pattern_literal is None:
        return None
    flags_literal = ""
    if ctor_match.group("flags"):
        parsed_flags = parse_js_string_literal(ctor_match.group("flags"))
        if parsed_flags is None:
            return None
        flags_literal = parsed_flags
    if any(flag not in "dgimsuvy" for flag in flags_literal):
        return None

    pattern = pattern_literal.replace(r"\/", "/")
    ignore_case = "i" in flags_literal
    return pattern, ignore_case


def normalize_type_expected(expected_expr: str) -> Optional[str]:
    value = unwrap_js_wrapper(expected_expr.strip().rstrip(";"))
    parsed_literal = parse_js_string_literal(value)
    normalized = (parsed_literal if parsed_literal is not None else value).strip().lower()

    aliases = {
        "str": "string",
        "string": "string",
        "number": "number",
        "numeric": "number",
        "int": "number",
        "integer": "number",
        "float": "number",
        "double": "number",
        "bool": "boolean",
        "boolean": "boolean",
        "array": "array",
        "object": "object",
        "null": "null",
    }
    return aliases.get(normalized)


def extract_response_code_expectations(script_text: str) -> Set[int]:
    constraints: List[Set[int]] = []

    def clamp_code(code: int) -> int:
        return max(100, min(599, int(code)))

    def add_constraint(codes: Set[int]) -> None:
        if codes:
            constraints.append(codes)

    for match in EXPECT_RESPONSE_CODE_EQUAL_RE.finditer(script_text):
        add_constraint({clamp_code(int(match.group("code")))})
    for match in EXPECT_RESPONSE_CODE_ONEOF_RE.finditer(script_text):
        one_of_codes: Set[int] = set()
        for token in match.group("codes").split(","):
            token = token.strip()
            if token.isdigit() and len(token) == 3:
                one_of_codes.add(clamp_code(int(token)))
        add_constraint(one_of_codes)
    for match in EXPECT_RESPONSE_CODE_INCLUDED_IN_ARRAY_RE.finditer(script_text):
        include_codes: Set[int] = set()
        for token in match.group("codes").split(","):
            token = token.strip()
            if token.isdigit() and len(token) == 3:
                include_codes.add(clamp_code(int(token)))
        add_constraint(include_codes)
    for match in EXPECT_RESPONSE_HAVE_STATUS_RE.finditer(script_text):
        add_constraint({clamp_code(int(match.group("code")))})

    for match in EXPECT_RESPONSE_CODE_COMPARE_RE.finditer(script_text):
        cmp_op = match.group("cmp")
        code = clamp_code(int(match.group("code")))
        if cmp_op in {"above", "greaterThan"}:
            add_constraint(set(range(code + 1, 600)))
        elif cmp_op in {"below", "lessThan"}:
            add_constraint(set(range(100, code)))

    for match in EXPECT_RESPONSE_CODE_WITHIN_RE.finditer(script_text):
        lower = clamp_code(int(match.group("lower")))
        upper = clamp_code(int(match.group("upper")))
        if lower > upper:
            lower, upper = upper, lower
        add_constraint(set(range(lower, upper + 1)))

    for match in EXPECT_RESPONSE_CODE_AT_LEAST_RE.finditer(script_text):
        lower = clamp_code(int(match.group("code")))
        add_constraint(set(range(lower, 600)))

    for match in EXPECT_RESPONSE_CODE_AT_MOST_RE.finditer(script_text):
        upper = clamp_code(int(match.group("code")))
        add_constraint(set(range(100, upper + 1)))

    if not constraints:
        return set()
    allowed = set(constraints[0])
    for constraint in constraints[1:]:
        allowed.intersection_update(constraint)
    return allowed


def extract_response_time_upper_bound_ms(script_text: str) -> Optional[int]:
    bounds: List[int] = []
    for match in EXPECT_RESPONSE_TIME_BELOW_RE.finditer(script_text):
        threshold = int(match.group("ms"))
        bounds.append(max(0, threshold - 1))
    for match in EXPECT_RESPONSE_TIME_AT_MOST_RE.finditer(script_text):
        bounds.append(int(match.group("ms")))
    if not bounds:
        return None
    return min(bounds)


def build_length_of_assertion_from_match(
    actual: str,
    expected: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    operator = "!=" if not_flag else "=="
    normalized_actual = normalize_js_expr(unwrap_js_wrapper(actual.strip()))
    actual_expr = actual if normalized_actual.endswith(".length") else f"{actual}.length"
    return build_occurrence_assertion_from_length(
        actual_expr=actual_expr,
        operator=operator,
        expected_expr=expected,
        alias_json_roots=alias_json_roots,
    )


def build_property_assertion_from_match(
    actual: str,
    args_expr: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    args = split_js_top_level_csv(args_expr)
    if not args:
        return None

    prop_token = args[0].strip()
    parsed_prop = parse_js_string_literal(prop_token)
    prop_path = (parsed_prop if parsed_prop is not None else prop_token).strip()
    if not prop_path:
        return None
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.(?:[A-Za-z_]\w*)|\[\d+\])*", prop_path):
        return None

    actual_prop_expr = f"{actual}.{prop_path}"
    if len(args) >= 2:
        expected_expr = args[1]
        return build_assertion_from_match(
            actual=actual_prop_expr,
            cmp_op="equal",
            expected=expected_expr,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )

    return build_has_content_assertion_from_match(
        actual=actual_prop_expr,
        has_content=not not_flag,
        alias_json_roots=alias_json_roots,
    )


def build_literal_state_assertion_from_match(
    actual: str,
    state: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    normalized_state = state.strip().lower()
    if normalized_state == "null":
        if not_flag:
            return build_assertion_from_match(
                actual=actual,
                cmp_op="equal",
                expected="null",
                not_flag=True,
                alias_json_roots=alias_json_roots,
            )
        return build_type_assertion_from_match(
            actual=actual,
            expected="null",
            not_flag=False,
            alias_json_roots=alias_json_roots,
        )
    if normalized_state in {"true", "false"}:
        return build_assertion_from_match(
            actual=actual,
            cmp_op="equal",
            expected=normalized_state,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
    return None


def extract_set_variable_element_xpaths(
    script_text: str,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for match in SET_CALL_RE.finditer(script_text):
        variable_name = match.group("name").strip()
        expr = match.group("expr").strip()
        xpath = json_expression_to_xpath(expr, value_suffix=None, alias_json_roots=alias_json_roots)
        if xpath:
            mapping[variable_name] = xpath
    return mapping


def build_assertion_from_match(
    actual: str,
    cmp_op: str,
    expected: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
    filter_alias_expressions: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    if cmp_op in {"equal", "eql"}:
        length_operator = "!=" if not_flag else "=="
        value_occurrence_candidate = build_value_occurrence_assertion_from_filtered_length(
            actual_expr=actual,
            operator=length_operator,
            expected_count_expr=expected,
            alias_json_roots=alias_json_roots,
            filter_alias_expressions=filter_alias_expressions,
        )
        if value_occurrence_candidate:
            return value_occurrence_candidate

    if cmp_op in {"above", "below"}:
        length_operator = ">" if cmp_op == "above" else "<"
        value_occurrence_candidate = build_value_occurrence_assertion_from_filtered_length(
            actual_expr=actual,
            operator=length_operator,
            expected_count_expr=expected,
            alias_json_roots=alias_json_roots,
            filter_alias_expressions=filter_alias_expressions,
        )
        if value_occurrence_candidate:
            return value_occurrence_candidate

    xpath = json_expression_to_xpath(actual, value_suffix="string()", alias_json_roots=alias_json_roots)
    if not xpath:
        return None

    cmp_value = normalize_expected_value(expected)
    name = f"{cmp_op} {xpath}"

    if cmp_op in {"equal", "eql"} and is_unquoted_numeric_literal(expected):
        operator = "!=" if not_flag else "="
        assertion = {
            "type": "numericAssertion",
            "numericAssertion": {
                "name": name,
                "configuration": {
                    "expectedValue": {"type": "fixed", "fixed": cmp_value},
                    "operator": operator,
                },
                "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
                "options": {"trimContent": True},
            },
        }
        return AssertorCandidate(assertion=assertion)

    if cmp_op in {"equal", "eql"} and not not_flag:
        assertion = {
            "type": "valueAssertion",
            "valueAssertion": {
                "name": name,
                "configuration": {"expectedValue": {"type": "fixed", "fixed": cmp_value}},
                "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
                "options": {"trimContent": True},
            },
        }
        return AssertorCandidate(assertion=assertion)

    if cmp_op in {"include", "contain"} or (cmp_op in {"equal", "eql"} and not_flag):
        string_operator = "contain"
        if cmp_op in {"equal", "eql"}:
            string_operator = "not equal" if not_flag else "equal"
        elif not_flag:
            string_operator = "not contain"

        assertion = {
            "type": "stringComparisonAssertion",
            "stringComparisonAssertion": {
                "name": name,
                "configuration": {
                    "expectedValue": {"type": "fixed", "fixed": cmp_value},
                    "stringOperator": string_operator,
                },
                "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
                "options": {"trimContent": True, "ignoreCase": False},
            },
        }
        return AssertorCandidate(assertion=assertion)

    if cmp_op in {"above", "below"}:
        operator = ">" if cmp_op == "above" else "<"
        occurrence_candidate = build_occurrence_assertion_from_length(
            actual_expr=actual,
            operator=operator,
            expected_expr=expected,
            alias_json_roots=alias_json_roots,
        )
        if occurrence_candidate:
            return occurrence_candidate

        assertion = {
            "type": "numericAssertion",
            "numericAssertion": {
                "name": name,
                "configuration": {
                    "expectedValue": {"type": "fixed", "fixed": cmp_value},
                    "operator": operator,
                },
                "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
                "options": {"trimContent": True},
            },
        }
        return AssertorCandidate(assertion=assertion)

    return None


def build_regex_assertion_from_match(
    actual: str,
    expected: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    xpath = json_expression_to_xpath(actual, value_suffix="string()", alias_json_roots=alias_json_roots)
    if not xpath:
        return None

    parsed = parse_regular_expression_expected(expected)
    if not parsed:
        return None

    regular_expression, ignore_case = parsed
    assertion = {
        "type": "regularExpressionAssertion",
        "regularExpressionAssertion": {
            "name": f"match {xpath}",
            "configuration": {
                "condition": "not match" if not_flag else "match",
                "regularExpression": {"type": "fixed", "fixed": regular_expression},
            },
            "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
            "options": {"trimContent": True, "ignoreCase": ignore_case},
        },
    }
    return AssertorCandidate(assertion=assertion)


def build_has_content_assertion_from_match(
    actual: str,
    has_content: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    xpath = json_expression_to_xpath(actual, value_suffix=None, alias_json_roots=alias_json_roots)
    if not xpath:
        return None

    assertion = {
        "type": "hasContentAssertion",
        "hasContentAssertion": {
            "name": f"has content {xpath}",
            "configuration": {"hasContent": {"type": "fixed", "fixed": "true" if has_content else "false"}},
            "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
            "options": {"trimContent": True},
        },
    }
    return AssertorCandidate(assertion=assertion)


def build_variable_get_has_content_assertion_from_match(
    variable_name: str,
    has_content: bool,
    set_variable_element_xpaths: Dict[str, str],
) -> Optional[AssertorCandidate]:
    xpath = set_variable_element_xpaths.get(variable_name)
    if not xpath:
        return None

    assertion = {
        "type": "hasContentAssertion",
        "hasContentAssertion": {
            "name": f"has content ${{{variable_name}}}",
            "configuration": {"hasContent": {"type": "fixed", "fixed": "true" if has_content else "false"}},
            "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
            "options": {"trimContent": True},
        },
    }
    return AssertorCandidate(assertion=assertion)


def build_has_children_assertion_from_match(
    actual: str,
    has_children: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    xpath = json_expression_to_xpath(actual, value_suffix=None, alias_json_roots=alias_json_roots)
    if not xpath:
        return None

    assertion = {
        "type": "hasChildrenAssertion",
        "hasChildrenAssertion": {
            "name": f"has children {xpath}",
            "configuration": {"hasChildren": {"type": "fixed", "fixed": "true" if has_children else "false"}},
            "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
        },
    }
    return AssertorCandidate(assertion=assertion)


def build_type_assertion_from_match(
    actual: str,
    expected: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    if not_flag:
        return None

    xpath = json_expression_to_xpath(actual, value_suffix=None, alias_json_roots=alias_json_roots)
    if not xpath:
        return None

    expected_type = normalize_type_expected(expected)
    if not expected_type:
        return None

    assertion = {
        "type": "typeAssertion",
        "typeAssertion": {
            "name": f"type {expected_type} {xpath}",
            "configuration": {"expectedType": {"type": "fixed", "fixed": expected_type}},
            "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
        },
    }
    return AssertorCandidate(assertion=assertion)


def build_range_assertion_from_match(
    actual: str,
    lower_expr: str,
    upper_expr: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    if not_flag:
        return None

    xpath = json_expression_to_xpath(actual, value_suffix="string()", alias_json_roots=alias_json_roots)
    if not xpath:
        return None

    lower_value = normalize_expected_value(lower_expr)
    upper_value = normalize_expected_value(upper_expr)
    assertion_type = infer_range_assertion_type(lower_expr, upper_expr)

    if assertion_type == "dateTimeRangeAssertion":
        assertion = {
            "type": "dateTimeRangeAssertion",
            "dateTimeRangeAssertion": {
                "name": f"dateTime range {xpath}",
                "configuration": {
                    "lowerBoundDateTime": {"type": "fixed", "fixed": lower_value},
                    "upperBoundDateTime": {"type": "fixed", "fixed": upper_value},
                },
                "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
            },
        }
        return AssertorCandidate(assertion=assertion)

    if assertion_type == "dateRangeAssertion":
        assertion = {
            "type": "dateRangeAssertion",
            "dateRangeAssertion": {
                "name": f"date range {xpath}",
                "configuration": {
                    "lowerBoundDate": {"type": "fixed", "fixed": lower_value},
                    "upperBoundDate": {"type": "fixed", "fixed": upper_value},
                },
                "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
            },
        }
        return AssertorCandidate(assertion=assertion)

    assertion = {
        "type": "numericRangeAssertion",
        "numericRangeAssertion": {
            "name": f"numeric range {xpath}",
            "configuration": {
                "lowerBoundValue": {"type": "fixed", "fixed": lower_value},
                "upperBoundValue": {"type": "fixed", "fixed": upper_value},
            },
            "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
        },
    }
    return AssertorCandidate(assertion=assertion)


def build_or_assertion_from_match(
    actual: str,
    values_expr: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    if not_flag:
        return None
    if normalize_js_expr(unwrap_js_wrapper(actual.strip())) == "pm.response.code":
        return None

    xpath = json_expression_to_xpath(actual, value_suffix="string()", alias_json_roots=alias_json_roots)
    if not xpath:
        return None

    values = split_js_top_level_csv(values_expr)
    if len(values) < 2:
        return None

    nested_assertions: List[Dict[str, Any]] = []
    for value in values:
        child = build_assertion_from_match(
            actual=actual,
            cmp_op="equal",
            expected=value,
            not_flag=False,
            alias_json_roots=alias_json_roots,
        )
        if child:
            nested_assertions.append(child.assertion)

    if len(nested_assertions) < 2:
        return None

    return AssertorCandidate(
        assertion={
            "type": "orAssertion",
            "orAssertion": {
                "name": f"oneOf {xpath}",
                "configuration": {"assertions": nested_assertions},
            },
        }
    )


def build_and_assertion_from_match(
    actual: str,
    first_cmp: str,
    first_expected: str,
    chain_text: str,
    not_flag: bool,
    alias_json_roots: Optional[Dict[str, str]] = None,
    filter_alias_expressions: Optional[Dict[str, str]] = None,
) -> Optional[AssertorCandidate]:
    if not_flag:
        return None

    parts: List[Tuple[str, str]] = [(first_cmp, first_expected)]
    for match in EXPECT_INCLUDE_CHAIN_PART_RE.finditer(chain_text):
        parts.append((match.group("cmp"), match.group("expected")))
    if len(parts) < 2:
        return None

    nested_assertions: List[Dict[str, Any]] = []
    for cmp_op, expected in parts:
        child = build_assertion_from_match(
            actual=actual,
            cmp_op=cmp_op,
            expected=expected,
            not_flag=False,
            alias_json_roots=alias_json_roots,
            filter_alias_expressions=filter_alias_expressions,
        )
        if not child:
            return None
        nested_assertions.append(child.assertion)

    xpath = json_expression_to_xpath(actual, value_suffix="string()", alias_json_roots=alias_json_roots)
    name = f"and {xpath}" if xpath else "and assertion"
    return AssertorCandidate(
        assertion={
            "type": "andAssertion",
            "andAssertion": {
                "name": name,
                "configuration": {"assertions": nested_assertions},
            },
        }
    )


def extract_assertion_candidates(
    script_text: str,
    context_script_text: Optional[str] = None,
) -> List[AssertorCandidate]:
    candidates: List[AssertorCandidate] = []
    context_text = context_script_text if context_script_text is not None else script_text
    alias_json_roots = extract_alias_json_roots(context_text)
    filter_alias_expressions = extract_filter_alias_expressions(
        context_text,
        alias_json_roots=alias_json_roots,
    )
    set_variable_element_xpaths = extract_set_variable_element_xpaths(
        context_text,
        alias_json_roots=alias_json_roots,
    )
    include_chain_matches = list(EXPECT_INCLUDE_CHAIN_RE.finditer(script_text))
    include_chain_spans = [match.span() for match in include_chain_matches]

    def overlaps_include_chain(span: Tuple[int, int]) -> bool:
        start, end = span
        for chain_start, chain_end in include_chain_spans:
            if start < chain_end and end > chain_start:
                return True
        return False

    for match in EXPECT_EQUAL_RE.finditer(script_text):
        actual = match.group("actual")
        cmp_op = match.group("cmp")
        expected = match.group("expected")
        not_flag = bool(match.group("not"))
        if cmp_op in {"include", "contain"} and overlaps_include_chain(match.span()):
            continue
        candidate = build_assertion_from_match(
            actual,
            cmp_op,
            expected,
            not_flag,
            alias_json_roots=alias_json_roots,
            filter_alias_expressions=filter_alias_expressions,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_MATCH_RE.finditer(script_text):
        actual = match.group("actual")
        expected = match.group("expected")
        not_flag = bool(match.group("not"))
        candidate = build_regex_assertion_from_match(
            actual=actual,
            expected=expected,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_EXIST_RE.finditer(script_text):
        actual = match.group("actual")
        not_flag = bool(match.group("not"))
        candidate = build_has_content_assertion_from_match(
            actual=actual,
            has_content=not not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_ENV_GET_EXIST_RE.finditer(script_text):
        variable_name = match.group("name").strip()
        not_flag = bool(match.group("not"))
        candidate = build_variable_get_has_content_assertion_from_match(
            variable_name=variable_name,
            has_content=not not_flag,
            set_variable_element_xpaths=set_variable_element_xpaths,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_LITERAL_STATE_RE.finditer(script_text):
        actual = match.group("actual")
        state = match.group("state")
        not_flag = bool(match.group("not"))
        candidate = build_literal_state_assertion_from_match(
            actual=actual,
            state=state,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_PROPERTY_RE.finditer(script_text):
        actual = match.group("actual")
        args_expr = match.group("args")
        not_flag = bool(match.group("not"))
        candidate = build_property_assertion_from_match(
            actual=actual,
            args_expr=args_expr,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_EMPTY_RE.finditer(script_text):
        actual = match.group("actual")
        not_flag = bool(match.group("not"))
        candidate = build_has_children_assertion_from_match(
            actual=actual,
            has_children=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_TYPE_RE.finditer(script_text):
        actual = match.group("actual")
        expected = match.group("expected")
        not_flag = bool(match.group("not"))
        candidate = build_type_assertion_from_match(
            actual=actual,
            expected=expected,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_WITHIN_RE.finditer(script_text):
        actual = match.group("actual")
        lower_expr = match.group("lower")
        upper_expr = match.group("upper")
        not_flag = bool(match.group("not"))
        candidate = build_range_assertion_from_match(
            actual=actual,
            lower_expr=lower_expr,
            upper_expr=upper_expr,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_ONEOF_RE.finditer(script_text):
        actual = match.group("actual")
        values_expr = match.group("values")
        not_flag = bool(match.group("not"))
        candidate = build_or_assertion_from_match(
            actual=actual,
            values_expr=values_expr,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for match in include_chain_matches:
        actual = match.group("actual")
        first_cmp = match.group("cmp")
        first_expected = match.group("expected")
        chain_text = match.group("chain")
        not_flag = bool(match.group("not"))
        candidate = build_and_assertion_from_match(
            actual=actual,
            first_cmp=first_cmp,
            first_expected=first_expected,
            chain_text=chain_text,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
            filter_alias_expressions=filter_alias_expressions,
        )
        if candidate:
            candidates.append(candidate)

    for match in EXPECT_LENGTH_OF_RE.finditer(script_text):
        actual = match.group("actual")
        expected = match.group("expected")
        not_flag = bool(match.group("not"))
        candidate = build_length_of_assertion_from_match(
            actual=actual,
            expected=expected,
            not_flag=not_flag,
            alias_json_roots=alias_json_roots,
        )
        if candidate:
            candidates.append(candidate)

    for regex, operator in ((EXPECT_AT_LEAST_RE, ">="), (EXPECT_AT_MOST_RE, "<=")):
        for match in regex.finditer(script_text):
            actual = match.group("actual")
            expected = match.group("expected")
            value_occurrence_candidate = build_value_occurrence_assertion_from_filtered_length(
                actual_expr=actual,
                operator=operator,
                expected_count_expr=expected,
                alias_json_roots=alias_json_roots,
                filter_alias_expressions=filter_alias_expressions,
            )
            if value_occurrence_candidate:
                candidates.append(value_occurrence_candidate)
                continue

            occurrence_candidate = build_occurrence_assertion_from_length(
                actual_expr=actual,
                operator=operator,
                expected_expr=expected,
                alias_json_roots=alias_json_roots,
            )
            if occurrence_candidate:
                candidates.append(occurrence_candidate)
                continue

            xpath = json_expression_to_xpath(
                actual,
                value_suffix="string()",
                alias_json_roots=alias_json_roots,
            )
            if not xpath:
                continue
            cmp_value = normalize_expected_value(expected)
            assertion = {
                "type": "numericAssertion",
                "numericAssertion": {
                    "name": f"{operator} {xpath}",
                    "configuration": {
                        "expectedValue": {"type": "fixed", "fixed": cmp_value},
                        "operator": operator,
                    },
                    "selectedElement": {"xpath": xpath, "extractionType": "entireElement"},
                    "options": {"trimContent": True},
                },
            }
            candidates.append(AssertorCandidate(assertion=assertion))

    return candidates


def decode_postman_js_string(raw_value: str) -> str:
    raw = str(raw_value)
    try:
        return bytes(raw, "utf-8").decode("unicode_escape")
    except Exception:
        return raw


def _find_matching_brace(text: str, open_brace_index: int) -> int:
    depth = 0
    quote: Optional[str] = None
    escape = False

    for index in range(open_brace_index, len(text)):
        ch = text[index]
        if quote is not None:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == quote:
                quote = None
            continue

        if ch in {"'", '"', "`"}:
            quote = ch
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return index
            continue

    return -1


def extract_postman_test_blocks(script_text: str) -> List[Tuple[str, str, int, int]]:
    blocks: List[Tuple[str, str, int, int]] = []
    search_from = 0
    while search_from < len(script_text):
        match = PM_TEST_NAME_RE.search(script_text, search_from)
        if not match:
            break

        name = decode_postman_js_string(match.group("name")).strip()
        callback_body_start = script_text.find("{", match.end())
        if callback_body_start == -1:
            search_from = match.end()
            continue
        callback_body_end = _find_matching_brace(script_text, callback_body_start)
        if callback_body_end == -1:
            search_from = match.end()
            continue

        statement_end = callback_body_end + 1
        while statement_end < len(script_text) and script_text[statement_end].isspace():
            statement_end += 1
        if statement_end < len(script_text) and script_text[statement_end] == ")":
            statement_end += 1
            while statement_end < len(script_text) and script_text[statement_end].isspace():
                statement_end += 1
            if statement_end < len(script_text) and script_text[statement_end] == ";":
                statement_end += 1

        body = script_text[callback_body_start + 1 : callback_body_end]
        blocks.append((name, body, match.start(), statement_end))
        search_from = statement_end

    return blocks


def extract_postman_test_names(script_text: str) -> List[str]:
    return [name for name, _, _, _ in extract_postman_test_blocks(script_text) if name]


def set_assertion_candidate_display_name(candidate: AssertorCandidate, name: str) -> AssertorCandidate:
    assertion = dict(candidate.assertion)
    for key, value in candidate.assertion.items():
        if key == "type" or not isinstance(value, dict):
            continue
        if "name" not in value:
            continue
        renamed = dict(value)
        renamed["name"] = name
        assertion[key] = renamed
        return AssertorCandidate(assertion=assertion)
    return candidate


def apply_postman_test_names_to_assertions(
    candidates: Sequence[AssertorCandidate],
    test_names: Sequence[str],
) -> List[AssertorCandidate]:
    if not candidates:
        return []
    if not test_names:
        return list(candidates)

    renamed: List[AssertorCandidate] = []
    for index, candidate in enumerate(candidates):
        label = test_names[index] if index < len(test_names) else test_names[-1]
        label = label.strip()
        if not label:
            renamed.append(candidate)
            continue
        renamed.append(set_assertion_candidate_display_name(candidate, label))
    return renamed


def extract_assertion_candidates_aligned_with_tests(script_text: str) -> List[AssertorCandidate]:
    blocks = extract_postman_test_blocks(script_text)
    if not blocks:
        return extract_assertion_candidates(script_text)

    aligned: List[AssertorCandidate] = []
    consumed_spans: List[Tuple[int, int]] = []

    for name, body, span_start, span_end in blocks:
        consumed_spans.append((span_start, span_end))
        block_candidates = extract_assertion_candidates(body, context_script_text=script_text)
        if not block_candidates:
            continue
        if name:
            block_candidates = [set_assertion_candidate_display_name(candidate, name) for candidate in block_candidates]
        aligned.extend(block_candidates)

    # Preserve assertions that may exist outside pm.test(...) blocks.
    outside_parts: List[str] = []
    cursor = 0
    for span_start, span_end in sorted(consumed_spans):
        if cursor < span_start:
            outside_parts.append(script_text[cursor:span_start])
        cursor = max(cursor, span_end)
    if cursor < len(script_text):
        outside_parts.append(script_text[cursor:])

    if outside_parts:
        outside_text = "\n".join(part for part in outside_parts if part.strip())
        if outside_text.strip():
            aligned.extend(extract_assertion_candidates(outside_text))

    return aligned


def extract_databank_candidates(script_text: str) -> List[DataBankCandidate]:
    candidates: List[DataBankCandidate] = []
    alias_json_roots = extract_alias_json_roots(script_text)
    for match in SET_CALL_RE.finditer(script_text):
        variable_name = match.group("name").strip()
        expr = match.group("expr").strip()
        xpath = json_expression_to_xpath(expr, value_suffix="text()", alias_json_roots=alias_json_roots)
        if not xpath:
            continue
        candidates.append(DataBankCandidate(variable_name=variable_name, xpath=xpath))
    return candidates


def detect_intents(script_text: str) -> List[str]:
    intents: List[str] = []
    if SET_CALL_RE.search(script_text):
        intents.append("data-extraction")
    if ITERATION_DATA_GET_RE.search(script_text):
        intents.append("iteration-data")
    if (
        EXPECT_EQUAL_RE.search(script_text)
        or EXPECT_MATCH_RE.search(script_text)
        or EXPECT_EXIST_RE.search(script_text)
        or EXPECT_LITERAL_STATE_RE.search(script_text)
        or EXPECT_PROPERTY_RE.search(script_text)
        or EXPECT_EMPTY_RE.search(script_text)
        or EXPECT_TYPE_RE.search(script_text)
        or EXPECT_WITHIN_RE.search(script_text)
        or EXPECT_ONEOF_RE.search(script_text)
        or EXPECT_INCLUDE_CHAIN_RE.search(script_text)
        or EXPECT_LENGTH_OF_RE.search(script_text)
        or EXPECT_AT_LEAST_RE.search(script_text)
        or EXPECT_AT_MOST_RE.search(script_text)
    ):
        intents.append("assertion")
    if (
        EXPECT_RESPONSE_CODE_EQUAL_RE.search(script_text)
        or EXPECT_RESPONSE_CODE_ONEOF_RE.search(script_text)
        or EXPECT_RESPONSE_CODE_INCLUDED_IN_ARRAY_RE.search(script_text)
        or EXPECT_RESPONSE_HAVE_STATUS_RE.search(script_text)
        or EXPECT_RESPONSE_CODE_COMPARE_RE.search(script_text)
        or EXPECT_RESPONSE_CODE_WITHIN_RE.search(script_text)
        or EXPECT_RESPONSE_CODE_AT_LEAST_RE.search(script_text)
        or EXPECT_RESPONSE_CODE_AT_MOST_RE.search(script_text)
    ):
        intents.append("response-code-validation")
    if EXPECT_RESPONSE_TIME_BELOW_RE.search(script_text) or EXPECT_RESPONSE_TIME_AT_MOST_RE.search(script_text):
        intents.append("performance-validation")
    if re.search(r"\bif\s*\(|\bfor\s*\(|\bwhile\s*\(|\bthrow\s+new\s+Error\b", script_text):
        intents.append("control-flow")
    if re.search(r"\bconsole\.log\s*\(", script_text):
        intents.append("logging")
    return intents


def requires_script_fallback(
    occurrence: ScriptOccurrence,
    has_unmapped_set_assignments: bool,
    native_created: bool,
    script_text: str,
    safe_prerequest_set_only: bool = False,
) -> bool:
    # Native-only policy:
    # unsupported/unmapped scripting is omitted instead of generating placeholder scripts.
    if occurrence.phase == PHASE_PRE:
        return not safe_prerequest_set_only
    if occurrence.phase == PHASE_UNKNOWN:
        return True
    if has_unmapped_set_assignments:
        return True
    # Keep hard warnings for constructs without a reliable native mapping.
    if HARD_ADVANCED_SCRIPT_PATTERN_RE.search(script_text):
        return True
    # Reduce noise for simple guarded checks when native mappings were created.
    if SOFT_CONDITIONAL_PATTERN_RE.search(script_text):
        return not native_created
    return not native_created


def can_generate_functional_extension_script(
    occurrence: ScriptOccurrence,
    script_text: str,
    conversion_mode: str,
) -> bool:
    # Converter policy: do not emit script fallback unless it is guaranteed executable in SOAtest.
    # Current implementation does not provide a full Postman-runtime-to-SOAtest-script translation
    # that can guarantee correctness for pm.* APIs, so fallback generation is disabled.
    return False


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

    def get_environment(self, environment_id: str) -> Dict[str, Any]:
        return self._request("GET", "/environments", query={"id": environment_id})

    def create_environment(self, parent_suite_id: str, name: str, variables: List[Dict[str, str]]) -> Dict[str, Any]:
        body = {
            "parent": {"id": parent_suite_id},
            "local": {"name": name, "active": True, "variables": variables},
        }
        return self._request("POST", "/environments", body=body)

    def update_environment(self, environment_id: str, name: str, variables: List[Dict[str, str]]) -> Dict[str, Any]:
        body = {"local": {"name": name, "active": True, "variables": variables}}
        return self._request("PUT", "/environments", query={"id": environment_id}, body=body)

    def descendants_assets(self, resource_id: str) -> Dict[str, Any]:
        return self._request("GET", "/descendants/assets", query={"id": resource_id})

    def get_rest_client(self, rest_client_id: str) -> Dict[str, Any]:
        return self._request("GET", "/tools/restClients", query={"id": rest_client_id})

    def update_rest_client(self, rest_client_id: str, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("PUT", "/tools/restClients", query={"id": rest_client_id}, body=body)

    def create_json_databank(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/tools/jsonDataBanks", body=body)

    def create_json_assertor(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/tools/jsonAssertors", body=body)

    def create_extension_tool(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", "/tools/extensionTools", body=body)

    def delete_tool(self, tool_id: str) -> None:
        self._request("DELETE", "/tools", query={"id": tool_id})


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
        raise RuntimeError(f"Target .tst not found: {parent_id}/{target_name}")

    suites = client.get_children(parent_id=tst_id, resource_type="testSuite")
    if not suites:
        suites = client.get_children(parent_id=tst_id)

    top_suite_id = ""
    for suite in suites:
        if str(suite.get("type", "")).lower() == "testsuite":
            top_suite_id = str(suite.get("id", ""))
            break
    if not top_suite_id and suites:
        top_suite_id = str(suites[0].get("id", ""))
    if not top_suite_id:
        raise RuntimeError("Could not resolve top-level Test Suite for target .tst.")

    return tst_id, top_suite_id


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


def collect_script_created_variable_names(
    targets: Sequence[RequestScriptTarget],
    non_request_occurrences: Sequence[ScriptOccurrence],
) -> List[str]:
    seen: Set[str] = set()
    ordered: List[str] = []

    def add_from_script(script_text: str) -> None:
        for match in SET_CALL_RE.finditer(script_text):
            name = match.group("name").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            ordered.append(name)

    for target in targets:
        for occ in target.occurrences:
            add_from_script(occ.script_text)
    for occ in non_request_occurrences:
        add_from_script(occ.script_text)
    return ordered


def collect_prerequest_set_variable_names(
    targets: Sequence[RequestScriptTarget],
) -> List[str]:
    seen: Set[str] = set()
    ordered: List[str] = []
    for target in targets:
        for occ in target.occurrences:
            if occ.phase != PHASE_PRE:
                continue
            for match in SET_CALL_RE.finditer(occ.script_text):
                name = match.group("name").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                ordered.append(name)
    return ordered


def collect_prerequest_set_variable_values(
    targets: Sequence[RequestScriptTarget],
) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for target in targets:
        for occ in target.occurrences:
            if occ.phase != PHASE_PRE:
                continue
            parsed = extract_safe_prerequest_set_assignments(occ.script_text)
            if parsed:
                values.update(parsed)
    return values


def collect_iteration_data_variable_names(
    targets: Sequence[RequestScriptTarget],
    non_request_occurrences: Sequence[ScriptOccurrence],
) -> List[str]:
    seen: Set[str] = set()
    ordered: List[str] = []

    def add_from_script(script_text: str) -> None:
        for match in ITERATION_DATA_GET_RE.finditer(script_text):
            name = match.group("name").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            ordered.append(name)

    for target in targets:
        for occ in target.occurrences:
            add_from_script(occ.script_text)
    for occ in non_request_occurrences:
        add_from_script(occ.script_text)
    return ordered


NUMERIC_VAR_HINT_RE = re.compile(r"(id|qty|quantity|count|number|stock|total|index)$", re.IGNORECASE)


def default_value_for_pre_request_var(name: str) -> str:
    n = name.strip()
    if NUMERIC_VAR_HINT_RE.search(n):
        return "1"
    return "SET_ME"


def ensure_environment_has_pre_request_defaults(
    client: SoaVirtClient,
    top_suite_id: str,
    environment_name: str,
    variable_names: Sequence[str],
    value_overrides: Optional[Dict[str, str]] = None,
) -> List[str]:
    if not variable_names:
        return []

    env_id = ""
    env_name = environment_name
    existing_envs = client.get_children(parent_id=top_suite_id, resource_type="environment")
    for env in existing_envs:
        if str(env.get("name", "")).lower() == environment_name.lower():
            env_id = str(env.get("id", ""))
            env_name = str(env.get("name", environment_name))
            break

    existing_variables: List[Dict[str, str]] = []
    if env_id:
        try:
            env_payload = client.get_environment(env_id)
            local = env_payload.get("local", {}) if isinstance(env_payload, dict) else {}
            variables = local.get("variables", []) if isinstance(local, dict) else []
            if isinstance(variables, list):
                for entry in variables:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name", "")).strip()
                    if not name:
                        continue
                    existing_variables.append({"name": name, "value": str(entry.get("value", ""))})
        except RuntimeError:
            existing_variables = []

    existing_names = {entry["name"] for entry in existing_variables}
    added: List[str] = []
    for name in variable_names:
        if name in existing_names:
            continue
        override_value = None
        if value_overrides:
            override_value = value_overrides.get(name)
        existing_variables.append(
            {
                "name": name,
                "value": override_value if override_value is not None else default_value_for_pre_request_var(name),
            }
        )
        existing_names.add(name)
        added.append(name)

    if not added:
        return []

    if env_id:
        client.update_environment(env_id, env_name, existing_variables)
    else:
        created = client.create_environment(top_suite_id, environment_name, existing_variables)
        created_id = str(created.get("id", ""))
        if not created_id:
            raise RuntimeError("SOAVirt API did not return environment id after create.")
    return added


def ensure_environment_has_variables(
    client: SoaVirtClient,
    top_suite_id: str,
    environment_name: str,
    variable_names: Sequence[str],
    default_value: str = "SET_ME",
) -> List[str]:
    if not variable_names:
        return []

    env_id = ""
    env_name = environment_name
    existing_envs = client.get_children(parent_id=top_suite_id, resource_type="environment")
    for env in existing_envs:
        if str(env.get("name", "")).lower() == environment_name.lower():
            env_id = str(env.get("id", ""))
            env_name = str(env.get("name", environment_name))
            break

    existing_variables: List[Dict[str, str]] = []
    if env_id:
        try:
            env_payload = client.get_environment(env_id)
            local = env_payload.get("local", {}) if isinstance(env_payload, dict) else {}
            variables = local.get("variables", []) if isinstance(local, dict) else []
            if isinstance(variables, list):
                for entry in variables:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name", "")).strip()
                    if not name:
                        continue
                    existing_variables.append({"name": name, "value": str(entry.get("value", ""))})
        except RuntimeError:
            existing_variables = []

    existing_names = {entry["name"] for entry in existing_variables}
    added: List[str] = []
    for name in variable_names:
        if name in existing_names:
            continue
        existing_variables.append({"name": name, "value": str(default_value)})
        existing_names.add(name)
        added.append(name)

    if not added:
        return []

    if env_id:
        client.update_environment(env_id, env_name, existing_variables)
    else:
        created = client.create_environment(top_suite_id, environment_name, existing_variables)
        created_id = str(created.get("id", ""))
        if not created_id:
            raise RuntimeError("SOAVirt API did not return environment id after create.")
    return added


def remove_script_created_environment_placeholders(
    client: SoaVirtClient,
    top_suite_id: str,
    environment_name: str,
    variable_names: Sequence[str],
    removable_values: Sequence[str] = ("", "SET_ME"),
) -> List[str]:
    if not variable_names:
        return []

    existing_envs = client.get_children(parent_id=top_suite_id, resource_type="environment")
    env_id = ""
    env_name = environment_name
    for env in existing_envs:
        if str(env.get("name", "")).lower() == environment_name.lower():
            env_id = str(env.get("id", ""))
            env_name = str(env.get("name", environment_name))
            break
    if not env_id:
        return []

    env_payload = client.get_environment(env_id)
    local = env_payload.get("local", {}) if isinstance(env_payload, dict) else {}
    variables = local.get("variables", []) if isinstance(local, dict) else []
    if not isinstance(variables, list):
        return []

    target_names = set(variable_names)
    removable_values_set = {str(v) for v in removable_values}
    kept: List[Dict[str, str]] = []
    removed: List[str] = []
    for entry in variables:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        value = str(entry.get("value", ""))
        if name in target_names and value in removable_values_set:
            removed.append(name)
            continue
        kept.append({"name": name, "value": value})

    if not removed:
        return []

    client.update_environment(env_id, env_name, kept)
    return removed


def normalize_tool_name_for_compare(name: str) -> str:
    value = str(name).strip().lower()
    return re.sub(r"\[\d+\]$", "", value).strip()


def iter_descendant_nodes(node: Any) -> Sequence[Dict[str, Any]]:
    stack: List[Any] = [node]
    out: List[Dict[str, Any]] = []
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            out.append(current)
            children = current.get("children")
            if isinstance(children, list):
                stack.extend(children)
        elif isinstance(current, list):
            stack.extend(current)
    return out


def remove_existing_tool_by_name(client: SoaVirtClient, parent_id: str, name: str) -> None:
    target = normalize_tool_name_for_compare(name)
    delete_ids: Set[str] = set()

    # Fast path: direct children lookup under requested parent.
    for child in client.get_children(parent_id=parent_id):
        child_name = normalize_tool_name_for_compare(child.get("name", ""))
        if child_name == target:
            child_id = str(child.get("id", ""))
            if child_id:
                delete_ids.add(child_id)

    # Defensive path: /children on traffic providers may miss tools.
    if parent_id.endswith("/Response Traffic") or parent_id.endswith("/Request Traffic"):
        rest_client_id = parent_id.rsplit("/", 1)[0]
        try:
            descendants = client.descendants_assets(rest_client_id)
            for node in iter_descendant_nodes(descendants):
                node_id = str(node.get("id", ""))
                if not node_id or not node_id.startswith(parent_id + "/"):
                    continue
                node_name = normalize_tool_name_for_compare(node.get("name", ""))
                if node_name == target:
                    delete_ids.add(node_id)
        except RuntimeError:
            pass

    for tool_id in sorted(delete_ids):
        try:
            client.delete_tool(tool_id)
        except RuntimeError:
            pass


def build_rest_client_update_payload_for_codes(current: Dict[str, Any], codes: Sequence[int]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        key: value
        for key, value in current.items()
        if key not in {"id", "url", "relationships", "referenced"}
    }

    misc = payload.setdefault("misc", {})
    codes_text = ",".join(str(code) for code in sorted(set(codes)))
    misc["validHttpResponseCodes"] = {"type": "fixed", "fixed": codes_text}
    return payload


def build_rest_client_update_payload_for_timeout(current: Dict[str, Any], timeout_ms: int) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        key: value
        for key, value in current.items()
        if key not in {"id", "url", "relationships", "referenced"}
    }

    misc = payload.setdefault("misc", {})
    timeout = misc.setdefault("timeout", {})
    timeout["action"] = timeout.get("action", "failOnTimeout")
    milliseconds = timeout.setdefault("milliseconds", {})
    milliseconds["mode"] = "Custom"
    milliseconds["value"] = int(timeout_ms)
    return payload


def create_json_databank_tool(
    client: SoaVirtClient,
    parent_id: str,
    name: str,
    candidates: Sequence[DataBankCandidate],
) -> str:
    remove_existing_tool_by_name(client, parent_id, name)
    selected_elements = []
    for candidate in candidates:
        selected_elements.append(
            {
                "dataSourceColumn": {"customColumn": {"customColumnName": candidate.variable_name}},
                "selectedElement": {
                    "xpath": candidate.xpath,
                    "options": {"contentOnly": "textContent"},
                },
            }
        )

    body = {
        "name": name,
        "parent": {"id": parent_id},
        "toolSettings": {
            "selectedElements": selected_elements,
            "options": {
                "allowAlteration": False,
                "extractEmptyElementsAs": {"enabled": False, "value": ""},
                "extractMissingElementsAs": {"enabled": False, "value": ""},
                "saveExpectedJson": False,
            },
        },
    }
    created = client.create_json_databank(body)
    return str(created.get("id", ""))


def create_json_assertor_tool(
    client: SoaVirtClient,
    parent_id: str,
    name: str,
    candidates: Sequence[AssertorCandidate],
) -> str:
    remove_existing_tool_by_name(client, parent_id, name)
    body = {
        "name": name,
        "parent": {"id": parent_id},
        "toolSettings": {
            "assertions": [candidate.assertion for candidate in candidates],
            "expectedJson": {"saveExpectedJson": False},
        },
    }
    created = client.create_json_assertor(body)
    return str(created.get("id", ""))


def write_external_script_file(
    output_dir: Path,
    request_name: str,
    occurrence: ScriptOccurrence,
    script_text_override: Optional[str] = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_request = sanitize_filename(request_name)[:80]
    file_name = f"{safe_request}.{occurrence.phase}.{occurrence.event_index + 1}.js"
    path = output_dir / file_name
    header = (
        f"// Source: {occurrence.json_path}\n"
        f"// Phase: {occurrence.phase}\n"
        f"// Scope: {occurrence.scope}\n\n"
    )
    script_text = occurrence.script_text if script_text_override is None else script_text_override
    path.write_text(header + script_text, encoding="utf-8")
    return path


def create_extension_tool(
    client: SoaVirtClient,
    parent_id: str,
    name: str,
    language: str,
    storage_mode: str,
    script_text: str,
    external_script_path: Optional[Path],
) -> str:
    remove_existing_tool_by_name(client, parent_id, name)
    tool_settings: Dict[str, Any] = {
        "language": language,
        "exitCodeIndicatesSuccess": False,
        "useDataSource": False,
    }
    if storage_mode == SCRIPT_STORAGE_EXTERNAL and external_script_path is not None:
        tool_settings["value"] = {"file": {"location": {"external": str(external_script_path)}}}
    else:
        tool_settings["value"] = {"text": script_text}

    body = {
        "name": name,
        "parent": {"id": parent_id},
        "toolSettings": tool_settings,
    }
    created = client.create_extension_tool(body)
    return str(created.get("id", ""))


def build_preserve_mode_extension_script(script_text: str) -> str:
    shim = """// Preserved Postman script fallback with compatibility shim.
// Note: Postman runtime APIs (pm.*) are not natively available in SOAtest Extension Tools.
// Native SOAtest tools are created for mapped patterns (for example pm.*.set -> JSON Data Bank).
var pm = (typeof pm !== "undefined" && pm) || (function () {
  function noop() {}
  function buildToChain() {
    var chain = {};
    chain.equal = noop;
    chain.eql = noop;
    chain.include = noop;
    chain.contain = noop;
    chain.above = noop;
    chain.below = noop;
    chain.oneOf = noop;
    chain["true"] = noop;
    chain["false"] = noop;
    chain.not = chain;
    chain.be = chain;
    chain.at = chain;
    chain.least = noop;
    chain.most = noop;
    return chain;
  }
  return {
    response: {
      code: 0,
      json: function () { return {}; },
      text: function () { return ""; }
    },
    environment: { set: noop, get: function () { return null; } },
    collectionVariables: { set: noop, get: function () { return null; } },
    variables: { set: noop, get: function () { return null; } },
    test: function (name, fn) {
      if (typeof fn === "function") {
        try { fn(); } catch (e) {}
      }
    },
    expect: function () { return { to: buildToChain() }; }
  };
})();
"""
    return shim + "\ntry {\n" + script_text + "\n} catch (e) {\n  // Preserved fallback intentionally suppresses runtime incompatibilities.\n}\n"


def build_rest_client_update_payload(current: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in current.items()
        if key not in {"id", "url", "relationships", "referenced"}
    }


def rewrite_rest_client_script_variable_references(
    client: SoaVirtClient,
    rest_client_id: str,
    script_variable_names: Sequence[str],
) -> bool:
    if not script_variable_names:
        return False

    current = client.get_rest_client(rest_client_id)
    payload = build_rest_client_update_payload(current)
    changed = False

    resource = payload.get("resource")
    if isinstance(resource, dict):
        literal_text = resource.get("literalText")
        if isinstance(literal_text, dict):
            fixed = literal_text.get("fixed")
            if isinstance(fixed, str):
                rewritten = rewrite_script_variable_references(fixed, script_variable_names)
                if rewritten != fixed:
                    literal_text["fixed"] = rewritten
                    changed = True

    body_payload = payload.get("payload")
    if isinstance(body_payload, dict):
        input_block = body_payload.get("input")
        if isinstance(input_block, dict):
            literal_block = input_block.get("literal")
            if isinstance(literal_block, dict):
                text = literal_block.get("text")
                if isinstance(text, str):
                    rewritten = rewrite_script_variable_references(text, script_variable_names)
                    if rewritten != text:
                        literal_block["text"] = rewritten
                        changed = True

    transport = payload.get("httpOptions", {}).get("transport", {}) if isinstance(payload, dict) else {}
    if isinstance(transport, dict):
        for version_key in ("http10", "http11", "http20"):
            version_obj = transport.get(version_key)
            if not isinstance(version_obj, dict):
                continue
            headers = version_obj.get("httpHeaders")
            if isinstance(headers, dict):
                literal_headers = headers.get("literal")
                if isinstance(literal_headers, str):
                    rewritten = rewrite_script_variable_references(literal_headers, script_variable_names)
                    if rewritten != literal_headers:
                        headers["literal"] = rewritten
                        changed = True

    if not changed:
        return False

    client.update_rest_client(rest_client_id, payload)
    return True


def apply_request_event(
    client: SoaVirtClient,
    rest_client_id: str,
    request_name: str,
    occurrence: ScriptOccurrence,
    conversion_mode: str,
    storage_mode: str,
    script_language: str,
    external_script_dir: Optional[Path],
    script_variable_names: Sequence[str],
) -> EventApplyResult:
    result = EventApplyResult()
    result.intents = detect_intents(occurrence.script_text)

    if not occurrence.has_executable_code:
        return result

    request_parent_id = f"{rest_client_id}/Request Traffic"
    response_parent_id = f"{rest_client_id}/Response Traffic"
    is_pre = occurrence.phase == PHASE_PRE
    is_post = occurrence.phase == PHASE_POST

    native_created = False
    has_unmapped_set_assignments = False
    safe_prerequest_set_only = False
    if is_pre:
        safe_prerequest_set_only = extract_safe_prerequest_set_assignments(occurrence.script_text) is not None
    if is_post:
        databank_candidates = extract_databank_candidates(occurrence.script_text)
        set_assignments = [m.group("name").strip() for m in SET_CALL_RE.finditer(occurrence.script_text)]
        if set_assignments:
            mapped_assignments = {candidate.variable_name for candidate in databank_candidates}
            unmapped = [name for name in set_assignments if name not in mapped_assignments]
            if unmapped:
                has_unmapped_set_assignments = True
                unique = ", ".join(sorted(set(unmapped)))
                result.warnings.append(
                    "Could not map some script variable assignments to JSON Data Bank paths: "
                    f"{unique}. Downstream variable references may remain unresolved."
                )

        if databank_candidates:
            try:
                db_name = "JSON Data Bank"
                tool_id = create_json_databank_tool(
                    client=client,
                    parent_id=response_parent_id,
                    name=db_name,
                    candidates=databank_candidates,
                )
                if tool_id:
                    result.created_tool_ids.append(tool_id)
                    native_created = True
            except RuntimeError as exc:
                result.warnings.append(f"JSON Data Bank creation failed: {exc}")

        assertor_candidates = extract_assertion_candidates_aligned_with_tests(occurrence.script_text)
        if assertor_candidates:
            rewritten_assertors: List[AssertorCandidate] = []
            for candidate in assertor_candidates:
                rewritten_assertors.append(
                    AssertorCandidate(
                        assertion=rewrite_script_variable_references_in_structure(
                            candidate.assertion, script_variable_names
                        )
                    )
                )
            assertor_candidates = rewritten_assertors
        if assertor_candidates:
            try:
                assertor_name = "JSON Assertor"
                tool_id = create_json_assertor_tool(
                    client=client,
                    parent_id=response_parent_id,
                    name=assertor_name,
                    candidates=assertor_candidates,
                )
                if tool_id:
                    result.created_tool_ids.append(tool_id)
                    native_created = True
            except RuntimeError as exc:
                result.warnings.append(f"JSON Assertor creation failed: {exc}")

        response_codes = extract_response_code_expectations(occurrence.script_text)
        response_timeout_ms = extract_response_time_upper_bound_ms(occurrence.script_text)
        if response_codes or response_timeout_ms is not None:
            try:
                current = client.get_rest_client(rest_client_id)
                updated: Dict[str, Any] = current
                if response_codes:
                    updated = build_rest_client_update_payload_for_codes(updated, sorted(response_codes))
                if response_timeout_ms is not None:
                    updated = build_rest_client_update_payload_for_timeout(updated, response_timeout_ms)
                client.update_rest_client(rest_client_id, updated)
                result.updated_rest_client = True
                native_created = True
            except RuntimeError as exc:
                result.warnings.append(f"REST client options update failed: {exc}")

    needs_fallback_script = requires_script_fallback(
        occurrence=occurrence,
        has_unmapped_set_assignments=has_unmapped_set_assignments,
        native_created=native_created,
        script_text=occurrence.script_text,
        safe_prerequest_set_only=safe_prerequest_set_only,
    )
    if needs_fallback_script:
        if can_generate_functional_extension_script(
            occurrence=occurrence,
            script_text=occurrence.script_text,
            conversion_mode=conversion_mode,
        ):
            parent_id = response_parent_id
            if is_pre:
                parent_id = request_parent_id
            elif occurrence.phase == PHASE_UNKNOWN:
                parent_id = f"{rest_client_id}/Response Traffic"

            script_name = f"Postman Script {occurrence.phase} {occurrence.event_index + 1}"
            external_path: Optional[Path] = None
            if storage_mode == SCRIPT_STORAGE_EXTERNAL and external_script_dir is not None:
                external_path = write_external_script_file(
                    output_dir=external_script_dir,
                    request_name=request_name,
                    occurrence=occurrence,
                    script_text_override=occurrence.script_text,
                )
            try:
                tool_id = create_extension_tool(
                    client=client,
                    parent_id=parent_id,
                    name=script_name,
                    language=script_language,
                    storage_mode=storage_mode,
                    script_text=occurrence.script_text,
                    external_script_path=external_path,
                )
                if tool_id:
                    result.created_tool_ids.append(tool_id)
            except RuntimeError as exc:
                result.warnings.append(f"Extension Tool fallback creation failed: {exc}")
        else:
            result.warnings.append(
                "Script logic has no guaranteed native/functional SOAtest implementation; omitted per native-only policy."
            )

    return result


def apply_non_request_event(
    client: SoaVirtClient,
    top_suite_id: str,
    occurrence: ScriptOccurrence,
    storage_mode: str,
    script_language: str,
    external_script_dir: Optional[Path],
) -> EventApplyResult:
    result = EventApplyResult()
    result.intents = detect_intents(occurrence.script_text)
    if not occurrence.has_executable_code:
        return result

    result.warnings.append(
        "Collection/folder scope script omitted: no guaranteed native or fully functional SOAtest script equivalent."
    )
    return result


def build_report(
    collection_name: str,
    input_file: Path,
    target_tst_id: str,
    mode: str,
    storage_mode: str,
    request_event_rows: List[Dict[str, Any]],
    non_request_rows: List[Dict[str, Any]],
    warnings: List[str],
) -> Dict[str, Any]:
    created_native_tools = 0
    preserved_script_tools = 0
    rest_client_updates = 0

    for row in request_event_rows + non_request_rows:
        for tool_id in row.get("createdToolIds", []):
            lower = str(tool_id).lower()
            if "postman script data bank" in lower or "postman script assertor" in lower:
                created_native_tools += 1
            elif "/response traffic/postman script " in lower or "/request traffic/postman script " in lower:
                preserved_script_tools += 1
            else:
                created_native_tools += 1
        if row.get("updatedRestClient", False):
            rest_client_updates += 1

    return {
        "collectionName": collection_name,
        "inputFile": input_file.name,
        "targetTstId": target_tst_id,
        "mode": mode,
        "scriptStorage": storage_mode,
        "summary": {
            "requestScriptEventsProcessed": len(request_event_rows),
            "nonRequestScriptEventsProcessed": len(non_request_rows),
            "nativeToolsCreated": created_native_tools,
            "preservedScriptToolsCreated": preserved_script_tools,
            "restClientsUpdated": rest_client_updates,
            "warningCount": len(warnings),
        },
        "requestEvents": request_event_rows,
        "nonRequestEvents": non_request_rows,
        "warnings": warnings,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Postman pre-request and test scripts into SOAtest native tools. "
            "Unsupported script logic is omitted rather than emitted as non-functional fallback."
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
        "--environment-name",
        default="Default Environment",
        help=(
            "SOAtest environment name used for script-created variable declarations "
            "(default: Default Environment)."
        ),
    )
    parser.add_argument(
        "--declare-script-created-env-vars",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Declare discovered script-created variables in the SOAtest environment (default: disabled). "
            "Use --no-declare-script-created-env-vars to disable."
        ),
    )
    parser.add_argument(
        "--script-created-env-default-value",
        default="SET_ME",
        help=(
            "Placeholder value assigned when creating script-created environment variables "
            "(default: SET_ME)."
        ),
    )
    parser.add_argument(
        "--username", default=None, help="Optional SOAVirt basic-auth username."
    )
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

    request_targets, non_request_occurrences = collect_script_targets(collection)

    effective_conversion_mode = SCRIPT_MODE_PREFER_NATIVE
    effective_storage_mode = SCRIPT_STORAGE_EMBEDDED
    effective_script_language = DEFAULT_SCRIPT_LANGUAGE
    external_script_dir: Optional[Path] = None

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
            test_file_name_without_ext=target_name,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    descendants = client.descendants_assets(tst_id)
    rest_lookup = build_rest_client_lookup(descendants)

    request_rows: List[Dict[str, Any]] = []
    non_request_rows: List[Dict[str, Any]] = []
    warnings: List[str] = []
    script_created_variable_names = collect_script_created_variable_names(request_targets, non_request_occurrences)
    prerequest_set_variable_names = collect_prerequest_set_variable_names(request_targets)
    prerequest_set_variable_values = collect_prerequest_set_variable_values(request_targets)
    iteration_data_variable_names = collect_iteration_data_variable_names(request_targets, non_request_occurrences)
    added_environment_variables: List[str] = []
    removed_placeholder_variables: List[str] = []
    added_prerequest_defaults: List[str] = []
    added_iteration_data_defaults: List[str] = []
    rest_clients_rewritten_for_script_vars = 0

    if args.declare_script_created_env_vars:
        try:
            added_environment_variables = ensure_environment_has_variables(
                client=client,
                top_suite_id=top_suite_id,
                environment_name=args.environment_name,
                variable_names=script_created_variable_names,
                default_value=args.script_created_env_default_value,
            )
        except RuntimeError as exc:
            warnings.append(f"Failed to ensure script-created variables in environment: {exc}")
    else:
        try:
            removed_placeholder_variables = remove_script_created_environment_placeholders(
                client=client,
                top_suite_id=top_suite_id,
                environment_name=args.environment_name,
                variable_names=script_created_variable_names,
            )
        except RuntimeError as exc:
            warnings.append(f"Failed to remove script-created environment placeholders: {exc}")

        try:
            added_prerequest_defaults = ensure_environment_has_pre_request_defaults(
                client=client,
                top_suite_id=top_suite_id,
                environment_name=args.environment_name,
                variable_names=prerequest_set_variable_names,
                value_overrides=prerequest_set_variable_values,
            )
        except RuntimeError as exc:
            warnings.append(f"Failed to ensure pre-request default variables in environment: {exc}")

    try:
        added_iteration_data_defaults = ensure_environment_has_pre_request_defaults(
            client=client,
            top_suite_id=top_suite_id,
            environment_name=args.environment_name,
            variable_names=iteration_data_variable_names,
        )
    except RuntimeError as exc:
        warnings.append(f"Failed to ensure iteration-data variables in environment: {exc}")

    for target in request_targets:
        key = (target.folder_path, target.request_name)
        ids = rest_lookup.get(key)
        if not ids:
            warnings.append(
                "Could not map request scripts to SOAtest REST Client: "
                f"folder={'/'.join(target.folder_path) if target.folder_path else '(root)'} name={target.request_name}"
            )
            for occurrence in target.occurrences:
                request_rows.append(
                    {
                        "folderHierarchy": list(target.folder_path),
                        "requestName": target.request_name,
                        "jsonPath": occurrence.json_path,
                        "phase": occurrence.phase,
                        "scope": occurrence.scope,
                        "applied": False,
                        "createdToolIds": [],
                        "updatedRestClient": False,
                        "intents": detect_intents(occurrence.script_text),
                        "warnings": ["REST Client mapping not found."],
                    }
                )
            continue

        rest_client_id = ids.pop(0)

        try:
            if rewrite_rest_client_script_variable_references(
                client=client,
                rest_client_id=rest_client_id,
                script_variable_names=script_created_variable_names,
            ):
                rest_clients_rewritten_for_script_vars += 1
        except RuntimeError as exc:
            warnings.append(
                f"Failed to rewrite script-created variable references in REST Client {rest_client_id}: {exc}"
            )

        for occurrence in target.occurrences:
            event_result = apply_request_event(
                client=client,
                rest_client_id=rest_client_id,
                request_name=target.request_name,
                occurrence=occurrence,
                conversion_mode=effective_conversion_mode,
                storage_mode=effective_storage_mode,
                script_language=effective_script_language,
                external_script_dir=external_script_dir,
                script_variable_names=script_created_variable_names,
            )
            request_rows.append(
                {
                    "folderHierarchy": list(target.folder_path),
                    "requestName": target.request_name,
                    "restClientId": rest_client_id,
                    "jsonPath": occurrence.json_path,
                    "phase": occurrence.phase,
                    "scope": occurrence.scope,
                    "applied": bool(event_result.created_tool_ids) or event_result.updated_rest_client,
                    "createdToolIds": event_result.created_tool_ids,
                    "updatedRestClient": event_result.updated_rest_client,
                    "intents": event_result.intents,
                    "warnings": event_result.warnings,
                }
            )
            warnings.extend(event_result.warnings)

    for occurrence in non_request_occurrences:
        event_result = apply_non_request_event(
            client=client,
            top_suite_id=top_suite_id,
            occurrence=occurrence,
            storage_mode=effective_storage_mode,
            script_language=effective_script_language,
            external_script_dir=external_script_dir,
        )
        non_request_rows.append(
            {
                "folderHierarchy": list(occurrence.folder_hierarchy),
                "requestName": occurrence.request_name,
                "jsonPath": occurrence.json_path,
                "phase": occurrence.phase,
                "scope": occurrence.scope,
                "applied": bool(event_result.created_tool_ids),
                "createdToolIds": event_result.created_tool_ids,
                "updatedRestClient": False,
                "intents": event_result.intents,
                "warnings": event_result.warnings,
            }
        )
        warnings.extend(event_result.warnings)

    report = build_report(
        collection_name=collection_name,
        input_file=args.postman_collection,
        target_tst_id=tst_id,
        mode=effective_conversion_mode,
        storage_mode=effective_storage_mode,
        request_event_rows=request_rows,
        non_request_rows=non_request_rows,
        warnings=warnings,
    )

    summary = report.get("summary", {})
    print(f"Target .tst: {tst_id}")
    print(f"Script conversion mode: {effective_conversion_mode}")
    print("Script fallback policy: disabled unless guaranteed functional translation exists.")
    print(f"Request script events processed: {summary.get('requestScriptEventsProcessed', 0)}")
    print(f"Non-request script events processed: {summary.get('nonRequestScriptEventsProcessed', 0)}")
    print(f"Script-created variables discovered: {len(script_created_variable_names)}")
    print(f"Pre-request set variables discovered: {len(prerequest_set_variable_names)}")
    print(f"Iteration-data variables discovered: {len(iteration_data_variable_names)}")
    print(f"Script-created variables added to environment: {len(added_environment_variables)}")
    print(f"Script-created environment placeholders removed: {len(removed_placeholder_variables)}")
    print(f"Pre-request default variables added to environment: {len(added_prerequest_defaults)}")
    print(f"Iteration-data variables added to environment defaults: {len(added_iteration_data_defaults)}")
    print(f"REST clients rewritten for script-created columns: {rest_clients_rewritten_for_script_vars}")
    print(f"Native tools created: {summary.get('nativeToolsCreated', 0)}")
    print(f"Preserved script tools created: {summary.get('preservedScriptToolsCreated', 0)}")
    print(f"REST clients updated: {summary.get('restClientsUpdated', 0)}")
    print(f"Warnings: {summary.get('warningCount', 0)}")
    if external_script_dir is not None:
        print(f"External script directory: {external_script_dir}")
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
