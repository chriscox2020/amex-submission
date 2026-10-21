#!/usr/bin/env bash
# Render API deploy for amex-submission. NOT RUN YET - needs Chris's Render account,
# card on file, API key, and the GitHub repo connected to Render.
#   export RENDER_API_KEY=...   (from the secure form, never from chat)
#   export REPO=https://github.com/<owner>/amex-submission
set -euo pipefail
API=https://api.render.com/v1; H=(-H "Authorization: Bearer $RENDER_API_KEY" -H "Accept: application/json" -H "Content-Type: application/json")
step=${1:-help}
case $step in
owner)    curl -fsS "${H[@]}" "$API/owners?limit=20" ;;                       # -> OWNER_ID (tea-...)
create)   curl -fsS "${H[@]}" -X POST "$API/services" -d @- <<JSON
{"type":"web_service","name":"amex-submission","ownerId":"$OWNER_ID","repo":"$REPO","branch":"main","autoDeploy":"no",
 "serviceDetails":{"runtime":"python","plan":"starter","region":"ohio","healthCheckPath":"/healthz","numInstances":1,
  "envSpecificDetails":{"buildCommand":"pip install -r requirements.txt",
   "startCommand":"gunicorn app:app --workers 1 --threads 8 --bind 0.0.0.0:\$PORT --timeout 60"},
  "disk":{"name":"amex-data","mountPath":"/var/data","sizeGB":1}},
 "envVars":[{"key":"PYTHON_VERSION","value":"3.12.7"},{"key":"DATA_DIR","value":"/var/data"},
  {"key":"PUBLIC_BASE","value":"https://amex.coxresearch.com"},{"key":"TOKEN_SECRET","generateValue":true},
  {"key":"ADMIN_PASSWORD","generateValue":true},{"key":"EMAIL_MODE","value":"dryrun"},
  {"key":"RECORD_TO","value":"littledude@mail.grokbot.com"},{"key":"RECORD_CC","value":""},
  {"key":"MAIL_FROM","value":"Chris AI <littledude@coxresearch.com>"},{"key":"SMTP_HOST","value":"smtp.office365.com"},
  {"key":"SMTP_PORT","value":"587"},{"key":"SMTP_USER","value":"littledude@coxresearch.com"}]}
JSON
          ;;                                                                     # -> SERVICE_ID (srv-...)
status)   curl -fsS "${H[@]}" "$API/services/$SERVICE_ID/deploys?limit=3" ;;
env)      curl -fsS "${H[@]}" "$API/services/$SERVICE_ID/env-vars" ;;            # read ADMIN_PASSWORD / TOKEN_SECRET
setenv)   curl -fsS "${H[@]}" -X PUT "$API/services/$SERVICE_ID/env-vars/$KEY" -d "{\"value\":\"$VALUE\"}" ;;
deploy)   curl -fsS "${H[@]}" -X POST "$API/services/$SERVICE_ID/deploys" -d '{"clearCache":"do_not_clear"}' ;;
domain)   curl -fsS "${H[@]}" -X POST "$API/services/$SERVICE_ID/custom-domains" -d '{"name":"amex.coxresearch.com"}' ;;
verify)   curl -fsS "${H[@]}" -X POST "$API/services/$SERVICE_ID/custom-domains/amex.coxresearch.com/verify" ;;
logs)     curl -fsS "${H[@]}" "$API/logs?ownerId=$OWNER_ID&resource=$SERVICE_ID&limit=100&direction=backward" ;;
upload)   curl -fsS -u "Chris:$ADMIN_PASSWORD" -H "Content-Type: application/json" -X POST \
            "${BASE:-https://amex-submission.onrender.com}/admin/upload" --data-binary @"statements/$STMT.json" ;;
*) echo "steps: owner create status env setenv deploy domain verify logs upload"; exit 1 ;;
esac
