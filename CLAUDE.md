# CLAUDE.md - Hoval Connect API Project Context

## What This Is
Reverse-engineered API documentation for the Hoval Connect IoT platform. Hoval makes HVAC systems (heating, ventilation, hot water) connected via IoT gateways to Azure.

## Auth Flow (2-step)
1. **ID Token**: OAuth2 password grant → `https://akwc5scsc.accounts.ondemand.com/oauth2/token` (client_id: `991b54b2-7e67-47ef-81fe-572e21c59899`). Use `id_token` not `access_token`.
2. **Plant Access Token**: `GET /v1/plants/{plantId}/settings` → returns JWT in `token` field. Required as `X-Plant-Access-Token` header for most plant endpoints.

Token lifetimes: id_token 30min, plant access token ~15min.

## API Base URL
`https://azure-iot-prod.hoval.com/core`

## Key Endpoints
- `/api/my-plants` — list plants (no PAT needed)
- `/v1/plants/{id}/circuits` — circuit list with details
- `/v3/api/statistics/live-values/{id}?circuitPath=X&circuitType=Y` — live sensor data
- `/v2/api/weather/forecast/{id}` — 4-day forecast
- `/v1/plant-events/{id}` — errors/events
- `/v3/api-docs` — full OpenAPI 3.1 spec (~450KB)

## Circuit Types
HK (heating), BL (boiler), WW (warm water), FRIWA (fresh water), HV (ventilation), SOL (solar), SOLB (solar buffer), PS (pool), GW (gateway)

## Project Structure
```
README.md              — Full API documentation
examples/
  hoval_client.py      — Python client with token caching
  get-live-values.sh   — Bash example script
```

## Development Notes
- No credentials in repo — examples take email/password as arguments
- Python client auto-refreshes both tokens with TTL caching
- OpenAPI spec available at runtime via `/v3/api-docs` (auth required)
- Some endpoints are partner/business-only (403 for regular users)
- `business/plants/{id}/plant-structure` needs business role
- Temperature history (`/v3/api/statistics/temperature/`) needs `datapoints` param — valid datapoint IDs not yet discovered
- Energy stats return empty for HV circuit (probably only relevant for HK/WW/SOL)
