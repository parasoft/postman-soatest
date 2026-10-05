---
name: postman-auth-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when implementing Postman authorization settings into existing SOAtest REST Client tests after endpoint sequence creation.
---

# Postman Authorization Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill converts Postman authorization settings into SOAtest REST Client authentication settings for an existing generated `.tst`.

Supported in this phase:
- `basic`
- `digest`
- `ntlm`
- `awsv4` (AWS Signature / SigV4)
- `apikey` (Postman API Key)
- `bearer` (mapped to explicit REST Client `Authorization` header)
- `oauth2` (detected/reported as persistence failure; handled by template fallback)
- `jwt` (detected/reported as persistence failure; handled by template fallback)
- `inherit` (Inherit Auth from Parent)
- `noauth`

## Authorization Resolution
The skill resolves effective authorization in hierarchy order:
1. Collection-level auth
2. Folder-level auth
3. Request-level auth

Rules:
- `inherit` resolves to the nearest parent auth.
- `noauth` explicitly disables auth for that request.
- `basic`, `digest`, `ntlm`, and `awsv4` map to SOAtest REST Client auth settings.
- `apikey` maps to explicit REST Client request data (manual auth path):
  - `in: header` (or omitted) -> HTTP header `<key>: <value>`
  - `in: query` -> URL query parameter `<key>=<value>`
  - Postman variable syntax in key/value is converted from `{{var}}` to `${var}`
- `bearer` maps to explicit request header `Authorization: Bearer <token>` on the target REST Client and disables SOAtest `performAuthentication` for that request.
- `oauth2` and `jwt` are reported as authentication persistence failures in Step 4 so orchestrator fallback runs consistently.
- Request-level Postman OAuth2 current-token overrides (`oauth2.accessToken`) are preserved by fallback as explicit request `Authorization` headers.
- Postman JWT Bearer settings are delegated to template fallback, which generates a signed JWT and applies `Authorization: Bearer <jwt>` at request level. Currently supported JWT signing algorithms are HS256, HS384, and HS512 only.
- Unsupported auth types other than `oauth2`/`jwt` are skipped without failing conversion.

## AWS Signature Mapping (Postman -> SOAtest)
- `auth.type: awsv4` -> SOAtest `AwsSignatureAuthentication`
- `awsv4.accessKey` -> `config.accessKeyId.fixedValue.value`
- `awsv4.secretKey` -> `config.secretAccessKey.fixedValue.password`
- `awsv4.sessionToken` -> `config.sessionToken.fixedValue.value` (when present)
- `awsv4.service` -> `config.service.fixedValue.value` (when present)
- `awsv4.region` -> `config.region.fixedValue.value` (when present)
- `awsv4.addAuthDataToRequest` containing `query` -> `config.authMode: 1` (query signing)
- otherwise, header signing is used (no `authMode` override).

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_auth_implementation.py "path/to/collection.postman_collection"
```

Optional:
- `--target-test-file-name`
- `--parent-id`
- `--postman-environment`
- `--workspace-root`
- `--api-base`
- `--username`
- `--password`
- `--timeout-sec`

## Notes
- Input requirement is only a Postman collection path; all other parameters are optional overrides.
- The script updates REST Client auth in the already-created `.tst` and does not create a second test file.

