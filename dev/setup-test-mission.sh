#!/usr/bin/env bash
# Create a 1km × 1km test mission centered at Eric's home location.
#
# Usage:
#   ./dev/setup-test-mission.sh                          # defaults to localhost:8000
#   ./dev/setup-test-mission.sh https://abc.ngrok.app    # against an ngrok tunnel
#
# Prints the join_code and bearer_token so you can paste them into the app.
set -euo pipefail

SERVER="${1:-http://localhost:8000}"

# Center (your current location).
CENTER_LAT=36.99866
CENTER_LON=-122.06598

# 1km box → ~0.00449° lat × ~0.00562° lon at this latitude
# (1km / 111320 = 0.00898°; lon scaled by cos(37°) ≈ 0.7986)
HALF_LAT=0.00449
HALF_LON=0.00562

MIN_LAT=$(python3 -c "print(${CENTER_LAT} - ${HALF_LAT})")
MAX_LAT=$(python3 -c "print(${CENTER_LAT} + ${HALF_LAT})")
MIN_LON=$(python3 -c "print(${CENTER_LON} - ${HALF_LON})")
MAX_LON=$(python3 -c "print(${CENTER_LON} + ${HALF_LON})")

PLS_TS=$(date +%s)

read -r -d '' BODY <<JSON || true
{
  "name": "Test mission — Eric (1km box)",
  "subject_description": "Solo hiker, last seen near campus, wearing dark jacket",
  "pls_lat": ${CENTER_LAT},
  "pls_lon": ${CENTER_LON},
  "pls_ts": ${PLS_TS},
  "area_geojson": {
    "type": "Polygon",
    "coordinates": [[
      [${MIN_LON}, ${MIN_LAT}],
      [${MAX_LON}, ${MIN_LAT}],
      [${MAX_LON}, ${MAX_LAT}],
      [${MIN_LON}, ${MAX_LAT}],
      [${MIN_LON}, ${MIN_LAT}]
    ]]
  },
  "display_name": "Eric",
  "callsign": "E1"
}
JSON

echo "POST ${SERVER}/missions"
echo "  center  = (${CENTER_LAT}, ${CENTER_LON})"
echo "  bbox    = lat[${MIN_LAT}, ${MAX_LAT}]  lon[${MIN_LON}, ${MAX_LON}]"
echo

RESPONSE=$(curl -sS -X POST "${SERVER}/missions" \
  -H "Content-Type: application/json" \
  -d "${BODY}")

echo "${RESPONSE}" | python3 -m json.tool

# Pull out the join code and token for easy copy-paste.
JOIN_CODE=$(echo "${RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('join_code',''))")
TOKEN=$(echo "${RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('bearer_token',''))")
MID=$(echo "${RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mission_id',''))")

if [[ -n "${JOIN_CODE}" ]]; then
  echo
  echo "─────────────────────────────────────────────────"
  echo "  mission_id:  ${MID}"
  echo "  join_code:   ${JOIN_CODE}"
  echo "  token:       ${TOKEN}"
  echo "─────────────────────────────────────────────────"
fi
