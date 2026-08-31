#!/usr/bin/env python3
"""Run the public GUI while filtering one known-benign arecord message.

The half-duplex Roxi/C-Media audio workaround intentionally stops arecord
before playback. ALSA may log "read error: Interrupted system call" while that
capture process exits. That message is expected and should not make the GUI
report a radio fault.
"""

import importlib.util
from http.server import ThreadingHTTPServer
from pathlib import Path

GUI_PATH = Path('/opt/zello-bridge/gui.py')

spec = importlib.util.spec_from_file_location('zello_bridge_gui', GUI_PATH)
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)

_original_link_status = gui.link_status


def link_status_without_benign_arecord_fault(link):
    status = _original_link_status(link)
    error = str(status.get('current_error') or '')
    upper = error.upper()

    benign_arecord_interrupt = (
        'ARECORD:' in upper
        and 'READ ERROR: INTERRUPTED SYSTEM CALL' in upper
    )

    if benign_arecord_interrupt:
        status['current_error'] = ''

        if not status.get('service_active'):
            status['health'] = 'stopped'
            status['health_text'] = 'Bridge stopped'
        elif not status.get('zello_online') or not status.get('roxi_connected'):
            status['health'] = 'degraded'
            status['health_text'] = 'Degraded'
        else:
            status['health'] = 'healthy'
            status['health_text'] = 'Healthy'

    return status


gui.link_status = link_status_without_benign_arecord_fault


if __name__ == '__main__':
    print(
        f'Zello Bridge GUI listening on http://{gui.HOST}:{gui.PORT}',
        flush=True,
    )
    ThreadingHTTPServer((gui.HOST, gui.PORT), gui.H).serve_forever()
