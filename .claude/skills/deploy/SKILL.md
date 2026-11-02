---
name: deploy
description: Use when deploying the dashboard to Fly.io, setting up a fork's Fly app, or diagnosing a broken deploy - the one-machine rule the login limiter depends on, the Fly-Client-IP header, the allowlist .dockerignore, the CI deploy job and its FLY_API_TOKEN, why Fly's health check cannot see a missing login secret, and the Deno Deploy cut-over.
---

# Hosting on Fly.io

The app runs on Fly.io as one always-on machine built from the root
`Dockerfile` and configured by `fly.toml`. CI's `deploy` job in
`.github/workflows/ci.yml` ships it on a push to `main`, but only once every
other job has passed, then runs `scripts/smoke_deploy.sh` against
`https://<app>.fly.dev`. `README.md`'s "Deployment" section carries the summary
table. It is moving there from Deno Deploy, which shuts down about six months
after Deno's 9 October 2026 announcement; see "Cut-over" below.

Six things are non-obvious enough to state here:

- **Exactly one machine.** `server/login_post_limiter.ts` counts failed
  sign-ins in the server process: a second machine doubles the guess budget,
  and a stopped one forgets it. `fly.toml` turns auto-stop off and keeps one
  running; CI deploys with `--ha=false`, because a first `fly deploy` otherwise
  creates two machines. `fly status` should list one. If it lists two,
  `fly scale count 1`.
- **`LOGIN_CLIENT_IP_HEADER = "Fly-Client-IP"` in `fly.toml` is load-bearing.**
  Every request reaches the machine from Fly's proxy, so without it the limiter
  keys every visitor on the proxy's address, and five wrong guesses from anyone
  lock everyone out for a minute. The limiter refuses `X-Forwarded-For` and
  `Forwarded` as the header, because a client can append to both. Fly's docs do
  not say whether the proxy overwrites a client-sent `Fly-Client-IP`; the
  probe below answers that for the live app. Even if it does not, the global
  limit, which keys on nothing a client sends, still caps guessing at 64 a
  minute.
- **Fly's health check is liveness only, and cannot see a missing login
  secret.** Fly's checks pass on a 2xx and follow no redirect, and `/login` is
  the one page that answers 200 without a session. The gate exempts it before
  reading the secrets, so it renders even with both unset. A missing secret is
  a 503 on every _gated_ route, which is what the smoke test's logged-out
  `GET /` looks for: it wants a 303 to `/login`, and a 503 fails the CI job
  naming both variables.
- **`.dockerignore` is an allowlist.** It names the files the build reads and
  excludes everything else, so `.env`, `.venv` and the publisher PDFs under
  `data/test_data/` cannot reach the build context. A new top-level file the
  build imports must be added there, and the CI `container` job fails until it
  is.
- **The image runs `deno serve -A --cached-only --host 0.0.0.0 --port 8000
  _fresh/server.js`.** `0.0.0.0`, unlike `deno task start`'s `127.0.0.1`
  (`.claude/rules/login.md`), because Fly's proxy reaches the machine over its
  private network. `--cached-only`, because the two `RUN` steps cached every
  module, so a missing one fails the start rather than being fetched in
  production. The whole tree stays in the image, because the server bundle
  still imports `fresh` through the `deno.json` import map. Never
  `_fresh/compiled-entry.js`: the plugin emits it beside `server.js`, but it
  calls `Deno.serve({ port: Deno.env.get("PORT") })` and so passes a string
  where a number is required.
- **The Deno tag is pinned to an exact 2.9.x patch, and bumped by hand.** The
  2.9 LTS line receives monthly bug-fix and security releases until October
  2027 and nothing after, so that monthly bump, together with the local Deno,
  is the image's security maintenance. After October 2027 the app has to be off
  Deno.

Because `data/*.json` is committed and bundled at build time, regenerating data
and pushing it to `main` republishes the site — `tests/filters_test.ts`'s caveat
about regeneration now has a production consequence as well as a test one.

## Setting up an app (once per app, by a person)

These touch an account and its secrets, so the researcher runs them:

1. `brew install flyctl`, then `fly auth login`. In Claude Code, type
   `! fly auth login` so the browser login lands in the session.
2. If this is a fork, edit `fly.toml` first: `app` becomes the host name
   `<app>.fly.dev` and must be unique across Fly, and `primary_region` should
   be the region nearest the readers (`fly platform regions`).
3. `fly launch --no-deploy --copy-config` registers the app from `fly.toml`
   without building anything.
4. `fly secrets set DASHBOARD_PASSPHRASE='…' DASHBOARD_SESSION_SECRET='…'`, in
   the same single quotes `.env` uses. The passphrase keeps apostrophes out for
   that reason. `openssl rand -base64 32` makes the session secret. Never
   import the whole `.env`: the rest is the pipeline's database password and
   API keys.
5. `fly tokens create deploy`, stored as the repository secret `FLY_API_TOKEN`
   in GitHub. Store it in **one** repository only: this project pushes to two
   remotes, and CI's deploy job runs wherever the secret exists and skips with a
   notice where it does not.
6. Push to `main`, or run `fly deploy --remote-only --ha=false` once by hand.
   Then `scripts/smoke_deploy.sh https://<app>.fly.dev` should print five `ok`
   lines with `SMOKE_PASSPHRASE` set, or four and a skip without it.

## Diagnosis

| Symptom                                                      | Cause and fix                                                                                                                                        |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| every gated page answers 503, `/login` renders                | a login secret is unset: `fly secrets list`, then `fly secrets set` the missing one (this restarts the machine)                                      |
| everyone gets "Too many sign-in attempts" at once            | `LOGIN_CLIENT_IP_HEADER` is missing from `fly.toml`'s `[env]`, or `fly status` lists more than one machine                                            |
| the CI `deploy` job printed "FLY_API_TOKEN is not set"       | this repository holds no token, which is expected on the second remote and on a fork that hosts elsewhere                                             |
| the CI `container` job fails at `deno task build`            | a file the build imports is missing from `.dockerignore`'s allowlist                                                                                  |
| the machine starts and exits at once (`fly logs`)            | `--cached-only` met a module the build did not cache; reproduce with `container build` and `container run` (README, "Deployment")                    |

**Probing whether `Fly-Client-IP` can be spoofed**, once, from a machine whose
address may be locked out for a minute:

1. POST five wrong passphrases to `/login` with `Sec-Fetch-Site: same-origin`
   to exhaust your own bucket.
2. POST a sixth with a made-up `Fly-Client-IP` header.

A 429 means the proxy overwrote the header, which is the safe answer. A 401
means it passed the client's value through, and the per-client limit can be
dodged, though not the global one. Record the result here.

## Cut-over from Deno Deploy

The Deno Deploy project at `https://csvd-dashboard.mathieubpoiriericm.deno.net`
keeps serving until it is deleted, and Deno Deploy itself shuts down in about
April 2027 (no date published). Once the Fly app answers and the smoke test
passes:

1. Write the new URL into `hosting.url` in `disease/manifest.json` and into
   `disease/README.md`, as the `new-disease` checklist does for a fork, and
   replace the old one in `README.md` and in the root `CLAUDE.md`'s "Hosting".
2. Tell the readers. A session cookie belongs to its host, so everyone signs in
   once more on the new address.
3. Delete the Deno Deploy project once nobody uses the old address. The
   researcher does this in the Deno Deploy console; it cannot be undone.
