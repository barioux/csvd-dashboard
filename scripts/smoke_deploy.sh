#!/usr/bin/env bash
# Check a running dashboard from outside, the way a visitor reaches it. This is
# what a deploy has to pass before anyone looks at it. Needs bash and curl.
#
#   scripts/smoke_deploy.sh <base-url>
#
# With SMOKE_PASSPHRASE set it also signs in and opens a gated page. Without
# it, only what a logged-out visitor sees is checked, so it can run against
# production without the secret. It stops at the first failure, non-zero.
#
# CI's container job runs it against the image with throwaway secrets, and its
# deploy job runs it against the live site after every deploy.

set -euo pipefail

base="${1:?usage: scripts/smoke_deploy.sh <base-url>}"
base="${base%/}"

fail() {
  echo "FAIL $*" >&2
  exit 1
}

ok() {
  echo "ok   $*"
}

# The status code alone; 000 when nothing answered.
status_of() {
  curl -sS -o /dev/null -w '%{http_code}' "$@" || true
}

# 1. The server is up: /login renders without a session.
code=$(status_of "$base/login")
[ "$code" = 200 ] || fail "GET /login answered $code, want 200"
ok "GET /login renders"

# 2. The login secrets are set: a logged-out visitor is sent to /login. With
# either secret unset the gate answers 503 here while /login above still
# renders, because the gate exempts /login before it reads them. That is why
# this, and not /login or Fly's health check, is the configuration check.
read -r code location < <(
  curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' "$base/" ||
    echo "000"
)
[ "$code" != 503 ] ||
  fail "GET / answered 503: DASHBOARD_PASSPHRASE or DASHBOARD_SESSION_SECRET is unset"
[ "$code" = 303 ] && [[ "${location:-}" == */login\?next=%2F ]] ||
  fail "GET / answered $code ${location:-with no Location}, want 303 to /login?next=%2F"
ok "GET / sends a logged-out visitor to /login"

# 3. The protected data chunk is refused without a session. The middleware
# matches the file name's shape, not the file, so no build hash is needed.
code=$(status_of "$base/assets/protected-data-smoke.js")
[ "$code" = 401 ] ||
  fail "a logged-out GET of a protected-data chunk answered $code, want 401"
ok "protected data refused without a session"

# 4. Responses leave compressed (server/compression.ts), not at full size.
encoding=$(
  curl -sS -o /dev/null -D - -H 'Accept-Encoding: gzip, br' "$base/login" |
    tr -d '\r' |
    awk 'tolower($1) == "content-encoding:" { print tolower($2) }'
)
[[ "$encoding" == gzip || "$encoding" == br ]] ||
  fail "GET /login was not compressed (Content-Encoding: ${encoding:-none})"
ok "GET /login compressed ($encoding)"

# 5. Signing in works end to end, when there is a passphrase to sign in with.
if [ -z "${SMOKE_PASSPHRASE:-}" ]; then
  echo "skip sign-in: SMOKE_PASSPHRASE is unset"
  exit 0
fi

# Sec-Fetch-Site is what a browser sends with the form, and what csrf() in
# main.ts accepts. The passphrase goes in on stdin, never on a command line.
response=$(
  printf '%s' "$SMOKE_PASSPHRASE" |
    curl -sS -o /dev/null -D - -H 'Sec-Fetch-Site: same-origin' \
      --data-urlencode 'passphrase@-' --data-urlencode 'next=/' \
      "$base/login" |
    tr -d '\r'
)
code=$(printf '%s\n' "$response" | awk 'NR == 1 { print $2 }')
[ "$code" = 303 ] ||
  fail "POST /login answered ${code:-nothing}, want 303 (401 is a refused passphrase)"

# The session cookie is replayed by hand rather than through a cookie jar: it
# is Secure on every host but localhost, so curl would withhold it from a
# plain-HTTP address such as a local container's. It is never printed, and it
# reaches curl on stdin too.
cookie=$(
  printf '%s\n' "$response" |
    awk 'tolower($1) == "set-cookie:" && /HttpOnly/ {
      sub(/^[^:]*: */, ""); sub(/;.*/, ""); print; exit
    }'
)
[ -n "$cookie" ] || fail "POST /login set no session cookie"
code=$(printf 'Cookie: %s\n' "$cookie" | status_of -H @- "$base/")
[ "$code" = 200 ] || fail "GET / with the new session answered $code, want 200"
ok "signing in opens the dashboard"
