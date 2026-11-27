"""Local signed transport/tool tests. No paid model/phone request."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from xml.etree.ElementTree import fromstring

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator
import app
import conversation
import configure
from configure import validate_url
from private_files import is_private

CFG={'account_sid':'AC'+'1'*32,'auth_token':'synthetic-auth-token',
     'openai_api_key':'synthetic-never-sent','model':'gpt-6.1-terra',
     'public_base':'https://pilot.example.invalid'}


class FakeStream:
    def __init__(self, text='', calls=()):self.text=text;self.calls=calls
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def __aiter__(self):return self.events()
    async def events(self):
        if self.text:yield SimpleNamespace(type='response.output_text.delta',delta=self.text)
    async def get_final_response(self):return SimpleNamespace(output=list(self.calls))


class FakeClient:
    def __init__(self, responses):self.answers=list(responses);self.requests=[];self.responses=self
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def stream(self,**kwargs):self.requests.append(kwargs);return self.answers.pop(0)


class PilotTests(unittest.TestCase):
    def setUp(self):
        app.sessions.clear()
        self.patch=patch.object(app,'config',return_value=CFG);self.patch.start();self.addCleanup(self.patch.stop)
        self.client=TestClient(app.app)
        self.sid='CA'+'2'*32

    def post(self,path,extra=None):
        data={'AccountSid':CFG['account_sid'],'CallSid':self.sid,**(extra or {})}
        sig=RequestValidator(CFG['auth_token']).compute_signature(CFG['public_base']+path,data)
        return self.client.post(path,data=data,headers={'X-Twilio-Signature':sig})

    def begin(self):
        response=self.post('/voice');self.assertEqual(response.status_code,200)
        relay=fromstring(response.text).find('Connect/ConversationRelay')
        self.assertEqual(relay.attrib['url'],'wss://pilot.example.invalid/relay')
        return relay.find('Parameter').attrib['value']

    def ws(self):
        sig=RequestValidator(CFG['auth_token']).compute_signature(CFG['public_base']+'/relay',{})
        return self.client.websocket_connect('/relay',headers={'X-Twilio-Signature':sig})

    def setup(self,ws,nonce):
        ws.send_json({'type':'setup','accountSid':CFG['account_sid'],'callSid':self.sid,
                      'customParameters':{'nonce':nonce}})

    def test_invalid_signature_and_wrong_account_rejected(self):
        self.assertEqual(self.client.post('/voice',data={}).status_code,403)
        self.assertEqual(self.post('/voice',{'AccountSid':'AC-other'}).status_code,403)
        self.assertFalse(app.sessions)

    def test_unsigned_websocket_and_wrong_nonce_rejected(self):
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect('/relay'):pass
        self.begin()
        with self.assertRaises(WebSocketDisconnect):
            with self.ws() as ws:
                self.setup(ws,'wrong');ws.receive_json()

    def test_sources_exclude_staff_contacts_and_validate_hashes(self):
        content=app.knowledge()
        self.assertIn('KB-TERMINAL-LSPC15MF',content)
        self.assertIn('KB-PURCHASE',content)
        self.assertNotIn('KB-STAFF-CONTACTS',content)
        self.assertNotIn('support@lsp.io',content)

    @unittest.skipUnless(app.inventory.path.is_file(),
                         'real inventory not present at '+str(app.inventory.path))
    def test_read_only_inventory_tool_and_stream(self):
        call=SimpleNamespace(type='function_call',name='lookup_device',call_id='tool-1',
            arguments=json.dumps({'hospital':'RAH','bed_id':'7G13101','device_id':''}))
        fake=FakeClient([FakeStream(calls=[call]),FakeStream('The device was found; payment status is unknown.')])
        nonce=self.begin()
        with patch.object(conversation,'AsyncOpenAI',return_value=fake):
            with self.ws() as ws:
                self.setup(ws,nonce)
                ws.send_json({'type':'prompt','last':True,'voicePrompt':'Royal Adelaide, bed 7G13101.'})
                self.assertEqual(ws.receive_json()['type'],'text')
                self.assertTrue(ws.receive_json()['last'])
        output=fake.requests[1]['input'][-1]
        result=json.loads(output['output'])
        self.assertTrue(result['device_found']);self.assertFalse(result['account_state_known'])
        self.assertIsNone(result['balance_owing'])
        self.assertEqual({t['name'] for t in fake.requests[0]['tools']},{'lookup_device', 'finish_call', 'get_guide', 'report_problem', 'start_flow', 'flow_record', 'flow_confirm', 'flow_cancel'})
        self.assertFalse(fake.requests[0]['store'])

    def test_clinical_end_callback_is_fixed_and_ends_call(self):
        call=SimpleNamespace(type='function_call',name='finish_call',call_id='end-1',
                             arguments='{"reason":"clinical"}')
        nonce=self.begin()
        with patch.object(conversation,'AsyncOpenAI',return_value=FakeClient([FakeStream(calls=[call])])):
            with self.ws() as ws:
                self.setup(ws,nonce)
                ws.send_json({'type':'prompt','last':True,'voicePrompt':'I need a nurse.'})
                self.assertEqual(ws.receive_json()['type'],'end')
        end=self.post('/end');self.assertIn('nurse call button',end.text)
        self.assertIsNotNone(fromstring(end.text).find('Hangup'))

    def test_url_rejects_credentials_and_paths(self):
        for url in ['http://host','https://user:secret@host','https://host/voice','https://host?key=x']:
            with self.assertRaises(ValueError):validate_url(url)
        self.assertEqual(validate_url('https://pilot.example.invalid/'),'https://pilot.example.invalid')

    def test_interruption_cancels_unheard_generation_and_repairs_history(self):
        class BlockingStream(FakeStream):
            async def events(self):
                yield SimpleNamespace(type='response.output_text.delta',delta='Hello, this part was not heard.')
                await asyncio.Event().wait()
        fake=FakeClient([BlockingStream(),FakeStream('What would you like to check?')])
        nonce=self.begin()
        with patch.object(conversation,'AsyncOpenAI',return_value=fake):
            with self.ws() as ws:
                self.setup(ws,nonce)
                ws.send_json({'type':'prompt','last':True,'voicePrompt':'Hello.'})
                ws.receive_json()
                ws.send_json({'type':'interrupt','utteranceUntilInterrupt':'Hello'})
                ws.send_json({'type':'prompt','last':True,'voicePrompt':'Wait, another question.'})
                ws.receive_json();self.assertTrue(ws.receive_json()['last'])
        previous=[x for x in fake.requests[1]['input'] if x.get('role')=='assistant']
        self.assertEqual(previous,[{'role':'assistant','content':'Hello'}])

    def test_setup_stores_secrets_privately_without_printing(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'config.json'
            with patch.object(configure,'CONFIG',path),patch('sys.argv',['configure.py']),\
                 patch.object(configure,'getpass',side_effect=['AC'+'1'*32,'private-token','private-key']),\
                 patch('builtins.input',return_value=''),contextlib.redirect_stdout(io.StringIO()) as out:
                configure.main()
            self.assertTrue(is_private(path))
            self.assertNotIn('private-token',out.getvalue());self.assertNotIn('private-key',out.getvalue())
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['model'],'gpt-6.1-terra')


if __name__=='__main__':unittest.main()
