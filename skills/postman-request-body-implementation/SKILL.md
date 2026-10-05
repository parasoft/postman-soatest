---
name: postman-request-body-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when implementing Postman request bodies into existing SOAtest REST Client tests after endpoint sequence creation.
---

# Postman Request Body Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill converts Postman request body content into SOAtest REST Client payload settings for an existing generated `.tst`.

Supported body modes in this phase:
- `none`
- `raw`
- `form-data`
- `x-www-form-urlencoded`
- `graphql`
- `file` (Postman binary request body)

## Conversion Rules
- Preserves request order and request-to-REST Client mapping from the generated `.tst` structure.
- Converts Postman variable syntax `{{variableName}}` to SOAtest variable syntax `${variableName}` in body content.
- Ignores disabled `form-data` and `x-www-form-urlencoded` fields.
- Preserves as much body information as possible and emits warnings when exact representation is not available.
- Detects Postman `file`/binary request bodies and preserves source metadata, but marks them for manual setup because SOAVirt REST API does not expose native REST binary file-attachment payload fields.

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_request_body_implementation.py "path/to/collection.postman_collection"
```

Optional:
- `--target-test-file-name`
- `--parent-id`
- `--form-data-file-mode` (`variable` default, `placeholder` legacy)
- `--api-base`
- `--username`
- `--password`
- `--timeout-sec`

## Notes
- The script updates REST Client request payloads in an already-created `.tst` and does not create a second test file.
- The script prints per-run body conversion findings and warnings directly to terminal output.
- For `form-data` file fields, default behavior maps each file field to a `${...FilePath}` variable and prints setup hints.
- For Postman binary (`mode: file`) request bodies, the tool records source path/variable and emits a manual follow-up hint for SOAtest payload configuration.
- This phase does not convert assertions, response validation, scripts, or databanking.

