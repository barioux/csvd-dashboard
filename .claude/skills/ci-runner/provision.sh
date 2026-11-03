#!/usr/bin/env bash
# Provisions the self-hosted CI runners on a dedicated Ubuntu VM: Docker for
# the postgres service containers, Chromium's system packages, and RUNNERS
# instances of GitHub's runner, each a systemd service with its own E2E_PORT.
#
# Run it as root on the VM, with a registration token fetched on a machine
# where `gh` is signed in (the token lasts an hour and registers every
# instance):
#
#   token=$(gh api -X POST \
#     repos/mathieubpoiriericm/csvd-dashboard-private/actions/runners/registration-token \
#     --jq .token)
#   ssh <vm> "sudo RUNNER_TOKEN=$token bash -s" < .claude/skills/ci-runner/provision.sh
#
# Re-running it is safe: an instance that is already configured is left alone,
# so raising RUNNERS adds the missing ones. Never point REPO_URL at the public
# repository -- a fork's pull request would run its code on this machine.
set -euo pipefail

REPO_URL=${REPO_URL:-https://github.com/mathieubpoiriericm/csvd-dashboard-private}
RUNNERS=${RUNNERS:-6}
LABEL=${LABEL:-csvd-ci}
# e2e/package.json's @playwright/test. Re-run this script with the new one
# after an upgrade: the workflow skips --with-deps on a self-hosted runner.
PLAYWRIGHT_VERSION=${PLAYWRIGHT_VERSION:-1.62.1}
RUNNER_USER=runner
RUNNER_HOME=/home/$RUNNER_USER

: "${RUNNER_TOKEN:?set RUNNER_TOKEN to a registration token; see the header}"
if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo)" >&2
  exit 1
fi

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
# nodejs/npm only run the one-off install-deps below; jobs get their Node
# from actions/setup-node. unzip and xz-utils are what the setup actions
# extract their downloads with.
apt-get install -y docker.io git curl jq unzip xz-utils ca-certificates \
  nodejs npm qemu-guest-agent unattended-upgrades
systemctl enable --now docker
systemctl enable --now qemu-guest-agent || true

echo "== the runner user (docker, no sudo)"
id "$RUNNER_USER" &>/dev/null || useradd --create-home --shell /bin/bash "$RUNNER_USER"
usermod -aG docker "$RUNNER_USER"

echo "== Chromium's system packages, for Playwright $PLAYWRIGHT_VERSION"
npx -y "playwright@$PLAYWRIGHT_VERSION" install-deps chromium

echo "== GitHub's runner"
release=$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest)
version=$(jq -r .tag_name <<<"$release" | sed 's/^v//')
sha=$(jq -r .body <<<"$release" |
  sed -n 's/.*<!-- BEGIN SHA linux-x64 -->\([0-9a-f]\{64\}\)<!-- END SHA linux-x64 -->.*/\1/p')
if [[ -z $sha ]]; then
  echo "no linux-x64 checksum in the v$version release notes; not installing" >&2
  exit 1
fi
tarball=$RUNNER_HOME/actions-runner-linux-x64-$version.tar.gz
if [[ ! -f $tarball ]]; then
  curl -fsSLo "$tarball" \
    "https://github.com/actions/runner/releases/download/v$version/actions-runner-linux-x64-$version.tar.gz"
fi
echo "$sha  $tarball" | sha256sum -c -

for i in $(seq 1 "$RUNNERS"); do
  dir=$RUNNER_HOME/runner-$i
  if [[ -f $dir/.runner ]]; then
    echo "== runner-$i is already configured"
    continue
  fi
  echo "== runner-$i"
  mkdir -p "$dir"
  tar -xzf "$tarball" -C "$dir"
  chown -R "$RUNNER_USER:" "$dir"
  sudo -u "$RUNNER_USER" bash -c "cd '$dir' && ./config.sh --unattended \
    --url '$REPO_URL' --token '$RUNNER_TOKEN' \
    --name 'ryzen-$i' --labels '$LABEL' --work _work --replace"
  # Several jobs share this host, so each instance serves the e2e build on
  # its own port; playwright.config.ts reads E2E_PORT. config.sh's env.sh
  # keeps an existing .env and appends to it, so this goes in after it.
  echo "E2E_PORT=$((8100 + i))" >>"$dir/.env"
  chown "$RUNNER_USER:" "$dir/.env"
  (cd "$dir" && ./svc.sh install "$RUNNER_USER" && ./svc.sh start)
done

echo "== weekly Docker prune"
cat >/etc/systemd/system/ci-docker-prune.service <<'EOF'
[Unit]
Description=Prune the CI runners' Docker images and build cache

[Service]
Type=oneshot
ExecStart=/usr/bin/docker system prune -af --filter until=168h
EOF
cat >/etc/systemd/system/ci-docker-prune.timer <<'EOF'
[Unit]
Description=Weekly Docker prune for the CI runners

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now ci-docker-prune.timer

echo "== done"
systemctl list-units --no-pager 'actions.runner.*'
