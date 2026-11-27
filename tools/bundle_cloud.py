"""Generate a small, allowlisted Worker multipart manifest. No credentials."""
import base64
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK_SHA = '31eef1a11301aea58e4df3e584ff16b285f3dc2585c21fca75b75d6583ca0f0b'


def main():
    files = {}
    for name in ('worker', 'api', 'state', 'provider', 'token_helper', 'caller_sessions'):
        files[name + '.py'] = ((ROOT / 'cloud' / (name + '.py')).read_bytes(), 'text/x-python')
    for name in ('stackchan_ai', 'stackchan_calls', 'caller_auth', 'agent_profile'):
        files[name + '.py'] = ((ROOT / 'service' / (name + '.py')).read_bytes(), 'text/x-python')
    for path in sorted((ROOT / 'service/agora_token').glob('*.py')):
        files['agora_token/' + path.name] = (path.read_bytes(), 'text/x-python')
    files['caller.html'] = ((ROOT / 'service/web/index.html').read_bytes(), 'text/plain')
    for name in ('stackchan.png', 'lines.png'):
        files[name] = ((ROOT / 'service/web' / name).read_bytes(), 'application/octet-stream')
    sdk = (ROOT / 'service/web/agora.js').read_bytes()
    if hashlib.sha256(sdk).hexdigest() != SDK_SHA:
        raise SystemExit('Web SDK checksum changed; review before packaging')
    files['agora.js.gz'] = (gzip.compress(sdk, mtime=0), 'application/octet-stream')
    manifest = [{'name': name, 'type': kind, 'base64': base64.b64encode(data).decode()}
                for name, (data, kind) in files.items()]
    encoded = json.dumps(manifest, separators=(',', ':')).encode()
    release = hashlib.sha256(encoded).hexdigest()[:16]
    out = ROOT / 'build/cloud'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'modules.json').write_bytes(encoded)
    (out / 'release.txt').write_text(release + '\n')
    print(json.dumps({'release': release, 'modules': len(manifest), 'upload_bytes': sum(len(v[0]) for v in files.values())}))


if __name__ == '__main__':
    main()
