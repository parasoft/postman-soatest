---
name: postman-variable-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when implementing Postman external/pre-declared variables into an existing SOAtest .tst environment after endpoint sequence creation.
---

# Postman Variable Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill discovers variables from a Postman Collection and applies only external/pre-declared variables to an existing SOAtest `.tst` environment.

## Variable Coverage
Scans:
- Request URLs
- Request bodies
- Headers
- Authentication sections
- Pre-request scripts
- Test scripts

Classifies:
- `external/pre-declared`
- `script-created`
- `both referenced and script-created`

## Implementation Rule
Only `external/pre-declared` variables are created/updated in SOAtest environment variables.
- Variable names are preserved exactly.
- Values are resolved in this order:
  1. Exported Postman environment JSON value (`--postman-environment`, when provided)
  2. Collection-level Postman variable value (`collection.variable[]`)
  3. `SET_ME` fallback (or `--placeholder`)
- Collection-level Postman variables are imported automatically into the SOAtest environment under the root test suite.

`script-created` and `both` variables are discovered and summarized in terminal output in this phase.

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_variable_implementation.py "path/to/collection.postman_collection"
```

Optional:
- `--postman-environment`
- `--target-test-file-name`
- `--parent-id`
- `--environment-name`
- `--placeholder`
- `--api-base`
- `--username`
- `--password`
- `--timeout-sec`

## Notes
- If `--postman-environment` is omitted, the script continues and prints a note that no Postman environment file was provided.

