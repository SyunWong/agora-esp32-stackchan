"""Offline failure-injection tests. No network, serial access or live credentials."""
import base64
import binascii
from email import policy
from email.parser import BytesParser
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

import deploy_cloud as cloud
import install_interactive as device


def image(seed=1):
    header = bytearray(24); header[0]=0xe9; header[1]=1; header[12]=9; header[23]=1
    payload=bytes([seed])*64
    data=bytes(header)+struct.pack('<II',0x3c000000,len(payload))+payload
    checksum=0xef
    for byte in payload: checksum ^= byte
    end=(len(data)+16)&~15
    data+=b'\0'*(end-len(data)-1)+bytes([checksum])
    return data+hashlib.sha256(data).digest()


def flash():
    blob=bytearray(b'\xff'*device.SIZE)
    table=b''.join(struct.pack('<HBBII16sI',0x50aa,*row[:4],row[4].encode(),0) for row in device.LAYOUT)
    table+=b'\xeb\xeb'+b'\xff'*14+hashlib.md5(table).digest()
    blob[0x8000:0x8000+len(table)]=table
    app=image();blob[device.OFFSET:device.OFFSET+len(app)]=app
    return bytes(blob)


class CloudTests(unittest.TestCase):
    def test_multipart(self):
        modules=[{'name':'worker.py','type':'text/x-python','base64':base64.b64encode(b'print("ok")').decode()},
                 {'name':'robot.png','type':'application/octet-stream','base64':base64.b64encode(b'\x00\xff\r\n').decode()}]
        metadata={'bindings':[{'name':'TEST','type':'secret_text','text':'synthetic-secret'}]}
        body,kind=cloud.multipart(modules,metadata)
        msg=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+kind+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+body)
        parts={p.get_param('name',header='content-disposition'):p.get_payload(decode=True) for p in msg.iter_parts()}
        self.assertEqual(json.loads(parts['metadata']),metadata)
        self.assertEqual(parts['robot.png'],b'\x00\xff\r\n')

    def test_existing_or_partial_inventory_never_writes(self):
        for listing in ({'result':[{'id':'robot'}]}, {'result':[], 'result_info':{'total_count':2}},
                        {'result':[], 'result_info':{'cursor':'next'}}):
            calls=[]
            def fake(*args): calls.append(args[0]);return listing
            with self.assertRaises(RuntimeError):cloud.create('a'*32,'robot',[],{},'synthetic',fake)
            self.assertEqual(calls,['GET'])

    def test_upload_once(self):
        calls=[]
        def fake(*args):
            calls.append(args[0])
            if args[0]=='GET':return {'result':[]}
            raise RuntimeError('synthetic timeout')
        with self.assertRaises(RuntimeError):cloud.create('a'*32,'new-robot',[],{},'synthetic',fake)
        self.assertEqual(calls,['GET','PUT'])

    def test_reject_module_injection(self):
        with self.assertRaises(ValueError):
            cloud.multipart([{'name':'bad"\r\n','type':'text/plain','base64':''}],{})


class DeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.data=flash()

    def test_layout_and_image(self):
        device.validate_table(self.data[0x8000:0x9000])
        device.validate_image(image())
        device.validate_image(image()+b'old remaining bytes',padded=True)
        for bad in (image()[:-1], image()+b'extra', b'\xe9'+image()[1:12]+b'\x00'+image()[13:]):
            with self.assertRaises(ValueError):device.validate_image(bad)
        broken=bytearray(self.data[0x8000:0x9000]);broken[5]^=1
        with self.assertRaises(ValueError):device.validate_table(broken)

    def test_ota_selection(self):
        device.require_ota0(b'\xff'*8192)
        for seq,state,ok in ((1,2,True),(2,2,False),(3,2,True),(1,1,False),(1,3,False)):
            row=struct.pack('<I20xII',seq,state,binascii.crc32(struct.pack('<I',seq),0xffffffff)&0xffffffff)
            data=row+b'\xff'*(8192-32)
            if ok:device.require_ota0(data)
            else:
                with self.assertRaises(ValueError):device.require_ota0(data)

    def exercise(self, failure=None):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);backup=root/'backup';backup.mkdir()
            (backup/'flash.bin').write_bytes(self.data)
            (backup/'backup.json').write_text(json.dumps({'usb_identity':'a'*12,'sha256':device.sha(self.data)}))
            calls=[]; live=image(2)+b'\xff'*(device.CAPACITY-len(image(2)))
            def fake(usb,out,label,args):
                calls.append((label,args[:]))
                if label in (failure if isinstance(failure, tuple) else (failure,)):raise RuntimeError('synthetic operation failure')
                if label=='security':(out/'security.log').write_text('Secure Boot: Disabled\nFlash Encryption: Disabled\n')
                elif label=='preflight-read':Path(args[-1]).write_bytes(self.data[:device.OFFSET])
                elif label=='read-current-app':Path(args[-1]).write_bytes(live)
                elif label=='restore-current-app':self.assertEqual(Path(args[-1]).read_bytes(),live)
                if args[0] in ('write-flash','verify-flash'):self.assertEqual(args[1],'0x20000')
            out=root/'attempt'
            if failure:
                with self.assertRaises(RuntimeError):device.install('a'*12,backup,image(3),out,fake)
            else:device.install('a'*12,backup,image(3),out,fake)
            status=json.loads((out/'result.json').read_text())['status']
            return calls,status

    def test_success_writes_only_app(self):
        calls,status=self.exercise()
        self.assertEqual(status,'installed_verified')
        self.assertEqual([x[0] for x in calls if x[1][0]=='write-flash'],['write-candidate'])

    def test_preflight_failure_never_writes(self):
        calls,status=self.exercise('preflight-read')
        self.assertEqual(status,'refused_before_write')
        self.assertFalse(any(x[1][0]=='write-flash' for x in calls))

    def test_failure_restores_immediately_previous_app(self):
        calls,status=self.exercise('verify-candidate')
        self.assertEqual(status,'previous_app_restored')
        self.assertEqual([x[0] for x in calls if x[1][0]=='write-flash'],['write-candidate','restore-current-app'])

    def test_unfinished_recovery_is_reported(self):
        calls,status=self.exercise(('verify-candidate','restore-current-app'))
        self.assertEqual(status,'recovery_required_keep_usb_connected')

    def test_backup_identity_and_hash(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'flash.bin').write_bytes(self.data)
            (p/'backup.json').write_text(json.dumps({'usb_identity':'b'*12,'sha256':device.sha(self.data)}))
            with self.assertRaises(ValueError):device.validate_backup(p,'a'*12)


if __name__=='__main__':unittest.main()
