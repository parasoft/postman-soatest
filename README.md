# Postman to SOAtest Converter

## Table of Contents

- [Overview](#overview)
- [Prerequisites](#prerequisites)
- [Integrating Skills Into Your Project](#integrating-skills-into-your-project)
- [Running the Converter](#running-the-converter)
- [Skills Overview](#skills-overview)
- [Orchestrator Workflow](#orchestrator-workflow)
- [Current Limitations](#current-limitations)

## Overview

This repository contains an AI agent skill that converts exported Postman collections into SOAtest .tst files. It creates the SOAtest test scenario structure, then applies conversion phases to handle URL parameters, environment variables, authentication, headers, request payloads, scripts, notes, and HTTP options.

## Prerequisites

- Python 3.10+ installed
- Parasoft SOAtest & Virtualize installed
- SOAtest server running (default API base: `http://localhost:9080/soavirt/api/v6`)

## Integrating Skills Into Your Project

### Option A: Use Copilot inside SOAtest

Copy the entire `skills` folder into your SOAtest project (e.g. TestAssets) at the subfolder `.github/skills`. This puts the conversion skill into a location where Copilot in Eclipse can discover and use it.

### Option B: Use from an external AI client

Copy the `skills` folder into an appropriate location where your AI client discovers skills. Alternatively, clone this repository and open your AI coding client from the repository root so the client can discover skills in this project.

## Running the Converter

First export the collections you wish to convert and their associated environments (if needed) from Postman. Then ask your AI agent to use the `postman-to-soatest` skill and provide all required inputs:

- Postman Collection path
- Postman Environment path (only when required by the collection)
- SOAtest workspace root
- SOAtest parent folder (for example `/TestAssets`)
- Output `.tst` name

Example prompt template:

```text
Invoke postman-to-soatest:
Collection: <collection-path>
Environment: <environment-path>
SOAtest workspace: <workspace-root>
Parent folder: /TestAssets
Output: <output-name>
```

Notes:

- If required inputs are missing, conversion stops with a clear error.
- If the requested output name already exists, the converter automatically makes the name unique by appending a suffix.

## Skills Overview

`postman-to-soatest` is the entry point for conversion. All other skills are helper skills.

| Skill | Purpose |
|---|---|
| `postman-to-soatest` | Orchestrates the complete Postman-to-SOAtest conversion workflow. |
| `postman-endpoint-sequence` | Creates the initial `.tst` file and the sequence of API calls from the collection/folder/request sequence. |
| `postman-params-implementation` | Configures query parameters within API calls. |
| `postman-variable-implementation` | Imports external variables and values into the SOAtest environment. |
| `postman-auth-implementation` | Maps Postman authentication settings to SOAtest authentication profiles and request-level auth configuration. |
| `soatest-template-fallback` | Does `.tst` level editing for features that are not supported by the SOAtest REST API. |
| `postman-header-implementation` | Configures explicit request headers. |
| `postman-request-body-implementation` | Configures request body modes. |
| `postman-script-implementation` | Converts Postman scripts to equivalent SOAtest tools (for example Data Banks and Assertors). |
| `postman-notes-implementation` | Maps collection, folder, and request documentation into SOAtest notes/comments. |
| `postman-http-options-implementation` | Configures supported HTTP option settings. |

## Orchestrator Workflow

1. Run the `postman-to-soatest` entry point skill with the collection and environment exported from Postman.
2. Build the base SOAtest endpoint/test structure.
3. Use the SOAtest REST API to configure URL params, variables, and authentication.
4. Fallback checkpoint: if any supported auth settings cannot be configured through the SOAtest REST API, `.tst` file editing is applied.
5. Apply SOAtest REST API-based conversion for headers, request payloads, and scripts.
6. Fallback checkpoint: when cases are detected that cannot be configured through the SOAtest REST API, `.tst` file editing is applied.
7. Apply notes and HTTP options.
8. Run final validation to ensure all required phases completed successfully.
9. Generate the final SOAtest `.tst` file.

## Current Limitations

- HTTP/2 is not supported. REST Clients are configured with HTTP/1 and warnings are logged.
- The Postman auth types OAuth 1.0, Hawk, and ASAP (Atlassian) are not supported.
- Scripts are omitted when no equivalent SOAtest functionality exists.
