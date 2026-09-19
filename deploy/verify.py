"""Read-only runtime validation. Exits nonzero on an unsafe deployment."""
import json
import subprocess
def run(*args):
    return subprocess.check_output(args, text=True).strip()
info = json.loads(run('docker','inspect','fns-receipts-mcp'))[0]
host = info['HostConfig']
assert set(info['NetworkSettings']['Networks']) == {'mcp_home'}
assert host['Dns'] == ['1.1.1.1','9.9.9.9']
assert host['ReadonlyRootfs'] and 'ALL' in host['CapDrop']
assert info['Config']['User'] == '10001:10001'
assert host['Sysctls']['net.ipv6.conf.all.disable_ipv6'] == '1'
assert all(p['HostIp']=='127.0.0.1' for ports in host['PortBindings'].values() for p in ports)
assert info['State']['Health']['Status'] == 'healthy'
ip = run('docker','exec','fns-receipts-mcp','python','-c',
         "import urllib.request; print(urllib.request.urlopen('https://api.ipify.org',timeout=20).read().decode())")
assert ip == '77.242.108.147', 'Unexpected egress route'
print('Runtime OK: private ports, non-root, read-only rootfs, mcp_home, home egress, DNS, IPv6 disabled.')
