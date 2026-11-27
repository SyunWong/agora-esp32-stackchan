"""Fetch the pinned browser SDK; validate before writing. No secrets or services."""
import hashlib
import io
from pathlib import Path
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://registry.npmjs.org/agora-rtc-sdk-ng/-/agora-rtc-sdk-ng-4.24.10.tgz'
SHA = '31eef1a11301aea58e4df3e584ff16b285f3dc2585c21fca75b75d6583ca0f0b'


def main():
    if not (ROOT / '.recipe-root').is_file():
        raise SystemExit('Run this in an exported recipe checkout')
    with urllib.request.urlopen(URL, timeout=30) as response:
        raw = response.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise SystemExit('Unexpected package size')
    # Read only a checksum-matching JS member; never extract archive paths.
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        matches = []
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith('.js') and member.size < 10 * 1024 * 1024:
                data = archive.extractfile(member).read()
                if hashlib.sha256(data).hexdigest() == SHA:
                    matches.append(data)
    if not matches:
        raise SystemExit('Pinned SDK not found; no file written')
    path = ROOT / 'service/web/agora.js'
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != SHA:
        raise SystemExit('Existing different SDK preserved')
    path.write_bytes(matches[0])
    print('Pinned Agora Web SDK verified and saved.')


if __name__ == '__main__':
    main()
