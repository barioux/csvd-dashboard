/** In-process protection for the public POST /login password check. */

import type { Middleware } from "fresh";

import { SESSION_COOKIE } from "../lib/auth.ts";

export const LOGIN_LIMIT_WINDOW_MS = 60_000;
export const LOGIN_PER_CLIENT_LIMIT = 5;
export const LOGIN_GLOBAL_LIMIT = 64;
export const LOGIN_MAX_TRACKED_CLIENTS = 64;

type AttemptState = "pending" | "failed";
type AttemptOutcome = "auth-failure" | "success" | "other";

interface Attempt {
  id: number;
  client: string;
  createdAt: number;
  state: AttemptState;
}

interface AcceptedReservation {
  allowed: true;
  settle(outcome: AttemptOutcome): void;
}

interface RejectedReservation {
  allowed: false;
  retryAfterSeconds: number;
}

type Reservation = AcceptedReservation | RejectedReservation;

export interface LoginPostLimiterOptions {
  perClientLimit?: number;
  globalLimit?: number;
  windowMs?: number;
  maxTrackedClients?: number;
  now?: () => number;
  trustedClientIpHeader?: string;
}

interface ResolvedOptions {
  perClientLimit: number;
  globalLimit: number;
  windowMs: number;
  maxTrackedClients: number;
  now: () => number;
  trustedClientIpHeader: string | undefined;
}

/** Headers a client may append to, so no single value in them names the client. */
const FORWARDING_LISTS = new Set(["x-forwarded-for", "forwarded"]);

function positiveInteger(value: number, name: string): number {
  if (!Number.isInteger(value) || value <= 0) {
    throw new TypeError(`${name} must be a positive integer`);
  }
  return value;
}

function trustedHeaderName(name: string | undefined): string | undefined {
  const trimmed = name?.trim();
  if (!trimmed) return undefined;
  // Headers rejects an invalid name with a TypeError, so a typo in the
  // deployment's setting fails at startup instead of on every sign-in.
  new Headers().has(trimmed);
  if (FORWARDING_LISTS.has(trimmed.toLowerCase())) {
    throw new TypeError(
      `${trimmed} is a client-appendable list and cannot identify a client`,
    );
  }
  return trimmed;
}

function resolveOptions(options: LoginPostLimiterOptions): ResolvedOptions {
  return {
    perClientLimit: positiveInteger(
      options.perClientLimit ?? LOGIN_PER_CLIENT_LIMIT,
      "perClientLimit",
    ),
    globalLimit: positiveInteger(
      options.globalLimit ?? LOGIN_GLOBAL_LIMIT,
      "globalLimit",
    ),
    windowMs: positiveInteger(
      options.windowMs ?? LOGIN_LIMIT_WINDOW_MS,
      "windowMs",
    ),
    maxTrackedClients: positiveInteger(
      options.maxTrackedClients ?? LOGIN_MAX_TRACKED_CLIENTS,
      "maxTrackedClients",
    ),
    now: options.now ?? Date.now,
    trustedClientIpHeader: trustedHeaderName(options.trustedClientIpHeader),
  };
}

/** The socket peer only: source ports and client-controlled forwarding headers are ignored. */
export function remoteClientKey(
  address: Deno.ServeHandlerInfo["remoteAddr"],
): string {
  if ("hostname" in address) return `${address.transport}:${address.hostname}`;
  if ("path" in address) return `${address.transport}:${address.path}`;
  return address.transport;
}

/**
 * Treat separator-normalized aliases as the same login endpoint. This is
 * deliberately a little broader than today's router so a future Fresh route
 * normalization cannot expose an unreserved password check.
 */
export function isLoginPostPath(pathname: string, basePath = ""): boolean {
  let candidate = pathname;
  if (basePath) {
    if (candidate === basePath) candidate = "/";
    else if (candidate.startsWith(`${basePath}/`)) {
      candidate = candidate.slice(basePath.length);
    } else {
      return false;
    }
  }

  try {
    const segments = decodeURIComponent(candidate).split("/").filter(Boolean);
    return segments.length === 1 && segments[0] === "login";
  } catch (error) {
    if (error instanceof URIError) return false;
    throw error;
  }
}

/**
 * A timer-free, bounded attempt ledger. A reservation is inserted synchronously
 * before the route begins its asynchronous password check, so concurrent guesses
 * cannot all observe the same remaining slot. Expired entries are pruned on the
 * next request; globalLimit bounds tokens and maxTrackedClients bounds map keys.
 */
class AttemptLedger {
  readonly #options: ResolvedOptions;
  #attempts: Attempt[] = [];
  #byClient = new Map<string, Attempt[]>();
  #nextId = 1;

  constructor(options: ResolvedOptions) {
    this.#options = options;
  }

  reserve(client: string): Reservation {
    const now = this.#options.now();
    this.#prune(now);

    const clientAttempts = this.#byClient.get(client) ?? [];
    const waits: number[] = [];
    if (clientAttempts.length >= this.#options.perClientLimit) {
      waits.push(this.#retryAfter(clientAttempts, now));
    }
    if (this.#attempts.length >= this.#options.globalLimit) {
      waits.push(this.#retryAfter(this.#attempts, now));
    }
    if (
      clientAttempts.length === 0 &&
      this.#byClient.size >= this.#options.maxTrackedClients
    ) {
      waits.push(this.#retryAfterTrackedClientSlot(now));
    }

    if (waits.length > 0) {
      return { allowed: false, retryAfterSeconds: Math.max(...waits) };
    }

    const attempt: Attempt = {
      id: this.#nextId++,
      client,
      createdAt: now,
      state: "pending",
    };
    this.#attempts.push(attempt);
    this.#byClient.set(client, [...clientAttempts, attempt]);

    let settled = false;
    return {
      allowed: true,
      settle: (outcome) => {
        if (settled) return;
        settled = true;
        this.#settle(attempt, outcome);
      },
    };
  }

  #settle(attempt: Attempt, outcome: AttemptOutcome): void {
    if (!this.#attempts.includes(attempt)) return;

    if (outcome === "auth-failure") {
      attempt.state = "failed";
      return;
    }

    if (outcome === "success") {
      // Clear this successful request and completed earlier failures for the
      // same peer. Earlier guesses still in flight remain reserved and become
      // failures if their responses later say so.
      this.#remove((candidate) =>
        candidate === attempt ||
        (candidate.client === attempt.client &&
          candidate.id <= attempt.id && candidate.state === "failed")
      );
      return;
    }

    // Parser/config/server failures did not prove a bad password. Release only
    // this reservation without forgiving earlier authentication failures.
    this.#remove((candidate) => candidate === attempt);
  }

  #prune(now: number): void {
    this.#remove((attempt) =>
      attempt.createdAt + this.#options.windowMs <= now
    );
  }

  #remove(predicate: (attempt: Attempt) => boolean): void {
    if (!this.#attempts.some(predicate)) return;
    this.#attempts = this.#attempts.filter((attempt) => !predicate(attempt));
    this.#rebuildClientIndex();
  }

  #rebuildClientIndex(): void {
    const next = new Map<string, Attempt[]>();
    for (const attempt of this.#attempts) {
      const entries = next.get(attempt.client);
      if (entries) entries.push(attempt);
      else next.set(attempt.client, [attempt]);
    }
    this.#byClient = next;
  }

  #retryAfter(attempts: readonly Attempt[], now: number): number {
    const retryAt = Math.min(
      ...attempts.map((attempt) => attempt.createdAt + this.#options.windowMs),
    );
    return Math.max(1, Math.ceil((retryAt - now) / 1000));
  }

  #retryAfterTrackedClientSlot(now: number): number {
    const retryAt = Math.min(
      ...this.#byClient.values().map((attempts) =>
        Math.max(
          ...attempts.map((attempt) =>
            attempt.createdAt + this.#options.windowMs
          ),
        )
      ),
    );
    return Math.max(1, Math.ceil((retryAt - now) / 1000));
  }
}

function tooManyRequests(retryAfterSeconds: number): Response {
  return new Response("Too many sign-in attempts. Try again later.", {
    status: 429,
    headers: {
      "Cache-Control": "private, no-store",
      "Content-Type": "text/plain; charset=utf-8",
      "Retry-After": String(retryAfterSeconds),
    },
  });
}

function issuedSession(response: Response): boolean {
  return response.status >= 200 && response.status < 400 &&
    response.headers.getSetCookie().some((cookie) =>
      cookie.trimStart().startsWith(`${SESSION_COOKIE}=`)
    );
}

/**
 * Limit exact POST /login requests per client and across this process.
 *
 * Counters are deliberately process-local: restarts clear them and N replicas
 * allow roughly N times the global limit. A deployment with multiple workers or
 * instances still needs a distributed ingress limit.
 *
 * The client is the socket peer, ctx.info.remoteAddr, unless the deployment
 * names `trustedClientIpHeader`. Behind a proxying host every socket peer is
 * the proxy, so all visitors would otherwise share one bucket and five wrong
 * guesses from anyone would lock out everyone. Name only a header the host's
 * ingress proxy sets on every request (Fly.io's `Fly-Client-IP`), and only
 * where that proxy is the sole way in. A request without it falls back to the
 * socket peer. X-Forwarded-For and Forwarded are refused outright: a client
 * can append to both. Even a spoofable header cannot raise the guess rate past
 * the global limit, which keys on nothing a client sends.
 */
export function loginPostLimiter<State>(
  options: LoginPostLimiterOptions = {},
): Middleware<State> {
  const resolved = resolveOptions(options);
  const header = resolved.trustedClientIpHeader;
  const ledger = new AttemptLedger(resolved);

  return async function loginPostLimiter(ctx) {
    if (
      ctx.req.method !== "POST" ||
      !isLoginPostPath(ctx.url.pathname, ctx.config.basePath)
    ) {
      return await ctx.next();
    }

    const forwarded = header ? ctx.req.headers.get(header) : null;
    const reservation = ledger.reserve(
      forwarded ? `header:${forwarded}` : remoteClientKey(ctx.info.remoteAddr),
    );
    if (!reservation.allowed) {
      return tooManyRequests(reservation.retryAfterSeconds);
    }

    let response: Response;
    try {
      response = await ctx.next();
    } catch (error) {
      reservation.settle("other");
      throw error;
    }

    reservation.settle(
      response.status === 401
        ? "auth-failure"
        : issuedSession(response)
        ? "success"
        : "other",
    );
    return response;
  };
}
