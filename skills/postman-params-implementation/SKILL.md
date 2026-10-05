---
name: postman-params-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when implementing Postman URL query key/value parameters into existing SOAtest REST Client tests after endpoint sequence creation.
---

# Postman Params Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill converts Postman URL query parameters into SOAtest REST Client URL query parameters for an existing generated `.tst`.

## Conversion Rules
- Reads query entries from `request.url.query` for every request.
- Maps Postman `key` -> SOAtest query parameter name.
- Maps Postman `value` -> SOAtest query parameter value.
- Ignores Postman query parameter descriptions.
- Converts Postman variable syntax `{{variableName}}` to SOAtest syntax `${variableName}`.
- Preserves request-to-REST Client mapping and request order from the generated `.tst`.

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_params_implementation.py "path/to/collection.postman_collection"
```

Optional:
- `--target-test-file-name`
- `--parent-id`
- `--api-base`
- `--username`
- `--password`
- `--timeout-sec`

## Notes
- The script updates REST Client URLs in an already-created `.tst` and does not create a second test file.
- Requests without a `request.url.query` list are skipped without failing conversion.

