#!/usr/bin/env bash
# Run only by the restricted server-side CI dispatcher after successful checks.
set -euo pipefail
umask 077
revision="${1:?A full commit SHA is required}"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || exit 2
root=/opt/fns-receipts-mcp
release="$root/releases/$revision"
cd "$release"
systemctl is-active --quiet mcp-home-guard wg-quick@mcp-home
docker network inspect mcp_home >/dev/null
export FNS_IMAGE="fns-receipts-mcp:$revision"
docker compose -f deploy/compose.yaml config --quiet
docker build --label "org.opencontainers.image.revision=$revision" -t "$FNS_IMAGE" .
docker run --rm --network none -v "$release/tests:/app/tests:ro" "$FNS_IMAGE" python -m unittest discover -v

old_image=""
old_release=""
if docker inspect fns-receipts-mcp >/dev/null 2>&1; then
    old_image=$(docker inspect --format '{{.Image}}' fns-receipts-mcp)
    old_release=$(readlink -f "$root/current")
fi
rollback() {
    result=$?
    trap - EXIT
    if [[ -n "$old_image" && -f "$old_release/deploy/compose.yaml" ]]; then
        echo 'Deployment failed; restoring previous image.' >&2
        FNS_IMAGE="$old_image" docker compose -f "$old_release/deploy/compose.yaml" up -d --no-build --wait --wait-timeout 150 || true
    else
        docker compose -f deploy/compose.yaml stop || true
    fi
    exit "$result"
}
trap rollback EXIT
docker compose -f deploy/compose.yaml up -d --no-build --wait --wait-timeout 150
docker exec -i fns-receipts-mcp python < deploy/smoke.py
python3 deploy/verify.py
ln -sfn "$release" "$root/current"
printf '%s\n' "$revision" > "$root/deployed-revision"
trap - EXIT
echo "Deployed fns-receipts-mcp: $revision"
