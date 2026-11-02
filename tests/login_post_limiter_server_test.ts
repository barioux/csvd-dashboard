import { assertEquals, assertThrows } from "@std/assert";

import { SESSION_COOKIE } from "../lib/auth.ts";
import {
  isLoginPostPath,
  LOGIN_PER_CLIENT_LIMIT,
  loginPostLimiter,
  remoteClientKey,
} from "../server/login_post_limiter.ts";

type Limiter = ReturnType<typeof loginPostLimiter>;

function info(hostname: string, port = 12_345): Deno.ServeHandlerInfo {
  return {
    remoteAddr: { transport: "tcp", hostname, port },
  } as Deno.ServeHandlerInfo;
}

function invoke(
  limiter: Limiter,
  options: {
    hostname?: string;
    port?: number;
    forwardedFor?: string;
    headers?: Record<string, string>;
    method?: string;
    pathname?: string;
    basePath?: string;
    next?: () => Response | Promise<Response>;
  } = {},
): Promise<Response> {
  const pathname = options.pathname ?? "/login";
  const headers = new Headers(options.headers);
  if (options.forwardedFor) {
    headers.set("X-Forwarded-For", options.forwardedFor);
  }
  const req = new Request(`http://localhost${pathname}`, {
    method: options.method ?? "POST",
    headers,
  });
  return Promise.resolve(
    limiter({
      req,
      url: new URL(req.url),
      info: info(options.hostname ?? "192.0.2.1", options.port),
      config: { basePath: options.basePath ?? "" },
      next: options.next ?? (() => new Response("wrong", { status: 401 })),
    } as never),
  );
}

function failure(): Response {
  return new Response("wrong", { status: 401 });
}

function success(): Response {
  return new Response(null, {
    status: 303,
    headers: {
      Location: "/",
      "Set-Cookie": `${SESSION_COOKIE}=signed-token; Path=/; HttpOnly`,
    },
  });
}

function assertLimited(response: Response, retryAfter: string): void {
  assertEquals(response.status, 429);
  assertEquals(response.headers.get("retry-after"), retryAfter);
  assertEquals(response.headers.get("cache-control"), "private, no-store");
  assertEquals(response.headers.get("location"), null);
}

Deno.test("login aliases normalize to one protected POST endpoint", () => {
  for (
    const pathname of [
      "/login",
      "/login/",
      "//login///",
      "/%6Cogin",
      "/login%2F",
    ]
  ) {
    assertEquals(isLoginPostPath(pathname), true, pathname);
  }
  assertEquals(isLoginPostPath("/dashboard//login/", "/dashboard"), true);

  for (const pathname of ["/login/x", "/log/in", "/login-attempt"]) {
    assertEquals(isLoginPostPath(pathname), false, pathname);
  }
  assertEquals(isLoginPostPath("/dashboard/login", "/dash"), false);
  assertEquals(isLoginPostPath("/%ZZ"), false);
});

Deno.test("remote client identity uses the socket peer and ignores source port", () => {
  assertEquals(
    remoteClientKey(info("192.0.2.20", 1111).remoteAddr),
    remoteClientKey(info("192.0.2.20", 9999).remoteAddr),
  );
});

// Behind a proxying host every socket peer is the proxy, so without a trusted
// client-IP header all visitors would share one bucket.
const PROXY = "172.16.0.2";

Deno.test("a configured client-IP header keys clients behind a shared proxy", async () => {
  const limiter = loginPostLimiter({
    perClientLimit: 1,
    globalLimit: 10,
    trustedClientIpHeader: "Fly-Client-IP",
  });
  const viaProxy = (client: string) =>
    invoke(limiter, { hostname: PROXY, headers: { "Fly-Client-IP": client } });

  assertEquals((await viaProxy("198.51.100.7")).status, 401);
  assertEquals((await viaProxy("203.0.113.8")).status, 401);
  assertLimited(await viaProxy("198.51.100.7"), "60");
});

Deno.test("the client-IP header is ignored unless configured", async () => {
  const limiter = loginPostLimiter({ perClientLimit: 1, globalLimit: 10 });
  assertEquals(
    (await invoke(limiter, {
      hostname: PROXY,
      headers: { "Fly-Client-IP": "198.51.100.7" },
    })).status,
    401,
  );
  assertLimited(
    await invoke(limiter, {
      hostname: PROXY,
      headers: { "Fly-Client-IP": "203.0.113.8" },
    }),
    "60",
  );
});

Deno.test("a missing or blank client-IP header falls back to the socket peer", async () => {
  const limiter = loginPostLimiter({
    perClientLimit: 1,
    globalLimit: 10,
    trustedClientIpHeader: "Fly-Client-IP",
  });
  assertEquals((await invoke(limiter, { hostname: "192.0.2.1" })).status, 401);
  assertEquals(
    (await invoke(limiter, {
      hostname: "192.0.2.2",
      headers: { "Fly-Client-IP": " " },
    })).status,
    401,
  );
  assertLimited(
    await invoke(limiter, {
      hostname: "192.0.2.1",
      headers: { "Fly-Client-IP": "" },
    }),
    "60",
  );
});

Deno.test("forwarding lists and invalid names cannot be the client-IP header", () => {
  for (
    const name of ["X-Forwarded-For", "x-forwarded-for", "Forwarded", "Fly IP"]
  ) {
    assertThrows(
      () => loginPostLimiter({ trustedClientIpHeader: name }),
      TypeError,
      undefined,
      name,
    );
  }
});

Deno.test("eight concurrent guesses visibly exceed the conservative default", async () => {
  const limiter = loginPostLimiter();
  const releases: Array<() => void> = [];
  const guesses = Array.from({ length: 8 }, (_, index) =>
    invoke(limiter, {
      port: 20_000 + index,
      forwardedFor: `198.51.100.${index + 1}`,
      next: () =>
        new Promise<Response>((resolve) => {
          releases.push(() => resolve(failure()));
        }),
    }));

  assertEquals(releases.length, LOGIN_PER_CLIENT_LIMIT);
  const denied = await Promise.all(guesses.slice(LOGIN_PER_CLIENT_LIMIT));
  for (const response of denied) assertLimited(response, "60");

  for (const release of releases) release();
  const accepted = await Promise.all(guesses.slice(0, LOGIN_PER_CLIENT_LIMIT));
  assertEquals(accepted.map((response) => response.status), [
    401,
    401,
    401,
    401,
    401,
  ]);
});

Deno.test("per-client reservations expire and never trust forwarding headers", async () => {
  let now = 1_000;
  const limiter = loginPostLimiter({
    perClientLimit: 2,
    globalLimit: 10,
    windowMs: 10_000,
    now: () => now,
  });

  assertEquals(
    (await invoke(limiter, { forwardedFor: "198.51.100.1" })).status,
    401,
  );
  assertEquals(
    (await invoke(limiter, {
      port: 54_321,
      forwardedFor: "203.0.113.99",
    })).status,
    401,
  );
  assertLimited(await invoke(limiter), "10");

  now = 11_000;
  assertEquals((await invoke(limiter)).status, 401);
});

Deno.test("the global limit and client-key bound stop address fan-out", async () => {
  let now = 2_000;
  const globalLimiter = loginPostLimiter({
    perClientLimit: 5,
    globalLimit: 2,
    maxTrackedClients: 5,
    windowMs: 20_000,
    now: () => now,
  });
  assertEquals(
    (await invoke(globalLimiter, { hostname: "192.0.2.1" })).status,
    401,
  );
  assertEquals(
    (await invoke(globalLimiter, { hostname: "192.0.2.2" })).status,
    401,
  );
  assertLimited(await invoke(globalLimiter, { hostname: "192.0.2.3" }), "20");

  const keyBoundLimiter = loginPostLimiter({
    perClientLimit: 5,
    globalLimit: 10,
    maxTrackedClients: 2,
    windowMs: 20_000,
    now: () => now,
  });
  await invoke(keyBoundLimiter, { hostname: "198.51.100.1" });
  await invoke(keyBoundLimiter, { hostname: "198.51.100.2" });
  assertLimited(
    await invoke(keyBoundLimiter, { hostname: "198.51.100.3" }),
    "20",
  );
  now = 22_000;
  assertEquals(
    (await invoke(keyBoundLimiter, { hostname: "198.51.100.3" })).status,
    401,
  );
});

Deno.test("client-key cap waits until an entire staggered bucket expires", async () => {
  let now = 0;
  const limiter = loginPostLimiter({
    perClientLimit: 3,
    globalLimit: 10,
    maxTrackedClients: 2,
    windowMs: 10_000,
    now: () => now,
  });

  await invoke(limiter, { hostname: "192.0.2.1" });
  now = 4_000;
  await invoke(limiter, { hostname: "192.0.2.1" });
  now = 5_000;
  await invoke(limiter, { hostname: "192.0.2.2" });

  now = 6_000;
  assertLimited(
    await invoke(limiter, { hostname: "192.0.2.3" }),
    "8",
  );

  now = 10_000;
  assertLimited(
    await invoke(limiter, { hostname: "192.0.2.3" }),
    "4",
  );

  now = 14_000;
  assertEquals(
    (await invoke(limiter, { hostname: "192.0.2.3" })).status,
    401,
  );
});

Deno.test("success resets failures while non-auth responses only release themselves", async () => {
  const limiter = loginPostLimiter({ perClientLimit: 2, globalLimit: 10 });
  assertEquals((await invoke(limiter)).status, 401);
  assertEquals((await invoke(limiter, { next: success })).status, 303);
  assertEquals((await invoke(limiter)).status, 401);
  assertEquals((await invoke(limiter)).status, 401);
  assertLimited(await invoke(limiter), "60");

  const otherLimiter = loginPostLimiter({ perClientLimit: 2, globalLimit: 10 });
  assertEquals((await invoke(otherLimiter)).status, 401);
  assertEquals(
    (await invoke(otherLimiter, {
      next: () => new Response("bad form", { status: 400 }),
    })).status,
    400,
  );
  assertEquals((await invoke(otherLimiter)).status, 401);
  assertLimited(await invoke(otherLimiter), "60");
});

Deno.test("slash aliases reserve but their canonical redirect does not erase failures", async () => {
  const limiter = loginPostLimiter({ perClientLimit: 2, globalLimit: 10 });
  assertEquals((await invoke(limiter)).status, 401);
  assertEquals(
    (await invoke(limiter, {
      pathname: "/login/",
      next: () =>
        new Response(null, { status: 308, headers: { Location: "/login" } }),
    })).status,
    308,
  );
  assertEquals((await invoke(limiter)).status, 401);
  assertLimited(
    await invoke(limiter, { pathname: "//login///" }),
    "60",
  );
});

Deno.test("non-login methods bypass the limiter and thrown handlers release", async () => {
  const limiter = loginPostLimiter({ perClientLimit: 1, globalLimit: 2 });
  assertEquals(
    (await invoke(limiter, { method: "GET", next: failure })).status,
    401,
  );
  assertEquals(
    (await invoke(limiter, { pathname: "/other", next: failure })).status,
    401,
  );

  let threw = false;
  try {
    await invoke(limiter, {
      next: () => {
        throw new Error("test failure");
      },
    });
  } catch (error) {
    threw = error instanceof Error && error.message === "test failure";
  }
  assertEquals(threw, true);
  assertEquals((await invoke(limiter)).status, 401);
});
