#!/usr/bin/env python3
"""
Template fallback patcher for SOAtest .tst YAML.

API-first remains the primary path. This script auto-runs fallback patching
for auth persistence, Difference assertions, and binary request bodies when needed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
SOATEST_VAR_REFERENCE_RE = re.compile(r"^\$\{([^{}]+)\}$")
SCHEMA_VERSION_RE = re.compile(r"(?m)^schemaVersion:\s*.+$")
SCHEMA_VERSION_VALUE_RE = re.compile(r"(?m)^schemaVersion:\s*(.+)$")

RESOURCES_DIR = Path(__file__).resolve().parents[1] / "resources"
DEFAULT_DIFFERENCE_ASSERTIONS_TEMPLATE = RESOURCES_DIR / "difference_assertions_template.tst"
DEFAULT_BINARY_BODY_TEMPLATE = RESOURCES_DIR / "binary_body_template.tst"


DIGEST_PROFILE_TEMPLATE = (
    "  - $type: DigestAuthentication\n"
    "    name: ${AUTH_PROFILE_NAME}\n"
    "    username:\n"
    "      fixedValue:\n"
    "        $type: StringTestValue\n"
    "        username: \"${AUTH_USERNAME}\"\n"
    "    password:\n"
    "      fixedValue:\n"
    "        $type: StringTestValue\n"
    "        password: \"${AUTH_PASSWORD}\"\n"
)


NTLM_PROFILE_TEMPLATE = (
    "  - $type: NTLMAuthentication\n"
    "    name: ${AUTH_PROFILE_NAME}\n"
    "    username:\n"
    "      fixedValue:\n"
    "        $type: StringTestValue\n"
    "        username: \"${AUTH_USERNAME}\"\n"
    "    password:\n"
    "      fixedValue:\n"
    "        $type: StringTestValue\n"
    "        password: \"${AUTH_PASSWORD}\"\n"
    "${AUTH_NTLM_DOMAIN_BLOCK}"
    "${AUTH_NTLM_WORKSTATION_BLOCK}"
)


AWS_SIGNATURE_PROFILE_TEMPLATE = (
    "  - $type: AwsSignatureAuthentication\n"
    "    name: ${AUTH_PROFILE_NAME}\n"
    "    config:\n"
    "${AUTH_AWS_AUTH_MODE_BLOCK}"
    "      accessKeyId:\n"
    "        values:\n"
    "        - $type: ScriptedValue\n"
    "        fixedValue:\n"
    "          $type: StringTestValue\n"
    "          value: \"${AUTH_ACCESS_KEY_ID}\"\n"
    "      secretAccessKey:\n"
    "        values:\n"
    "        - $type: ScriptedValue\n"
    "        fixedValue:\n"
    "          $type: StringTestValue\n"
    "          value: \"${AUTH_SECRET_ACCESS_KEY}\"\n"
    "${AUTH_AWS_SESSION_TOKEN_BLOCK}"
    "${AUTH_AWS_SERVICE_BLOCK}"
    "${AUTH_AWS_REGION_BLOCK}"
)


OAUTH2_PROFILE_TEMPLATE = (
    "  - $type: OAuth2Authentication\n"
    "    name: ${AUTH_PROFILE_NAME}\n"
    "    config:\n"
    "      codeVerifier:\n"
    "        values:\n"
    "        - $type: AutomaticValue\n"
    "        - $type: ScriptedValue\n"
    "        fixedValue:\n"
    "          $type: StringTestValue\n"
    "        selectedIndex: 0\n"
    "      grantType: ${AUTH_OAUTH_GRANT_TYPE}\n"
    "      redirectURI:\n"
    "        values:\n"
    "        - $type: ScriptedValue\n"
    "        fixedValue:\n"
    "          $type: StringTestValue\n"
    "        selectedIndex: 0\n"
    "      tokenURI:\n"
    "        values:\n"
    "        - $type: ScriptedValue\n"
    "        fixedValue:\n"
    "          $type: StringTestValue\n"
    "          tokenURI: \"${AUTH_OAUTH_TOKEN_URI}\"\n"
    "      clientID:\n"
    "        values:\n"
    "        - $type: ScriptedValue\n"
    "        fixedValue:\n"
    "          $type: StringTestValue\n"
    "          clientID: \"${AUTH_OAUTH_CLIENT_ID}\"\n"
    "      clientSecret:\n"
    "        values:\n"
    "        - $type: ScriptedValue\n"
    "        fixedValue:\n"
    "          $type: StringTestValue\n"
    "          clientSecret: \"${AUTH_OAUTH_CLIENT_SECRET}\"\n"
    "${AUTH_OAUTH_SCOPE_BLOCK}"
    "${AUTH_OAUTH_AUDIENCE_BLOCK}"
    "${AUTH_OAUTH_ACCESS_TOKEN_BLOCK}"
)


OAUTH_GRANT_TYPE_TO_SOATEST: Dict[str, int] = {
    "client_credentials": 2,
}


@dataclass(frozen=True)
class AuthSpec:
    mode: str
    username: str = ""
    password: str = ""
    domain: str = ""
    workstation: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    service: str = ""
    region: str = ""
    aws_auth_mode: Optional[int] = None
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_grant_type: str = ""
    oauth_add_token_to: str = ""
    oauth_scope: str = ""
    oauth_audience: str = ""
    oauth_access_token: str = ""
    oauth_header_prefix: str = ""
    jwt_payload: str = ""
    jwt_secret: str = ""
    jwt_algorithm: str = ""
    jwt_add_token_to: str = ""
    jwt_header_prefix: str = ""
    jwt_headers: str = ""
    jwt_secret_base64_encoded: Optional[bool] = None


@dataclass(frozen=True)
class RequestTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    effective_auth: AuthSpec


@dataclass(frozen=True)
class AuthProfile:
    mode: str
    name: str
    username: str
    password: str
    domain: str = ""
    workstation: str = ""
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    service: str = ""
    region: str = ""
    aws_auth_mode: Optional[int] = None
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_grant_type: str = ""
    oauth_add_token_to: str = ""
    oauth_scope: str = ""
    oauth_audience: str = ""


@dataclass(frozen=True)
class RequestExpectation:
    request_name: str
    mode: str
    auth_profile_name: str = ""
    oauth_access_token: str = ""
    oauth_add_token_to: str = ""
    oauth_header_prefix: str = ""
    jwt_access_token: str = ""
    jwt_add_token_to: str = ""
    jwt_header_prefix: str = ""


@dataclass(frozen=True)
class BinaryBodyTarget:
    request_name: str
    body_mode: str
    source_reference: str = ""
    content_type: str = ""


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def convert_postman_variable_syntax(text: str) -> str:
    if not text:
        return ""
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", str(text))


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
            if value:
                return convert_postman_variable_syntax(value)
    return ""


def extract_postman_binary_source(body_obj: Dict[str, Any]) -> str:
    file_obj = body_obj.get("file") if isinstance(body_obj.get("file"), dict) else None
    if isinstance(file_obj, dict):
        src = file_obj.get("src")
        if isinstance(src, list):
            for candidate in src:
                value = str(candidate).strip()
                if value:
                    return convert_postman_variable_syntax(value)
        elif isinstance(src, str):
            value = src.strip()
            if value:
                return convert_postman_variable_syntax(value)

    binary_obj = body_obj.get("binary")
    if isinstance(binary_obj, str):
        value = binary_obj.strip()
        if value:
            return convert_postman_variable_syntax(value)
    if isinstance(binary_obj, dict):
        src = binary_obj.get("src")
        if isinstance(src, str):
            value = src.strip()
            if value:
                return convert_postman_variable_syntax(value)
    return ""


def parse_yaml_scalar(value: str) -> str:
    raw = str(value).strip()
    if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
        return raw[1:-1]
    return raw


def load_postman_environment_values(path: Path) -> Dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}

    values = payload.get("values")
    if not isinstance(values, list):
        return {}

    result: Dict[str, str] = {}
    for entry in values:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "")).strip()
        if not key:
            continue
        if entry.get("enabled", True) is False:
            continue
        raw_value = entry.get("value")
        if raw_value is None:
            continue
        value = str(raw_value)
        if value == "":
            continue
        result[key] = value
    return result


def resolve_soatest_variable_reference(text: str, environment_values: Dict[str, str]) -> str:
    value = str(text or "").strip()
    match = SOATEST_VAR_REFERENCE_RE.fullmatch(value)
    if not match:
        return value
    variable_name = match.group(1).strip()
    resolved = environment_values.get(variable_name)
    return resolved if resolved else value


def ensure_schema_version(content: str, target_schema_version: str = "13") -> str:
    newline = "\r\n" if "\r\n" in content else "\n"
    if SCHEMA_VERSION_RE.search(content):
        return SCHEMA_VERSION_RE.sub(f"schemaVersion: {target_schema_version}", content, count=1)

    # Insert schemaVersion near the header (after productVersion when present).
    insert_after = re.search(r"(?m)^productVersion:\s*.+$", content)
    if not insert_after:
        insert_after = re.search(r"(?m)^parasoftVersion:\s*.+$", content)

    insert_line = f"schemaVersion: {target_schema_version}"
    if insert_after:
        pos = insert_after.end()
        return content[:pos] + newline + insert_line + content[pos:]

    # Fallback: insert after document start marker if present.
    if content.startswith(f"---{newline}"):
        pos = len(f"---{newline}")
        return content[:pos] + insert_line + newline + content[pos:]
    if content.startswith("---"):
        # Unusual newline; insert after first line.
        first_newline = content.find("\n")
        pos = first_newline + 1 if first_newline >= 0 else len(content)
        return content[:pos] + insert_line + newline + content[pos:]

    return insert_line + newline + content


def _parse_schema_version_number(value: str) -> Optional[int]:
    text = parse_yaml_scalar(value).strip()
    if not text:
        return None
    if not re.fullmatch(r"\d+", text):
        return None
    try:
        return int(text, 10)
    except ValueError:
        return None


def ensure_min_schema_version(content: str, minimum_schema_version: str) -> str:
    current_match = SCHEMA_VERSION_VALUE_RE.search(content)
    minimum_number = _parse_schema_version_number(minimum_schema_version)
    if minimum_number is None:
        return ensure_schema_version(content, minimum_schema_version)

    if current_match:
        current_number = _parse_schema_version_number(current_match.group(1))
        if current_number is not None and current_number >= minimum_number:
            return content

    return ensure_schema_version(content, minimum_schema_version)


def extract_auth_credential(raw: Any, key: str) -> str:
    if isinstance(raw, dict):
        value = raw.get(key)
        return convert_postman_variable_syntax(value) if value is not None else ""
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("key", "")).lower() == key.lower():
                return convert_postman_variable_syntax(entry.get("value", ""))
    return ""


def normalize_oauth_grant_type(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text


def map_oauth_grant_type_to_soatest(value: str) -> int:
    normalized = normalize_oauth_grant_type(value)
    return OAUTH_GRANT_TYPE_TO_SOATEST.get(normalized, 2)


def normalize_oauth_add_token_to(value: str, default: str = "header") -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return default
    if "query" in normalized:
        return "queryParams"
    return "header"


def parse_bool_like(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def parse_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_base64_secret(secret: str) -> bytes:
    normalized = str(secret or "").strip()
    if not normalized:
        return b""
    padding = "=" * (-len(normalized) % 4)
    return base64.b64decode(normalized + padding)


def build_jwt_token(
    payload_text: str,
    secret_text: str,
    algorithm: str,
    header_text: str,
    secret_base64_encoded: Optional[bool],
) -> str:
    normalized_algorithm = str(algorithm or "HS256").strip().upper() or "HS256"
    digest_by_alg = {
        "HS256": hashlib.sha256,
        "HS384": hashlib.sha384,
        "HS512": hashlib.sha512,
    }
    digest = digest_by_alg.get(normalized_algorithm)
    if digest is None:
        return ""

    payload = parse_json_object(payload_text)
    if not payload:
        return ""

    header = {"typ": "JWT", "alg": normalized_algorithm}
    header_overrides = parse_json_object(header_text)
    if header_overrides:
        header.update(header_overrides)
        header["alg"] = normalized_algorithm

    try:
        header_segment = _base64url_encode(json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        payload_segment = _base64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        signing_input = f"{header_segment}.{payload_segment}"

        if secret_base64_encoded is True:
            secret_bytes = _decode_base64_secret(secret_text)
        else:
            secret_bytes = str(secret_text or "").encode("utf-8")

        signature = hmac.new(secret_bytes, signing_input.encode("utf-8"), digest).digest()
        signature_segment = _base64url_encode(signature)
    except (ValueError, TypeError):
        return ""

    return f"{signing_input}.{signature_segment}"


def build_jwt_access_token(auth: AuthSpec, environment_values: Dict[str, str]) -> str:
    payload = convert_postman_variable_syntax(auth.jwt_payload)
    secret = convert_postman_variable_syntax(auth.jwt_secret)
    headers = convert_postman_variable_syntax(auth.jwt_headers)
    algorithm = convert_postman_variable_syntax(auth.jwt_algorithm)

    resolved_payload = resolve_soatest_variable_reference(payload, environment_values)
    resolved_secret = resolve_soatest_variable_reference(secret, environment_values)
    resolved_headers = resolve_soatest_variable_reference(headers, environment_values)
    resolved_algorithm = resolve_soatest_variable_reference(algorithm, environment_values)

    token = build_jwt_token(
        payload_text=resolved_payload or payload,
        secret_text=resolved_secret or secret,
        algorithm=resolved_algorithm or algorithm or "HS256",
        header_text=resolved_headers or headers,
        secret_base64_encoded=auth.jwt_secret_base64_encoded,
    )
    return token


def parse_auth_object(auth_obj: Any) -> AuthSpec:
    if not isinstance(auth_obj, dict):
        return AuthSpec(mode="inherit")

    auth_type = str(auth_obj.get("type", "inherit")).strip().lower() or "inherit"
    if auth_type == "inherit":
        return AuthSpec(mode="inherit")
    if auth_type == "noauth":
        return AuthSpec(mode="noauth")
    if auth_type == "digest":
        raw = auth_obj.get("digest")
        return AuthSpec(
            mode="digest",
            username=extract_auth_credential(raw, "username"),
            password=extract_auth_credential(raw, "password"),
        )
    if auth_type == "ntlm":
        raw = auth_obj.get("ntlm")
        return AuthSpec(
            mode="ntlm",
            username=extract_auth_credential(raw, "username"),
            password=extract_auth_credential(raw, "password"),
            domain=extract_auth_credential(raw, "domain"),
            workstation=extract_auth_credential(raw, "workstation"),
        )
    if auth_type == "awsv4":
        raw = auth_obj.get("awsv4")
        add_to_request = extract_auth_credential(raw, "addAuthDataToRequest").strip().lower()
        aws_auth_mode = 1 if "query" in add_to_request else None
        return AuthSpec(
            mode="awsv4",
            access_key_id=extract_auth_credential(raw, "accessKey"),
            secret_access_key=extract_auth_credential(raw, "secretKey"),
            session_token=extract_auth_credential(raw, "sessionToken"),
            service=extract_auth_credential(raw, "service"),
            region=extract_auth_credential(raw, "region"),
            aws_auth_mode=aws_auth_mode,
        )
    if auth_type == "oauth2":
        raw = auth_obj.get("oauth2")
        grant_type = extract_auth_credential(raw, "grant_type") or extract_auth_credential(raw, "grantType")
        return AuthSpec(
            mode="oauth2",
            oauth_token_url=extract_auth_credential(raw, "accessTokenUrl"),
            oauth_client_id=extract_auth_credential(raw, "clientId"),
            oauth_client_secret=extract_auth_credential(raw, "clientSecret"),
            oauth_grant_type=grant_type,
            oauth_add_token_to=normalize_oauth_add_token_to(
                extract_auth_credential(raw, "addTokenTo"),
                default="",
            ),
            oauth_scope=extract_auth_credential(raw, "scope"),
            oauth_audience=extract_auth_credential(raw, "audience"),
            oauth_access_token=extract_auth_credential(raw, "accessToken"),
            oauth_header_prefix=extract_auth_credential(raw, "headerPrefix") or extract_auth_credential(raw, "tokenType"),
        )
    if auth_type == "jwt":
        raw = auth_obj.get("jwt")
        return AuthSpec(
            mode="jwt",
            jwt_payload=extract_auth_credential(raw, "payload"),
            jwt_secret=extract_auth_credential(raw, "secret"),
            jwt_algorithm=extract_auth_credential(raw, "algorithm") or "HS256",
            jwt_add_token_to=normalize_oauth_add_token_to(
                extract_auth_credential(raw, "addTokenTo"),
                default="",
            ),
            jwt_header_prefix=extract_auth_credential(raw, "headerPrefix") or "Bearer",
            jwt_headers=extract_auth_credential(raw, "header"),
            jwt_secret_base64_encoded=parse_bool_like(extract_auth_credential(raw, "isSecretBase64Encoded")),
        )
    if auth_type == "basic":
        return AuthSpec(mode="basic")
    return AuthSpec(mode="unsupported")


def resolve_effective_auth(raw_auth: AuthSpec, parent_effective: AuthSpec) -> AuthSpec:
    if raw_auth.mode == "inherit":
        return parent_effective
    if raw_auth.mode == "oauth2" and parent_effective.mode == "oauth2":
        return AuthSpec(
            mode="oauth2",
            oauth_token_url=raw_auth.oauth_token_url or parent_effective.oauth_token_url,
            oauth_client_id=raw_auth.oauth_client_id or parent_effective.oauth_client_id,
            oauth_client_secret=raw_auth.oauth_client_secret or parent_effective.oauth_client_secret,
            oauth_grant_type=raw_auth.oauth_grant_type or parent_effective.oauth_grant_type,
            oauth_add_token_to=raw_auth.oauth_add_token_to or parent_effective.oauth_add_token_to,
            oauth_scope=raw_auth.oauth_scope or parent_effective.oauth_scope,
            oauth_audience=raw_auth.oauth_audience or parent_effective.oauth_audience,
            oauth_access_token=raw_auth.oauth_access_token or parent_effective.oauth_access_token,
            oauth_header_prefix=raw_auth.oauth_header_prefix or parent_effective.oauth_header_prefix,
        )
    if raw_auth.mode == "jwt" and parent_effective.mode == "jwt":
        return AuthSpec(
            mode="jwt",
            jwt_payload=raw_auth.jwt_payload or parent_effective.jwt_payload,
            jwt_secret=raw_auth.jwt_secret or parent_effective.jwt_secret,
            jwt_algorithm=raw_auth.jwt_algorithm or parent_effective.jwt_algorithm,
            jwt_add_token_to=raw_auth.jwt_add_token_to or parent_effective.jwt_add_token_to,
            jwt_header_prefix=raw_auth.jwt_header_prefix or parent_effective.jwt_header_prefix,
            jwt_headers=raw_auth.jwt_headers or parent_effective.jwt_headers,
            jwt_secret_base64_encoded=(
                raw_auth.jwt_secret_base64_encoded
                if raw_auth.jwt_secret_base64_encoded is not None
                else parent_effective.jwt_secret_base64_encoded
            ),
        )
    return raw_auth


def walk_postman_requests_with_auth(
    items: Any,
    folder_path: Tuple[str, ...],
    parent_effective: AuthSpec,
    targets: List[RequestTarget],
) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name", "")).strip()
        request_obj = item.get("request")
        child_items = item.get("item")
        item_level_auth = parse_auth_object(item.get("auth")) if "auth" in item else AuthSpec(mode="inherit")

        if isinstance(request_obj, dict):
            request_raw_auth = (
                parse_auth_object(request_obj.get("auth"))
                if isinstance(request_obj.get("auth"), dict)
                else item_level_auth
            )
            effective = resolve_effective_auth(request_raw_auth, parent_effective)
            targets.append(
                RequestTarget(
                    folder_path=folder_path,
                    request_name=name or "Unnamed Request",
                    effective_auth=effective,
                )
            )
            continue

        if isinstance(child_items, list):
            folder_effective = resolve_effective_auth(item_level_auth, parent_effective)
            next_folder = folder_path + ((name or "Unnamed Folder"),)
            walk_postman_requests_with_auth(
                items=child_items,
                folder_path=next_folder,
                parent_effective=folder_effective,
                targets=targets,
            )


def walk_postman_binary_body_targets(items: Any, targets: List[BinaryBodyTarget]) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        request_obj = item.get("request")
        child_items = item.get("item")

        if isinstance(request_obj, dict):
            body_obj = request_obj.get("body")
            if isinstance(body_obj, dict):
                mode = str(body_obj.get("mode", "")).strip().lower()
                if mode in {"file", "binary"}:
                    source_reference = extract_postman_binary_source(body_obj)
                    content_type = extract_header_content_type(request_obj) or "application/octet-stream"
                    request_name = str(item.get("name", "Unnamed Request")).strip() or "Unnamed Request"
                    targets.append(
                        BinaryBodyTarget(
                            request_name=request_name,
                            body_mode=mode,
                            source_reference=source_reference,
                            content_type=content_type,
                        )
                    )
            continue

        if isinstance(child_items, list):
            walk_postman_binary_body_targets(child_items, targets)


def resolve_local_tst_path(workspace_root: Path, parent_id: str, target_test_file_name: str) -> Path:
    relative_parts = [part for part in parent_id.strip("/").split("/") if part]
    relative = Path(*relative_parts) if relative_parts else Path()
    return workspace_root / relative / f"{sanitize_filename(target_test_file_name)}.tst"


def build_auth_profile_name(
    mode: str,
    index: int,
    raw_password: str,
    resolved_password: str,
    aws_auth_mode: Optional[int] = None,
    oauth_add_token_to: str = "",
) -> str:
    lowered = f"{raw_password} {resolved_password}".lower()
    if mode == "digest":
        if index == 0:
            return "Digest"
        if "bad" in lowered:
            return "Bad Digest"
        return f"Digest {index + 1}"

    if mode == "ntlm":
        if index == 0:
            return "NTLM"
        if "bad" in lowered:
            return "Bad NTLM"
        return f"NTLM {index + 1}"

    if mode == "awsv4":
        base = "AWS Signature Query" if aws_auth_mode == 1 else "AWS Signature"
        if "bad" in lowered:
            return f"Bad {base}"
        if index == 0:
            return base
        return f"{base} {index + 1}"

    if mode == "oauth2":
        base = "OAuth 2.0 query param" if normalize_oauth_add_token_to(oauth_add_token_to) == "queryParams" else "OAuth 2.0"
        if "bad" in lowered:
            return f"Bad {base}"
        if index == 0:
            return base
        return f"{base} {index + 1}"

    return f"Auth {index + 1}"


def resolve_profile_identity(
    mode: str,
    username: str,
    password: str,
    domain: str,
    workstation: str,
    access_key_id: str,
    secret_access_key: str,
    session_token: str,
    service: str,
    region: str,
    aws_auth_mode: Optional[int],
    oauth_token_url: str,
    oauth_client_id: str,
    oauth_client_secret: str,
    oauth_grant_type: str,
    oauth_add_token_to: str,
    oauth_scope: str,
    oauth_audience: str,
    environment_values: Dict[str, str],
) -> Tuple[str, ...]:
    resolved_username = resolve_soatest_variable_reference(username, environment_values)
    resolved_password = resolve_soatest_variable_reference(password, environment_values)

    if mode == "ntlm":
        resolved_domain = resolve_soatest_variable_reference(domain, environment_values)
        resolved_workstation = resolve_soatest_variable_reference(workstation, environment_values)
        return (
            resolved_username or username,
            resolved_password or password,
            resolved_domain or domain,
            resolved_workstation or workstation,
        )

    if mode == "awsv4":
        resolved_access_key = resolve_soatest_variable_reference(access_key_id, environment_values)
        resolved_secret_key = resolve_soatest_variable_reference(secret_access_key, environment_values)
        resolved_session_token = resolve_soatest_variable_reference(session_token, environment_values)
        resolved_service = resolve_soatest_variable_reference(service, environment_values)
        resolved_region = resolve_soatest_variable_reference(region, environment_values)
        return (
            resolved_access_key or access_key_id,
            resolved_secret_key or secret_access_key,
            resolved_session_token or session_token,
            resolved_service or service,
            resolved_region or region,
            str(aws_auth_mode if aws_auth_mode is not None else ""),
        )

    if mode == "oauth2":
        resolved_token_url = resolve_soatest_variable_reference(oauth_token_url, environment_values)
        resolved_client_id = resolve_soatest_variable_reference(oauth_client_id, environment_values)
        resolved_client_secret = resolve_soatest_variable_reference(oauth_client_secret, environment_values)
        resolved_scope = resolve_soatest_variable_reference(oauth_scope, environment_values)
        resolved_audience = resolve_soatest_variable_reference(oauth_audience, environment_values)
        return (
            resolved_token_url or oauth_token_url,
            resolved_client_id or oauth_client_id,
            resolved_client_secret or oauth_client_secret,
            normalize_oauth_grant_type(oauth_grant_type) or "client_credentials",
            normalize_oauth_add_token_to(oauth_add_token_to),
            resolved_scope or oauth_scope,
            resolved_audience or oauth_audience,
        )

    return (
        resolved_username or username,
        resolved_password or password,
    )


def build_expectations(
    targets: Sequence[RequestTarget],
    environment_values: Dict[str, str],
) -> Tuple[List[RequestExpectation], List[AuthProfile]]:
    expectations: List[RequestExpectation] = []
    auth_profiles: List[AuthProfile] = []
    profile_key_to_name: Dict[Tuple[str, ...], str] = {}
    used_profile_names: set[str] = set()
    mode_profile_counts: Dict[str, int] = {"digest": 0, "ntlm": 0, "awsv4": 0, "oauth2": 0}

    for target in targets:
        mode = target.effective_auth.mode
        if mode in {"digest", "ntlm", "awsv4", "oauth2"}:
            raw_username = convert_postman_variable_syntax(target.effective_auth.username)
            raw_password = convert_postman_variable_syntax(target.effective_auth.password)
            raw_domain = convert_postman_variable_syntax(target.effective_auth.domain)
            raw_workstation = convert_postman_variable_syntax(target.effective_auth.workstation)
            raw_access_key_id = convert_postman_variable_syntax(target.effective_auth.access_key_id)
            raw_secret_access_key = convert_postman_variable_syntax(target.effective_auth.secret_access_key)
            raw_session_token = convert_postman_variable_syntax(target.effective_auth.session_token)
            raw_service = convert_postman_variable_syntax(target.effective_auth.service)
            raw_region = convert_postman_variable_syntax(target.effective_auth.region)
            raw_oauth_token_url = convert_postman_variable_syntax(target.effective_auth.oauth_token_url)
            raw_oauth_client_id = convert_postman_variable_syntax(target.effective_auth.oauth_client_id)
            raw_oauth_client_secret = convert_postman_variable_syntax(target.effective_auth.oauth_client_secret)
            raw_oauth_grant_type = normalize_oauth_grant_type(target.effective_auth.oauth_grant_type)
            raw_oauth_add_token_to = normalize_oauth_add_token_to(target.effective_auth.oauth_add_token_to)
            raw_oauth_scope = convert_postman_variable_syntax(target.effective_auth.oauth_scope)
            raw_oauth_audience = convert_postman_variable_syntax(target.effective_auth.oauth_audience)
            raw_oauth_access_token = convert_postman_variable_syntax(target.effective_auth.oauth_access_token)
            raw_oauth_header_prefix = convert_postman_variable_syntax(target.effective_auth.oauth_header_prefix)
            identity_values = resolve_profile_identity(
                mode=mode,
                username=raw_username,
                password=raw_password,
                domain=raw_domain,
                workstation=raw_workstation,
                access_key_id=raw_access_key_id,
                secret_access_key=raw_secret_access_key,
                session_token=raw_session_token,
                service=raw_service,
                region=raw_region,
                aws_auth_mode=target.effective_auth.aws_auth_mode,
                oauth_token_url=raw_oauth_token_url,
                oauth_client_id=raw_oauth_client_id,
                oauth_client_secret=raw_oauth_client_secret,
                oauth_grant_type=raw_oauth_grant_type,
                oauth_add_token_to=raw_oauth_add_token_to,
                oauth_scope=raw_oauth_scope,
                oauth_audience=raw_oauth_audience,
                environment_values=environment_values,
            )
            identity_key = (mode, *identity_values)

            profile_name = profile_key_to_name.get(identity_key)
            if profile_name is None:
                candidate = build_auth_profile_name(
                    mode=mode,
                    index=mode_profile_counts.get(mode, 0),
                    raw_password=raw_password or raw_secret_access_key or raw_oauth_client_secret,
                    resolved_password=(
                        identity_values[2]
                        if mode == "oauth2" and len(identity_values) > 2
                        else (identity_values[1] if len(identity_values) > 1 else "")
                    ),
                    aws_auth_mode=target.effective_auth.aws_auth_mode,
                    oauth_add_token_to=raw_oauth_add_token_to,
                )
                profile_name = candidate
                if profile_name in used_profile_names:
                    suffix = 2
                    while f"{candidate} {suffix}" in used_profile_names:
                        suffix += 1
                    profile_name = f"{candidate} {suffix}"

                profile_key_to_name[identity_key] = profile_name
                used_profile_names.add(profile_name)
                mode_profile_counts[mode] = mode_profile_counts.get(mode, 0) + 1

                auth_profiles.append(
                    AuthProfile(
                        mode=mode,
                        name=profile_name,
                        username=raw_username or identity_values[0],
                        password=raw_password or identity_values[1],
                        domain=(raw_domain or identity_values[2]) if mode == "ntlm" else "",
                        workstation=(raw_workstation or identity_values[3]) if mode == "ntlm" else "",
                        access_key_id=(raw_access_key_id or identity_values[0]) if mode == "awsv4" else "",
                        secret_access_key=(raw_secret_access_key or identity_values[1]) if mode == "awsv4" else "",
                        session_token=(raw_session_token or identity_values[2]) if mode == "awsv4" else "",
                        service=(raw_service or identity_values[3]) if mode == "awsv4" else "",
                        region=(raw_region or identity_values[4]) if mode == "awsv4" else "",
                        aws_auth_mode=target.effective_auth.aws_auth_mode if mode == "awsv4" else None,
                        oauth_token_url=(raw_oauth_token_url or identity_values[0]) if mode == "oauth2" else "",
                        oauth_client_id=(raw_oauth_client_id or identity_values[1]) if mode == "oauth2" else "",
                        oauth_client_secret=(raw_oauth_client_secret or identity_values[2]) if mode == "oauth2" else "",
                        oauth_grant_type=(raw_oauth_grant_type or identity_values[3]) if mode == "oauth2" else "",
                        oauth_add_token_to=(raw_oauth_add_token_to or identity_values[4]) if mode == "oauth2" else "",
                        oauth_scope=(raw_oauth_scope or identity_values[5]) if mode == "oauth2" else "",
                        oauth_audience=(raw_oauth_audience or identity_values[6]) if mode == "oauth2" else "",
                    )
                )

            expectations.append(
                RequestExpectation(
                    request_name=target.request_name,
                    mode=mode,
                    auth_profile_name=profile_name,
                    oauth_access_token=raw_oauth_access_token if mode == "oauth2" else "",
                    oauth_add_token_to=raw_oauth_add_token_to if mode == "oauth2" else "",
                    oauth_header_prefix=raw_oauth_header_prefix if mode == "oauth2" else "",
                )
            )
        elif mode == "jwt":
            raw_jwt_add_token_to = normalize_oauth_add_token_to(target.effective_auth.jwt_add_token_to)
            raw_jwt_header_prefix = convert_postman_variable_syntax(target.effective_auth.jwt_header_prefix) or "Bearer"
            generated_token = build_jwt_access_token(target.effective_auth, environment_values)
            expectations.append(
                RequestExpectation(
                    request_name=target.request_name,
                    mode="jwt",
                    jwt_access_token=generated_token,
                    jwt_add_token_to=raw_jwt_add_token_to,
                    jwt_header_prefix=raw_jwt_header_prefix,
                )
            )
        elif mode == "noauth":
            expectations.append(RequestExpectation(request_name=target.request_name, mode="noauth"))

    return expectations, auth_profiles


def _normalize_bearer_value(prefix: str, token: str) -> str:
    raw_prefix = str(prefix or "").strip()
    raw_token = str(token or "").strip()
    if not raw_token:
        return ""
    if not raw_prefix:
        raw_prefix = "Bearer"
    if raw_token.lower().startswith(raw_prefix.lower() + " "):
        return raw_token
    return f"{raw_prefix} {raw_token}"


def patch_oauth2_access_token_override_in_common_body(
    common_body: str,
    indent: str,
    access_token: str,
    add_token_to: str,
    header_prefix: str,
) -> Tuple[str, bool]:
    if not access_token:
        return common_body, False

    normalized_add_to = normalize_oauth_add_token_to(add_token_to)
    if normalized_add_to != "header":
        return common_body, False

    bearer_value = _normalize_bearer_value(header_prefix, access_token)
    if not bearer_value:
        return common_body, False

    key_indent = f"{indent}  "
    child_indent = f"{indent}    "

    lines = common_body.splitlines(keepends=True)
    header_line_index = None
    for idx, line in enumerate(lines):
        if re.match(rf"^{re.escape(key_indent)}httpHeaders:\s*(?:#.*)?\r?$", line.rstrip("\n")):
            header_line_index = idx
            break

    header_entry = [
        f"{child_indent}properties:\n",
        f"{child_indent}- name: Authorization\n",
        f"{child_indent}  value:\n",
        f"{child_indent}    values:\n",
        f"{child_indent}    - $type: ScriptedValue\n",
        f"{child_indent}    fixedValue:\n",
        f"{child_indent}      $type: StringTestValue\n",
        f"{child_indent}      value: {bearer_value}\n",
    ]

    if header_line_index is None:
        if not common_body.endswith("\n"):
            common_body += "\n"
        updated = common_body + f"{key_indent}httpHeaders:\n" + "".join(header_entry)
        return updated, True

    block_start = header_line_index + 1
    block_end = block_start
    while block_end < len(lines):
        line = lines[block_end]
        if line.startswith(child_indent):
            block_end += 1
            continue
        break

    existing_block = lines[block_start:block_end]

    # If there is already a properties list, drop any existing Authorization entry.
    if any(re.match(rf"^{re.escape(child_indent)}properties:\s*$", ln.rstrip("\n")) for ln in existing_block):
        rebuilt: List[str] = []
        in_properties = False
        items: List[List[str]] = []
        current: List[str] = []

        for ln in existing_block:
            if re.match(rf"^{re.escape(child_indent)}properties:\s*$", ln.rstrip("\n")):
                in_properties = True
                rebuilt.append(ln)
                continue

            if not in_properties:
                continue

            if re.match(rf"^{re.escape(child_indent)}- name:\s*", ln):
                if current:
                    items.append(current)
                current = [ln]
                continue

            if current:
                current.append(ln)

        if current:
            items.append(current)

        kept_items: List[List[str]] = []
        for item in items:
            name_line = item[0]
            name_match = re.match(rf"^{re.escape(child_indent)}- name:\s*(.+?)\s*$", name_line.rstrip("\n"))
            if name_match and parse_yaml_scalar(name_match.group(1)) == "Authorization":
                continue
            kept_items.append(item)

        for item in kept_items:
            rebuilt.extend(item)

        # Append/replace Authorization at end.
        rebuilt.extend(header_entry[1:])  # skip 'properties:' because we kept it.
        updated_lines = lines[:block_start] + rebuilt + lines[block_end:]
        return "".join(updated_lines), True

    # Replace mode/empty header config with a concrete Authorization property.
    updated_lines = lines[:block_start] + header_entry + lines[block_end:]
    return "".join(updated_lines), True


def ensure_auth_http11_transport(block: str) -> Tuple[str, bool]:
    """Ensure digest/ntlm-auth REST clients use HTTP 1.1 manager protocol metadata."""
    pattern = re.compile(
        r"(?m)^(?P<indent>\s*)transportProperties:\n"
        r"(?P=indent)  manager:\n"
    )
    match = pattern.search(block)
    if not match:
        return block, False

    manager_indent = match.group("indent")
    manager_start = match.end()
    manager_rest = block[manager_start:]
    manager_end_match = re.search(
        rf"(?m)^{re.escape(manager_indent)}  (?!properties:)[A-Za-z_][A-Za-z0-9_-]*:",
        manager_rest,
    )
    manager_end = manager_start + manager_end_match.start() if manager_end_match else len(block)
    manager_body = block[manager_start:manager_end]

    if re.search(r"(?m)^" + re.escape(manager_indent) + r"    protocol:\s*1\s*$", manager_body):
        return block, False

    updated = block[:manager_start] + f"{manager_indent}    protocol: 1\n" + block[manager_start:]
    return updated, True


def render_auth_profile(profile: AuthProfile) -> str:
    if profile.mode == "digest":
        block = DIGEST_PROFILE_TEMPLATE
        block = block.replace("${AUTH_PROFILE_NAME}", profile.name)
        block = block.replace("${AUTH_USERNAME}", profile.username)
        block = block.replace("${AUTH_PASSWORD}", profile.password)
        return block

    if profile.mode == "ntlm":
        block = NTLM_PROFILE_TEMPLATE
        block = block.replace("${AUTH_PROFILE_NAME}", profile.name)
        block = block.replace("${AUTH_USERNAME}", profile.username)
        block = block.replace("${AUTH_PASSWORD}", profile.password)
        domain_block = ""
        workstation_block = ""
        if profile.domain:
            domain_block = (
                "    domain:\n"
                "      fixedValue:\n"
                "        $type: StringTestValue\n"
                f"        domain: \"{profile.domain}\"\n"
            )
        if profile.workstation:
            workstation_block = (
                "    workstation:\n"
                "      fixedValue:\n"
                "        $type: StringTestValue\n"
                f"        workstation: \"{profile.workstation}\"\n"
            )
        block = block.replace("${AUTH_NTLM_DOMAIN_BLOCK}", domain_block)
        block = block.replace("${AUTH_NTLM_WORKSTATION_BLOCK}", workstation_block)
        return block

    if profile.mode == "awsv4":
        block = AWS_SIGNATURE_PROFILE_TEMPLATE
        block = block.replace("${AUTH_PROFILE_NAME}", profile.name)
        block = block.replace("${AUTH_ACCESS_KEY_ID}", profile.access_key_id)
        block = block.replace("${AUTH_SECRET_ACCESS_KEY}", profile.secret_access_key)

        auth_mode_block = ""
        if profile.aws_auth_mode is not None:
            auth_mode_block = f"      authMode: {int(profile.aws_auth_mode)}\n"

        session_token_block = ""
        if profile.session_token:
            session_token_block = (
                "      sessionToken:\n"
                "        values:\n"
                "        - $type: ScriptedValue\n"
                "        fixedValue:\n"
                "          $type: StringTestValue\n"
                f"          value: \"{profile.session_token}\"\n"
            )

        service_block = ""
        if profile.service:
            service_block = (
                "      service:\n"
                "        values:\n"
                "        - $type: ScriptedValue\n"
                "        fixedValue:\n"
                "          $type: StringTestValue\n"
                f"          value: \"{profile.service}\"\n"
            )

        region_block = ""
        if profile.region:
            region_block = (
                "      region:\n"
                "        values:\n"
                "        - $type: ScriptedValue\n"
                "        fixedValue:\n"
                "          $type: StringTestValue\n"
                f"          value: \"{profile.region}\"\n"
            )

        block = block.replace("${AUTH_AWS_AUTH_MODE_BLOCK}", auth_mode_block)
        block = block.replace("${AUTH_AWS_SESSION_TOKEN_BLOCK}", session_token_block)
        block = block.replace("${AUTH_AWS_SERVICE_BLOCK}", service_block)
        block = block.replace("${AUTH_AWS_REGION_BLOCK}", region_block)
        return block

    if profile.mode == "oauth2":
        block = OAUTH2_PROFILE_TEMPLATE
        block = block.replace("${AUTH_PROFILE_NAME}", profile.name)
        block = block.replace("${AUTH_OAUTH_GRANT_TYPE}", str(map_oauth_grant_type_to_soatest(profile.oauth_grant_type)))
        block = block.replace("${AUTH_OAUTH_TOKEN_URI}", profile.oauth_token_url)
        block = block.replace("${AUTH_OAUTH_CLIENT_ID}", profile.oauth_client_id)
        block = block.replace("${AUTH_OAUTH_CLIENT_SECRET}", profile.oauth_client_secret)

        scope_block = ""
        if profile.oauth_scope:
            scope_block = (
                "      scope:\n"
                "        values:\n"
                "        - $type: ScriptedValue\n"
                "        fixedValue:\n"
                "          $type: StringTestValue\n"
                f"          scope: \"{profile.oauth_scope}\"\n"
            )

        audience_block = ""
        if profile.oauth_audience:
            audience_block = (
                "      audience:\n"
                "        values:\n"
                "        - $type: ScriptedValue\n"
                "        fixedValue:\n"
                "          $type: StringTestValue\n"
                f"          audience: \"{profile.oauth_audience}\"\n"
            )

        access_token_block = ""
        if normalize_oauth_add_token_to(profile.oauth_add_token_to) == "queryParams":
            access_token_block = "      accessToken: 1\n"

        block = block.replace("${AUTH_OAUTH_SCOPE_BLOCK}", scope_block)
        block = block.replace("${AUTH_OAUTH_AUDIENCE_BLOCK}", audience_block)
        block = block.replace("${AUTH_OAUTH_ACCESS_TOKEN_BLOCK}", access_token_block)
        return block

    return ""


def ensure_auth_profiles(content: str, auth_profiles: Sequence[AuthProfile]) -> Tuple[str, Dict[str, int], List[str]]:
    if not auth_profiles:
        return content, {"digest": 0, "ntlm": 0, "awsv4": 0, "oauth2": 0}, []

    warnings: List[str] = []
    marker = "  authentications:\n"
    required_by_key = {(profile.mode, profile.name): profile for profile in auth_profiles}
    added_by_mode: Dict[str, int] = {"digest": 0, "ntlm": 0, "awsv4": 0, "oauth2": 0}
    type_to_mode = {
        "DigestAuthentication": "digest",
        "NTLMAuthentication": "ntlm",
        "AwsSignatureAuthentication": "awsv4",
        "OAuth2Authentication": "oauth2",
    }
    mode_to_type = {
        "digest": "DigestAuthentication",
        "ntlm": "NTLMAuthentication",
        "awsv4": "AwsSignatureAuthentication",
        "oauth2": "OAuth2Authentication",
    }

    marker_index = content.find(marker)
    if marker_index < 0:
        tests_match = re.search(r"(?m)^  tests:\n", content)
        if not tests_match:
            warnings.append("Could not locate suite tests block; auth profile insertion skipped.")
            return content, added_by_mode, warnings

        insertion = marker + "".join(render_auth_profile(profile) for profile in auth_profiles)
        insert_at = tests_match.start()
        for profile in auth_profiles:
            added_by_mode[profile.mode] = added_by_mode.get(profile.mode, 0) + 1
        return content[:insert_at] + insertion + content[insert_at:], added_by_mode, warnings

    block_start = marker_index + len(marker)
    tail = content[block_start:]
    boundary_match = re.search(r"(?m)^  (?!- )[A-Za-z_][A-Za-z0-9_-]*:", tail)
    block_end = block_start + boundary_match.start() if boundary_match else len(content)

    auth_body = content[block_start:block_end]
    entry_matches = list(re.finditer(r"(?m)^  - \$type: [^\n]+\n", auth_body))

    if not entry_matches:
        new_body = "".join(render_auth_profile(profile) for profile in auth_profiles)
        updated = content[:block_start] + new_body + content[block_end:]
        for profile in auth_profiles:
            added_by_mode[profile.mode] = added_by_mode.get(profile.mode, 0) + 1
        return updated, added_by_mode, warnings

    prefix = auth_body[: entry_matches[0].start()]
    entries: List[str] = []
    for idx, match in enumerate(entry_matches):
        start = match.start()
        end = entry_matches[idx + 1].start() if idx + 1 < len(entry_matches) else len(auth_body)
        entries.append(auth_body[start:end])

    seen_required_keys: set[Tuple[str, str]] = set()
    rebuilt_entries: List[str] = []
    for entry in entries:
        type_match = re.search(r"(?m)^  - \$type:\s*([^\n]+)\s*$", entry)
        entry_type = parse_yaml_scalar(type_match.group(1)) if type_match else ""
        entry_mode = type_to_mode.get(entry_type)
        if entry_mode in {"digest", "ntlm", "awsv4", "oauth2"}:
            name_match = re.search(r"(?m)^    name:\s*(.+)\s*$", entry)
            if name_match:
                profile_name = parse_yaml_scalar(name_match.group(1))
                required = required_by_key.get((entry_mode, profile_name))
                if required is not None:
                    rendered = render_auth_profile(required)
                    if rendered:
                        rebuilt_entries.append(rendered)
                        seen_required_keys.add((entry_mode, profile_name))
                        continue
        rebuilt_entries.append(entry)

    for profile in auth_profiles:
        profile_key = (profile.mode, profile.name)
        if profile_key in seen_required_keys:
            continue
        rendered = render_auth_profile(profile)
        if not rendered:
            warnings.append(
                f"Unsupported auth profile mode '{profile.mode}' for profile '{profile.name}'; skipped."
            )
            continue
        # If a profile exists with the same name but different type, keep existing entry and append the new one.
        if profile.mode not in mode_to_type:
            continue
        rebuilt_entries.append(rendered)
        added_by_mode[profile.mode] = added_by_mode.get(profile.mode, 0) + 1

    new_body = prefix + "".join(rebuilt_entries)
    updated = content[:block_start] + new_body + content[block_end:]
    return updated, added_by_mode, warnings


def patch_auth_in_rest_client_block(block: str, expectation: RequestExpectation) -> Tuple[str, bool, bool]:
    common_block_pattern = re.compile(
        r"(?m)^(?P<indent>\s*)common:\n"
        r"(?P<body>(?:(?P=indent)  (?!common:).*(?:\n|$))*)"
    )

    changed = False
    matched = False

    def build_auth_section(indent: str) -> str:
        if expectation.mode in {"digest", "ntlm", "awsv4", "oauth2"}:
            return (
                f"{indent}  auth:\n"
                f"{indent}    useDefault: false\n"
                f"{indent}    authName: {expectation.auth_profile_name}\n"
            )
        return (
            f"{indent}  auth:\n"
            f"{indent}    useDefault: false\n"
            f"{indent}    customType: 1\n"
        )

    def repl(match: re.Match[str]) -> str:
        nonlocal changed, matched
        matched = True

        indent = match.group("indent")
        body = match.group("body")
        method_marker = f"{indent}  method:\n"
        method_pos = body.find(method_marker)
        if method_pos < 0:
            return match.group(0)

        # Remove any existing auth subsection before re-inserting canonical auth.
        body_without_auth = re.sub(
            rf"(?m)^{re.escape(indent)}  auth:\n(?:{re.escape(indent)}    .*\n)*",
            "",
            body,
        )

        method_pos = body_without_auth.find(method_marker)
        if method_pos < 0:
            return match.group(0)

        auth_section = build_auth_section(indent)
        rebuilt_body = body_without_auth[:method_pos] + auth_section + body_without_auth[method_pos:]

        if expectation.mode in {"oauth2", "jwt"}:
            access_token = expectation.oauth_access_token if expectation.mode == "oauth2" else expectation.jwt_access_token
            add_token_to = expectation.oauth_add_token_to if expectation.mode == "oauth2" else expectation.jwt_add_token_to
            header_prefix = expectation.oauth_header_prefix if expectation.mode == "oauth2" else expectation.jwt_header_prefix
            rebuilt_body, header_changed = patch_oauth2_access_token_override_in_common_body(
                rebuilt_body,
                indent=indent,
                access_token=access_token,
                add_token_to=add_token_to,
                header_prefix=header_prefix,
            )
            changed = changed or header_changed

        rebuilt = f"{indent}common:\n{rebuilt_body}"
        if rebuilt != match.group(0):
            changed = True
        return rebuilt

    updated = common_block_pattern.sub(repl, block)
    if expectation.mode in {"digest", "ntlm"}:
        updated, transport_changed = ensure_auth_http11_transport(updated)
        changed = changed or transport_changed
    return updated, changed, matched


def patch_rest_clients(content: str, expectations: Sequence[RequestExpectation]) -> Tuple[str, int, int, int, List[str]]:
    warnings: List[str] = []

    queues: Dict[str, List[RequestExpectation]] = {}
    for expectation in expectations:
        queues.setdefault(expectation.request_name, []).append(expectation)

    starts = [m.start() for m in re.finditer(r"(?m)^\s*-\s+\$type:\s+RESTClientToolTest\s*$", content)]
    if not starts:
        return content, 0, 0, len(expectations), ["No RESTClientToolTest blocks found in target .tst."]

    patched_count = 0
    already_matching_count = 0
    missing_count = 0

    parts: List[str] = []
    last_index = 0

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(content)
        parts.append(content[last_index:start])
        block = content[start:end]
        last_index = end

        # Prefer the RESTClient tool name (request name) over test case display name.
        name_match = re.search(
            r"(?m)^\s+iconName:\s*RESTClient\s*$\n^\s+name:\s*(.+)\s*$",
            block,
        )
        if not name_match:
            name_match = re.search(r"(?m)^\s+name:\s*(.+)\s*$", block)
        if not name_match:
            parts.append(block)
            continue

        request_name = parse_yaml_scalar(name_match.group(1))
        queue = queues.get(request_name)
        if not queue:
            parts.append(block)
            continue

        expectation = queue.pop(0)
        patched_block, changed, matched_common = patch_auth_in_rest_client_block(block, expectation)
        if not matched_common:
            warnings.append(
                f"Could not locate transport common/method auth anchor for request '{request_name}'."
            )
            parts.append(block)
            continue

        if changed:
            patched_count += 1
        else:
            already_matching_count += 1
        parts.append(patched_block)

    parts.append(content[last_index:])
    updated_content = "".join(parts)

    for queue in queues.values():
        missing_count += len(queue)

    return updated_content, patched_count, already_matching_count, missing_count, warnings


@dataclass(frozen=True)
class BinaryBodyTemplate:
    request_name: str
    literal_block: str


def _find_yaml_mapping_block_at_indent(text: str, key_name: str, indent: str) -> Optional[Tuple[int, int, str]]:
    key_match = re.search(rf"(?m)^{re.escape(indent)}{re.escape(key_name)}:\s*$", text)
    if not key_match:
        return None

    start = key_match.start()
    end = len(text)
    indent_len = len(indent)
    boundary_re = re.compile(r"(?m)^(?P<indent>[ \t]*)\S")
    for match in boundary_re.finditer(text, key_match.end()):
        if len(match.group("indent")) <= indent_len:
            end = match.start()
            break
    return start, end, text[start:end]


def _extract_rest_client_literal_block(rest_client_block: str) -> Optional[Tuple[str, str]]:
    tool_indent = _find_rest_client_tool_indent(rest_client_block)
    if tool_indent is None:
        return None
    literal_match = _find_yaml_mapping_block_at_indent(rest_client_block, "literal", tool_indent)
    if literal_match is None:
        return None
    _, _, literal_block = literal_match
    return tool_indent, literal_block


def load_binary_body_templates(
    template_content: str,
) -> Tuple[Dict[str, BinaryBodyTemplate], Optional[BinaryBodyTemplate], List[str]]:
    templates: Dict[str, BinaryBodyTemplate] = {}
    default_template: Optional[BinaryBodyTemplate] = None
    warnings: List[str] = []

    starts = [m.start() for m in re.finditer(r"(?m)^\s*-\s+\$type:\s+RESTClientToolTest\s*$", template_content)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(template_content)
        block = template_content[start:end]

        name_match = re.search(
            r"(?m)^\s+iconName:\s*RESTClient\s*$\n^\s+name:\s*(.+)\s*$",
            block,
        )
        if not name_match:
            name_match = re.search(r"(?m)^\s+name:\s*(.+)\s*$", block)
        if not name_match:
            continue

        request_name = parse_yaml_scalar(name_match.group(1))
        extracted = _extract_rest_client_literal_block(block)
        if extracted is None:
            warnings.append(
                f"Binary template request '{request_name}' skipped: could not locate RESTClient literal block."
            )
            continue

        _, literal_block = extracted
        if re.search(r"(?m)^\s*file:\s*$", literal_block) is None:
            warnings.append(
                f"Binary template request '{request_name}' skipped: literal block does not contain a file attachment."
            )
            continue

        entry = BinaryBodyTemplate(
            request_name=request_name,
            literal_block=literal_block.rstrip() + "\n",
        )
        templates[request_name] = entry
        if default_template is None:
            default_template = entry

    return templates, default_template, warnings


def _normalize_binary_source_reference(source_reference: str) -> str:
    value = str(source_reference or "").strip()
    if not value:
        return ""
    if value.startswith("/") and re.match(r"^/[A-Za-z]:[\/]", value):
        value = value[1:]
    return value


def _yaml_double_quote(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _apply_binary_target_to_literal_block(literal_block: str, target: BinaryBodyTarget) -> str:
    updated = literal_block
    source_value = _normalize_binary_source_reference(target.source_reference)
    if source_value:
        file_path_pattern = re.compile(
            r"(?ms)(^\s*file:\s*$.*?^\s*location:\s*$\n)(?P<indent>\s*)path:\s*.+$"
        )
        match = file_path_pattern.search(updated)
        if match:
            replacement = f"{match.group(1)}{match.group('indent')}path: {_yaml_double_quote(source_value)}\n"
            updated = updated[: match.start()] + replacement + updated[match.end() :]

    content_type = str(target.content_type or "").strip()
    if content_type:
        type_match = re.search(r"(?m)^(?P<indent>\s*)type:\s*.+$", updated)
        if type_match:
            updated = (
                updated[: type_match.start()]
                + f"{type_match.group('indent')}type: {content_type}"
                + updated[type_match.end() :]
            )

    return updated


def patch_binary_request_bodies(
    content: str,
    binary_targets: Sequence[BinaryBodyTarget],
    templates: Dict[str, BinaryBodyTemplate],
    default_template: Optional[BinaryBodyTemplate],
) -> Tuple[str, int, int, int, int, List[str]]:
    warnings: List[str] = []
    if not binary_targets:
        return content, 0, 0, 0, 0, warnings

    starts = [m.start() for m in re.finditer(r"(?m)^\s*-\s+\$type:\s+RESTClientToolTest\s*$", content)]
    if not starts:
        return (
            content,
            0,
            0,
            len(binary_targets),
            len({target.request_name for target in binary_targets}),
            ["No RESTClientToolTest blocks found in target .tst."],
        )

    queues: Dict[str, List[BinaryBodyTarget]] = {}
    for target in binary_targets:
        queues.setdefault(target.request_name, []).append(target)

    patched_count = 0
    already_matching_count = 0
    template_missing_count = 0

    parts: List[str] = []
    last_index = 0

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(content)
        parts.append(content[last_index:start])
        block = content[start:end]
        last_index = end

        name_match = re.search(
            r"(?m)^\s+iconName:\s*RESTClient\s*$\n^\s+name:\s*(.+)\s*$",
            block,
        )
        if not name_match:
            name_match = re.search(r"(?m)^\s+name:\s*(.+)\s*$", block)
        if not name_match:
            parts.append(block)
            continue

        request_name = parse_yaml_scalar(name_match.group(1))
        queue = queues.get(request_name)
        if not queue:
            parts.append(block)
            continue

        target = queue.pop(0)
        template = templates.get(request_name)
        used_default_template = False
        if template is None:
            template = default_template
            used_default_template = template is not None
        if template is None:
            template_missing_count += 1
            source_display = target.source_reference or "(not exported by Postman)"
            warnings.append(
                f"No binary-body template entry found for request '{request_name}' (source: {source_display})."
            )
            parts.append(block)
            continue

        extracted = _extract_rest_client_literal_block(block)
        if extracted is None:
            warnings.append(
                f"Could not locate RESTClient literal block for request '{request_name}' in target .tst."
            )
            parts.append(block)
            continue

        tool_indent, _ = extracted
        target_literal_block = _reindent_block(_apply_binary_target_to_literal_block(template.literal_block, target), tool_indent)
        literal_match = _find_yaml_mapping_block_at_indent(block, "literal", tool_indent)
        if used_default_template:
            warnings.append(
                f"Binary fallback for request '{request_name}' used built-in default template entry '{template.request_name}'."
            )

        if literal_match is None:
            mode_anchor = re.search(rf"(?m)^{re.escape(tool_indent)}mode:\s*$", block)
            insert_at = mode_anchor.start() if mode_anchor else len(block)
            patched_block = block[:insert_at] + target_literal_block + block[insert_at:]
            patched_count += 1
            parts.append(patched_block)
            continue

        literal_start, literal_end, current_literal_block = literal_match
        if current_literal_block == target_literal_block:
            already_matching_count += 1
            parts.append(block)
            continue

        patched_block = block[:literal_start] + target_literal_block + block[literal_end:]
        patched_count += 1
        parts.append(patched_block)

    parts.append(content[last_index:])
    updated_content = "".join(parts)

    missing_count = 0
    for queue in queues.values():
        missing_count += len(queue)

    return (
        updated_content,
        patched_count,
        already_matching_count,
        missing_count,
        template_missing_count,
        warnings,
    )


@dataclass(frozen=True)
class DifferenceAssertionTemplate:
    request_name: str
    assertion_items: Tuple[str, ...]
    assertion_names: Tuple[str, ...]
    generic_assertion_tool_item: str
    http_client_endpoint_value: Optional[str] = None
    url_parameters_block: Optional[str] = None


def _reindent_block(block: str, indent: str) -> str:
    lines = block.splitlines(keepends=True)
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return block
    min_indent = min(len(re.match(r"^\s*", line).group(0)) for line in non_empty)
    rebuilt: List[str] = []
    for line in lines:
        if not line.strip():
            rebuilt.append(line)
            continue
        rebuilt.append(indent + line[min_indent:])
    return "".join(rebuilt)


def _extract_yaml_list_item_block(text: str, start_index: int, item_indent: str) -> str:
    """
    Return the YAML block for a single list item starting at start_index.

    The list item is considered complete when we hit the next non-empty line
    that is indented at the same or lower level than the item's "-" line.
    """
    if start_index < 0:
        return ""

    indent_len = len(item_indent)
    boundary_re = re.compile(r"(?m)^(?P<indent>\s*)\S")
    for match in boundary_re.finditer(text, start_index + 1):
        other_indent = match.group("indent")
        if len(other_indent) <= indent_len:
            return text[start_index : match.start()]
    return text[start_index:]


def _find_yaml_mapping_block(text: str, key_name: str) -> Optional[Tuple[int, int, str, str]]:
    key_match = re.search(rf"(?m)^(?P<indent>[ \t]*){re.escape(key_name)}:\s*$", text)
    if not key_match:
        return None

    start = key_match.start()
    end = len(text)
    indent = key_match.group("indent")
    indent_len = len(indent)
    boundary_re = re.compile(r"(?m)^(?P<indent>[ \t]*)\S")
    for match in boundary_re.finditer(text, key_match.end()):
        if len(match.group("indent")) <= indent_len:
            end = match.start()
            break
    return start, end, indent, text[start:end]


def _extract_http_client_endpoint_value(rest_client_block: str) -> Optional[str]:
    endpoint_match = re.search(r"(?m)^[ \t]*HTTPClient_Endpoint:\s*(.+)\s*$", rest_client_block)
    if not endpoint_match:
        return None
    return endpoint_match.group(1).strip()


def _apply_template_endpoint_and_query(
    block: str,
    template: DifferenceAssertionTemplate,
    tool_indent: str,
) -> Tuple[str, bool]:
    changed = False

    endpoint_value = template.http_client_endpoint_value
    if endpoint_value:
        endpoint_match = re.search(r"(?m)^(?P<indent>[ \t]*)HTTPClient_Endpoint:\s*(?P<value>.+)\s*$", block)
        if endpoint_match:
            current = endpoint_match.group("value").strip()
            if current != endpoint_value:
                block = block[: endpoint_match.start("value")] + endpoint_value + block[endpoint_match.end("value") :]
                changed = True

    if template.url_parameters_block:
        target_url_parameters = _reindent_block(template.url_parameters_block, tool_indent)
        existing = _find_yaml_mapping_block(block, "urlParameters")
        if existing:
            existing_start, existing_end, _, existing_block = existing
            if existing_block != target_url_parameters:
                block = block[:existing_start] + target_url_parameters + block[existing_end:]
                changed = True
        else:
            tool_indent_len = len(tool_indent)
            anchor = re.search(
                rf"(?m)^\s{{{tool_indent_len}}}(transportProperties|outputProviders|literal|mode|literalQuery|literalPath|resourceMethod|payloadFormat|baseUrl):\s*$",
                block,
            )
            if anchor:
                block = block[: anchor.start()] + target_url_parameters + block[anchor.start() :]
                changed = True

    return block, changed


def _extract_difference_assertion_items(rest_client_block: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    items: List[str] = []
    names: List[str] = []
    for match in re.finditer(r"(?m)^(?P<indent>\s*)-\s+\$type:\s+DifferenceAssertion\s*$", rest_client_block):
        indent = match.group("indent")
        item = _extract_yaml_list_item_block(rest_client_block, match.start(), indent).rstrip() + "\n"
        items.append(item)

        # Capture the first "name:" field after the $type line (assertion display name).
        name_match = re.search(r"(?m)^\s*name:\s*(.+)\s*$", item)
        names.append(parse_yaml_scalar(name_match.group(1)) if name_match else "")
    return tuple(items), tuple(names)


def _extract_generic_assertion_tool_item(rest_client_block: str) -> Optional[str]:
    for match in re.finditer(r"(?m)^(?P<indent>\s*)-\s+\$type:\s+GenericAssertionTool\s*$", rest_client_block):
        indent = match.group("indent")
        item = _extract_yaml_list_item_block(rest_client_block, match.start(), indent)
        if "$type: DifferenceAssertion" in item:
            return _strip_yaml_mapping_field(item, field_name="errorsOutput").rstrip() + "\n"
    return None


def _strip_yaml_mapping_field(block: str, field_name: str) -> str:
    """
    Remove a mapping field (and its indented children) from a YAML block.

    This is used to drop fields that are syntactically valid YAML but not supported
    by the SOAtest YAML schema for certain tool types (for example errorsOutput).
    """
    lines = block.splitlines(keepends=True)
    if not lines:
        return block

    rebuilt: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(rf"^(?P<indent>[ \t]*){re.escape(field_name)}:\s*$", line)
        if not match:
            rebuilt.append(line)
            i += 1
            continue

        base_indent_len = len(match.group("indent"))
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip():
                i += 1
                continue
            nxt_indent_len = len(re.match(r"^[ \t]*", nxt).group(0))
            if nxt_indent_len <= base_indent_len:
                break
            i += 1
        # Skip the field block entirely.

    return "".join(rebuilt)


def _find_rest_client_tool_indent(block: str) -> Optional[str]:
    tool_key = re.search(r"(?m)^(?P<indent>\s*)tool:\s*$", block)
    if not tool_key:
        return None
    tail = block[tool_key.end() :]
    rest_client_type = re.search(r"(?m)^(?P<indent>[ \t]*)\$type:\s*RESTClient\s*$", tail)
    if not rest_client_type:
        return None
    return rest_client_type.group("indent")


def _strip_misplaced_difference_generic_assertion_tools(block: str, tool_indent: str) -> Tuple[str, int]:
    """
    Remove GenericAssertionTool list items containing DifferenceAssertion that are not direct children
    of RESTClient.outputTools (these commonly get inserted under nested outputProviders.*.outputTools).
    """
    removed = 0
    matches = list(re.finditer(r"(?m)^(?P<indent>\s*)-\s+\$type:\s+GenericAssertionTool\s*$", block))
    if not matches:
        return block, 0

    keep: List[str] = []
    cursor = 0
    for match in matches:
        indent = match.group("indent")
        # A correctly placed list item under tool.outputTools should be exactly two spaces deeper.
        if len(indent) == len(tool_indent) + 2:
            continue

        item = _extract_yaml_list_item_block(block, match.start(), indent)
        if "$type: DifferenceAssertion" not in item:
            continue

        keep.append(block[cursor : match.start()])
        cursor = match.start() + len(item)
        removed += 1

    if removed == 0:
        return block, 0

    keep.append(block[cursor:])
    return "".join(keep), removed


def _has_difference_assertions_under_tool_outputtools(block: str, tool_indent: str) -> bool:
    indent_len = len(tool_indent)
    output_tools_key = re.search(rf"(?m)^\s{{{indent_len}}}outputTools:\s*$", block)
    if not output_tools_key:
        return False
    tail = block[output_tools_key.end() :]
    next_tool_key = re.search(rf"(?m)^\s{{{indent_len}}}[A-Za-z_][A-Za-z0-9_-]*:\s*$", tail)
    region = tail[: next_tool_key.start()] if next_tool_key else tail
    return "$type: DifferenceAssertion" in region


def load_difference_assertion_templates(template_content: str) -> Dict[str, DifferenceAssertionTemplate]:
    templates: Dict[str, DifferenceAssertionTemplate] = {}
    starts = [m.start() for m in re.finditer(r"(?m)^\s*-\s+\$type:\s+RESTClientToolTest\s*$", template_content)]
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(template_content)
        block = template_content[start:end]

        name_match = re.search(
            r"(?m)^\s+iconName:\s*RESTClient\s*$\n^\s+name:\s*(.+)\s*$",
            block,
        )
        if not name_match:
            name_match = re.search(r"(?m)^\s+name:\s*(.+)\s*$", block)
        if not name_match:
            continue

        request_name = parse_yaml_scalar(name_match.group(1))
        assertion_items, assertion_names = _extract_difference_assertion_items(block)
        if not assertion_items:
            continue

        generic_item = _extract_generic_assertion_tool_item(block)
        if not generic_item:
            continue

        template_url_parameters = _find_yaml_mapping_block(block, "urlParameters")
        template_url_parameters_block = template_url_parameters[3] if template_url_parameters else None
        templates[request_name] = DifferenceAssertionTemplate(
            request_name=request_name,
            assertion_items=assertion_items,
            assertion_names=assertion_names,
            generic_assertion_tool_item=generic_item,
            http_client_endpoint_value=_extract_http_client_endpoint_value(block),
            url_parameters_block=template_url_parameters_block,
        )
    return templates


def patch_difference_assertions(
    content: str,
    templates: Dict[str, DifferenceAssertionTemplate],
) -> Tuple[str, int, int, int, List[str]]:
    warnings: List[str] = []
    if not templates:
        return content, 0, 0, 0, warnings

    # Some SOAtest YAML readers reject this field; strip it defensively.
    content = _strip_yaml_mapping_field(content, field_name="errorsOutput")
    # DifferenceAssertion templates we copy are schemaVersion 13; ensure target schema matches.
    content = ensure_schema_version(content, "13")

    starts = [m.start() for m in re.finditer(r"(?m)^\s*-\s+\$type:\s+RESTClientToolTest\s*$", content)]
    if not starts:
        return content, 0, 0, len(templates), ["No RESTClientToolTest blocks found in target .tst."]

    missing_by_name = {name: True for name in templates}

    added = 0
    already_present = 0

    parts: List[str] = []
    last_index = 0

    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(content)
        parts.append(content[last_index:start])
        block = content[start:end]
        last_index = end

        name_match = re.search(
            r"(?m)^\s+iconName:\s*RESTClient\s*$\n^\s+name:\s*(.+)\s*$",
            block,
        )
        if not name_match:
            name_match = re.search(r"(?m)^\s+name:\s*(.+)\s*$", block)
        if not name_match:
            parts.append(block)
            continue

        request_name = parse_yaml_scalar(name_match.group(1))
        template = templates.get(request_name)
        if not template:
            parts.append(block)
            continue

        tool_indent = _find_rest_client_tool_indent(block)
        if not tool_indent:
            warnings.append(f"Could not locate RESTClient tool block for request '{request_name}'.")
            parts.append(block)
            continue
        tool_indent_len = len(tool_indent)

        # Repair previous runs that accidentally injected tools into nested outputProviders.*.outputTools.
        block, removed_misplaced = _strip_misplaced_difference_generic_assertion_tools(block, tool_indent)
        if removed_misplaced:
            warnings.append(
                f"Removed {removed_misplaced} misplaced Difference assertion tool(s) for request '{request_name}'."
            )

        missing_by_name.pop(request_name, None)

        block, endpoint_or_query_changed = _apply_template_endpoint_and_query(block, template, tool_indent)
        if endpoint_or_query_changed:
            warnings.append(f"Updated endpoint/query mapping from template for request '{request_name}'.")

        # Prefer inserting into an existing XMLAssertionTool assertions list under tool.outputTools.
        output_tools_key = re.search(rf"(?m)^\s{{{tool_indent_len}}}outputTools:\s*$", block)
        search_region = block[output_tools_key.start() :] if output_tools_key else block
        xml_match = re.search(r"(?m)^\s+\$type:\s+XMLAssertionTool\s*$", search_region)
        if xml_match:
            xml_abs_end = (output_tools_key.start() if output_tools_key else 0) + xml_match.end()
            tail = block[xml_abs_end:]
            assertions_match = re.search(r"(?m)^(?P<indent>\s*)assertions:\s*$", tail)
            if assertions_match:
                assertions_indent = assertions_match.group("indent")
                post_assertions = tail[assertions_match.end() :]
                message_match = re.search(
                    rf"(?m)^{re.escape(assertions_indent)}message:\s*$",
                    post_assertions,
                )
                insert_at = xml_abs_end + assertions_match.end()
                if message_match:
                    insert_at = xml_abs_end + assertions_match.end() + message_match.start()

                to_insert: List[str] = []
                for item, name in zip(template.assertion_items, template.assertion_names):
                    if name:
                        assertion_name_exists = re.search(
                            rf"(?ms)^\s*-\s+\$type:\s+DifferenceAssertion\s*$.*?^\s+name:\s*{re.escape(name)}\s*$",
                            block,
                        )
                        if assertion_name_exists:
                            already_present += 1
                            continue
                    if not name and "$type: DifferenceAssertion" in block:
                        already_present += 1
                        continue
                    to_insert.append(_reindent_block(item, assertions_indent))

                if to_insert:
                    added += len(to_insert)
                    block = block[:insert_at] + "".join(to_insert) + block[insert_at:]
                parts.append(block)
                continue

        # Otherwise, inject a GenericAssertionTool item from the template into tool.outputTools.
        if _has_difference_assertions_under_tool_outputtools(block, tool_indent):
            already_present += len(template.assertion_items)
            parts.append(block)
            continue

        output_tools_key = re.search(rf"(?m)^\s{{{tool_indent_len}}}outputTools:\s*$", block)
        if output_tools_key:
            insert_at = output_tools_key.end()
            item_indent = tool_indent + "  "
            injected = _reindent_block(template.generic_assertion_tool_item, item_indent)
            parts.append(block[:insert_at] + "\n" + injected + block[insert_at:])
            added += len(template.assertion_items)
            continue

        # Create outputTools after the RESTClient name if possible.
        name_line = re.search(rf"(?m)^\s{{{tool_indent_len}}}name:\s*.+\s*$", block)
        anchor_end = name_line.end() if name_line else None
        if anchor_end is None:
            rest_client_type = re.search(rf"(?m)^\s{{{tool_indent_len}}}\$type:\s*RESTClient\s*$", block)
            anchor_end = rest_client_type.end() if rest_client_type else None
        if anchor_end is None:
            warnings.append(
                f"Could not locate RESTClient name/type anchor for request '{request_name}' when inserting Difference assertions."
            )
            parts.append(block)
            continue

        item_indent = tool_indent + "  "
        injected = _reindent_block(template.generic_assertion_tool_item, item_indent)
        output_tools_block = f"\n{tool_indent}outputTools:\n{injected}"
        parts.append(block[:anchor_end] + output_tools_block + block[anchor_end:])
        added += len(template.assertion_items)

    parts.append(content[last_index:])
    updated_content = "".join(parts)

    missing_count = len(missing_by_name)
    return updated_content, added, already_present, missing_count, warnings


def prune_unreferenced_basic_profiles(content: str) -> Tuple[str, int]:
    """Remove unreferenced auto-generated Basic profiles (Basic, Basic 2, ...)."""
    marker = "  authentications:\n"
    marker_index = content.find(marker)
    if marker_index < 0:
        return content, 0

    block_start = marker_index + len(marker)
    tail = content[block_start:]
    boundary_match = re.search(r"(?m)^  (?!- )[A-Za-z_][A-Za-z0-9_-]*:", tail)
    block_end = block_start + boundary_match.start() if boundary_match else len(content)

    auth_body = content[block_start:block_end]
    entry_matches = list(re.finditer(r"(?m)^  - \$type: [^\n]+\n", auth_body))
    if not entry_matches:
        return content, 0

    referenced_auth_names = {
        parse_yaml_scalar(m.group(1))
        for m in re.finditer(r"(?m)^\s+authName:\s*(.+)\s*$", content)
    }

    prefix = auth_body[: entry_matches[0].start()]
    entries: List[str] = []
    for idx, match in enumerate(entry_matches):
        start = match.start()
        end = entry_matches[idx + 1].start() if idx + 1 < len(entry_matches) else len(auth_body)
        entries.append(auth_body[start:end])

    removed = 0
    rebuilt_entries: List[str] = []
    for entry in entries:
        type_match = re.search(r"(?m)^  - \$type:\s*([^\n]+)\s*$", entry)
        entry_type = parse_yaml_scalar(type_match.group(1)) if type_match else ""
        if entry_type != "BasicAuthentication":
            rebuilt_entries.append(entry)
            continue

        name_match = re.search(r"(?m)^    name:\s*(.+)\s*$", entry)
        if not name_match:
            rebuilt_entries.append(entry)
            continue

        profile_name = parse_yaml_scalar(name_match.group(1))
        if not re.fullmatch(r"Basic(?: \d+)?", profile_name):
            rebuilt_entries.append(entry)
            continue

        if profile_name in referenced_auth_names:
            rebuilt_entries.append(entry)
            continue

        removed += 1

    if removed == 0:
        return content, 0

    new_body = prefix + "".join(rebuilt_entries)
    updated = content[:block_start] + new_body + content[block_end:]
    return updated, removed


def load_collection_name(collection_path: Path) -> str:
    try:
        payload = json.loads(collection_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return sanitize_filename(collection_path.stem or "Postman Collection")

    if not isinstance(payload, dict):
        return sanitize_filename(collection_path.stem or "Postman Collection")

    info = payload.get("info")
    if isinstance(info, dict):
        return sanitize_filename(str(info.get("name", collection_path.stem or "Postman Collection")))
    return sanitize_filename(collection_path.stem or "Postman Collection")


def load_collection_targets(collection_path: Path) -> Tuple[str, List[RequestTarget]]:
    payload = json.loads(collection_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Postman collection root must be a JSON object.")

    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    collection_name = str(info.get("name", collection_path.stem or "Postman Collection"))

    root_default = AuthSpec(mode="noauth")
    collection_raw = parse_auth_object(payload.get("auth")) if "auth" in payload else AuthSpec(mode="inherit")
    collection_effective = resolve_effective_auth(collection_raw, root_default)

    targets: List[RequestTarget] = []
    walk_postman_requests_with_auth(
        items=payload.get("item"),
        folder_path=(),
        parent_effective=collection_effective,
        targets=targets,
    )
    return collection_name, targets


def load_collection_binary_body_targets(collection_path: Path) -> List[BinaryBodyTarget]:
    payload = json.loads(collection_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Postman collection root must be a JSON object.")
    targets: List[BinaryBodyTarget] = []
    walk_postman_binary_body_targets(payload.get("item"), targets)
    return targets


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Apply template fallback auth patching to an existing SOAtest .tst. "
            "Current scope: digest/ntlm/awsv4/oauth2/noauth REST client auth persistence, "
            "plus automatic Difference assertion and binary request-body fallback patching."
        )
    )
    parser.add_argument("postman_collection", type=Path, help="Path to exported Postman Collection JSON.")
    parser.add_argument(
        "--mode",
        choices=["auth", "difference-assertions", "binary-body", "both", "all"],
        default="auth",
        help=(
            "Which fallback patches to apply. "
            "'auth' patches REST Client auth (default); "
            "'difference-assertions' inserts Difference assertions using a built-in template; "
            "'binary-body' patches Postman binary request bodies using a built-in literal/file template block; "
            "'both' applies auth + Difference fallback; "
            "'all' applies auth + Difference + binary-body fallback."
        ),
    )
    parser.add_argument(
        "--postman-environment",
        type=Path,
        default=None,
        help=(
            "Optional Postman environment JSON used to resolve ${var} identities when deduplicating "
            "digest/ntlm/awsv4/oauth2 profiles."
        ),
    )
    workspace_root_env = os.environ.get("SOAVIRT_WORKSPACE_ROOT")
    workspace_root_default = Path(workspace_root_env) if workspace_root_env else None
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=workspace_root_default,
        help=(
            "Local SOAtest workspace root used to locate the target .tst for fallback patching. "
            "Provide --workspace-root or set SOAVIRT_WORKSPACE_ROOT."
        ),
    )
    parser.add_argument(
        "--parent-id",
        default="/TestAssets",
        help="SOAVirt parent folder id where target .tst exists (default: /TestAssets).",
    )
    parser.add_argument(
        "--target-test-file-name",
        default=None,
        help="Optional target .tst test file name without extension. Defaults to Postman collection name.",
    )
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
            "ERROR: Workspace root is required for template fallback patching. "
            "Provide --workspace-root or set SOAVIRT_WORKSPACE_ROOT.",
            file=sys.stderr,
        )
        return 1
    if not args.workspace_root.exists() or not args.workspace_root.is_dir():
        print(
            f"ERROR: Workspace root does not exist or is not a directory: {args.workspace_root}",
            file=sys.stderr,
        )
        return 1

    if not args.postman_collection.exists():
        print(f"ERROR: Input file does not exist: {args.postman_collection}", file=sys.stderr)
        return 1

    if args.postman_environment is not None and not args.postman_environment.exists():
        print(
            f"ERROR: Postman environment file does not exist: {args.postman_environment}",
            file=sys.stderr,
        )
        return 1
    do_auth = args.mode in {"auth", "both", "all"}
    do_diff = args.mode in {"difference-assertions", "both", "all"}
    do_binary = args.mode in {"binary-body", "all"}

    if do_diff and not DEFAULT_DIFFERENCE_ASSERTIONS_TEMPLATE.exists():
        print(
            f"ERROR: Missing built-in Difference assertion template: {DEFAULT_DIFFERENCE_ASSERTIONS_TEMPLATE}",
            file=sys.stderr,
        )
        return 1

    if do_binary and not DEFAULT_BINARY_BODY_TEMPLATE.exists():
        print(
            f"ERROR: Missing built-in binary body template: {DEFAULT_BINARY_BODY_TEMPLATE}",
            file=sys.stderr,
        )
        return 1

    environment_values: Dict[str, str] = {}
    if do_auth and args.postman_environment is not None:
        environment_values = load_postman_environment_values(args.postman_environment)

    collection_name = load_collection_name(args.postman_collection)
    request_targets: List[RequestTarget] = []
    binary_targets: List[BinaryBodyTarget] = []
    if do_auth:
        try:
            collection_name, request_targets = load_collection_targets(args.postman_collection)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Input is not valid JSON: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"ERROR: Failed to read collection: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    if do_binary:
        try:
            binary_targets = load_collection_binary_body_targets(args.postman_collection)
        except json.JSONDecodeError as exc:
            print(f"ERROR: Input is not valid JSON: {exc}", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"ERROR: Failed to read collection: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    target_name = args.target_test_file_name or collection_name
    target_path = resolve_local_tst_path(args.workspace_root, args.parent_id, target_name)
    if not target_path.exists():
        print(f"ERROR: Target .tst file not found for fallback patching: {target_path}", file=sys.stderr)
        return 2

    expectations: List[RequestExpectation] = []
    auth_profiles: List[AuthProfile] = []
    if do_auth:
        expectations, auth_profiles = build_expectations(request_targets, environment_values)

    try:
        content = target_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Failed to read target .tst: {exc}", file=sys.stderr)
        return 3

    updated = content

    patched_count = 0
    already_matching = 0
    missing_count = 0
    removed_basic_profiles = 0
    added_by_mode: Dict[str, int] = {}
    auth_warnings: List[str] = []
    if do_auth and expectations:
        updated, added_by_mode, profile_warnings = ensure_auth_profiles(updated, auth_profiles)
        updated, patched_count, already_matching, missing_count, patch_warnings = patch_rest_clients(
            updated, expectations
        )
        updated, removed_basic_profiles = prune_unreferenced_basic_profiles(updated)
        has_awsv4_profile = any(profile.mode == "awsv4" for profile in auth_profiles)
        has_oauth2_profile = any(profile.mode == "oauth2" for profile in auth_profiles)
        if has_awsv4_profile:
            updated = ensure_min_schema_version(updated, "13")
        elif has_oauth2_profile:
            updated = ensure_min_schema_version(updated, "05")
        auth_warnings = [*profile_warnings, *patch_warnings]

    diff_added = 0
    diff_already_present = 0
    diff_missing = 0
    diff_warnings: List[str] = []
    if do_diff:
        try:
            template_content = DEFAULT_DIFFERENCE_ASSERTIONS_TEMPLATE.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: Failed to read built-in Difference assertion template: {exc}", file=sys.stderr)
            return 3
        templates = load_difference_assertion_templates(template_content)
        updated, diff_added, diff_already_present, diff_missing, diff_warnings = patch_difference_assertions(
            updated, templates
        )

    binary_patched = 0
    binary_already_matching = 0
    binary_missing_in_tst = 0
    binary_template_missing = 0
    binary_warnings: List[str] = []
    if do_binary:
        try:
            binary_template_content = DEFAULT_BINARY_BODY_TEMPLATE.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: Failed to read built-in binary body template: {exc}", file=sys.stderr)
            return 3
        binary_templates, default_binary_template, binary_template_warnings = load_binary_body_templates(binary_template_content)
        (
            updated,
            binary_patched,
            binary_already_matching,
            binary_missing_in_tst,
            binary_template_missing,
            binary_patch_warnings,
        ) = patch_binary_request_bodies(updated, binary_targets, binary_templates, default_binary_template)
        binary_warnings = [*binary_template_warnings, *binary_patch_warnings]

    warnings = [*auth_warnings, *diff_warnings, *binary_warnings]

    try:
        if updated != content:
            target_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Failed to write patched .tst: {exc}", file=sys.stderr)
        return 4

    print(f"Target .tst: {target_path}")
    digest_required = sum(1 for profile in auth_profiles if profile.mode == "digest")
    ntlm_required = sum(1 for profile in auth_profiles if profile.mode == "ntlm")
    awsv4_required = sum(1 for profile in auth_profiles if profile.mode == "awsv4")
    oauth2_required = sum(1 for profile in auth_profiles if profile.mode == "oauth2")
    jwt_evaluated = sum(1 for expectation in expectations if expectation.mode == "jwt")
    digest_added = int(added_by_mode.get("digest", 0))
    ntlm_added = int(added_by_mode.get("ntlm", 0))
    awsv4_added = int(added_by_mode.get("awsv4", 0))
    oauth2_added = int(added_by_mode.get("oauth2", 0))
    print(f"Digest/NTLM/AWS/OAuth2/JWT/noauth requests evaluated: {len(expectations)}")
    print(f"Digest profiles required: {digest_required}")
    print(f"NTLM profiles required: {ntlm_required}")
    print(f"AWS Signature profiles required: {awsv4_required}")
    print(f"OAuth2 profiles required: {oauth2_required}")
    print(f"JWT requests requiring header fallback: {jwt_evaluated}")
    print(f"Digest profiles added by template fallback: {digest_added}")
    print(f"NTLM profiles added by template fallback: {ntlm_added}")
    print(f"AWS Signature profiles added by template fallback: {awsv4_added}")
    print(f"OAuth2 profiles added by template fallback: {oauth2_added}")
    print(f"Auth profiles added by template fallback: {digest_added + ntlm_added + awsv4_added + oauth2_added}")
    print(f"REST clients patched by template fallback: {patched_count}")
    print(f"Requests already matching expected auth: {already_matching}")
    print(f"Requests not found in .tst: {missing_count}")
    print(f"Difference assertions added by template fallback: {diff_added}")
    print(f"Difference assertions already present: {diff_already_present}")
    print(f"Difference assertion requests not found in .tst: {diff_missing}")
    print(f"Binary request bodies evaluated: {len(binary_targets)}")
    print(f"Binary literal/file payload blocks patched by template fallback: {binary_patched}")
    print(f"Binary payload blocks already matching template: {binary_already_matching}")
    print(f"Binary body requests not found in .tst: {binary_missing_in_tst}")
    print(f"Binary body template entries missing for requests: {binary_template_missing}")
    print(f"Template fallback warnings: {len(warnings)}")
    print(f"Unused Basic profiles removed: {removed_basic_profiles}")
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

