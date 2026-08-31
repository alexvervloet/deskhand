#!/usr/bin/env bash
# Render the GitHub social preview from demo/social-card.html.
#
#   ./demo/social-card.sh
#
# Upload the result at Settings -> General -> Social preview. GitHub wants
# 1280x640; this renders at 2x and leaves the downscale to whoever displays it,
# so the type stays sharp on a retina timeline.
#
# Uses the system Chrome, like demo/screenshot.mjs — an authoring tool, not a
# dependency of the product.
set -euo pipefail

cd "$(dirname "$0")/.."
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

"$CHROME" --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=2 --window-size=1280,640 \
  --screenshot=demo/social-card.png \
  "file://$PWD/demo/social-card.html" 2>/dev/null

echo "wrote demo/social-card.png ($(du -h demo/social-card.png | cut -f1))"
