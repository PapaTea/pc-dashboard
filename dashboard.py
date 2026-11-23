"""PC dashboard for an old phone over USB.

One process: HTTP server + sampler + media control (Windows GSMTC) + USB watchdog.

Config lives in config.json next to this file (see config.example.json).
Every key is optional — sensible defaults are used when one is missing.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import psutil

try:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as GSMTC,
    )
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
    )
    HAS_MEDIA = True
except Exception:  # winrt is optional: everything else works without it
    GSMTC = None
    PlaybackStatus = None
    HAS_MEDIA = False

HERE = os.path.dirname(os.path.abspath(__file__))
NPROC = psutil.cpu_count() or 1
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW on Windows

DEFAULTS = {
    'port': 8765,
    'adb': '',                       # empty -> auto-detect
    'serial': '',                    # empty -> the single connected device
    'usb_bridge': True,
    'browser': 'com.yandex.browser/.YandexBrowserActivity',
    'browser_pkg': 'com.yandex.browser',
    'rotation': 1,                   # 0 = none, 1 = 90° landscape, 3 = 270°
    'fullscreen_tap': [800, 400],    # tap that enters fullscreen, device pixels
    'screen_timeout_ms': 1800000,
    'disk_path': 'C:\\',
    'hermes_db': '',                 # optional: Hermes state.db, powers the mascot
}


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(os.path.join(HERE, 'config.json'), encoding='utf-8') as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print('config.json:', e, flush=True)
    return cfg


CFG = load_config()
PORT = int(CFG['port'])
BROWSER = str(CFG['browser'])
BROWSER_PKG = str(CFG['browser_pkg'])
ROTATION = str(int(CFG['rotation']))
TAP = [str(int(v)) for v in CFG['fullscreen_tap'][:2]]
SCREEN_TIMEOUT = str(int(CFG['screen_timeout_ms']))
DISK = str(CFG['disk_path'])
SERIAL = str(CFG.get('serial') or os.environ.get('ADB_SERIAL') or '')


def find_adb():
    if CFG.get('adb') and os.path.exists(CFG['adb']):
        return CFG['adb']
    for p in (os.path.join(HERE, 'adb.exe'),
              os.path.join(HERE, 'platform-tools', 'adb.exe')):
        if os.path.exists(p):
            return p
    return shutil.which('adb') or 'adb'


ADB = find_adb()


def find_hermes_db():
    if CFG.get('hermes_db'):
        return CFG['hermes_db']
    cand = os.path.expandvars(r'%LOCALAPPDATA%\hermes\state.db')
    return cand if os.path.exists(cand) else ''


DB = find_hermes_db()
LAST_MSG_SQL = ("select id, role, timestamp, length(coalesce(content,'')), session_id "
                "from messages order by id desc limit 1")

_lock = threading.Lock()
_s = {
    'cores': [], 'cpu': 0.0, 'net': {'down': 0.0, 'up': 0.0}, 'top': None,
    'gpu': None, 'np': None, 'agent': {'busy': False, 'idle_sec': 9999, 'notify': False},
    'phone_ts': 0.0,
}


# ---------------------------------------------------------------- media
class Media(threading.Thread):
    """Polls the Windows media session and forwards next / prev / toggle."""

    def __init__(self):
        super().__init__(daemon=True)
        self.mgr = None
        self.cmds = []
        self.loop = None
        self.event = None

    def send(self, act):
        self.cmds.append(act)
        # wake the media thread immediately instead of waiting for the next tick
        try:
            if self.loop and self.event:
                self.loop.call_soon_threadsafe(self.event.set)
        except Exception:
            pass

    def run(self):
        if not HAS_MEDIA:
            return
        try:
            asyncio.run(self.main())
        except Exception as e:
            print('media:', e, flush=True)

    async def main(self):
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        while True:
            try:
                if self.mgr is None:
                    self.mgr = await GSMTC.request_async()
                s = self.mgr.get_current_session()
                info = {'playing': False, 'title': '', 'artist': '', 'position': 0,
                        'duration': 0, 'app': ''}
                if s is not None:
                    props = await s.try_get_media_properties_async()
                    pb = s.get_playback_info()
                    tl = s.get_timeline_properties()
                    info = {
                        'playing': pb.playback_status == PlaybackStatus.PLAYING,
                        'title': props.title or '',
                        'artist': props.artist or '',
                        'position': int(tl.position.total_seconds()),
                        'duration': int(tl.end_time.total_seconds()),
                        'app': s.source_app_user_model_id or '',
                    }
                info['_ts'] = time.time()
                with _lock:
                    _s['np'] = info

                while self.cmds:
                    act = self.cmds.pop(0)
                    if s is None:
                        continue
                    try:
                        if act == 'next':
                            await s.try_skip_next_async()
                        elif act == 'prev':
                            await s.try_skip_previous_async()
                        elif act == 'toggle':
                            await s.try_toggle_play_pause_async()
                    except Exception:
                        pass
            except Exception:
                self.mgr = None
            try:
                await asyncio.wait_for(self.event.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            self.event.clear()


# ---------------------------------------------------------------- USB watchdog
class UsbBridge(threading.Thread):
    """Keeps `adb reverse`, screen-on, rotation lock and the browser page alive."""

    def __init__(self):
        super().__init__(daemon=True)
        self.was_up = False
        self.showing = False

    def adb(self, *args, timeout=10):
        cmd = [ADB] + (['-s', SERIAL] if SERIAL else []) + list(args)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, creationflags=NO_WINDOW)
            return (r.stdout or '').strip()
        except Exception:
            return ''

    def foreground(self):
        out = self.adb('shell', 'dumpsys', 'activity', 'activities')
        for line in out.splitlines():
            if 'topResumedActivity' in line or 'mResumedActivity' in line:
                for part in line.split():
                    if '/' in part and '.' in part:
                        return part.split('/')[0].strip('{}')
        return ''

    def show(self):
        self.showing = True
        self.adb('shell', 'am', 'start', '-a', 'android.intent.action.VIEW',
                 '-d', f'http://127.0.0.1:{PORT}/', '-n', BROWSER)
        time.sleep(11)
        self.adb('shell', 'input', 'tap', *TAP)   # a tap enters fullscreen
        time.sleep(5)
        self.adb('shell', 'input', 'tap', *TAP)
        time.sleep(3)
        with _lock:
            _s['phone_ts'] = time.time()
        self.showing = False

    def run(self):
        while True:
            up = any(l.endswith('\tdevice') or l.endswith(' device')
                     for l in self.adb('devices').splitlines())
            try:
                if up and not self.was_up:
                    self.adb('reverse', f'tcp:{PORT}', f'tcp:{PORT}')
                    self.adb('shell', 'svc', 'power', 'stayon', 'usb')
                    self.adb('shell', 'settings', 'put', 'system', 'screen_off_timeout',
                             SCREEN_TIMEOUT)
                    self.adb('shell', 'settings', 'put', 'system', 'accelerometer_rotation', '0')
                    self.adb('shell', 'settings', 'put', 'system', 'user_rotation', ROTATION)
                    threading.Thread(target=self.show, daemon=True).start()
                    self.was_up = True
                elif up and self.was_up:
                    # the page counts as alive while the phone keeps polling /api/stats
                    with _lock:
                        phone_ts = _s.get('phone_ts', 0)
                    stale = not self.showing and (time.time() - phone_ts) > 25
                    fg = self.foreground()
                    if stale or (fg and BROWSER_PKG not in fg):
                        threading.Thread(target=self.show, daemon=True).start()
                    if ROTATION not in self.adb('shell', 'settings', 'get', 'system', 'user_rotation'):
                        self.adb('shell', 'settings', 'put', 'system', 'accelerometer_rotation', '0')
                        self.adb('shell', 'settings', 'put', 'system', 'user_rotation', ROTATION)
                    if f'tcp:{PORT}' not in self.adb('reverse', '--list'):
                        self.adb('reverse', f'tcp:{PORT}', f'tcp:{PORT}')
                        self.adb('shell', 'svc', 'power', 'stayon', 'usb')
                        threading.Thread(target=self.show, daemon=True).start()
                else:
                    self.was_up = False
            except Exception:
                pass
            time.sleep(8)


# ---------------------------------------------------------------- sampler
def read_gpu():
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=3,
            creationflags=NO_WINDOW).stdout.strip().splitlines()[0]
        u, mu, mt, t = [x.strip() for x in out.split(',', 3)]
        return {'util': int(u), 'mem_used': int(mu), 'mem_total': int(mt), 'temp': int(t)}
    except Exception:
        return None


def last_message():
    """Last row of the Hermes message log: (id, role, timestamp, text length, session)."""
    if not DB:
        return None
    try:
        con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=1.0)
        try:
            return con.execute(LAST_MSG_SQL).fetchone()
        finally:
            con.close()
    except Exception:
        return None


def log_edge(txt):
    try:
        with open(os.path.join(HERE, 'edges.log'), 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%H:%M:%S")} {txt}\n')
    except Exception:
        pass


def sampler():
    prev = prev_t = None
    notify_until, gpu_ts = 0, 0
    busy_start, last_id = 0, None
    while True:
        try:
            cores = psutil.cpu_percent(interval=0.5, percpu=True)
            now = time.time()
            io = psutil.net_io_counters()
            if prev and now - prev_t > 0.2:
                dt = now - prev_t
                down = round((io.bytes_recv - prev[0]) / dt / 1024, 1)
                up = round((io.bytes_sent - prev[1]) / dt / 1024, 1)
            else:
                down = up = 0.0
            prev, prev_t = (io.bytes_recv, io.bytes_sent), now

            with _lock:
                gpu = _s['gpu']
            if gpu is None or now - gpu_ts > 4:
                gpu = read_gpu()
                gpu_ts = now

            # "agent busy / finished" — only when the optional Hermes DB is present
            row = last_message()
            agent = {'busy': False, 'idle_sec': 9999, 'notify': False, 'busy_sec': 0}
            if row is not None:
                mid, role, ts, clen, _sid = row
                idle = round(now - (ts or now))
                if mid != last_id:
                    if last_id is not None and role == 'assistant' and clen > 0:
                        notify_until = now + 8   # finished answer -> the mascot calls
                        log_edge(f'answer, {clen} chars')
                    last_id = mid
                agent['busy'] = idle < 10
                agent['idle_sec'] = idle
                if agent['busy']:
                    busy_start = busy_start or now
                else:
                    busy_start = 0
                agent['busy_sec'] = round(now - busy_start) if busy_start else 0
            agent['notify'] = now < notify_until

            with _lock:
                _s.update({'cores': cores,
                           'cpu': round(sum(cores) / len(cores), 1) if cores else 0.0,
                           'net': {'down': down, 'up': up}, 'top': None, 'gpu': gpu,
                           'agent': agent})
        except Exception:
            pass


def stats():
    with _lock:
        s = dict(_s)
    vm = psutil.virtual_memory()
    try:
        du = psutil.disk_usage(DISK)
        disk = {'pct': round(du.percent), 'free': round(du.free / 2**30)}
    except Exception:
        disk = {'pct': 0, 'free': 0}
    try:
        freq = round(psutil.cpu_freq().current) if psutil.cpu_freq() else 0
    except Exception:
        freq = 0
    s.update({
        'ts': time.time(), 'freq': freq,
        'ram': {'pct': round(vm.percent), 'used': round(vm.used / 2**30, 1),
                'total': round(vm.total / 2**30, 1)},
        'disk': disk,
        'uptime': int(time.time() - psutil.boot_time()),
    })
    return s


# ---------------------------------------------------------------- HTTP
MEDIA = None


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if 'android' in (self.headers.get('User-Agent') or '').lower():
            with _lock:
                _s['phone_ts'] = time.time()
        p = self.path.split('?')[0]
        files = {'/': ('index.html', 'text/html; charset=utf-8'),
                 '/index.html': ('index.html', 'text/html; charset=utf-8'),
                 '/manifest.json': ('manifest.json', 'application/manifest+json'),
                 '/icon.png': ('icon.png', 'image/png')}
        if p == '/api/stats':
            self._send(200, json.dumps(stats(), ensure_ascii=False),
                       'application/json; charset=utf-8')
        elif p in files:
            name, ctype = files[p]
            try:
                with open(os.path.join(HERE, name), 'rb') as f:
                    self._send(200, f.read(), ctype)
            except Exception:
                self._send(404, 'no', 'text/plain')
        else:
            self._send(404, 'no', 'text/plain')

    def do_POST(self):
        if self.path.split('?')[0] != '/api/media':
            self._send(404, 'no', 'text/plain')
            return
        n = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception:
            body = {}
        act = body.get('action', 'toggle')
        if act not in ('next', 'prev', 'toggle'):
            self._send(400, '{"ok":false}', 'application/json')
            return
        if MEDIA is not None:
            MEDIA.send(act)
        self._send(200, json.dumps({'ok': True, 'action': act}), 'application/json')


class Srv(ThreadingHTTPServer):
    allow_reuse_address = False   # on Windows this keeps two copies from sharing the port
    daemon_threads = True


if __name__ == '__main__':
    try:
        srv = Srv(('0.0.0.0', PORT), H)
    except OSError:
        raise SystemExit(0)       # port busy: another copy is already serving
    MEDIA = Media()
    MEDIA.start()
    threading.Thread(target=sampler, daemon=True).start()
    if CFG.get('usb_bridge', True):
        UsbBridge().start()
    print(f'serving on {PORT}', flush=True)
    srv.serve_forever()
