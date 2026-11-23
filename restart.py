"""Restart the dashboard: kill every old copy and start one fresh process."""
import json
import os
import subprocess
import sys
import time

import psutil

BASE = os.path.dirname(os.path.abspath(__file__))


def port():
    try:
        with open(os.path.join(BASE, 'config.json'), encoding='utf-8') as f:
            return int(json.load(f).get('port', 8765))
    except Exception:
        return 8765


PORT = port()
PYW = os.path.join(BASE, '.venv', 'Scripts', 'pythonw.exe')
if not os.path.exists(PYW):
    PYW = sys.executable


def kill_all():
    pids = []
    for p in psutil.process_iter(['pid', 'cmdline']):
        cl = ' '.join(p.info['cmdline'] or [])
        if 'dashboard.py' in cl and p.info['pid'] != os.getpid():
            pids.append(p.info['pid'])
    for pid in pids:
        subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)],
                       capture_output=True, creationflags=0x08000000)
    return pids


def running():
    out = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline']):
        cl = ' '.join(p.info['cmdline'] or [])
        if 'dashboard.py' in cl and p.info['pid'] != os.getpid():
            out.append(p.info['pid'])
    return out


print('killed:', kill_all())
time.sleep(2)
subprocess.Popen([PYW, os.path.join(BASE, 'dashboard.py')], cwd=BASE,
                 creationflags=0x08000000, close_fds=True)
time.sleep(7)

alive = running()
print('alive:', alive)
for pid in alive:
    try:
        p = psutil.Process(pid)
        print(' ', pid, p.name(), round(p.memory_info().rss / 2**20, 1), 'MB')
    except Exception:
        pass
try:
    import urllib.request
    print('http:', urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/stats', timeout=5).status)
except Exception as e:
    print('http ERR:', e)
