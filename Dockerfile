# The dashboard as Fly.io runs it: the Fresh build, served by Deno.
#
# The tag is an exact patch of the 2.9 LTS line. Deno 2.9 receives monthly
# bug-fix and security releases until October 2027 and nothing after, so
# bumping this tag each month is the whole of this image's security
# maintenance. Keep it equal to the Deno that runs locally and in CI.
FROM denoland/deno:2.9.8

WORKDIR /app
RUN chown deno:deno /app
USER deno

# Dependencies first, so a change to the source alone reuses this layer.
# --frozen fails the build when deno.lock disagrees with deno.json instead of
# resolving afresh; nodeModulesDir is "manual", so this is also what populates
# node_modules for Vite.
COPY --chown=deno:deno deno.json deno.lock ./
RUN deno install --frozen

# .dockerignore is an allowlist, so this copies the app and nothing else.
COPY --chown=deno:deno . .
RUN deno task build

EXPOSE 8000

# The whole tree stays: the server bundle still imports `fresh` through the
# deno.json import map, so _fresh/ alone does not start.
#
# 0.0.0.0, where `deno task start` binds 127.0.0.1: Fly's proxy reaches the
# machine over its private network. The loopback flag there exists for the URL
# `deno serve` prints to a browser on the same machine (.claude/rules/login.md);
# here the session cookie's Secure flag follows the public host name, and Fly
# terminates TLS in front of it.
#
# --cached-only: every module was cached by the two RUN steps above, so one
# that was not fails the start instead of being fetched in production.
#
# _fresh/server.js, never _fresh/compiled-entry.js (the deploy skill says why).
# The two login secrets come from `fly secrets`, never from the image.
CMD ["deno", "serve", "-A", "--cached-only", "--host", "0.0.0.0", "--port", "8000", "_fresh/server.js"]
