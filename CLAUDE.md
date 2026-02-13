# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Reverse-engineered API documentation for the Hoval Connect IoT platform. This is a **documentation-only repo** — no build system, no tests, no package manager. The repo contains API docs (README.md) and two example clients.

Hoval Connect is a cloud platform connecting Hoval HVAC systems (heating, ventilation, hot water) via IoT gateways to Azure IoT Hub.

## Running Examples

```bash
# Python client (requires `requests` library)
python examples/hoval_client.py <email> <password>

# Bash script (requires curl + python3 for JSON parsing)
./examples/get-live-values.sh <email> <password> <plantId> <circuitPath> <circuitType>
```

No credentials are stored in the repo — always passed as arguments.

## Authentication Architecture (2-step)

1. **ID Token**: OAuth2 password grant to SAP IAS (`https://akwc5scsc.accounts.ondemand.com/oauth2/token`), client_id `991b54b2-7e67-47ef-81fe-572e21c59899`. Use `id_token` from response, NOT `access_token`. Lifetime: 30min.
2. **Plant Access Token (PAT)**: Fetch via `GET /v1/plants/{plantId}/settings` using the id_token. Returns JWT in `token` field. Send as `X-Plant-Access-Token` header. Lifetime: ~15min.

Both tokens are auto-refreshed with TTL caching in the Python client.

## API Base URL

`https://azure-iot-prod.hoval.com/core`

## Key Endpoint Patterns

- Endpoints prefixed with `/api/` or `/v1/` need only the id_token (`Authorization: Bearer`)
- Most `/v1/plants/`, `/v2/api/`, `/v3/` endpoints also require `X-Plant-Access-Token`
- `/business/` endpoints require elevated (partner) access — regular users get 403

## Circuit Types

HK (heating), BL (boiler), WW (warm water), FRIWA (fresh water), HV (ventilation), SOL (solar), SOLB (solar buffer), PS (pool), GW (gateway)

## Known Gaps

- Temperature history (`/v3/api/statistics/temperature/`) requires `datapoints` param — valid datapoint IDs not yet discovered
- Energy stats return empty for HV circuit (likely only relevant for HK/WW/SOL)
- `business/plants/{id}/plant-structure` needs business role
- Full OpenAPI 3.1 spec available at `/v3/api-docs` (~450KB, auth required)
