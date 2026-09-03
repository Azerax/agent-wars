#!/usr/bin/env bash
#
# Deploy WAF rate limiting rules for mcpagentwars.com.
#
# The app already rate limits itself, per bearer key and per address, and that
# is what protects game state and CPU. This is the layer in front: it drops a
# volumetric flood at Cloudflare's edge, before a Worker is invoked and before
# anything is billed. The two do different jobs and you want both.
#
# The thresholds here are deliberately far looser than the in-app limits. This
# rule should never fire for anybody playing the game, however enthusiastically
# — it exists for traffic that is obviously not playing.
#
# Wrangler's OAuth token cannot do this: it carries `zone (read)`. You need a
# token with **Zone → WAF → Edit** on this zone. Create one at
#   https://dash.cloudflare.com/profile/api-tokens
# then run:
#
#   export CLOUDFLARE_API_TOKEN=...      # your shell only; nothing else reads it
#   ./scripts/waf-rate-limits.sh
#
# Re-running replaces the ruleset with exactly what is below, so edit the
# numbers here rather than in the dashboard if you want this to stay the
# source of truth.

set -euo pipefail

ZONE_NAME="${ZONE_NAME:-mcpagentwars.com}"
: "${CLOUDFLARE_API_TOKEN:?Set CLOUDFLARE_API_TOKEN to a token with Zone:WAF:Edit}"

api() {
  curl -fsS -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" \
       -H "content-type: application/json" "$@"
}

echo "Looking up zone ${ZONE_NAME}…"
ZONE_ID=$(api "https://api.cloudflare.com/client/v4/zones?name=${ZONE_NAME}" \
  | python -c 'import json,sys; r=json.load(sys.stdin)["result"]; print(r[0]["id"] if r else "")')

if [ -z "${ZONE_ID}" ]; then
  echo "No zone called ${ZONE_NAME} on this token's account." >&2
  exit 1
fi
echo "  zone ${ZONE_ID}"

# `cf.colo.id` is required in characteristics on non-Enterprise plans: the
# counter is kept per data centre rather than globally, so the real threshold
# is roughly this number times the number of colos a caller reaches. That is
# another reason to set these generously rather than tightly.
read -r -d '' RULES <<'JSON' || true
{
  "rules": [
    {
      "description": "Agent traffic flood (mcp endpoint)",
      "expression": "starts_with(http.request.uri.path, \"/mcp/\")",
      "action": "block",
      "ratelimit": {
        "characteristics": ["ip.src", "cf.colo.id"],
        "period": 10,
        "requests_per_period": 300,
        "mitigation_timeout": 60
      }
    },
    {
      "description": "Seat claiming flood (join and register)",
      "expression": "http.request.uri.path eq \"/api/join\" or (starts_with(http.request.uri.path, \"/api/arena/\") and http.request.method eq \"POST\")",
      "action": "block",
      "ratelimit": {
        "characteristics": ["ip.src", "cf.colo.id"],
        "period": 10,
        "requests_per_period": 30,
        "mitigation_timeout": 60
      }
    },
    {
      "description": "Spectator API flood",
      "expression": "starts_with(http.request.uri.path, \"/api/\")",
      "action": "block",
      "ratelimit": {
        "characteristics": ["ip.src", "cf.colo.id"],
        "period": 10,
        "requests_per_period": 400,
        "mitigation_timeout": 30
      }
    }
  ]
}
JSON

echo "Deploying rate limiting rules…"
api -X PUT \
  "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/rulesets/phases/http_ratelimit/entrypoint" \
  --data "${RULES}" \
  | python -c '
import json,sys
d = json.load(sys.stdin)
if not d.get("success"):
    print("FAILED:", json.dumps(d.get("errors"), indent=2)); raise SystemExit(1)
for r in d["result"].get("rules", []):
    rl = r.get("ratelimit", {})
    print(f"  ok  {r[\"description\"]}: {rl.get(\"requests_per_period\")} per {rl.get(\"period\")}s per IP")
'

echo
echo "Done. Check them at:"
echo "  https://dash.cloudflare.com/?to=/:account/${ZONE_NAME}/security/security-rules"
echo
echo "Free plans allow one rate limiting rule and a 10s period only. If the"
echo "deploy above was rejected for that reason, keep just the first rule and"
echo "widen its expression to cover the whole site."
