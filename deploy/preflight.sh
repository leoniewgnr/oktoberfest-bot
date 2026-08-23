#!/usr/bin/env bash
# Run this on the target machine BEFORE installing. It answers the only two
# questions that decide whether this host can run the bot at all.
set -u

echo "== 1. Can this network reach the booking API? =="
echo "   (Cloudflare challenges datacenter IPs; that is why the Hetzner box cannot.)"
code=$(curl -s -o /dev/null -w '%{http_code}' -m 20 \
  -H 'x-festzelt-os-company: KDLWJDR' -H 'accept: application/json' \
  -H 'user-agent: oktoberfest-watcher/2.0 (personal table watcher)' \
  https://schottenhamel-api.festzelt-os.com/lp/guestlists)
case "$code" in
  200) echo "   OK   HTTP 200 - this host can run every target." ;;
  403) echo "   FAIL HTTP 403 - challenged here too. Do not install; pick another host." ;;
  *)   echo "   ??   HTTP $code - unexpected; investigate before installing." ;;
esac

echo
echo "== 2. Can Playwright run here? (needed for the 4 browser tents) =="
arch=$(uname -m)
echo "   arch: $arch"
case "$arch" in
  x86_64|aarch64|arm64)
    echo "   OK   Playwright ships Chromium for this architecture." ;;
  armv7l|armv6l)
    echo "   LIMITED  32-bit ARM has no Playwright Chromium build."
    echo "            Set enabled_scraper_types to [\"api_fzos\",\"announcement\"];"
    echo "            the 4 form_select tents need an x86_64/aarch64 host." ;;
  *)  echo "   ??   unknown architecture; test 'playwright install chromium' manually." ;;
esac

echo
echo "== 3. Basics =="
for cmd in git python3 curl; do
  command -v "$cmd" >/dev/null && echo "   OK   $cmd $($cmd --version 2>&1 | head -1)" \
                               || echo "   MISSING $cmd"
done
python3 - <<'PY' 2>/dev/null || echo "   MISSING python3"
import sys
v = sys.version_info
print(f"   {'OK  ' if v >= (3, 9) else 'FAIL'} python {v.major}.{v.minor} (need >= 3.9)")
PY
