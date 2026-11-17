"""Exercise the actual monitoring loop with a simulated clock; no keys/audio are sent."""
import ast
import math
from pathlib import Path
import queue
import random
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

tree = ast.parse(Path('remote_fishing.py').read_text(encoding='utf-8'))
app_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'App')
monitor_node = next(n for n in app_node.body if isinstance(n, ast.FunctionDef) and n.name == 'monitor')
module = ast.fix_missing_locations(ast.Module(body=[monitor_node], type_ignores=[]))

class Clock:
    def __init__(self, manual_at=None, hwnd=42): self.t=0.; self.manual_at=manual_at; self.hwnd=hwnd; self.spaces=queue.Queue(); self.sent=False
    def monotonic(self): return self.t
    def sleep(self, duration):
        self.t += duration
        if self.manual_at is not None and not self.sent and self.t >= self.manual_at:
            self.sent=True; self.spaces.put((self.t, self.hwnd))
        if self.t > 100: raise AssertionError('monitor failed to stop')

def run_session(manual_at=None, splash=False, limit=65, hwnd=42, auto_focus=True):
    clock=Clock(manual_at, hwnd)
    sent=[]
    self=SimpleNamespace(stop=threading.Event(),paused=threading.Event(),events=queue.Queue(),
         sample=None,auto_focus_on_start=SimpleNamespace(get=lambda:auto_focus),
         hotkey_listener=SimpleNamespace(manual_spaces=clock.spaces))
    env=dict(time=clock,queue=queue,random=random,math=math,nullcontext=nullcontext,SAMPLE_RATE=44100,
        np=SimpleNamespace(zeros=lambda *a:0.,float32=float,sqrt=math.sqrt,mean=lambda x:x),
        ctypes=SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(GetForegroundWindow=lambda:42))),
        initialize_com=lambda:False, emergency_stop_pressed=lambda:False, foreground_is_remote=lambda _:True,
        foreground_title=lambda:'RustDesk',remote_client_from_title=lambda _: 'RustDesk',
        send_space=lambda _: (sent.append(clock.t) or 1,1),
        crop_visual_frame=lambda _: 1., visual_change_score=lambda *_: 1. if splash else 0.,
        traceback=__import__('traceback'))
    exec(compile(module,'remote_fishing.py','exec'),env)
    timing=dict(no_splash_stop=True,no_splash_seconds=limit,manual_space_reset=True,
        bite_delay_min=0,bite_delay_max=0,recast_delay_min=0,recast_delay_max=0,
        early_bite_filter=False,min_bite_seconds=0)
    env['monitor'](self,'',.7,30,timing,True,'ScanCode',False,3.,'Auto',
                   'Пиксели (эксперимент)',(1,2,3,4),.5,False,None)
    events=list(self.events.queue)
    return sent,events,clock

class SessionTests(unittest.TestCase):
    def test_no_splash_stops_and_does_not_send_again(self):
        sent,events,clock=run_session(limit=65)
        self.assertEqual(len(sent),3)
        self.assertGreaterEqual(clock.t,65)
        self.assertTrue(any(a=='finished' and 'нет всплесков' in b for a,b in events))
        self.assertLess(sent[-1],65)
    def test_manual_space_resets_cast_but_not_safety_timer(self):
        sent,events,clock=run_session(manual_at=20,limit=65)
        self.assertEqual(len(sent),2)
        self.assertGreaterEqual(sent[1],50)
        self.assertLess(sent[1],51)
        self.assertTrue(any(a=='log' and 'Manual SPACE' in b for a,b in events))
        self.assertLess(clock.t,66)
    def test_confirmed_splash_resets_watchdog(self):
        sent,events,clock=run_session(splash=True,limit=10)
        self.assertTrue(any(a=='log' and 'DETECT | BITE' in b for a,b in events))
        self.assertGreater(clock.t,10)
    def test_foreign_window_space_is_ignored(self):
        sent,events,_=run_session(manual_at=20,hwnd=99)
        self.assertEqual(len(sent),3)
        self.assertLess(sent[1],31)
        self.assertFalse(any(a=='log' and 'Manual SPACE' in b for a,b in events))
    def test_manual_cast_during_countdown_cancels_initial_auto_cast(self):
        sent,events,_=run_session(manual_at=.5,auto_focus=False,limit=35)
        self.assertEqual(len(sent),1)
        self.assertGreaterEqual(sent[0],30.5)
        self.assertTrue(any(a=='log' and 'Manual SPACE' in b for a,b in events))

if __name__=='__main__': unittest.main(verbosity=2)

