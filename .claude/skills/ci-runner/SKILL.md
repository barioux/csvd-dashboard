---
name: ci-runner
description: Use when CI jobs queue and never start, when moving CI between the home mini PC and GitHub's runners, after upgrading @playwright/test, when adding a job that serves on a port, or when provisioning, re-registering or cleaning up the self-hosted runners - the CI_RUNNER variable and its fallback, why the runners belong to the private repository alone, the per-runner E2E_PORT, and the VM's housekeeping.
---

# The self-hosted CI runners

The five test jobs in `.github/workflows/ci.yml` (`check`, `python`, `e2e`,
`empty-data`, both `adapt-fork` legs) run on
`${{ vars.CI_RUNNER || 'ubuntu-latest' }}`. The private repository
(`mathieubpoiriericm/csvd-dashboard-private`) sets the repository variable
`CI_RUNNER=csvd-ci`, which sends them to six runners, `ryzen-1` to `ryzen-6`, in
an Ubuntu VM on a dedicated mini PC at home (Proxmox VE on a Ryzen 9 9955HX, 32
GB). The public repository and every fork have no such variable and stay on
`ubuntu-latest`. `container` and `deploy` are hosted everywhere.

On GitHub's two-core private runners a push took about ten minutes to go green,
`adapt-fork` alone 8m45 (run 41546971078, 2 November 2026).

## Switching

```sh
R=mathieubpoiriericm/csvd-dashboard-private
gh variable set CI_RUNNER --body csvd-ci -R $R   # to the mini PC
gh variable delete CI_RUNNER -R $R               # back to ubuntu-latest
gh api repos/$R/actions/runners \
  --jq '.runners[] | "\(.name) \(.status) busy=\(.busy)"'
```

**Jobs that sit in "Queued" are the machine being down.** A job waiting for a
self-hosted label with no runner online waits up to a day rather than failing.
Delete the variable, then re-run the failed or queued run; the jobs go to
GitHub's runners. Set it again once `actions/runners` lists the instances
`online`.

## What is non-obvious

- **The runners are registered to the private repository and nowhere else.**
  The public repository takes pull requests from forks, and a self-hosted
  runner runs whatever code a pull request's workflow names. A runner
  registered there would hand the machine to anyone who opens one. The
  variable is only the selector; the registration is the boundary.
- **No job may take a fixed host port.** On GitHub's runners every job has a VM
  of its own; here six share one. The postgres services publish on a free port,
  read back through `job.services.postgres.ports['5432']` into
  `CSVD_TEST_DB_URL` (in a step, because the `job` context is not available in
  a job's `env`). Each runner's `.env` sets `E2E_PORT=810N`, which
  `e2e/playwright.config.ts` serves the build on. A new job that serves on a
  port, or a fixed container name or image tag like the `container` job's,
  either follows suit or stays on `ubuntu-latest`.
- **After upgrading `@playwright/test`, re-run the provisioning script with the
  new `PLAYWRIGHT_VERSION`.** The workflow passes `--with-deps` only on
  GitHub's runners, because a self-hosted one has no sudo. A newer Chromium
  needing a package the VM lacks fails to launch, with Playwright's "Host
  system is missing dependencies" or a missing `.so`.
- **Caches live on the VM's disk.** `setup-uv` and `setup-node` cache through
  GitHub's service only on hosted runners; here uv, npm, Deno and Playwright
  keep their caches under `/home/runner`. `actions/checkout` still cleans the
  workspace (`git clean -ffdx`), so `node_modules` and `.venv` are rebuilt from
  those caches on every job, which is what keeps one job's leftovers out of
  the next.
- **`adapt-fork` is sharded by position.** `--shard=K/N` keeps every N-th
  answer set from the K-th, so a set added to `ANSWER_SETS` lands in a leg
  without the matrix changing. A leg past the last set logs that and passes.

## Provisioning

`provision.sh`, beside this file, sets up the VM from a fresh Ubuntu Server:
Docker, a `runner` user in the `docker` group with no sudo, Chromium's system
packages through `npx playwright install-deps`, the runner instances as systemd
services, and a weekly `docker system prune` timer. Its header has the
invocation; it is safe to re-run, and raising `RUNNERS` adds instances.

The VM in Proxmox: CPU type `host` (the default `kvm64` hides Zen 5's
instruction sets), about 28 vCPUs, 24 GB, 120 GB of disk, the QEMU guest agent
on. Six instances cover one full run (five jobs, two of them the `adapt-fork`
legs); a second run arriving meanwhile queues briefly.

If `install-deps` refuses the VM's Ubuntu release, build the VM from the
release `ubuntu-latest` uses instead, which also keeps the two environments
alike.

To remove an instance (to rename it, or retire the VM):

```sh
token=$(gh api -X POST repos/$R/actions/runners/remove-token --jq .token)
# on the VM, in /home/runner/runner-N:
sudo ./svc.sh stop && sudo ./svc.sh uninstall
sudo -u runner ./config.sh remove --token "$token"
```

## Housekeeping

- Docker: `ci-docker-prune.timer` prunes weekly, so the `postgres:18` image is
  pulled again about once a week.
- `du -sh /home/runner/.cache/*` when the disk fills: uv, npm, Deno and
  Playwright keep every version they have fetched.
- `scripts/adapt_fork_check.ts` keeps a failed fork's copy under `/tmp`
  (`adapt-fork-*`); systemd-tmpfiles ages those out after ten days.
- The runners update themselves; unattended-upgrades covers the OS.
