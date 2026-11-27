"""Prepare a NEW recipe checkout. No network, deployment or flash operations."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import time
import warnings
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def origin(value):
    u = urlsplit(value)
    try:
        u.port
    except ValueError:
        raise ValueError('Invalid HTTPS port') from None
    if (value != 'https://' + u.netloc or u.scheme != 'https' or not u.hostname or u.username or u.password or
            u.path or u.query or u.fragment or u.netloc != u.netloc.lower()):
        raise ValueError('Use an exact HTTPS origin without a trailing slash')
    return value


def hidden(label):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        value = getpass.getpass(label + ': ')
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError('A non-empty value without control characters is required')
    return value


def private_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive creation prevents silently rotating credentials or overwriting a setup.
    with path.open('x', encoding='utf-8') as stream:
        os.chmod(path, 0o600)
        stream.write(text)


def bindings(public_origin, values):
    origin(public_origin)
    for key in ('AGORA_APP_ID', 'AGORA_APP_CERTIFICATE'):
        if not re.fullmatch(r'[0-9a-fA-F]{32}', values[key]):
            raise ValueError('Agora credentials must be 32 hexadecimal characters')
    pw = values['CALLER_PASSWORD']
    if not 1 <= len(pw) <= 128 or len(pw.encode()) > 256:
        raise ValueError('Website password exceeds the supported length')
    return dict(values, PUBLIC_ORIGIN=public_origin, DEVICE_ID='stackchan-main',
                DEVICE_KEY=secrets.token_hex(32), ENABLED='false')


def c_string(value):
    # Fixed-width octal escapes encode the actual UTF-8 bytes, including emoji.
    # JSON surrogate escapes are not valid C; hex escapes can consume following digits.
    return '"' + ''.join('\\%03o' % byte for byte in value.encode('utf-8')) + '"'


def header(control_origin, key, ssid, password):
    origin(control_origin)
    if not re.fullmatch(r'[0-9a-f]{64}', key):
        raise ValueError('Invalid device key')
    if not 1 <= len(ssid.encode()) <= 32 or not 8 <= len(password.encode()) <= 63:
        raise ValueError('Unsupported Wi-Fi credential lengths')
    values = {'STACKCHAN_WIFI_SSID': ssid, 'STACKCHAN_WIFI_PASSWORD': password,
              'STACKCHAN_CONTROL_URL': control_origin, 'STACKCHAN_CONTROL_KEY': key}
    return '#pragma once\n' + ''.join('#define ' + k + ' ' + c_string(v) + '\n'
            for k, v in values.items()) + '#define STACKCHAN_WIFI_OVERRIDE 1\n' + \
            '#define STACKCHAN_BUILD_UNIX_TIME ' + str(int(time.time())) + '\n'


def firmware_config(app):
    pieces = [app / n for n in ('sdkconfig.defaults', 'sdkconfig.defaults.esp32s3',
              'components/gen_bmgr_codes/board_manager.defaults')]
    text = '\n'.join(p.read_text() for p in pieces)
    if 'CONFIG_ESP_BOARD_M5STACK_CORES3=y' not in text:
        raise ValueError('Generate the CoreS3 board profile first')
    overrides = {'CONFIG_STACKCHAN_AUDIO_PROBE': 'n', 'CONFIG_STACKCHAN_INTERACTIVE_AI': 'y',
        'CONFIG_STACKCHAN_STATUS_SCREEN': 'y', 'CONFIG_STACKCHAN_HEAD_MOTION': 'n',
        'CONFIG_STACKCHAN_INTERACTIVE_TEST_SECONDS': '0',
        'CONFIG_STACKCHAN_MEDIA_DIAGNOSTICS': 'n', 'CONFIG_STACKCHAN_NETWORK_DIAGNOSTICS': 'n',
        'CONFIG_STACKCHAN_TLS_HANDSHAKE_DIAGNOSTICS': 'n',
        'CONFIG_ESPTOOLPY_FLASHMODE_QIO': 'n', 'CONFIG_ESPTOOLPY_FLASHMODE_DIO': 'y'}
    for name, value in overrides.items():
        text = re.sub(r'^(?:' + name + r'=.*|# ' + name + r' is not set)\n?', '', text, flags=re.M)
        text += '\n' + (f'# {name} is not set' if value == 'n' else f'{name}={value}') + '\n'
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('cloud'); p.add_argument('--origin', required=True)
    p = sub.add_parser('firmware'); p.add_argument('--control-origin', required=True)
    args = parser.parse_args()
    if not (ROOT / '.recipe-root').is_file():
        parser.error('Run this only in an exported recipe checkout')
    os.umask(0o077)
    target = ROOT / 'private/bindings.json'
    if args.command == 'cloud':
        origin(args.origin)
        if target.exists():
            parser.error('Existing bindings preserved; refusing to overwrite')
        names = ('AGORA_APP_ID', 'AGORA_APP_CERTIFICATE', 'DEEPSEEK_API_KEY',
                 'FISH_AUDIO_API_KEY', 'CALLER_PASSWORD')
        values = {name: hidden(name) for name in names}
        private_write(target, json.dumps(bindings(args.origin, values), indent=2) + '\n')
    else:
        origin(args.control_origin)
        app = ROOT / 'esp-webrtc-solution/solutions/agora_demo'
        hp, cp = app / 'main/stackchan_session_local.h', app / 'sdkconfig'
        if hp.exists() or cp.exists():
            parser.error('Existing firmware configuration preserved; refusing to overwrite')
        config = firmware_config(app)
        b = json.loads(target.read_text())
        h = header(args.control_origin, b['DEVICE_KEY'], hidden('Wi-Fi SSID'), hidden('Wi-Fi password'))
        private_write(hp, h); private_write(cp, config)
    print('Private configuration prepared; values suppressed. Nothing deployed or flashed.')


if __name__ == '__main__':
    main()
