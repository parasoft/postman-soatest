---
name: postman-http-options-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when Postman requests define HTTP version or automatic redirect behavior that must be applied to existing SOAtest REST Clients in a generated .tst.
---

# Postman HTTP Options Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill maps Postman request HTTP options into SOAtest REST Client HTTP options for an existing `.tst`.

Current coverage:
- HTTP version (`Auto`, `HTTP/1.x`)
- Automatically follow redirects

## Mapping
Scans:
- `protocolProfileBehavior` at collection, folder, and request levels

Applies:
- Inherited effective settings per request
- Request-level overrides when present

Converts:
- `followRedirects`/`disableRedirects` -> `generalSettings.followHttpRedirects`
- `requestVersion`/`httpVersion`/`protocolVersion`:
  - `Auto`: keep current SOAtest transport unchanged
  - `HTTP/1.x`: set HTTP/1 transport
  - `HTTP/2`: not supported in REST Client conversion; emits warning and downgrades to HTTP/1

Unsupported behavior:
- HTTP/2 is not supported in this converter's REST Client conversion path. HTTP/2 requests are downgraded to HTTP/1 with explicit warnings.

## Important Note (HTTP/2 Parity)
- Postman can declare HTTP/2 at request settings level.
- This step applies transport updates through REST Client mappings only.
- Equivalent HTTP/2 behavior in SOAtest requires a Messaging Client path with the Parasoft HTTP/2 Transport Extension.
- HTTP/2 warning output is expected and indicates intentional downgrade behavior, not silent conversion success.


## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_http_options_implementation.py "path/to/collection.postman_collection"
```

Optional:
- `--target-test-file-name`
- `--parent-id`
- `--api-base`
- `--username`
- `--password`
- `--timeout-sec`

