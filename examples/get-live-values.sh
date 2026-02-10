#!/bin/bash
# Hoval Connect API - Get Live Values Example
# Usage: ./get-live-values.sh <email> <password> <plantId> <circuitPath> <circuitType>

set -e

EMAIL="${1:?Usage: $0 <email> <password> <plantId> <circuitPath> <circuitType>}"
PASSWORD="${2:?}"
PLANT_ID="${3:?}"
CIRCUIT_PATH="${4:?}"
CIRCUIT_TYPE="${5:?}"

BASE="https://azure-iot-prod.hoval.com/core"
CLIENT_ID="991b54b2-7e67-47ef-81fe-572e21c59899"
IDP="https://akwc5scsc.accounts.ondemand.com/oauth2/token"

# Step 1: Get ID token
echo "Authenticating..."
TOKEN_RESP=$(curl -s -X POST "$IDP" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&client_id=$CLIENT_ID&username=$EMAIL&password=$PASSWORD&scope=openid")

ID_TOKEN=$(echo "$TOKEN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['id_token'])")

# Step 2: Get Plant Access Token
echo "Getting plant access token..."
PAT=$(curl -s "$BASE/v1/plants/$PLANT_ID/settings" \
  -H "Authorization: Bearer $ID_TOKEN" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# Step 3: Get live values
echo "Fetching live values for circuit $CIRCUIT_PATH ($CIRCUIT_TYPE)..."
curl -s "$BASE/v3/api/statistics/live-values/$PLANT_ID?circuitPath=$CIRCUIT_PATH&circuitType=$CIRCUIT_TYPE" \
  -H "Authorization: Bearer $ID_TOKEN" \
  -H "X-Plant-Access-Token: $PAT" | python3 -m json.tool
