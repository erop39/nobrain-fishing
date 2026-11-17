"""Integration checks. Never starts fishing, captures audio or sends keys."""
import copy
import http.client
import io
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest

import modern_desktop as ui

class SettingsTests(unittest.TestCase):
    def check(self, changes):
        return ui.validate_settings(changes, ui.DEFAULTS, ['System default'], [], ['Автоопределение WoW'])

    def test_related_timing_limits(self):
        self.assertEqual(self.check({'bite_delay_min': .7, 'bite_delay_max': 1.5})['bite_delay_max'], 1.5)
        for bad in ({'bite_delay_min': 1., 'bite_delay_max': .5},
                    {'early_bite_filter': True, 'timeout': 10, 'min_bite_seconds': 10},
                    {'threshold': float('nan')}, {'timeout': 3.5}, {'automation': 'true'},
                    {'audio_name': 'missing device'}, {'arbitrary': 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError): self.check(bad)

class HttpHandlerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = ui.Service(preview=True)
        cls.server = ui.make_server(cls.service)

    @classmethod
    def tearDownClass(cls):
        cls.server.server_close()

    def request(self, path, payload=None, headers=None):
        # Exercise the actual HTTP parser/handler in memory: this runner denies
        # outbound sockets, including loopback. No real connection is claimed.
        body = json.dumps(payload).encode() if payload is not None else b''
        h = {'X-Nobrain-Token': self.service.token,
             'Host': f'127.0.0.1:{self.server.server_port}',
             'Content-Length': str(len(body)), 'Connection': 'close'}
        h.update(headers or {})
        method = 'POST' if payload is not None else 'GET'
        raw = (f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k,v in h.items()) + '\r\n').encode() + body
        class MemorySocket:
            def __init__(self, incoming): self.incoming=incoming; self.output=bytearray()
            def makefile(self, *args): return io.BytesIO(self.incoming)
            def sendall(self, data): self.output.extend(data)
        transport = MemorySocket(raw)
        self.server.RequestHandlerClass(transport, ('127.0.0.1', 1), self.server)
        r = http.client.HTTPResponse(MemorySocket(bytes(transport.output)))
        r.begin()
        code, body, mime = r.status, r.read(), r.getheader('Content-Type')
        r.close()
        return code, body, mime

    def test_assets_and_utf8(self):
        for path, expected in (('/', 'text/html'), ('/style.css', 'text/css'),
                               ('/app.js', 'application/javascript'), ('/mark.svg', 'image/svg+xml')):
            code, body, mime = self.request(path)
            self.assertEqual(code, 200)
            self.assertTrue(body)
            self.assertEqual(mime.split(';')[0], expected)
        self.assertIn('Рыбалка', self.request('/')[1].decode('utf-8'))

    def test_security_boundaries(self):
        for headers in ({'X-Nobrain-Token': 'wrong'}, {'Origin': 'https://example.com'},
                        {'Host': 'example.com'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request('/api/state', headers=headers)[0], 403)
        self.assertEqual(self.request('/../settings.json')[0], 404)
        self.assertEqual(self.request('/api/command', {'action':'unknown'})[0], 400)

    def test_preview_settings_and_no_live_actions(self):
        r = self.request('/api/command', {'action': 'settings', 'settings': {'threshold': .78}})
        self.assertEqual(r[0], 200)
        self.assertEqual(json.loads(r[1])['settings']['threshold'], .78)
        for action in ('start', 'record', 'test_space'):
            self.assertEqual(self.request('/api/command', {'action': action})[0], 400)
        state = json.loads(self.request('/api/state')[1])
        self.assertFalse(state['running'])

class EngineIntegrationTests(unittest.TestCase):
    def test_real_engine_settings_samples_and_event_queue(self):
        import tkinter as tk
        from types import SimpleNamespace
        from tkinter import messagebox
        import remote_fishing as engine
        service = ui.Service()
        with tempfile.TemporaryDirectory() as temporary:
            old_config = engine.CONFIG_PATH
            old_hotkeys = engine.GlobalHotkeys
            old_message = {n:getattr(messagebox,n) for n in ('showerror','showwarning','showinfo')}
            engine.CONFIG_PATH = Path(temporary)/'settings.json'
            engine.GlobalHotkeys = lambda events: SimpleNamespace(close=lambda: None)
            for name,kind in [('showerror','error'),('showwarning','warning'),('showinfo','info')]:
                setattr(messagebox,name,lambda title='',message='',_kind=kind,**kw:service.message(_kind,title,message))
            class TestApp(engine.App):
                pillow_available = engine.PIL_AVAILABLE
                def setup_tray(self): pass
                def refresh_wow_processes(self):
                    self.wow_combo.configure(values=['Автоопределение WoW'])
            root=tk.Tk()
            root.withdraw()
            try:
                app=TestApp(root)
                root.withdraw()
                service.engine=app
                service.update()
                self.assertTrue(service.state['samples'])
                service.errors.clear()
                service.dispatch({'action':'settings','settings':{'threshold':.76,'bite_delay_min':.7,'bite_delay_max':1.5}})
                saved=json.loads(engine.CONFIG_PATH.read_text(encoding='utf-8'))
                self.assertEqual(saved['threshold'],.76)
                self.assertEqual(saved['bite_delay_min'],.7)
                self.assertFalse(app.running)
                sample=next(iter(app.sample_files))
                service.dispatch({'action':'settings','settings':{'sample_file':sample}})
                self.assertIsNotNone(app.sample)
                app.events.put(('stats',(12,9)))
                app.events.put(('score','Splash similarity: 0.780 / 0.760 | segment age 0.12s'))
                app.poll()
                service.update()
                self.assertEqual(service.state['stats']['bites'],9)
                self.assertIn('0.780',service.state['score'])
                service.dispatch({'action':'adjust_plus'})
                self.assertEqual(service.state['stats']['catches'],10)
                service.dispatch({'action':'reset_stats_confirmed'})
                self.assertEqual(service.state['stats'],dict(casts=0,bites=0,catches=0))
                # Check command dispatch without invoking actual monitoring or input.
                calls=[]
                app.start=lambda:calls.append('start')
                service.dispatch({'action':'start'})
                self.assertEqual(calls,['start'])
            finally:
                root.destroy()
                engine.CONFIG_PATH=old_config
                engine.GlobalHotkeys=old_hotkeys
                for name,value in old_message.items():setattr(messagebox,name,value)

if __name__=='__main__':unittest.main(verbosity=2)

