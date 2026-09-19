#!/usr/bin/env bash
# Root-owned fixed entrypoint. SSH key cannot request arbitrary shell commands.
set -euo pipefail
umask 077
[[ $# == 1 && "$1" =~ ^[a-f0-9]{40}$ ]] || exit 2
revision="$1"
root=/opt/fns-receipts-mcp
repo=gagara11/fns-receipt-parser
exec 9>"$root/deploy.lock"
flock -w 900 9
latest=$(gh api "repos/$repo/git/ref/heads/main" --jq .object.sha)
[[ "$latest" == "$revision" ]] || { echo 'Not the current main revision.' >&2; exit 1; }
# Check the exact workflow's successful test job, not merely a branch name.
run=$(gh api "repos/$repo/actions/workflows/deploy.yml/runs?head_sha=$revision&event=push" \
    --jq '.workflow_runs | map(select(.head_branch == "main")) | .[0].id')
[[ "$run" =~ ^[0-9]+$ ]] || exit 1
passed=$(gh api "repos/$repo/actions/runs/$run/jobs" \
    --jq '[.jobs[] | select(.name == "Security and tests" and .conclusion == "success")] | length')
[[ "$passed" == 1 ]] || { echo 'CI tests have not passed for this revision.' >&2; exit 1; }
git -C "$root/repo.git" fetch --quiet origin main
[[ "$(git -C "$root/repo.git" rev-parse FETCH_HEAD)" == "$revision" ]] || exit 1
release="$root/releases/$revision"
if [[ ! -d "$release" ]]; then
    stage=$(mktemp -d "$root/releases/.stage-XXXXXXXX")
    git -C "$root/repo.git" archive "$revision" | tar -x --no-same-owner -C "$stage"
    mv "$stage" "$release"
fi
bash "$release/deploy/deploy.sh" "$revision"
