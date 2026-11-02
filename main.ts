import { App, csrf, staticFiles } from "fresh";

import { compression } from "./server/compression.ts";
import { frameProtection } from "./server/frame_protection.ts";
import { loginPostLimiter } from "./server/login_post_limiter.ts";
import { protectDataAssets } from "./server/protected_data_assets.ts";
import { svgPolicy } from "./server/svg_policy.ts";
import type { State } from "./utils.ts";

export const app = new App<State>();

// The outer policy reaches static files, route responses, and early denials. The
// protected-data matcher must run before staticFiles(), which otherwise serves
// immutable assets immediately; every other asset stays public for /login.
// Outermost, so it also covers the protected-data chunk that the next
// middleware rewrites and the denials that never reach file routing. Nothing
// compressed before this: staticFiles() serves the built bundles verbatim.
app.use(compression());
app.use(frameProtection());
// Inside the framing policy, which appends its directive to this one, and
// outside staticFiles(), whose SVGs -- the institute logos among them -- are
// what it exists for.
app.use(svgPolicy());
app.use(protectDataAssets());
app.use(staticFiles());
// Reject cross-site writes before they consume a rate-limit reservation. The
// limiter then reserves atomically before POST /login verifies the passphrase.
// Behind a proxying host, LOGIN_CLIENT_IP_HEADER names the header its ingress
// sets with the visitor's address (fly.toml sets Fly-Client-IP); unset, the
// socket peer is the client, as it is locally and in e2e.
app.use(csrf());
app.use(
  loginPostLimiter({
    trustedClientIpHeader: Deno.env.get("LOGIN_CLIENT_IP_HEADER"),
  }),
);
app.fsRoutes();
