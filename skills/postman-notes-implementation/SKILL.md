---
name: postman-notes-implementation
description: Internal phase of the postman-to-soatest orchestrator. Use when mapping Postman collection/folder overview notes and request-level descriptions into SOAtest Test Suite Requirements and Notes after endpoint sequence creation.
---

# Postman Notes Implementation

## Overview

## Internal Only
This skill is an internal conversion phase. It is not a supported standalone conversion entry point. Run the orchestrator (`skills/postman-to-soatest/scripts/postman_to_soatest.py`) for all conversions.

This skill maps Postman collection/request documentation to SOAtest `Requirements and Notes`.

- Collection overview notes are written to the top/global Test Suite notes field.
- Folder overview notes are written to the corresponding Scenario/Test Suite notes field.
- Request docs are grouped by parent folder/suite and written into the corresponding suite notes section only when that suite has no folder overview note.
- Request-level descriptions are also mirrored into each request entry's Comment field in Requirements Tracking.

## Conversion Rules
- Reads collection overview from `info.description` first, then root-level `description` as fallback.
- Reads folder overview from folder-level `description`.
- Reads request description from `request.description` first, then item-level `description` as fallback.
- Preserves request order inside each suite notes block.
- Converts Postman variable syntax `{{var}}` to SOAtest syntax `${var}`.
- Writes plain Postman note text only (no generated markers or wrapper text).

## Internal Script (Maintainers Only)
Run:

```bash
python scripts/postman_notes_implementation.py "path/to/collection.postman_collection"
```

Optional:
- `--target-test-file-name`
- `--parent-id`
- `--api-base`
- `--username`
- `--password`
- `--timeout-sec`

## Notes
- This skill updates existing `.tst` suites and does not create a new test file.
- This phase only maps collection/request notes; it does not convert assertions, scripts, or response validations.



