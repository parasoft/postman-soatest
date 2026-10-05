---
name: postman-header-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when Postman requests include explicit custom HTTP headers that must be applied to existing SOAtest REST Clients in a generated .tst.
---

# Postman Header Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill maps explicit Postman request headers into SOAtest REST Client HTTP header configuration for an existing `.tst`.

It is intended for custom headers only. When request headers do not explicitly include `Authorization`, the step preserves any existing `Authorization` header already applied by auth implementation (for example Bearer mapping).

## Header Coverage
Scans:
- `request.header` for every Postman request

Converts:
- Enabled entries with non-empty `key`
- `{{variableName}}` syntax to `${variableName}`

Ignores:
- Disabled headers
- Entries with empty keys

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_header_implementation.py "path/to/collection.postman_collection"
```

Optional:
- `--target-test-file-name`
- `--parent-id`
- `--api-base`
- `--username`
- `--password`
- `--timeout-sec`

