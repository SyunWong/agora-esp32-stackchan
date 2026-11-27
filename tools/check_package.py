"""Regression checks for the exported source package. Run after fetch_web_sdk.py."""
import importlib.util
import base64
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

import deploy_cloud

ROOT=Path(__file__).resolve().parents[1]


def main():
    if not (ROOT/'.recipe-root').is_file():
        raise SystemExit('Run in the exported recipe checkout')
    if not (ROOT/'service/web/agora.js').is_file():
        raise SystemExit('Fetch the pinned Web SDK first')
    with tempfile.TemporaryDirectory(prefix='stackchan-package-test-') as d:
        p=Path(d)
        for name in ('cloud','service','tools'):
            shutil.copytree(ROOT/name,p/name,ignore=shutil.ignore_patterns('__pycache__'))
        (p/'.recipe-root').write_text('test')
        (p/'private').mkdir()
        bindings={'PUBLIC_ORIGIN':'https://robot.example.com','DEVICE_ID':'stackchan-main','ENABLED':'false',
            'AGORA_APP_ID':'a'*32,'AGORA_APP_CERTIFICATE':'b'*32,'DEEPSEEK_API_KEY':'synthetic-deepseek',
            'FISH_AUDIO_API_KEY':'synthetic-fish','DEVICE_KEY':'c'*64,'CALLER_PASSWORD':'synthetic-password'}
        (p/'private/bindings.json').write_text(json.dumps(bindings))
        index=p/'service/web/index.html'; index.write_text(index.read_text()+'\n<!-- index-source-regression -->\n')
        # Even a stale compatibility file must never become the published page.
        (p/'service/web/caller.html').write_text('STALE PAGE')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):modules,meta,release=deploy_cloud.prepare(p)
        page=next(base64.b64decode(m['base64']) for m in modules if m['name']=='caller.html')
        assert b'index-source-regression' in page and b'STALE PAGE' not in page
        byname={b['name']:b for b in meta['bindings']}
        assert all(byname[k]['type']=='secret_text' for k in deploy_cloud.SECRET_NAMES)
        assert byname['ENABLED']['text']=='true'
        assert meta['migrations']=={'new_tag':'v1','new_sqlite_classes':['StackChan']}
        assert meta['observability']['traces']['enabled'] is True
        assert all(bindings[k] not in output.getvalue() for k in deploy_cloud.SECRET_NAMES)
        # Exercise CLI default mode; mock request/hidden so any network/auth attempt fails.
        with patch.object(deploy_cloud,'ROOT',p), patch.object(sys,'argv',
             ['deploy_cloud.py','--account-id','d'*32,'--name','test-robot']), \
             patch.object(deploy_cloud,'create',side_effect=AssertionError('default mode attempted deploy')), \
             patch.object(deploy_cloud,'hidden',side_effect=AssertionError('default mode requested API token')), \
             contextlib.redirect_stdout(output):
            deploy_cloud.main()
        assert not list((p/'private').glob('deploy-*'))
    print('Package regressions passed: index.html source, secret binding types, migration, offline deployment default.')


if __name__=='__main__':main()
