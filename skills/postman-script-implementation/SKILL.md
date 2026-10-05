---
name: postman-script-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when a Postman collection contains pre-request/test scripts and you need intent-based conversion into native SOAtest tools while omitting unsupported script logic.
---

# Postman Script Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill converts Postman scripts into the closest SOAtest implementation.

It does not translate JavaScript line-by-line.
It recognizes script intent and applies native SOAtest tools first.
It does not emit placeholder or non-functional SOAtest script fallbacks.

## Conversion Policy
- Native-first conversion is always enforced.
- Uses native SOAtest tools when equivalent intent is detected.
- Omits unsupported script logic when no native equivalent exists.

## Phase Mapping
- Postman `prerequest` -> SOAtest pre-request stage (`Request Traffic` output provider)
- Postman `test` -> SOAtest post-request stage (`Response Traffic` output provider)

## Native Tool Mapping (Current)
- `pm.*.set("var", json...)` -> JSON Data Bank
- `pm.iterationData.get("var")` references -> SOAtest variable references (`${var}`) with environment defaults when missing
- `pm.expect(json...).to...` -> JSON Assertor (value, value occurrence, string comparison, numeric, occurrence, regex `match`/`not.match` including `new RegExp(...)`, plus structure assertions: has content, has children, and type; assertion names use `pm.test("...")` titles when available)
- `pm.expect(pm.response.code)...` and `pm.expect([codeA, codeB]).to.include(pm.response.code)` -> REST Client valid response codes
- `*.length` assertion patterns -> JSON Assertor occurrence assertions

## Scope Behavior (Important)
- Request-level scripts can map to native Data Bank/Assertor/REST-client options when patterns are supported.
- Collection-level and folder-level scripts are non-request scope; if no guaranteed native equivalent exists, they are omitted with warnings.
- This is expected behavior and prevents non-functional SOAtest output.

## Internal Script (Maintainers Only)-Created Variables
- JSON Data Banks write extracted values into data bank custom columns (`customColumn`) using script variable names.
- Request/assertion references to discovered script-created variables use standard SOAtest variable syntax (for example `${itemId}`) so downstream steps bind to databanked values.
- By default, this skill does **not** auto-create script-created variables in the SOAtest environment.
- This avoids placeholder environment values overriding databanked runtime flow values.
- For variables set only in pre-request scripts (and therefore not databanked from a response), the skill can add conservative environment defaults (for example `1` for numeric-looking names such as `*Id`/`*Quantity`) to prevent unresolved-variable failures.
- You can control this behavior with:
  - `--declare-script-created-env-vars` / `--no-declare-script-created-env-vars`
  - `--script-created-env-default-value`

## Internal Script (Maintainers Only) Output Policy
- Prefer native SOAtest tools over scripting whenever possible.
- Generate script output only when it is guaranteed executable in SOAtest.
- Never emit raw or minimally modified Postman `pm.*` runtime APIs into generated SOAtest output.
- If no native equivalent or guaranteed functional script implementation exists, omit the script logic and emit a warning.

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_script_implementation.py "path/to/collection.postman_collection.json"
```

Optional:
- `--target-test-file-name`
- `--api-base`
- `--parent-id`
- `--environment-name`
- `--declare-script-created-env-vars` / `--no-declare-script-created-env-vars`
- `--script-created-env-default-value`
- `--username`
- `--password`
- `--timeout-sec`

