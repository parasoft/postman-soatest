---
name: postman-to-soatest
description: Use when running the full Postman-to-SOAtest workflow by orchestrating endpoint sequence, params, variable, auth, required auth-persistence template fallback, header, body, script, notes, and HTTP options implementation skills.
---

# Postman to SOAtest

## Overview
This is the orchestration skill.
This is the only supported conversion entry point. Implementation skills are internal phases and must not be run directly for full conversions.

It runs the workflow in order:
1. `postman-endpoint-sequence`
2. `postman-params-implementation`
3. `postman-variable-implementation`
4. `postman-auth-implementation`
5. `soatest-template-fallback` (auth mode; auto-runs whenever Step 4 persistence failures are detected, including OAuth 2.0 and JWT Bearer)
6. `postman-header-implementation`
7. `postman-request-body-implementation`
8. `soatest-template-fallback` (binary-body mode; auto-runs as Step 6b when binary bodies are detected)
9. `postman-script-implementation`
10. `soatest-template-fallback` (difference mode; auto-runs as Step 7b when difference assertions are detected)
11. `postman-notes-implementation`
12. `postman-http-options-implementation`
13. End-to-end postflight validation (required before success)

Use this when you want a single command that builds the `.tst` sequence and applies supported mappings end-to-end.

## Python Precheck (Fail Fast)
Before running anything else, verify Python is available on the system `PATH`:

```bash
python --version
```

If this command fails, stop immediately and show this message (no discovery/fallback attempts):

- `ERROR: Python could not be found on PATH. Install Python and ensure 'python' is available on PATH, then run the skill again.`

Execution rule:
- Do **not** attempt to locate Python via absolute paths, environment-specific caches, or additional fallback discovery when `python --version` fails.
- Preserve existing behavior when `python --version` succeeds.

## SOAtest Server Precheck (Fail Fast)
Before running conversion phases, verify SOAtest server API reachability using the configured `--api-base` and `--parent-id`.

If the server is unavailable, stop immediately with user-facing guidance:
- `ERROR: Unable to connect to the SOAtest server. The converter requires the local SOAtest/SOAVirt server to be running before a conversion can begin. Start the SOAtest server (which hosts the SOAVirt REST API), or verify --api-base for the correct host/port.`

Execution rule:
- Do **not** print Python invocation/debugging instructions for server-unavailable failures.
- Do **not** continue to endpoint sequence or other phases when this precheck fails.

## Script
After the Python and server prechecks pass, run:

```bash
python skills/postman-to-soatest/scripts/postman_to_soatest.py "path/to/collection.postman_collection.json" --postman-environment "path/to/environment.postman_environment.json" --workspace-root "path/to/soatest_workspace" --test-file-name "MyConversion"
```

Required:
- `--workspace-root`
- `--test-file-name`

Conditionally required:
- `--postman-environment` when the collection contains Postman variable references (for example `{{BASE_URL}}`) that need external values.

Optional:
- `--api-base`
- `--parent-id`
- `--form-data-file-mode` (`variable` default, `placeholder` legacy)
- `--environment-name`
- `--username`
- `--password`
- `--timeout-sec`

## Expected Warnings (Important)
Common warning classes that can appear in successful runs:
- **Auth persistence warnings** during the authorization phase (`oauth2`/`jwt`) that are then resolved by auth template fallback.
- **Collection/folder script omitted warnings** during script conversion when non-request script logic has no guaranteed native mapping.
- **HTTP/2 downgraded warnings** during HTTP options conversion when HTTP/2 is detected and intentionally converted as HTTP/1 in REST Client output.
- **Binary request-body manual-setup warnings** during body conversion when Postman `mode=file` is detected but REST payload file-attachment fields are unavailable via SOAVirt API.
- **Binary body template fallback warnings** during Step 6b when fallback cannot locate a target literal block anchor or has limited patch context.

## Notes
- The orchestrator is non-interactive for naming: `--test-file-name` is required on every run and conversion starts only when all required inputs are provided.
- It must never overwrite an existing `.tst`; if a name already exists, the workflow auto-selects a unique numeric-suffix name and continues.
- Name-collision resolution runs once in orchestrator preflight; internal phases use the pre-resolved unique name and do not re-check existence.
- If `--postman-environment` is missing and collection variable references are detected, the workflow fails immediately with a clear error.
- OAuth2 request-level current-token overrides from Postman are preserved as explicit request `Authorization` headers during auth fallback.
- JWT Bearer auth from Postman is mapped through auth fallback as explicit request `Authorization` headers generated from JWT config.
- JWT signing support in fallback is currently limited to `HS256`, `HS384`, and `HS512`.
- Script conversion is native-first; unsupported script logic is omitted with warnings.
- If the selected target `.tst` already exists, the workflow auto-selects a unique numeric-suffix name and continues.
- Endpoint-sequence cleanup deletes only `.tst` files generated during the same runtime.
- For Difference fallback, the template contributes assertion blocks and endpoint/query mapping for matched requests.
- Postman runs may pass due to local script constants while SOAtest assertions evaluate response fields; runtime data availability still matters.


