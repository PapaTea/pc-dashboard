"""Measure CPU / RAM used by the dashboard (run it next to a live instance)."""
import time

import psutil

targets = {}
for p in psutil.process_iter(['pid', 'name', 'cmdline']):
    cl = ' '.join(p.info['cmdline'] or [])
    if 'dashboard.py' in cl or 'adb.exe' in cl.lower():
        try:
            p.cpu_percent(None)
            targets[p.pid] = p
        except Exception:
            pass

ram = sum(p.memory_info().rss for p in targets.values()) / 2**20
time.sleep(20)

total = 0.0
for pid, p in targets.items():
    try:
        c = p.cpu_percent(None) / 100.0
        total += c
        print(f'pid {pid} {p.name()}: {c*100:.2f}% of one core, {p.memory_info().rss/2**20:.1f} MB')
    except Exception:
        pass
print(f'TOTAL: {total*100:.2f}% of one core ({total/(psutil.cpu_count() or 1)*100:.2f}% of all cores)')
print(f'RAM: {ram:.1f} MB')
