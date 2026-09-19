"""One-time secret generation. Never prints key or MCP credential values."""
import os
import pwd
import secrets
import subprocess
from pathlib import Path

os.umask(0o077)
key = Path('/root/.ssh/id_ed25519_fns_receipts_ci')
if key.exists():
    raise SystemExit('Refusing to overwrite an existing CI key')
subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-C','fns-receipts-ci','-f',str(key)], check=True)
public = key.with_suffix('.pub').read_text().strip()
Path('/home/fns-receipts-deploy/.ssh/authorized_keys').write_text(
    'restrict,command="/usr/local/sbin/fns-receipts-ssh" ' + public + '\n')
account = pwd.getpwnam('fns-receipts-deploy')
os.chown('/home/fns-receipts-deploy/.ssh/authorized_keys', account.pw_uid, account.pw_gid)
sudoers = Path('/etc/sudoers.d/fns-receipts-deploy')
sudoers.write_text('fns-receipts-deploy ALL=(root) NOPASSWD: /usr/local/sbin/fns-receipts-deploy *\n')
sudoers.chmod(0o440)
token = Path('/opt/fns-receipts-mcp/secrets/mcp_token')
token.write_text(secrets.token_urlsafe(48)+'\n')
os.chown(token,10001,10001)
