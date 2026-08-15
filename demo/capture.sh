#!/usr/bin/env bash
# Regenerate the README's screenshots.
#
#   ./demo/capture.sh
#
# Builds the UI, brings up a clean stack with the agent running in-process,
# parks a run at the approval gate, and drives a real browser over it. Playwright
# is installed to a temp directory rather than into the repo — it is an authoring
# tool, not a dependency of the product.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"

echo "==> building the UI"
(cd frontend && npm run build >/dev/null)

echo "==> resetting the database"
docker compose up -d db >/dev/null
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' deskhand-pg 2>/dev/null)" = "healthy" ] && break
  sleep 2
done
"$PY" -m deskhand.migrate >/dev/null
"$PY" -m deskhand.seed >/dev/null

echo "==> starting the stack (agent runs in-process)"
RUN_WORKER_INLINE=1 "$PY" -m uvicorn deskhand.main:app --port 8000 --log-level warning \
  >/tmp/deskhand-capture.log 2>&1 &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT
sleep 4

echo "==> parking a run at the approval gate"
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"owner@northwind.test","password":"demo-password-123"}' \
  | "$PY" -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s -X POST http://127.0.0.1:8000/runs \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"ticket_reference":"NW-1"}' >/dev/null

for _ in $(seq 1 30); do
  STATUS=$(curl -s http://127.0.0.1:8000/approvals -H "Authorization: Bearer $TOKEN" \
    | "$PY" -c 'import sys,json;d=json.load(sys.stdin);print(len(d))')
  [ "$STATUS" != "0" ] && break
  sleep 1
done
echo "    approvals waiting: $STATUS"

echo "==> driving a browser"
WORK=${DESKHAND_PW_DIR:-$(mktemp -d)}
if [ ! -d "$WORK/node_modules/playwright" ]; then
  (cd "$WORK" && npm init -y >/dev/null && npm i --silent playwright@1.62.1 >/dev/null)
fi
# NODE_PATH does not apply to ESM imports, so the script runs from inside the
# install rather than pointing at it.
cp "$ROOT/demo/screenshot.mjs" "$WORK/screenshot.mjs"
(cd "$WORK" && OUT_DIR="$ROOT/demo" node screenshot.mjs)
echo "==> done"
