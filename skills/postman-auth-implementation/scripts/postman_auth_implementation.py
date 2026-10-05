#!/usr/bin/env python3
"""
Apply Postman authorization settings to an existing SOAtest .tst.

Supported now:
- Basic Authentication
- Digest Authentication
- NTLM Authentication
- AWS Signature (SigV4)
- API Key (header/query mapped as explicit header or URL query parameter)
- Bearer Token (mapped to explicit Authorization header)
- JWT Bearer (detected and reported for template fallback)
- OAuth 2.0 (detected and reported for template fallback)
- Inherit Auth from Parent
- No Auth
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


VAR_REFERENCE_RE = re.compile(r"{{\s*([^{}]+?)\s*}}")
SOATEST_VAR_REFERENCE_RE = re.compile(r"^\$\{([^{}]+)\}$")
AUTH_NAME_BASIC_RE = re.compile(r"authName:\s*Basic(?:\s+\d+)?")


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", str(name)).strip().rstrip(". ")
    return value or "postman-collection"


def convert_postman_variable_syntax(text: str) -> str:
    if not text:
        return ""
    return VAR_REFERENCE_RE.sub(lambda m: "${" + m.group(1).strip() + "}", str(text))


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
    if not resolved:
        return value
    return resolved


def normalize_basic_auth_profiles_yaml(content: str) -> str:
    """Collapse duplicate Basic auth references/profiles to one Basic profile using env vars."""
    updated = AUTH_NAME_BASIC_RE.sub("authName: Basic", content)

    marker = "  authentications:\n"
    start = updated.find(marker)
    if start < 0:
        return updated

    block_start = start + len(marker)
    tail = updated[block_start:]
    boundary_match = re.search(r"(?m)^  (?!- )[A-Za-z_][A-Za-z0-9_-]*:", tail)
    if not boundary_match:
        return updated
    end = block_start + boundary_match.start()

    canonical_block = (
        "  authentications:\n"
        "  - $type: BasicAuthentication\n"
        "    name: Basic\n"
        "    username:\n"
        "      fixedValue:\n"
        "        $type: StringTestValue\n"
        "        username: \"${username}\"\n"
        "    password:\n"
        "      fixedValue:\n"
        "        $type: StringTestValue\n"
        "        password: \"${password}\"\n"
    )
    return updated[:start] + canonical_block + updated[end:]


def normalize_basic_auth_profiles_in_file(path: Path) -> bool:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return False
    normalized = normalize_basic_auth_profiles_yaml(source)
    if normalized == source:
        return False
    path.write_text(normalized, encoding="utf-8")
    return True


def resolve_local_tst_path(workspace_root: Path, parent_id: str, test_file_name_without_ext: str) -> Path:
    relative_parts = [part for part in parent_id.strip("/").split("/") if part]
    relative = Path(*relative_parts) if relative_parts else Path()
    return workspace_root / relative / f"{sanitize_filename(test_file_name_without_ext)}.tst"


@dataclass(frozen=True)
class AuthSpec:
    mode: str
    auth_type: str
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
    bearer_token: str = ""
    bearer_prefix: str = "Bearer"
    api_key_name: str = ""
    api_key_value: str = ""
    api_key_in: str = "header"


@dataclass(frozen=True)
class RequestAuthTarget:
    folder_path: Tuple[str, ...]
    request_name: str
    effective_auth: AuthSpec
    source_auth_type: str


def resolve_auth_spec_with_environment(auth: AuthSpec, environment_values: Dict[str, str]) -> AuthSpec:
    if auth.mode in {"basic", "digest", "ntlm"}:
        return AuthSpec(
            mode=auth.mode,
            auth_type=auth.auth_type,
            username=resolve_soatest_variable_reference(auth.username, environment_values),
            password=resolve_soatest_variable_reference(auth.password, environment_values),
            domain=resolve_soatest_variable_reference(auth.domain, environment_values),
            workstation=resolve_soatest_variable_reference(auth.workstation, environment_values),
            access_key_id=auth.access_key_id,
            secret_access_key=auth.secret_access_key,
            session_token=auth.session_token,
            service=auth.service,
            region=auth.region,
            aws_auth_mode=auth.aws_auth_mode,
            bearer_token=auth.bearer_token,
            bearer_prefix=auth.bearer_prefix,
        )

    if auth.mode == "awsv4":
        return AuthSpec(
            mode=auth.mode,
            auth_type=auth.auth_type,
            username=auth.username,
            password=auth.password,
            domain=auth.domain,
            workstation=auth.workstation,
            access_key_id=resolve_soatest_variable_reference(auth.access_key_id, environment_values),
            secret_access_key=resolve_soatest_variable_reference(auth.secret_access_key, environment_values),
            session_token=resolve_soatest_variable_reference(auth.session_token, environment_values),
            service=resolve_soatest_variable_reference(auth.service, environment_values),
            region=resolve_soatest_variable_reference(auth.region, environment_values),
            aws_auth_mode=auth.aws_auth_mode,
            bearer_token=auth.bearer_token,
            bearer_prefix=auth.bearer_prefix,
        )

    if auth.mode == "bearer":
        resolved_prefix = resolve_soatest_variable_reference(auth.bearer_prefix, environment_values).strip()
        return AuthSpec(
            mode=auth.mode,
            auth_type=auth.auth_type,
            bearer_token=resolve_soatest_variable_reference(auth.bearer_token, environment_values),
            bearer_prefix=resolved_prefix or "Bearer",
        )

    return auth


class PostmanAuthHandler:
    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        raise NotImplementedError


class InheritAuthHandler(PostmanAuthHandler):
    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        return AuthSpec(mode="inherit", auth_type="inherit")


class NoAuthHandler(PostmanAuthHandler):
    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        return AuthSpec(mode="noauth", auth_type="noauth")


class CredentialAuthHandler(PostmanAuthHandler):
    auth_mode: str = ""

    @staticmethod
    def _extract_value(raw: Any, key: str) -> str:
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

    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        raw = auth_obj.get(self.auth_mode)
        return AuthSpec(
            mode=self.auth_mode,
            auth_type=self.auth_mode,
            username=self._extract_value(raw, "username"),
            password=self._extract_value(raw, "password"),
            domain=self._extract_value(raw, "domain"),
            workstation=self._extract_value(raw, "workstation"),
        )


class BasicAuthHandler(CredentialAuthHandler):
    auth_mode = "basic"


class DigestAuthHandler(CredentialAuthHandler):
    auth_mode = "digest"


class NtlmAuthHandler(CredentialAuthHandler):
    auth_mode = "ntlm"


class AwsSignatureAuthHandler(CredentialAuthHandler):
    auth_mode = "awsv4"

    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        raw = auth_obj.get(self.auth_mode)
        add_to_request = self._extract_value(raw, "addAuthDataToRequest").strip().lower()
        aws_auth_mode = 1 if "query" in add_to_request else None
        return AuthSpec(
            mode="awsv4",
            auth_type="awsv4",
            access_key_id=self._extract_value(raw, "accessKey"),
            secret_access_key=self._extract_value(raw, "secretKey"),
            session_token=self._extract_value(raw, "sessionToken"),
            service=self._extract_value(raw, "service"),
            region=self._extract_value(raw, "region"),
            aws_auth_mode=aws_auth_mode,
        )


class Oauth2AuthHandler(CredentialAuthHandler):
    auth_mode = "oauth2"

    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        return AuthSpec(mode="oauth2", auth_type="oauth2")


class JwtAuthHandler(CredentialAuthHandler):
    auth_mode = "jwt"

    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        return AuthSpec(mode="jwt", auth_type="jwt")


class BearerAuthHandler(CredentialAuthHandler):
    auth_mode = "bearer"

    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        raw = auth_obj.get(self.auth_mode)
        prefix = self._extract_value(raw, "prefix").strip() or "Bearer"
        return AuthSpec(
            mode="bearer",
            auth_type="bearer",
            bearer_token=self._extract_value(raw, "token"),
            bearer_prefix=prefix,
        )


class ApiKeyAuthHandler(CredentialAuthHandler):
    auth_mode = "apikey"

    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        raw = auth_obj.get(self.auth_mode)
        key_name = self._extract_value(raw, "key")
        key_value = self._extract_value(raw, "value")
        raw_location = self._extract_value(raw, "in").strip().lower()

        if raw_location in {"", "header"}:
            normalized_location = "header"
        elif raw_location in {"query", "queryparams"}:
            normalized_location = "query"
        else:
            return AuthSpec(mode="unsupported", auth_type="apikey")

        return AuthSpec(
            mode="apikey",
            auth_type="apikey",
            api_key_name=key_name,
            api_key_value=key_value,
            api_key_in=normalized_location,
        )


class UnknownAuthHandler(PostmanAuthHandler):
    def parse(self, auth_obj: Dict[str, Any]) -> AuthSpec:
        auth_type = str(auth_obj.get("type", "unknown")).strip().lower() or "unknown"
        return AuthSpec(mode="unsupported", auth_type=auth_type)


AUTH_HANDLERS: Dict[str, PostmanAuthHandler] = {
    "inherit": InheritAuthHandler(),
    "noauth": NoAuthHandler(),
    "basic": BasicAuthHandler(),
    "digest": DigestAuthHandler(),
    "ntlm": NtlmAuthHandler(),
    "awsv4": AwsSignatureAuthHandler(),
    "apikey": ApiKeyAuthHandler(),
    "bearer": BearerAuthHandler(),
    "oauth2": Oauth2AuthHandler(),
    "jwt": JwtAuthHandler(),
}
UNKNOWN_HANDLER = UnknownAuthHandler()


def parse_auth_object(auth_obj: Any) -> AuthSpec:
    if not isinstance(auth_obj, dict):
        return AUTH_HANDLERS["inherit"].parse({})

    auth_type = str(auth_obj.get("type", "inherit")).strip().lower() or "inherit"
    handler = AUTH_HANDLERS.get(auth_type, UNKNOWN_HANDLER)
    return handler.parse(auth_obj)


def resolve_effective_auth(raw_auth: AuthSpec, parent_effective: AuthSpec) -> AuthSpec:
    if raw_auth.mode == "inherit":
        return parent_effective
    return raw_auth


def walk_postman_requests_with_auth(
    items: Any,
    folder_path: Tuple[str, ...],
    parent_effective: AuthSpec,
    targets: List[RequestAuthTarget],
) -> None:
    if not isinstance(items, list):
        return

    for item in items:
        if not isinstance(item, dict):
            continue

        item_name = str(item.get("name", "Unnamed"))

        if isinstance(item.get("item"), list):
            folder_raw = parse_auth_object(item.get("auth")) if "auth" in item else AUTH_HANDLERS["inherit"].parse({})
            folder_effective = resolve_effective_auth(folder_raw, parent_effective)
            walk_postman_requests_with_auth(
                items=item.get("item"),
                folder_path=folder_path + (item_name,),
                parent_effective=folder_effective,
                targets=targets,
            )

        request_obj = item.get("request")
        if isinstance(request_obj, dict):
            if isinstance(request_obj.get("auth"), dict):
                request_raw = parse_auth_object(request_obj.get("auth"))
            elif isinstance(item.get("auth"), dict):
                request_raw = parse_auth_object(item.get("auth"))
            else:
                request_raw = AUTH_HANDLERS["inherit"].parse({})

            effective = resolve_effective_auth(request_raw, parent_effective)
            source_auth_type = request_raw.auth_type
            targets.append(
                RequestAuthTarget(
                    folder_path=folder_path,
                    request_name=item_name,
                    effective_auth=effective,
                    source_auth_type=source_auth_type,
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


def _credential_value_fixed(value: str) -> Dict[str, str]:
    return {
        "type": "fixed",
        "fixed": convert_postman_variable_syntax(value),
    }


def build_authentication_type_payload(effective_auth: AuthSpec) -> Optional[Dict[str, Any]]:
    if effective_auth.mode == "basic":
        return {
            "type": "basic",
            "basic": {
                "username": _credential_value_fixed(effective_auth.username),
                "password": _credential_value_fixed(effective_auth.password),
            },
        }

    if effective_auth.mode == "digest":
        return {
            "type": "digest",
            "digest": {
                "username": _credential_value_fixed(effective_auth.username),
                "password": _credential_value_fixed(effective_auth.password),
            },
        }

    if effective_auth.mode == "ntlm":
        ntlm_payload: Dict[str, Any] = {
            "username": _credential_value_fixed(effective_auth.username),
            "password": _credential_value_fixed(effective_auth.password),
        }
        if effective_auth.domain:
            ntlm_payload["domain"] = _credential_value_fixed(effective_auth.domain)
        if effective_auth.workstation:
            ntlm_payload["workstation"] = _credential_value_fixed(effective_auth.workstation)

        return {
            "type": "ntlm",
            "ntlm": ntlm_payload,
        }

    if effective_auth.mode == "awsv4":
        aws_payload: Dict[str, Any] = {
            "accessKeyId": _credential_value_fixed(effective_auth.access_key_id),
            "secretAccessKey": _credential_value_fixed(effective_auth.secret_access_key),
        }
        if effective_auth.session_token:
            aws_payload["sessionToken"] = _credential_value_fixed(effective_auth.session_token)
        if effective_auth.service:
            aws_payload["service"] = _credential_value_fixed(effective_auth.service)
        if effective_auth.region:
            aws_payload["region"] = _credential_value_fixed(effective_auth.region)
        if effective_auth.aws_auth_mode is not None:
            aws_payload["authMode"] = int(effective_auth.aws_auth_mode)

        return {
            "type": "awsSignature",
            "awsSignature": aws_payload,
        }

    return None


def _extract_perform_auth(rest_client: Dict[str, Any]) -> Dict[str, Any]:
    http_options = rest_client.get("httpOptions")
    if not isinstance(http_options, dict):
        return {}
    transport = http_options.get("transport")
    if not isinstance(transport, dict):
        return {}
    transport_type = str(transport.get("type", "http10")).lower()
    if transport_type not in {"http10", "http11"}:
        transport_type = "http10"
    transport_obj = transport.get(transport_type)
    if not isinstance(transport_obj, dict):
        return {}
    security = transport_obj.get("security")
    if not isinstance(security, dict):
        return {}
    http_auth = security.get("httpAuthentication")
    if not isinstance(http_auth, dict):
        return {}
    perform_auth = http_auth.get("performAuthentication")
    return perform_auth if isinstance(perform_auth, dict) else {}


def _extract_complex_value(node: Any) -> str:
    if isinstance(node, dict):
        for key in ("fixed", "masked", "literal", "value"):
            value = node.get(key)
            if isinstance(value, str):
                return value
    if isinstance(node, str):
        return node
    return ""


def _extract_headers_literal(rest_client: Dict[str, Any]) -> str:
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


def _parse_headers_literal(literal: str) -> List[Tuple[str, str]]:
    headers: List[Tuple[str, str]] = []
    for line in str(literal or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            headers.append((key.strip(), value.strip()))
        else:
            headers.append((stripped, ""))
    return headers


def _headers_literal(headers: Sequence[Tuple[str, str]]) -> str:
    lines: List[str] = []
    for key, value in headers:
        if value:
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}:")
    return "\n".join(lines)


def _get_header_value(literal: str, header_name: str) -> Optional[str]:
    target = header_name.strip().lower()
    for key, value in _parse_headers_literal(literal):
        if key.strip().lower() == target:
            return value.strip()
    return None


def _build_bearer_authorization_value(effective_auth: AuthSpec) -> str:
    prefix = (effective_auth.bearer_prefix or "Bearer").strip() or "Bearer"
    token = convert_postman_variable_syntax(effective_auth.bearer_token).strip()
    if not token:
        return prefix
    return f"{prefix} {token}"


def _extract_current_rest_url(rest_client: Dict[str, Any]) -> str:
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


def _parse_query_pairs(query_text: str) -> List[Tuple[str, Optional[str]]]:
    pairs: List[Tuple[str, Optional[str]]] = []
    if not query_text:
        return pairs

    for raw_part in str(query_text).split("&"):
        part = raw_part.strip()
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            pairs.append((key, value))
        else:
            pairs.append((part, None))
    return pairs


def _query_pairs_to_text(pairs: Sequence[Tuple[str, Optional[str]]]) -> str:
    parts: List[str] = []
    for key, value in pairs:
        if value is None:
            parts.append(key)
        else:
            parts.append(f"{key}={value}")
    return "&".join(parts)


def _get_query_parameter_value(url: str, param_name: str) -> Optional[str]:
    _, _, query_text = str(url or "").partition("?")
    target = str(param_name or "")
    for key, value in _parse_query_pairs(query_text):
        if key == target:
            return "" if value is None else value
    return None


def _upsert_query_parameter_in_url(current_url: str, param_name: str, param_value: str) -> str:
    base, sep, query_text = str(current_url or "").partition("?")
    pairs = _parse_query_pairs(query_text if sep else "")

    updated: List[Tuple[str, Optional[str]]] = []
    replaced = False
    for key, value in pairs:
        if key == param_name:
            if not replaced:
                updated.append((param_name, param_value))
                replaced = True
            continue
        updated.append((key, value))

    if not replaced:
        updated.append((param_name, param_value))

    rendered_query = _query_pairs_to_text(updated)
    if rendered_query:
        return f"{base}?{rendered_query}"
    return base


def _upsert_header_literal(literal: str, header_name: str, header_value: str) -> str:
    normalized_name = header_name.strip().lower()
    existing = _parse_headers_literal(literal)

    updated: List[Tuple[str, str]] = []
    replaced = False
    for key, value in existing:
        if key.strip().lower() == normalized_name:
            if not replaced:
                updated.append((header_name, header_value))
                replaced = True
            continue
        updated.append((key, value))

    if not replaced:
        updated.append((header_name, header_value))

    return _headers_literal(updated)


def _get_auth_credentials(authentication_type: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
    if mode == "basic":
        creds = authentication_type.get("basic")
        return creds if isinstance(creds, dict) else None

    if mode == "awsv4":
        for key in ("awsSignature", "awsv4", "aws"):
            creds = authentication_type.get(key)
            if isinstance(creds, dict):
                return creds
        return None

    creds = authentication_type.get(mode)
    if isinstance(creds, dict):
        return creds

    # Some SOAtest builds may normalize credentials under 'basic' even with non-basic protocol.
    fallback = authentication_type.get("basic")
    return fallback if isinstance(fallback, dict) else None


def auth_persisted_matches(rest_client: Dict[str, Any], effective_auth: AuthSpec) -> bool:
    perform_auth = _extract_perform_auth(rest_client)

    if effective_auth.mode == "bearer":
        if bool(perform_auth.get("enabled", False)):
            return False
        headers_literal = _extract_headers_literal(rest_client)
        expected = _build_bearer_authorization_value(effective_auth)
        actual = _get_header_value(headers_literal, "Authorization")
        return actual == expected

    if effective_auth.mode == "apikey":
        if bool(perform_auth.get("enabled", False)):
            return False

        api_key_name = convert_postman_variable_syntax(effective_auth.api_key_name).strip()
        api_key_value = convert_postman_variable_syntax(effective_auth.api_key_value)
        api_key_location = str(effective_auth.api_key_in or "header").strip().lower()

        if not api_key_name:
            return False

        if api_key_location == "query":
            current_url = _extract_current_rest_url(rest_client)
            actual = _get_query_parameter_value(current_url, api_key_name)
            return actual == api_key_value

        headers_literal = _extract_headers_literal(rest_client)
        actual = _get_header_value(headers_literal, api_key_name)
        return actual == api_key_value

    if effective_auth.mode in {"noauth", "inherit", "unsupported"}:
        return not bool(perform_auth.get("enabled", False))

    if not bool(perform_auth.get("enabled", False)):
        return False

    value = perform_auth.get("value")
    if not isinstance(value, dict):
        return False
    auth_type = value.get("authenticationType")
    if not isinstance(auth_type, dict):
        return False

    actual_type = str(auth_type.get("type", "")).strip().lower()
    allowed_type_names = {effective_auth.mode}
    if effective_auth.mode == "awsv4":
        allowed_type_names.update({"awssignature", "awsv4", "aws"})
    if actual_type not in allowed_type_names:
        return False

    credentials = _get_auth_credentials(auth_type, effective_auth.mode)
    if not isinstance(credentials, dict):
        return False

    expected_username = convert_postman_variable_syntax(effective_auth.username)
    if expected_username and _extract_complex_value(credentials.get("username")) != expected_username:
        return False

    expected_password = convert_postman_variable_syntax(effective_auth.password)
    if expected_password:
        password_node = credentials.get("password")
        if isinstance(password_node, dict) and str(password_node.get("type", "")).lower() == "masked":
            masked_value = password_node.get("masked")
            if not isinstance(masked_value, str) or not masked_value:
                return False
        elif _extract_complex_value(password_node) != expected_password:
            return False

    if effective_auth.mode == "ntlm":
        expected_domain = convert_postman_variable_syntax(effective_auth.domain)
        if expected_domain and _extract_complex_value(credentials.get("domain")) != expected_domain:
            return False
        expected_workstation = convert_postman_variable_syntax(effective_auth.workstation)
        if expected_workstation and _extract_complex_value(credentials.get("workstation")) != expected_workstation:
            return False

    if effective_auth.mode == "awsv4":
        expected_access_key = convert_postman_variable_syntax(effective_auth.access_key_id)
        if expected_access_key and _extract_complex_value(credentials.get("accessKeyId")) != expected_access_key:
            return False

        expected_secret_key = convert_postman_variable_syntax(effective_auth.secret_access_key)
        if expected_secret_key:
            secret_node = credentials.get("secretAccessKey")
            if isinstance(secret_node, dict) and str(secret_node.get("type", "")).lower() == "masked":
                masked_value = secret_node.get("masked")
                if not isinstance(masked_value, str) or not masked_value:
                    return False
            elif _extract_complex_value(secret_node) != expected_secret_key:
                return False

        expected_session_token = convert_postman_variable_syntax(effective_auth.session_token)
        if expected_session_token and _extract_complex_value(credentials.get("sessionToken")) != expected_session_token:
            return False

        expected_service = convert_postman_variable_syntax(effective_auth.service)
        if expected_service and _extract_complex_value(credentials.get("service")) != expected_service:
            return False

        expected_region = convert_postman_variable_syntax(effective_auth.region)
        if expected_region and _extract_complex_value(credentials.get("region")) != expected_region:
            return False

        if effective_auth.aws_auth_mode is not None:
            persisted_mode = credentials.get("authMode")
            try:
                if int(str(persisted_mode)) != int(effective_auth.aws_auth_mode):
                    return False
            except (TypeError, ValueError):
                return False

    return True


def build_rest_client_update_payload(current: Dict[str, Any], effective_auth: AuthSpec) -> Dict[str, Any]:
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
    security = transport_obj.setdefault("security", {})
    http_auth = security.setdefault("httpAuthentication", {})
    perform_auth = http_auth.setdefault("performAuthentication", {})

    auth_payload = build_authentication_type_payload(effective_auth)
    if auth_payload is None:
        perform_auth["enabled"] = False
        perform_auth.pop("value", None)
    else:
        perform_auth["enabled"] = True
        perform_auth["value"] = {
            "useGlobal": False,
            "authenticationType": auth_payload,
        }

    if effective_auth.mode == "bearer":
        current_headers_literal = _extract_headers_literal(current)
        authorization_value = _build_bearer_authorization_value(effective_auth)
        transport_obj["httpHeaders"] = {
            "type": "literal",
            "literal": _upsert_header_literal(current_headers_literal, "Authorization", authorization_value),
        }

    if effective_auth.mode == "apikey":
        api_key_name = convert_postman_variable_syntax(effective_auth.api_key_name).strip()
        api_key_value = convert_postman_variable_syntax(effective_auth.api_key_value)
        api_key_location = str(effective_auth.api_key_in or "header").strip().lower()

        if api_key_name:
            if api_key_location == "query":
                current_url = _extract_current_rest_url(current)
                new_url = _upsert_query_parameter_in_url(current_url, api_key_name, api_key_value)
                resource = payload.setdefault("resource", {})
                resource["type"] = "literalText"
                literal = resource.setdefault("literalText", {})
                literal["fixed"] = new_url
            else:
                current_headers_literal = _extract_headers_literal(current)
                transport_obj["httpHeaders"] = {
                    "type": "literal",
                    "literal": _upsert_header_literal(current_headers_literal, api_key_name, api_key_value),
                }

    return payload


def apply_auth(
    client: SoaVirtClient,
    tst_id: str,
    request_targets: Sequence[RequestAuthTarget],
) -> Tuple[Dict[str, int], int, int, List[str]]:
    descendants = client.descendants_assets(tst_id)
    lookup = build_rest_client_lookup(descendants)

    mode_counts: Dict[str, int] = {
        "basic": 0,
        "digest": 0,
        "ntlm": 0,
        "awsv4": 0,
        "apikey": 0,
        "bearer": 0,
        "oauth2": 0,
        "jwt": 0,
        "noauth": 0,
    }
    unsupported = 0
    update_failures = 0
    warnings: List[str] = []

    for target in request_targets:
        key = (target.folder_path, target.request_name)
        ids = lookup.get(key)
        if not ids:
            warnings.append(
                "Could not map request to SOAtest REST Client: "
                f"folder={'/'.join(target.folder_path) if target.folder_path else '(root)'} name={target.request_name}"
            )
            continue

        rest_client_id = ids.pop(0)
        if not rest_client_id:
            warnings.append(f"Mapped REST Client id was empty for request '{target.request_name}'")
            continue

        mode = target.effective_auth.mode
        if mode == "unsupported":
            unsupported += 1
            continue

        if mode in {"oauth2", "jwt"}:
            mode_counts[mode] += 1
            update_failures += 1
            auth_label = "OAuth 2.0" if mode == "oauth2" else "JWT Bearer"
            warnings.append(
                "Auth update did not persist as expected "
                f"for request '{target.request_name}' (mode={mode}). "
                f"SOAVirt API auth step does not persist {auth_label} settings; template fallback required."
            )
            continue

        try:
            current = client.get_rest_client(rest_client_id)
            updated = build_rest_client_update_payload(current, target.effective_auth)
            client.update_rest_client(rest_client_id, updated)
            persisted = client.get_rest_client(rest_client_id)
        except RuntimeError as exc:
            update_failures += 1
            warnings.append(
                f"Auth update failed for request '{target.request_name}' (mode={mode}): {exc}"
            )
            continue

        if not auth_persisted_matches(persisted, target.effective_auth):
            update_failures += 1
            persisted_auth = _extract_perform_auth(persisted)
            warnings.append(
                "Auth update did not persist as expected "
                f"for request '{target.request_name}' (mode={mode}). "
                f"Persisted performAuthentication={json.dumps(persisted_auth, ensure_ascii=False)}"
            )
            continue

        if mode in {"basic", "digest", "ntlm", "awsv4", "apikey", "bearer"}:
            mode_counts[mode] += 1
        else:
            mode_counts["noauth"] += 1

    return mode_counts, unsupported, update_failures, warnings


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert Postman authorization to SOAtest REST Client auth for an existing generated .tst "
            "(supports Basic, Digest, NTLM, AWS Signature, Bearer mapping, OAuth2/JWT detection for fallback, Inherit, No Auth)."
        )
    )
    parser.add_argument("postman_collection", type=Path, help="Path to exported Postman Collection JSON.")
    parser.add_argument(
        "--postman-environment",
        type=Path,
        default=None,
        help="Optional Postman environment JSON used to resolve ${var} auth credentials to concrete values.",
    )
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
    workspace_root_env = os.environ.get("SOAVIRT_WORKSPACE_ROOT")
    workspace_root_default = Path(workspace_root_env) if workspace_root_env else None
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=workspace_root_default,
        help=(
            "Optional local SOAtest workspace root used for Basic-auth profile normalization updates. "
            "Set with --workspace-root or SOAVIRT_WORKSPACE_ROOT."
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

    info = collection.get("info") if isinstance(collection.get("info"), dict) else {}
    collection_name = str(info.get("name", "Postman Collection"))
    target_name = args.target_test_file_name or collection_name

    root_default = AuthSpec(mode="noauth", auth_type="noauth")
    collection_raw = parse_auth_object(collection.get("auth")) if "auth" in collection else AUTH_HANDLERS["inherit"].parse({})
    collection_effective = resolve_effective_auth(collection_raw, root_default)

    if args.postman_environment is not None and not args.postman_environment.exists():
        print(
            f"ERROR: Postman environment file does not exist: {args.postman_environment}",
            file=sys.stderr,
        )
        return 1
    environment_values: Dict[str, str] = {}
    if args.postman_environment is not None:
        environment_values = load_postman_environment_values(args.postman_environment)

    request_targets: List[RequestAuthTarget] = []
    walk_postman_requests_with_auth(
        items=collection.get("item"),
        folder_path=(),
        parent_effective=collection_effective,
        targets=request_targets,
    )
    if environment_values:
        request_targets = [
            RequestAuthTarget(
                folder_path=target.folder_path,
                request_name=target.request_name,
                effective_auth=resolve_auth_spec_with_environment(target.effective_auth, environment_values),
                source_auth_type=target.source_auth_type,
            )
            for target in request_targets
        ]

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
        mode_counts, unsupported_count, update_failures, warnings = apply_auth(
            client=client,
            tst_id=tst_id,
            request_targets=request_targets,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    print(f"Target .tst: {tst_id}")
    print(f"Requests evaluated: {len(request_targets)}")
    print(f"Basic auth applied: {mode_counts['basic']}")
    print(f"Digest auth applied: {mode_counts['digest']}")
    print(f"NTLM auth applied: {mode_counts['ntlm']}")
    print(f"AWS Signature auth applied: {mode_counts['awsv4']}")
    print(f"API Key auth mapped: {mode_counts['apikey']}")
    print(f"Bearer auth mapped: {mode_counts['bearer']}")
    print(f"OAuth 2.0 requiring template fallback: {mode_counts['oauth2']}")
    print(f"JWT Bearer requiring template fallback: {mode_counts['jwt']}")
    print(f"No Auth/disabled applied: {mode_counts['noauth']}")
    print(f"Unsupported auth entries skipped: {unsupported_count}")
    print(f"Auth update failures: {update_failures}")

    # Normalize generated YAML to one shared Basic profile using environment variables.
    # This keeps authName references stable and avoids many duplicated Basic N profiles.
    if (
        mode_counts["basic"] > 0
        and mode_counts["digest"] == 0
        and mode_counts["ntlm"] == 0
        and mode_counts["awsv4"] == 0
        and mode_counts["bearer"] == 0
        and mode_counts["oauth2"] == 0
        and mode_counts["jwt"] == 0
    ):
        if args.workspace_root is None:
            warnings.append(
                "Skipped Basic auth profile normalization: no workspace root provided. "
                "Use --workspace-root or SOAVIRT_WORKSPACE_ROOT."
            )
        elif not args.workspace_root.exists() or not args.workspace_root.is_dir():
            warnings.append(
                f"Skipped Basic auth profile normalization: workspace root is invalid: {args.workspace_root}"
            )
        else:
            local_tst = resolve_local_tst_path(args.workspace_root, args.parent_id, target_name)
            if local_tst.exists():
                changed = normalize_basic_auth_profiles_in_file(local_tst)
                if changed:
                    print(f"Normalized Basic auth profiles in: {local_tst}")
            else:
                warnings.append(f"Could not normalize Basic auth profiles; file not found: {local_tst}")

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

