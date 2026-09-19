#!/usr/bin/env bash
# One-time CI access provisioning, not an application deployment.
set -euo pipefail
[[ $(id -u) == 0 ]] || exit 1
root=/opt/fns-receipts-mcp
user=fns-receipts-deploy
repo=gagara11/fns-receipt-parser
here=$(cd "$(dirname "$0")" && pwd)
[[ ! -e "$root" ]] || { echo 'Refusing to overwrite existing installation.' >&2; exit 1; }
systemctl is-active --quiet mcp-home-guard wg-quick@mcp-home
docker network inspect mcp_home >/dev/null
gh auth status >/dev/null 2>&1
id "$user" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$user"
install -d -m 700 "$root" "$root/releases"
install -d -m 700 -o 10001 -g 10001 "$root/data" "$root/secrets"
install -m 755 "$here/dispatch.sh" /usr/local/sbin/fns-receipts-deploy
install -m 755 "$here/ssh-command.sh" /usr/local/sbin/fns-receipts-ssh
install -d -m 700 -o "$user" -g "$user" "/home/$user/.ssh"
python3 "$here/provision.py"
visudo -cf /etc/sudoers.d/fns-receipts-deploy
git clone --bare "https://github.com/$repo.git" "$root/repo.git"
gh secret set FNS_DEPLOY_SSH_KEY --repo "$repo" < /root/.ssh/id_ed25519_fns_receipts_ci
echo 'Restricted CI deploy access provisioned. Application will be deployed by GitHub Actions.'
