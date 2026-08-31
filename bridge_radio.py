#!/usr/bin/env python3
import json
import os
import re
import runpy
import sys
from pathlib import Path

RADIOS_CONFIG = Path('/etc/zello-bridge-radios.json')
GLOBAL_CONFIG = Path('/etc/zello-bridge.json')
DEFAULT_CREDENTIAL_DIR = Path('/etc/zello-bridge-credentials')

if len(sys.argv) != 2:
    raise SystemExit('Usage: bridge_radio.py <radio-id>')

radio_id = sys.argv[1].strip()
data = json.loads(RADIOS_CONFIG.read_text())
radio = next((r for r in data.get('radios', []) if r.get('id') == radio_id), None)
if not radio:
    raise SystemExit(f'Unknown radio id: {radio_id}')

cred_path = Path(radio.get('credentials_file') or (DEFAULT_CREDENTIAL_DIR / f'{radio_id}.json'))
creds = json.loads(cred_path.read_text())
username = str(creds.get('username', '')).strip()
password = str(creds.get('password', ''))
if not username or not password:
    raise SystemExit(f'Missing Zello credentials for {radio_id}')

hw = radio.get('hardware', {})
serial_dev = hw.get('serial_by_path') or hw.get('serial_by_id')
if not serial_dev:
    raise SystemExit(f'Missing serial/PTT device for {radio_id}')

saved_audio_sys = str(hw.get('audio_sys_path', ''))
setup_card = hw.get('audio_card_at_setup')

def resolve_audio_device():
    # Strip the dynamic /sound/cardN suffix and match the stable USB path.
    usb_root = re.sub(r'/sound/card\d+$', '', saved_audio_sys)
    sound_root = Path('/sys/class/sound')
    if usb_root and sound_root.exists():
        for card in sorted(sound_root.glob('card*')):
            m = re.fullmatch(r'card(\d+)', card.name)
            if not m:
                continue
            try:
                device_path = str((card / 'device').resolve())
            except Exception:
                continue
            if device_path.startswith(usb_root):
                return f'plughw:{m.group(1)},0'
    if setup_card is not None:
        return f'plughw:{int(setup_card)},0'
    raise SystemExit(f'Unable to resolve audio device for {radio_id}')

global_cfg = json.loads(GLOBAL_CONFIG.read_text())
ws_url = str(global_cfg.get('zello_ws_url', '')).strip()
if not ws_url:
    raise SystemExit('Missing zello_ws_url in /etc/zello-bridge.json')
os.environ['ZELLO_WS_URL'] = ws_url
os.environ['ZELLO_USERNAME'] = username
os.environ['ZELLO_PASSWORD'] = password
os.environ['ZELLO_CHANNEL'] = str(radio.get('channel', '')).strip()
os.environ['ZELLO_SERIAL_DEV'] = str(serial_dev)
os.environ['ZELLO_AUDIO_DEV'] = resolve_audio_device()
os.environ['ZELLO_BRIDGE_SERVICE'] = f'zello-bridge@{radio_id}.service'

print(f'Loading saved radio: {radio.get("name", radio_id)}')
print(f'PTT device: {serial_dev}')
print(f'Audio device: {os.environ["ZELLO_AUDIO_DEV"]}')
runpy.run_path('/opt/zello-bridge/radio_engine_runner.py', run_name='__main__')
