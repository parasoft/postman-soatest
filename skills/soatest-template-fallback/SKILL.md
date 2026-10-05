---
name: soatest-template-fallback
description: Internal phase of the postman-to-soatest orchestrator. Use when SOAVirt API cannot reliably persist a required conversion detail and a template-driven plain-text .tst fallback must patch valid SOAtest content.
---

# SOAtest Template Fallback

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill provides a template-driven plain-text `.tst` fallback layer.

Primary rule:
1. Use SOAVirt API first for discovery, mapping, and normal updates.
2. If an aspect cannot be handled reliably via API, patch the generated `.tst` from a vetted template.

Modes:
- `auth`: digest/ntlm/awsv4/oauth2/jwt/noauth auth fallback.
- `difference-assertions`: inserts Difference assertions from a built-in vetted template and aligns request endpoint/query mapping.
- `binary-body`: copies binary request-body literal/file blocks from a built-in vetted template and injects request-specific source/content-type values.
- `both`: applies auth and Difference fallback in one run.
- `all`: applies auth + Difference + binary-body fallback in one run.

## Purpose
- Produce valid, importable SOAtest `.tst` content when API support is missing or unreliable.
- Preserve parity for unsupported Difference assertion types by copying vetted template assertion blocks.
- Keep fallback scoped to unsupported or non-persistent conversion aspects.

## Reference Files (Learning/Validation Only)
The following files are used only to learn structure and validate parity. They are not hardcoded runtime dependencies.

- Digest Postman environment: `artifacts/Boa.postman_environment.json`
- Digest Postman collection: `artifacts/Digest.postman_collection.json`
- Digest SOAtest sample: `artifacts/PR68847_HTTP_Digest_Auth.tst`
- NTLM Postman environment: `artifacts/goldfish.postman_environment.json`
- NTLM Postman collection: `artifacts/NTLM.postman_collection.json`
- NTLM SOAtest sample: `artifacts/NTLMtest.tst`
- AWS Postman environment: `artifacts/Boa.postman_environment.json`
- AWS Postman collection: `artifacts/AwsSignatureAuth.postman_collection.json`
- AWS SOAtest sample: `artifacts/AwsSignatureAuth.tst`
- OAuth2 Postman environment: `artifacts/cargo.postman_environment.json`
- OAuth2 Postman collection: `artifacts/OAuth2.0.postman_collection.json`
- OAuth2 SOAtest sample: `artifacts/OAuth2Example - Copy.tst`
- JWT Bearer Postman environment: `artifacts/cargo.postman_environment.json`
- JWT Bearer Postman collection: `artifacts/JWTBearerAuth.postman_collection.json`
- JWT Bearer SOAtest sample: `artifacts/JWTBearerAuthConversion.tst`
- Difference assertions Postman collection: `artifacts/fallbackAssertions.postman_collection.json`
- Difference assertions SOAtest template: `artifacts/fallbackAssertions.tst`
- Binary body Postman environment: `artifacts/BinaryBodyEnv.postman_environment.json`
- Binary body Postman collection: `artifacts/BinaryBodyMappingCheck.postman_collection.json`
- Binary body SOAtest template: `artifacts/BinaryBodyMappingCheckConversion.tst`

## Current Scope
- Auth fallback for REST Clients when API auth updates do not persist (digest, ntlm, awsv4, oauth2, jwt, noauth behavior).
- When an exported Postman OAuth2 auth block includes an `accessToken` value (for example, a synced/shared token), the fallback injects an explicit request-level `Authorization: Bearer <token>` HTTP header on the affected REST Client to match Postman’s “use current token” behavior.
- For Postman `jwt` auth blocks, fallback generates an HMAC-signed JWT from configured payload/secret/algorithm values and injects it as request-level `Authorization: Bearer <jwt>` (manual header path in SOAtest). Supported JWT signing algorithms are HS256, HS384, and HS512 only.
- OAuth2 token overrides remain request-level and do not create separate OAuth2 auth profiles unless base OAuth2 config values (token URL/client/grant/scope/audience/add-token-target) differ.
- Difference assertion fallback for unsupported SOAVirt API assertion types:
  - Numeric Difference Assertion
  - Date Difference Assertion
  - Date Time Difference Assertion
- For Difference fallback requests, template endpoint/query mapping is also propagated (`HTTPClient_Endpoint` and `urlParameters`) so assertion target elements can exist at runtime.
- For binary-body fallback requests, template `literal` file-attachment structure (including content type and file location fields) is copied onto matching REST Clients.

## Internal Script (Maintainers Only)
Run directly:

```bash
python scripts/soatest_template_fallback.py "path/to/collection.postman_collection.json" --workspace-root "path/to/soatest_workspace" --target-test-file-name "MyConversion"
```

Optional:
- `--mode` (`auth`, `difference-assertions`, `binary-body`, `both`, `all`)
- `--workspace-root` (required unless `SOAVIRT_WORKSPACE_ROOT` is set)
- `--parent-id`
- `--target-test-file-name`

This script patches an already generated `.tst` in place. It does not create a new `.tst` by itself.

## Fallback Workflow
1. Run API auth conversion first (`postman-auth-implementation`).
2. If auth persistence fails, run template fallback in `auth` mode (orchestrator does this automatically in Step 4b, including OAuth 2.0 failures).
3. Run script conversion (`postman-script-implementation`).
4. If binary bodies are detected, run template fallback in `binary-body` mode (orchestrator auto-runs this as Step 6b).
5. If Difference assertions are detected, run template fallback in `difference-assertions` mode (orchestrator auto-runs this as Step 7b).
6. Re-open and verify the patched `.tst` in SOAtest.

## Template Rules
- Use vetted templates only.
- Match REST Client tool names between generated `.tst` and template `.tst`.
- For Difference fallback, template blocks must include:
  - `DifferenceAssertion` content
  - request endpoint mapping (`HTTPClient_Endpoint`)
  - request query mapping (`urlParameters`) when assertions depend on query-driven response fields
- Replace only targeted nodes/fields.
- Preserve unrelated structure, ordering, IDs, and tool configuration.
- Skip unknown patterns rather than writing risky output.

## Validation Checklist
1. Resulting `.tst` opens/imports in SOAtest.
2. Required auth profiles (digest/ntlm/aws/oauth2) exist when auth mode is used, and JWT requests receive explicit `Authorization` headers.
3. Difference assertions appear on intended REST Clients when difference mode is used.
4. Request traffic contains the fields referenced by assertions (for example `endDate`, `endTs`).
5. Runtime smoke checks match expected statuses and assertion outcomes.

## Orchestrator Integration
Recommended order via orchestrator:
1. `postman-auth-implementation`
2. `soatest-template-fallback` auth mode (auto-runs when Step 4 reports auth persistence failures, including OAuth 2.0 and JWT Bearer)
3. `postman-script-implementation`
4. `soatest-template-fallback` binary-body mode (auto-run by orchestrator Step 6b when binary bodies are detected)
5. `soatest-template-fallback` difference mode (auto-run by orchestrator Step 7b when difference assertions are detected)

## Notes
- API-first remains mandatory; template fallback is a controlled exception path.
- Difference assertion parity depends on response data availability at runtime, not only assertion node presence.

