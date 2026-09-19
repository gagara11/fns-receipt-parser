#!/usr/bin/env bash
set -euo pipefail
if [[ "${SSH_ORIGINAL_COMMAND:-}" =~ ^deploy\ ([a-f0-9]{40})$ ]]; then
    exec sudo -n /usr/local/sbin/fns-receipts-deploy "${BASH_REMATCH[1]}"
fi
echo 'Only deploy <full commit SHA> is allowed.' >&2
exit 2
