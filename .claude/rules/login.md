---
paths:
  - "routes/_middleware.ts"
  - "routes/_app.tsx"
  - "routes/login.tsx"
  - "routes/logout.tsx"
  - "lib/auth.ts"
  - "main.ts"
  - "utils.ts"
  - "e2e/**"
---

# Login

The dashboard sits behind one shared passphrase and a 30-day signed cookie — no
store, no Deno KV, no auth library. Five things are load-bearing:

- **The gate is `routes/_middleware.ts`, and it fails closed.** It runs for
  every file route, exempts only `/login` and `/logout`, and answers 503 on
  every other route while `DASHBOARD_PASSPHRASE` or `DASHBOARD_SESSION_SECRET`
  is unset — in development too. The exemption comes first, so `/login` still
  renders without them, which is why Fly's liveness check on it cannot see a
  missing secret and `scripts/smoke_deploy.sh` checks `GET /` instead. There is no environment-sniffing bypass; put both
  in `.env`, which `deno task dev` and `deno task start` both load; an exported
  variable beats the file, so a host that injects secrets is unaffected. Static
  assets never reach the gate because `main.ts` registers `staticFiles()` first,
  which is what keeps the login page styled.
- **`deno task start` binds to `127.0.0.1`, and that flag is load-bearing.** The
  cookie is `Secure` on every hostname but `localhost` and `127.0.0.1`, and
  `deno serve` prints its bind address as the URL to open. Without the flag it
  prints `http://0.0.0.0:8000/`, where a browser drops the Secure cookie over
  plain HTTP: the login succeeds, the redirect back arrives with no session, and
  the form reappears with no error — which reads as a rejected passphrase.
  `e2e/tests/login.spec.ts` reads the task and signs in on the host it names.
- **`lib/auth.ts` is pure and takes the secret as an argument.** It never reads
  `Deno.env` except through the injectable reader in `loginConfig`, so the 100 %
  floor under `lib/` holds without touching the process environment. The token
  is `"<expiresMs>.<base64url HMAC-SHA256>"`, verified with
  `crypto.subtle.verify` (constant time); the passphrase compare goes through
  `timingSafeEqual` on two digests. The routes are thin: `routes/login.tsx` and
  `routes/logout.tsx` call `loginResponse` / `logoutResponse` and render the
  card.
- **`csrf()` in `main.ts` is what protects the two POSTs.** Both forms are
  same-origin; the plugin rejects a cross-site `Origin` or `Sec-Fetch-Site`. Do
  not turn `/logout` into a GET link — that is the request it guards.
- **The e2e suite runs pre-authenticated.** `playwright.config.ts` starts the
  server with the suite's own two values, the `setup` project signs in once and
  saves `storageState`, and `chromium` depends on it. A spec that asserts on the
  logged-out state has to opt out with
  `test.use({ storageState: { cookies: [], origins: [] } })`, as `login.spec.ts`
  does.

`_app.tsx` renders `/login` as a bare sheet (no navbar) and, for a verified
session, a "Sign out" form beside the theme toggle; the flag it reads is
`ctx.state.authenticated`, the one field in `State` (`utils.ts`).
