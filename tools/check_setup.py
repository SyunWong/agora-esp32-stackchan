"""Offline checks for portable setup logic; uses synthetic credentials only."""
import tempfile
from pathlib import Path
import configure

v = dict(AGORA_APP_ID='a'*32, AGORA_APP_CERTIFICATE='b'*32,
         DEEPSEEK_API_KEY='synthetic-llm', FISH_AUDIO_API_KEY='synthetic-tts', CALLER_PASSWORD='test password')
a = configure.bindings('https://robot.example.com', v)
b = configure.bindings('https://robot.example.com', v)
assert a['DEVICE_KEY'] != b['DEVICE_KEY'] and a['ENABLED'] == 'false'
for bad in ('http://robot.example.com', 'https://user:pw@robot.example.com',
            'https://robot.example.com/', 'https://robot.example.com?q=1'):
    try: configure.origin(bad)
    except ValueError: pass
    else: raise AssertionError('accepted invalid origin')
h = configure.header('https://robot.example.com', a['DEVICE_KEY'], 'test"network', 'password\\123')
assert 'AGORA_APP_CERTIFICATE' not in h and '#define STACKCHAN_WIFI_OVERRIDE 1' in h
assert configure.c_string('test"network') in h
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / 'private/settings'
    configure.private_write(p, 'first')
    try: configure.private_write(p, 'second')
    except FileExistsError: pass
    else: raise AssertionError('overwrote private settings')
    assert p.read_text() == 'first' and p.stat().st_mode & 0o777 == 0o600
    app = Path(d) / 'app'; (app / 'components/gen_bmgr_codes').mkdir(parents=True)
    (app / 'sdkconfig.defaults').write_text('CONFIG_STACKCHAN_AUDIO_PROBE=y\nCONFIG_STACKCHAN_HEAD_MOTION=y\n')
    (app / 'sdkconfig.defaults.esp32s3').write_text('')
    (app / 'components/gen_bmgr_codes/board_manager.defaults').write_text('CONFIG_ESP_BOARD_M5STACK_CORES3=y\nCONFIG_ESPTOOLPY_FLASHMODE_QIO=y\n')
    c = configure.firmware_config(app)
    assert 'CONFIG_STACKCHAN_AUDIO_PROBE=y' not in c and 'CONFIG_STACKCHAN_HEAD_MOTION=y' not in c
    assert 'CONFIG_ESPTOOLPY_FLASHMODE_QIO=y' not in c and 'CONFIG_STACKCHAN_INTERACTIVE_AI=y' in c
# Compile and run a tiny native program to prove byte-for-byte UTF-8 round trips.
import shutil
import subprocess
cc = shutil.which('cc')
if cc is None:
    raise SystemExit('A C compiler is required for the generated-header regression check')
with tempfile.TemporaryDirectory() as d:
    p = Path(d)
    ssid, password = 'Desk 🤖中文"\\9', 'pass🤖word\\123'
    (p / 'generated.h').write_text(configure.header('https://robot.example.com', a['DEVICE_KEY'], ssid, password))
    (p / 'check.c').write_text('#include "generated.h"\n#include <stdio.h>\n#include <string.h>\nint main(void) { fwrite(STACKCHAN_WIFI_SSID,1,strlen(STACKCHAN_WIFI_SSID),stdout); putchar(0); fwrite(STACKCHAN_WIFI_PASSWORD,1,strlen(STACKCHAN_WIFI_PASSWORD),stdout); }\n')
    subprocess.run([cc, str(p/'check.c'), '-o', str(p/'check')], check=True, capture_output=True)
    assert subprocess.check_output([str(p/'check')]) == ssid.encode()+b'\0'+password.encode()
print('Portable setup checks passed (offline; no credentials, calls or hardware).')
