---
name: deploy
description: Use when deploying the dashboard to Fly.io, setting up a fork's Fly app, or diagnosing a broken deploy - the one-machine rule the login limiter depends on, the Fly-Client-IP header, the allowlist .dockerignore, the CI deploy job and its FLY_API_TOKEN, why Fly's health check cannot see a missing login secret, the Fly-Client-IP spoof probe and its result, and retiring the old Deno Deploy project.
---

# Hosting on Fly.io

The app runs at `https://csvd-dashboard.fly.dev`, on Fly.io, as one always-on
machine built from the root `Dockerfile` and configured by `fly.toml`. CI's
`deploy` job in `.github/workflows/ci.yml` ships it on a push to `main`, but
only once every other job has passed, then runs `scripts/smoke_deploy.sh`
against `https://<app>.fly.dev`. `README.md`'s "Deployment" section carries the
summary table. It moved there on 2 November 2026 from Deno Deploy, which shuts
down about six months after Deno's 9 October 2026 announcement; "Retiring Deno
Deploy" below has what is left.

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
  not say whether the proxy overwrites a client-sent `Fly-Client-IP`, so the
  probe below was run against the live app on 2 November 2026: it does, and a
  visitor cannot pick their own bucket. Were that ever to change, the global
  limit, which keys on nothing a client sends, would still cap guessing at 64
  a minute.
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
2. POST a sixth with a made-up `Fly-Client-IP` header, and a seventh without
   one.
3. Confirm the header is configured on the running machine:
   `fly machine status <id> -d` lists `LOGIN_CLIENT_IP_HEADER` under `env`.
   Without it every request shares the proxy's bucket, which would also answer
   429 and pass for the safe result.

With the header configured, a 429 on the sixth means the proxy overwrote the
header, which is the safe answer. A 401 means it passed the client's value
through, and the per-client limit can be dodged, though not the global one.

**Result, 2 November 2026:** five 401s, then 429 with the made-up header and
429 without it, with `LOGIN_CLIENT_IP_HEADER = Fly-Client-IP` in machine
`853d9df4213e38`'s env. Fly's proxy overwrites a client-sent `Fly-Client-IP`.
Run the probe again if the app moves host or the proxy changes behaviour.

## Retiring Deno Deploy

The site moved from `https://csvd-dashboard.mathieubpoiriericm.deno.net` to
`https://csvd-dashboard.fly.dev` on 2 November 2026, and `hosting.url`,
`disease/README.md`, `README.md` and the root `CLAUDE.md` name the new address.
The Deno Deploy project keeps serving the old one until it is deleted, and Deno
Deploy itself shuts down in about April 2027 (no date published). Two steps are
left, and both are the researcher's:

1. Tell the readers the new address. A session cookie belongs to its host, so
   everyone signs in once more there, with the same passphrase.
2. Delete the Deno Deploy project once nobody uses the old address, in the Deno
   Deploy console. It cannot be undone. Then this section can go.
