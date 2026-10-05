---
name: postman-endpoint-sequence
description: Internal phase of the postman-to-soatest orchestrator. Use when creating a new SOAtest .tst from a Postman Collection while preserving only collection structure and request flow.
---

# Postman Endpoint Sequence

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill creates a brand-new SOAtest `.tst` from an exported Postman Collection.

It preserves:
- Collection name
- Folder hierarchy
- Request order
- Request name
- HTTP method
- Request URL

It does not create SOAtest environment variables.

## Output Mapping
- Postman collection name -> SOAtest `.tst` file name
- Postman folders -> SOAtest `TestSuite`
- Postman requests -> SOAtest `RESTClientToolTest`
- Postman request method + URL -> REST Client method + endpoint
- Postman `{{var}}` references in endpoint text -> SOAtest `${var}` syntax

## Guardrails
- The skill prompts for a `.tst` test file name when one is not explicitly provided.
- If a same-name `.tst` already exists, the tool never overwrites or renames automatically; it prompts and waits for the user to provide a different name or explicitly confirm overwrite when eligible.
- Cleanup only removes `.tst` files generated during the same script runtime.
- Skip unsupported or extra Postman fields without failing.

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/create_endpoint_sequence.py "path/to/collection.postman_collection" --workspace-root "path/to/soatest_workspace"
```

Optional overrides:
- `--test-file-name`
- `--api-base`
- `--parent-id`
- `--workspace-root` (required)
- `--username`
- `--password`
- `--timeout-sec`

