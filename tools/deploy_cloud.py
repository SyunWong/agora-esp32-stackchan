"""Plan or create a NEW Worker. Existing Workers are never upgraded by this tool."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import urllib.error
import urllib.request

from configure import hidden, origin, private_write

ROOT = Path(__file__).resolve().parents[1]
API = 'https://api.cloudflare.com/client/v4'
SECRET_NAMES = ('AGORA_APP_ID', 'AGORA_APP_CERTIFICATE', 'DEEPSEEK_API_KEY',
                'FISH_AUDIO_API_KEY', 'DEVICE_KEY', 'CALLER_PASSWORD')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(method, path, token, body=None, content_type='application/json'):
    req = urllib.request.Request(API + path, data=body, method=method, headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': content_type})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        # Provider error bodies may echo secrets. Do not log them or the request.
        raise RuntimeError('Cloud API HTTP failure: ' + str(error.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError('Cloud API transport failed; inspect account state before retrying') from None
    if len(raw) > 8 * 1024 * 1024:
        raise RuntimeError('Cloud API response exceeded limit')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RuntimeError('Invalid Cloud API response') from None
    if not isinstance(value, dict) or value.get('success') is not True:
        raise RuntimeError('Cloud API did not report success')
    return value


def prepare(root):
    # Always rebuild, so --apply cannot silently upload an obsolete local bundle.
    import bundle_cloud
    previous = bundle_cloud.ROOT
    try:
        bundle_cloud.ROOT = root
        bundle_cloud.main()
    finally:
        bundle_cloud.ROOT = previous
    raw = (root / 'build/cloud/modules.json').read_bytes()
    release = hashlib.sha256(raw).hexdigest()[:16]
    modules = json.loads(raw)
    b = json.loads((root / 'private/bindings.json').read_text())
    origin(b['PUBLIC_ORIGIN'])
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', b['DEVICE_ID']):
        raise ValueError('Invalid device identifier')
    for name in SECRET_NAMES:
        if not isinstance(b.get(name), str) or not b[name]:
            raise ValueError('Required secret missing: ' + name)
    if b.get('ENABLED') != 'false':
        raise ValueError('New-deployment source configuration must start disabled')
    bindings = [{'name': k, 'type': 'secret_text', 'text': b[k]} for k in SECRET_NAMES]
    plain = {'PUBLIC_ORIGIN': b['PUBLIC_ORIGIN'], 'DEVICE_ID': b['DEVICE_ID'],
             'ENABLED': 'true', 'RELEASE': release}
    bindings += [{'name': k, 'type': 'plain_text', 'text': v} for k, v in plain.items()]
    bindings.append({'name': 'STACKCHAN', 'type': 'durable_object_namespace', 'class_name': 'StackChan'})
    metadata = {'main_module': 'worker.py', 'compatibility_date': '2026-11-26',
        'compatibility_flags': ['python_workers', 'disable_python_external_sdk'],
        'bindings': bindings, 'migrations': {'new_tag': 'v1', 'new_sqlite_classes': ['StackChan']},
        'observability': {'enabled': True, 'head_sampling_rate': 0.1,
                          'traces': {'enabled': True, 'head_sampling_rate': 0.1}}}
    return modules, metadata, release


def multipart(modules, metadata):
    boundary = 'stackchan-' + secrets.token_hex(24)
    parts = []
    entries = [('metadata', 'application/json', json.dumps(metadata).encode())]
    entries += [(m['name'], m['type'], base64.b64decode(m['base64'], validate=True)) for m in modules]
    for name, kind, data in entries:
        if not re.fullmatch(r'[A-Za-z0-9_./-]+', name) or '..' in name.split('/'):
            raise ValueError('Invalid module name')
        if kind not in ('application/json', 'text/x-python', 'text/plain', 'application/octet-stream'):
            raise ValueError('Invalid module content type')
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{name}"\r\n'
                      f'Content-Type: {kind}\r\n\r\n').encode() + data + b'\r\n')
    return b''.join(parts) + f'--{boundary}--\r\n'.encode(), 'multipart/form-data; boundary=' + boundary


def create(account, name, modules, metadata, token, call=request):
    listing = call('GET', f'/accounts/{account}/workers/scripts', token)
    scripts = listing.get('result')
    info = listing.get('result_info') or {}
    if (not isinstance(scripts, list) or any(not isinstance(x, dict) or not isinstance(x.get('id'), str) for x in scripts)
            or info.get('cursor') or info.get('total_count', len(scripts)) != len(scripts)
            or info.get('total_pages', 1) > 1):
        raise RuntimeError('Could not establish a complete Worker inventory; refusing upload')
    if any(x['id'] == name for x in scripts):
        raise RuntimeError('Worker already exists; this tool only creates new Workers')
    body, kind = multipart(modules, metadata)
    # No automatic retries. A timeout can occur after a successful creation.
    return call('PUT', f'/accounts/{account}/workers/scripts/{name}', token, body, kind)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--apply', action='store_true', help='Create and enable the new Worker, uploading Secrets')
    a = parser.parse_args()
    if not (ROOT / '.recipe-root').is_file():
        parser.error('Run only in an exported recipe checkout')
    if not re.fullmatch(r'[0-9a-f]{32}', a.account_id) or not re.fullmatch(r'[a-z][a-z0-9-]{2,62}', a.name):
        parser.error('Invalid account ID or Worker name')
    modules, metadata, release = prepare(ROOT)
    print(json.dumps({'mode': 'apply' if a.apply else 'offline-plan', 'worker': a.name,
        'release': release, 'modules': len(modules), 'secret_names': list(SECRET_NAMES),
        'new_sqlite_namespace': 'StackChan', 'enable_service': True}))
    if not a.apply:
        return
    os.umask(0o077)
    # Reserve an attempt receipt before any remote mutation. Never overwrite evidence.
    receipt = ROOT / 'private' / ('deploy-' + a.name + '.json')
    record = {'account_id': a.account_id, 'worker': a.name, 'release': release, 'status': 'attempting'}
    private_write(receipt, json.dumps(record, indent=2) + '\n')
    try:
        token = hidden('Cloudflare API token (Workers Scripts Edit for this account)')
        create(a.account_id, a.name, modules, metadata, token)
        record['status'] = 'uploaded'
    except Exception:
        record['status'] = 'unconfirmed_inspect_account_before_retry'
        raise
    finally:
        receipt.write_text(json.dumps(record, indent=2) + '\n')
    print('Worker uploaded. Attach your configured custom domain in Cloudflare, then verify health/login. No call started.')


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError) as error:
        # Exceptions are locally fixed strings; do not print external response bodies.
        raise SystemExit(str(error)) from None
