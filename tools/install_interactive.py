"""Portable app-only installer for the documented 16 MiB StackChan layout.

Default install/restore is offline planning. backup reads the selected device.
Only install/restore --apply can write, and only ota_0 at 0x20000. No erasing,
partition rewriting, OTA switching, calibration changes or firmware downloads.
"""
import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SIZE, OFFSET, CAPACITY = 0x1000000, 0x20000, 0x4f0000
LAYOUT = [(1, 2, 0x9000, 0x4000, 'nvs'), (1, 0, 0xd000, 0x2000, 'otadata'),
          (1, 1, 0xf000, 0x1000, 'phy_init'), (0, 0x10, OFFSET, CAPACITY, 'ota_0'),
          (0, 0x11, 0x510000, CAPACITY, 'ota_1'), (1, 0x82, 0xa00000, 0x400000, 'assets'),
          (1, 3, 0xe00000, 0x10000, 'coredump')]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def identity(value):
    result = value.replace(':', '').lower()
    if not re.fullmatch(r'[0-9a-f]{12}', result):
        raise ValueError('USB identity must be a 12-hex-digit hardware identity')
    return result


def port_for(usb, timeout=30):
    from serial.tools import list_ports
    deadline = time.monotonic() + timeout
    while True:
        ports = [p.device for p in list_ports.comports()
                 if (p.serial_number or '').replace(':', '').lower() == usb]
        if len(ports) > 1:
            raise RuntimeError('USB identity is ambiguous')
        if ports:
            return ports[0]
        if time.monotonic() >= deadline:
            raise RuntimeError('Selected USB identity is not connected')
        time.sleep(.25)


def tool(usb, folder, label, args):
    # No shell, no cached port path, no write retries. Raw logs stay private.
    command = [sys.executable, '-m', 'esptool', '--chip', 'esp32s3', '--port', port_for(usb),
               '--baud', '460800', '--before', 'usb-reset', '--after', 'watchdog-reset'] + args
    with (folder / (label + '.log')).open('w') as stream:
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        raise RuntimeError('Device operation failed: ' + label + '; inspect the private log')


def validate_table(table):
    if len(table) != 4096:
        raise ValueError('Invalid partition table length')
    entries = []
    for offset in range(0, 4096, 32):
        entry = table[offset:offset+32]
        if entry[:2] == b'\xeb\xeb':
            if entry[2:16] != b'\xff'*14 or entry[16:] != hashlib.md5(table[:offset]).digest():
                raise ValueError('Partition table checksum mismatch')
            if table[offset+32:] != b'\xff'*(4096-offset-32):
                raise ValueError('Unexpected data after partition table')
            break
        if entry[:2] != b'\xaa\x50':
            raise ValueError('Missing partition entry or checksum')
        _, kind, subtype, start, size, label, flags = struct.unpack('<HBBII16sI', entry)
        if flags != 0:
            raise ValueError('Encrypted or flagged partition is unsupported')
        entries.append((kind, subtype, start, size, label.rstrip(b'\0').decode('ascii')))
    else:
        raise ValueError('Missing partition table checksum')
    if entries != LAYOUT:
        raise ValueError('Unsupported layout; no partition changes will be attempted')


def require_ota0(data):
    if len(data) != 8192:
        raise ValueError('Invalid OTA selection data')
    sequences = []
    for offset in (0, 4096):
        row = data[offset:offset+32]
        if row == b'\xff'*32:
            continue
        seq, state, crc = struct.unpack('<I20xII', row)
        expected = binascii.crc32(struct.pack('<I', seq), 0xffffffff) & 0xffffffff
        if not 0 < seq < 0xffffffff or crc != expected or state not in (2, 0xffffffff):
            raise ValueError('Ambiguous, pending or failed OTA state; refusing installation')
        sequences.append(seq)
    if sequences and (max(sequences)-1) % 2 != 0:
        raise ValueError('Device selects ota_1; tool will not alter OTA selection')


def validate_image(data, padded=False):
    if not 24 < len(data) <= CAPACITY or data[0] != 0xe9 or int.from_bytes(data[12:14], 'little') != 9:
        raise ValueError('Invalid ESP32-S3 application image or size')
    count = data[1]
    if not 1 <= count <= 16:
        raise ValueError('Invalid image segment count')
    cursor, checksum = 24, 0xef
    for _ in range(count):
        if cursor+8 > len(data):
            raise ValueError('Truncated image segment')
        length = int.from_bytes(data[cursor+4:cursor+8], 'little'); cursor += 8
        if length > len(data)-cursor:
            raise ValueError('Truncated image payload')
        for byte in data[cursor:cursor+length]:
            checksum ^= byte
        cursor += length
    end = (cursor+16) & ~15
    if end > len(data) or data[end-1] != checksum:
        raise ValueError('Image checksum mismatch')
    if data[23] != 1 or end+32 > len(data) or hashlib.sha256(data[:end]).digest() != data[end:end+32]:
        raise ValueError('Image requires a valid appended SHA256')
    length = end+32
    # A previously shorter flash write may leave old bytes beyond the image.
    # Preserve those bytes in rollback; they are not part of the image digest.
    if not padded and length != len(data):
        raise ValueError('Unexpected bytes after application image')
    return length


def validate_backup(folder, usb):
    meta = json.loads((folder / 'backup.json').read_text())
    data = (folder / 'flash.bin').read_bytes()
    if meta.get('usb_identity') != usb or len(data) != SIZE or sha(data) != meta.get('sha256'):
        raise ValueError('Backup identity, size or hash mismatch')
    validate_table(data[0x8000:0x9000]); require_ota0(data[0xd000:0xf000])
    validate_image(data[OFFSET:OFFSET+CAPACITY], padded=True)
    return data


def security(usb, out, run):
    run(usb, out, 'security', ['get-security-info'])
    text = (out/'security.log').read_text()
    if 'Secure Boot: Disabled' not in text or 'Flash Encryption: Disabled' not in text:
        raise ValueError('Cannot confirm secure boot and flash encryption are disabled')


def backup(usb, folder, run=tool):
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    security(usb, folder, run)
    run(usb, folder, 'flash-id', ['flash-id'])
    if not re.search(r'Detected flash size:\s*16\s*MB', (folder/'flash-id.log').read_text(), re.I):
        raise ValueError('Expected 16 MiB flash not detected')
    path = folder/'flash.bin'
    run(usb, folder, 'backup', ['read-flash', '0', hex(SIZE), str(path)])
    data = path.read_bytes()
    if len(data) != SIZE:
        raise ValueError('Incomplete backup')
    # Save the backup even if the device is unsupported; never modify its contents.
    (folder/'backup.json').write_text(json.dumps({'usb_identity': usb, 'bytes': len(data), 'sha256': sha(data)}, indent=2)+'\n')
    validate_backup(folder, usb)
    print('Full backup saved and checked. No flash writes were performed.')


def write_status(out, status, image):
    p = out/'result.json.tmp'
    p.write_text(json.dumps({'status': status, 'candidate_sha256': sha(image),
                            'app_offset': hex(OFFSET)}, indent=2)+'\n')
    p.replace(out/'result.json')


def install(usb, folder, image, out, run=tool):
    original = validate_backup(folder, usb)
    validate_image(image)
    out.mkdir(parents=True, exist_ok=False, mode=0o700)
    candidate, rollback = out/'candidate.bin', out/'restore-app.bin'
    candidate.write_bytes(image); rollback.write_bytes(original[OFFSET:OFFSET+CAPACITY])
    write_status(out, 'preflight', image)
    try:
        security(usb, out, run)
        check = out/'before-app.bin'
        run(usb, out, 'preflight-read', ['read-flash', '0', hex(OFFSET), str(check)])
        current = check.read_bytes()
        if len(current) != OFFSET:
            raise ValueError('Incomplete preflight read')
        # NVS may legitimately change; bootloader and partition layout must not.
        if current[:0x9000] != original[:0x9000]:
            raise ValueError('Bootloader or partition table differs from backup')
        require_ota0(current[0xd000:0xf000])
        # Roll back to the app present immediately before this attempt, not an old backup app.
        run(usb, out, 'read-current-app', ['read-flash', hex(OFFSET), hex(CAPACITY), str(rollback)])
        live_app = rollback.read_bytes()
        if len(live_app) != CAPACITY:
            raise ValueError('Incomplete current-app backup')
        validate_image(live_app, padded=True)
    except BaseException:
        write_status(out, 'refused_before_write', image)
        raise
    write_status(out, 'installing', image)
    try:
        run(usb, out, 'write-candidate', ['write-flash', hex(OFFSET), str(candidate)])
        run(usb, out, 'verify-candidate', ['verify-flash', hex(OFFSET), str(candidate)])
    except BaseException:
        write_status(out, 'recovery_pending', image)
        try:
            run(usb, out, 'restore-current-app', ['write-flash', hex(OFFSET), str(rollback)])
            run(usb, out, 'verify-restored-app', ['verify-flash', hex(OFFSET), str(rollback)])
        except BaseException:
            write_status(out, 'recovery_required_keep_usb_connected', image)
            raise
        write_status(out, 'previous_app_restored', image)
        raise
    write_status(out, 'installed_verified', image)
    print('Application installed and verified. Evidence: ' + str(out))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup', 'install', 'restore'))
    parser.add_argument('--usb-identity', required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--apply', action='store_true')
    a = parser.parse_args()
    if not (ROOT/'.recipe-root').is_file():
        parser.error('Run only in an exported recipe checkout')
    usb = identity(a.usb_identity)
    private = (ROOT/'private').resolve()
    folder = a.backup_dir.resolve()
    if private not in folder.parents:
        parser.error('Backup directory must be inside this checkout private/ directory')
    os.umask(0o077)
    if a.action == 'backup':
        if a.candidate or a.apply:
            parser.error('backup needs neither --candidate nor --apply; it only reads flash')
        backup(usb, folder)
        return
    data = validate_backup(folder, usb)
    if a.action == 'install':
        if not a.candidate:
            parser.error('install requires --candidate')
        image = a.candidate.read_bytes()
    else:
        if a.candidate:
            parser.error('restore uses the verified backup, not --candidate')
        app = data[OFFSET:OFFSET+CAPACITY]
        image = app[:validate_image(app, padded=True)]
    validate_image(image)
    print(json.dumps({'action': a.action, 'usb_identity': usb, 'sha256': sha(image),
                      'bytes': len(image), 'offset': hex(OFFSET), 'apply': a.apply}))
    if a.apply:
        out = private / ('install-' + str(time.time_ns()))
        install(usb, folder, image, out)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        main()
    except (ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None
