#!/bin/sh
# Screenshot a rendered scenario page with headless Chrome, so the page can be
# looked at (by a person, or by a model that can read images) without a browser
# session. The first snapshot tab is what renders; to see another instant,
# render it alone with `render.py --as-of <T> --out <file>` and point at that.
#
#   ./shot.sh [scenario] [height] [out.png]
#   ./shot.sh multistack 9000 /tmp/multistack.png
set -e
here=$(cd "$(dirname "$0")" && pwd)
scenario=${1:-pulumi-service}
height=${2:-2400}
out=${3:-$here/out/$scenario/shot.png}
page=$here/out/$scenario/index.html
[ -f "$page" ] || { echo "no $page — run ./render.py --scenario $scenario first" >&2; exit 1; }
chrome="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$chrome" ] || chrome=$(command -v chromium || command -v google-chrome || command -v chrome)
"$chrome" --headless=new --disable-gpu --hide-scrollbars --window-size=1280,"$height" \
  --virtual-time-budget=4000 --screenshot="$out" "file://$page" 2>/dev/null
echo "$out"
