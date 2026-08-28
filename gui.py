#!/usr/bin/env python3
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

CONFIG = Path('/etc/zello-bridge-gui.json')
RADIOS_CONFIG = Path('/etc/zello-bridge-radios.json')
SETUP_STATE = Path('/var/lib/zello-bridge/setup-scan.json')
CONTROL_FILE = Path('/var/lib/zello-bridge/controls.json')
DISPATCH_FILE = Path('/var/lib/zello-bridge/dispatch.json')
HOST = '0.0.0.0'
PORT = 8810


def run(cmd, timeout=5):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:
        return 1, '', str(exc)


def load_json(path, default):
    try:
        data = json.loads(path.read_text())
        return data
    except Exception:
        return default


def atomic_json(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def dispatch_ports(service):
    slot = zlib.crc32(service.encode('utf-8')) % 1000
    return 41000 + slot, 43000 + slot


def set_dispatch_ptt(service, active):
    data = load_json(DISPATCH_FILE, {'services': {}})
    if not isinstance(data, dict) or not isinstance(data.get('services'), dict):
        data = {'services': {}}
    if active:
        for entry in data['services'].values():
            if isinstance(entry, dict):
                entry['ptt_until'] = 0
        data['services'][service] = {'ptt_until': time.time() + 2.0, 'updated_at': time.time()}
    else:
        entry = data['services'].setdefault(service, {})
        entry['ptt_until'] = 0
        entry['updated_at'] = time.time()
    atomic_json(DISPATCH_FILE, data, 0o640)


class MonitorHub:
    def __init__(self, service):
        self.service = service
        self.port, _ = dispatch_ports(service)
        self.subscribers = set()
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', self.port))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(8192)
            except OSError:
                return
            with self.lock:
                subscribers = list(self.subscribers)
            for q in subscribers:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    try:
                        q.get_nowait()
                        q.put_nowait(data)
                    except Exception:
                        pass

    def subscribe(self):
        q = queue.Queue(maxsize=50)
        with self.lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)


HUBS = {}
HUBS_LOCK = threading.Lock()


def get_monitor_hub(service):
    with HUBS_LOCK:
        if service not in HUBS:
            HUBS[service] = MonitorHub(service)
        return HUBS[service]


def send_dispatch_audio(service, data):
    _, port = dispatch_ports(service)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for i in range(0, len(data), 3200):
            sock.sendto(data[i:i+3200], ('127.0.0.1', port))
    finally:
        sock.close()


def load_config():
    data = load_json(CONFIG, {})
    if isinstance(data.get('links'), list):
        cfg = dict(data)
        cfg['links'] = list(data['links'])
    else:
        cfg = {'title': 'Zello Radio Bridge', 'links': [{'name': 'TEST', 'service': 'zello-bridge-test.service'}]}

    known = {x.get('service') for x in cfg['links']}
    radios = load_json(RADIOS_CONFIG, {'radios': []})
    controls = load_json(CONTROL_FILE, {'services': {}})
    controls_changed = False
    if not isinstance(controls, dict) or not isinstance(controls.get('services'), dict):
        controls = {'services': {}}
    for radio in radios.get('radios', []) if isinstance(radios, dict) else []:
        radio_id = str(radio.get('id', '')).strip()
        if not radio_id:
            continue
        service = f'zello-bridge@{radio_id}.service'
        if service not in known:
            hw = radio.get('hardware', {}) if isinstance(radio.get('hardware'), dict) else {}
            cfg['links'].append({
                'name': radio.get('name') or radio_id.upper(),
                'service': service,
                'serial_device': hw.get('serial_by_path') or hw.get('serial_by_id') or '',
                'audio_capture': hw.get('audio_capture_at_setup') or '',
                'audio_playback': hw.get('audio_playback_at_setup') or '',
                'saved_binding': True,
                'radio_id': radio_id,
                'channel': radio.get('channel') or '',
                'gateway_user': radio.get('gateway_user') or '',
            })
            known.add(service)
        if service not in controls['services']:
            controls['services'][service] = {
                'receive_enabled': False,
                'transmit_enabled': False,
                'updated_at': time.time(),
            }
            controls_changed = True
    if controls_changed:
        atomic_json(CONTROL_FILE, controls, 0o640)
    return cfg


def load_controls():
    data = load_json(CONTROL_FILE, {'services': {}})
    if not isinstance(data, dict) or not isinstance(data.get('services'), dict):
        return {'services': {}}
    return data


def control_for(service):
    entry = load_controls()['services'].get(service, {})
    return {
        'receive_enabled': bool(entry.get('receive_enabled', True)),
        'transmit_enabled': bool(entry.get('transmit_enabled', True)),
    }


def set_control(service, key, enabled):
    data = load_controls()
    entry = data['services'].setdefault(service, {})
    current = control_for(service)
    entry.setdefault('receive_enabled', current['receive_enabled'])
    entry.setdefault('transmit_enabled', current['transmit_enabled'])
    entry[key] = bool(enabled)
    entry['updated_at'] = time.time()
    atomic_json(CONTROL_FILE, data, 0o640)
    return control_for(service)


def props(service):
    cmd = ['systemctl','show',service,'--property=ActiveState','--property=SubState',
           '--property=NRestarts','--property=MainPID','--property=MemoryCurrent',
           '--property=ExecMainStartTimestamp','--property=ActiveEnterTimestampMonotonic']
    rc, out, err = run(cmd)
    data = {}
    if rc == 0:
        for line in out.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                data[k] = v
    return data, err


def journal(service, count=250, since=None):
    cmd = ['journalctl', '-u', service]
    if since:
        cmd += ['--since', since]
    cmd += ['-n', str(count), '-o', 'short-unix', '--no-pager']
    rc, out, _ = run(cmd)
    rows = []
    if rc == 0:
        for line in out.splitlines():
            m = re.match(r'^(\d+\.\d+)\s+(.*)$', line)
            rows.append((float(m.group(1)), m.group(2)) if m else (0.0, line))
    return rows


def human_bytes(value):
    try:
        n = int(value)
    except Exception:
        return '—'
    if n < 0 or n >= 2**63 - 1:
        return '—'
    for unit in ('B','KB','MB','GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{n} B'
        n /= 1024


def uptime_seconds(p):
    try:
        entered = int(p.get('ActiveEnterTimestampMonotonic', '0'))
        return max(0, int(time.monotonic() - entered / 1_000_000)) if entered else 0
    except Exception:
        return 0


def device_status(link):
    explicit = []
    for key in ('serial_device','audio_capture','audio_playback'):
        if link.get(key):
            explicit.append(Path(link[key]).exists())
    if explicit:
        return all(explicit)
    serial_ok = ((Path('/dev/serial/by-path').exists() and any(Path('/dev/serial/by-path').iterdir())) or
                 (Path('/dev/serial/by-id').exists() and any(Path('/dev/serial/by-id').iterdir())))
    snd = Path('/dev/snd')
    audio_ok = snd.exists() and any(snd.glob('pcmC*D*c')) and any(snd.glob('pcmC*D*p'))
    return bool(serial_ok and audio_ok)


def link_status(link):
    service = link['service']
    p, _ = props(service)
    active = p.get('ActiveState') == 'active'
    started = p.get('ExecMainStartTimestamp') or None
    rows = journal(service, 250, started if active and started else None)
    now = time.time()
    online = rx_start = rx_stop = tx_start = tx_stop = vox = 0.0
    last_error_ts = last_rx = last_tx = 0.0
    last_error = ''

    for ts, line in rows:
        u = line.upper()
        if 'BRIDGE ONLINE' in u or ('CHANNEL:' in u and ' ONLINE' in u):
            online = max(online, ts)
        if 'RADIO -> ZELLO START' in u:
            rx_start = max(rx_start, ts); last_rx = max(last_rx, ts)
        if 'RADIO -> ZELLO STOP' in u:
            rx_stop = max(rx_stop, ts); last_rx = max(last_rx, ts)
        if 'ZELLO -> RADIO START' in u or 'DISPATCH CONSOLE -> RADIO START' in u:
            tx_start = max(tx_start, ts); last_tx = max(last_tx, ts)
        if 'ZELLO -> RADIO STOP' in u or 'DISPATCH CONSOLE -> RADIO STOP' in u:
            tx_stop = max(tx_stop, ts); last_tx = max(last_tx, ts)
        if 'VOX TRIGGERED' in u:
            vox = max(vox, ts); last_rx = max(last_rx, ts)
        if any(x in u for x in ('TRACEBACK',' EXCEPTION',' ERROR',' FAILED')):
            last_error_ts = ts; last_error = line

    rx_live = active and rx_start > rx_stop
    tx_keyed = active and tx_start > tx_stop
    zello = active and online > 0
    roxi = device_status(link)
    current_error = last_error if last_error and (not online or last_error_ts >= online) else ''
    controls = control_for(service)

    if not active:
        health, health_text = 'stopped', 'Bridge stopped'
    elif current_error:
        health, health_text = 'fault', 'Needs attention'
    elif not zello or not roxi:
        health, health_text = 'degraded', 'Degraded'
    else:
        health, health_text = 'healthy', 'Healthy'

    return {
        'name': link.get('name','Radio'), 'service': service,
        'saved_binding': bool(link.get('saved_binding')), 'radio_id': link.get('radio_id',''),
        'channel': link.get('channel',''), 'gateway_user': link.get('gateway_user',''),
        'status': 'online' if active and zello and roxi else ('starting' if active else 'stopped'),
        'health': health, 'health_text': health_text,
        'service_active': active, 'zello_online': zello, 'roxi_connected': roxi,
        'rx_active': bool(rx_live or (last_rx and now-last_rx < 2.5)),
        'tx_active': bool(tx_keyed or (last_tx and now-last_tx < 2.5)),
        'ptt': 'KEYED' if tx_keyed else 'IDLE',
        'memory': human_bytes(p.get('MemoryCurrent')), 'restarts': p.get('NRestarts','0'),
        'uptime_seconds': uptime_seconds(p) if active else 0,
        'last_rx_ts': last_rx, 'last_tx_ts': last_tx,
        'current_error': current_error,
        'receive_enabled': controls['receive_enabled'],
        'transmit_enabled': controls['transmit_enabled'],
    }


def udev_props(device):
    rc, out, _ = run(['udevadm','info','--query=property','--name',str(device)], 4)
    data = {}
    if rc == 0:
        for line in out.splitlines():
            if '=' in line:
                k, v = line.split('=', 1)
                data[k] = v
    return data


def common_prefix_score(a, b):
    a = a or ''; b = b or ''
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def live_sysfs_device(device, class_root, pattern):
    """Return live sysfs device path/name matching a passed device node."""
    try:
        st = os.stat(device)
        wanted = f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}"
    except Exception:
        return '', ''
    root = Path(class_root)
    if not root.exists():
        return '', ''
    for entry in sorted(root.glob(pattern)):
        try:
            if (entry / 'dev').read_text().strip() == wanted:
                return str((entry / 'device').resolve()), entry.name
        except Exception:
            continue
    return '', ''


def scan_hardware():
    serial = []
    byid = Path('/dev/serial/by-path')
    if byid.exists():
        for link in sorted(byid.iterdir()):
            sys_path, live_name = live_sysfs_device(link, '/sys/class/tty', 'ttyUSB*')
            if not sys_path:
                continue
            target = link
            p = udev_props(link)
            serial.append({
                'kind': 'serial', 'key': f"serial:{link.name}",
                'label': f"{p.get('ID_VENDOR','FTDI')} {p.get('ID_MODEL','USB Serial')}",
                'by_id': str(link), 'device': str(target),
                'vendor': p.get('ID_VENDOR',''), 'model': p.get('ID_MODEL',''),
                'serial': p.get('ID_SERIAL_SHORT',''), 'id_path': p.get('ID_PATH',''),
                'sys_path': sys_path,
            })

    audio = []
    snd = Path('/dev/snd')
    if snd.exists():
        for ctl in sorted(snd.glob('controlC*')):
            live_sys_path, live_name = live_sysfs_device(ctl, '/sys/class/sound', 'controlC*')
            if not live_sys_path:
                continue
            m = re.search(r'C(\d+)$', ctl.name)
            if not m:
                continue
            card = int(m.group(1))
            p = udev_props(ctl)
            cap = snd / f'pcmC{card}D0c'
            play = snd / f'pcmC{card}D0p'
            sys_path = live_sys_path
            id_path = p.get('ID_PATH','')
            audio.append({
                'kind': 'audio', 'key': f"audio:{id_path or sys_path or card}",
                'label': f"{p.get('ID_VENDOR','USB')} {p.get('ID_MODEL','Audio Device')}",
                'card': card, 'control': str(ctl),
                'capture': str(cap) if cap.exists() else '',
                'playback': str(play) if play.exists() else '',
                'alsa': f'plughw:{card},0',
                'vendor': p.get('ID_VENDOR',''), 'model': p.get('ID_MODEL',''),
                'serial': p.get('ID_SERIAL_SHORT',''), 'id_path': id_path,
                'sys_path': sys_path,
            })
    return {'at': time.time(), 'serial': serial, 'audio': audio}


def item_identity(item):
    if item['kind'] == 'serial':
        return item.get('by_id') or item.get('id_path') or item.get('device')
    return item.get('id_path') or item.get('sys_path') or item.get('key')


def diff_scan(base, current):
    old_serial = {item_identity(x) for x in base.get('serial', [])}
    old_audio = {item_identity(x) for x in base.get('audio', [])}
    new_serial = [x for x in current.get('serial', []) if item_identity(x) not in old_serial]
    new_audio = [x for x in current.get('audio', []) if item_identity(x) not in old_audio]
    pairs = []
    for s in new_serial:
        for a in new_audio:
            score = max(common_prefix_score(s.get('id_path'), a.get('id_path')),
                        common_prefix_score(s.get('sys_path'), a.get('sys_path')))
            pairs.append({'serial_key': s['key'], 'audio_key': a['key'], 'score': score})
    pairs.sort(key=lambda x: x['score'], reverse=True)
    return {'new_serial': new_serial, 'new_audio': new_audio, 'pairs': pairs,
            'auto_pair': pairs[0] if len(new_serial) == 1 and len(new_audio) == 1 else None}


def load_setup_state():
    return load_json(SETUP_STATE, {'baseline': None, 'current': None, 'diff': None})


def save_setup_state(state):
    atomic_json(SETUP_STATE, state, 0o600)


def existing_radios():
    data = load_json(RADIOS_CONFIG, {'radios': []})
    return data if isinstance(data.get('radios'), list) else {'radios': []}


def slugify(value):
    s = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')
    return s or f'radio-{int(time.time())}'


def save_binding(body):
    state = load_setup_state()
    diff = state.get('diff') or {}
    serials = {x['key']: x for x in diff.get('new_serial', [])}
    audios = {x['key']: x for x in diff.get('new_audio', [])}
    serial = serials.get(body.get('serial_key'))
    audio = audios.get(body.get('audio_key'))
    if not serial or not audio:
        raise ValueError('Select the newly detected serial and audio devices.')
    name = str(body.get('name','')).strip()
    channel = str(body.get('channel','')).strip()
    gateway_user = str(body.get('gateway_user','')).strip()
    password = str(body.get('password',''))
    if not name or not channel:
        raise ValueError('Radio name and Zello channel are required.')
    if not gateway_user or not password:
        raise ValueError('Zello gateway username and password are required.')
    radio_id = slugify(name)
    credential_dir = Path('/etc/zello-bridge-credentials')
    credential_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(credential_dir, 0o700)
    credential_file = credential_dir / f'{radio_id}.json'
    atomic_json(credential_file, {'username': gateway_user, 'password': password}, 0o600)
    radio = {
        'id': radio_id, 'name': name, 'channel': channel,
        'gateway_user': gateway_user,
        'credentials_file': str(credential_file),
        'hardware': {
            'serial_by_id': serial.get('by_id',''), 'serial_number': serial.get('serial',''),
            'serial_id_path': serial.get('id_path',''), 'serial_sys_path': serial.get('sys_path',''),
            'audio_id_path': audio.get('id_path',''), 'audio_sys_path': audio.get('sys_path',''),
            'audio_card_at_setup': audio.get('card'), 'audio_alsa_at_setup': audio.get('alsa',''),
            'audio_capture_at_setup': audio.get('capture',''), 'audio_playback_at_setup': audio.get('playback',''),
        },
        'created_at': time.time(),
        'enabled': False,
        'note': 'Hardware binding saved by guided setup. Bridge service creation is performed after validation.'
    }
    data = existing_radios()
    data['radios'] = [x for x in data['radios'] if x.get('id') != radio['id']]
    data['radios'].append(radio)
    atomic_json(RADIOS_CONFIG, data, 0o640)
    controls = load_controls()
    service = f'zello-bridge@{radio_id}.service'
    controls['services'][service] = {
        'receive_enabled': False,
        'transmit_enabled': False,
        'updated_at': time.time(),
    }
    atomic_json(CONTROL_FILE, controls, 0o640)
    run(['systemctl', 'enable', service], 12)
    radio['enabled'] = True
    data['radios'] = [x for x in data['radios'] if x.get('id') != radio['id']]
    data['radios'].append(radio)
    atomic_json(RADIOS_CONFIG, data, 0o640)
    return radio


def update_credentials(body):
    radio_id = str(body.get('radio_id','')).strip()
    username = str(body.get('username','')).strip()
    password = str(body.get('password',''))
    channel = str(body.get('channel','')).strip()
    if not radio_id or not username or not password or not channel:
        raise ValueError('Radio, Zello gateway username, password, and channel are required.')
    data = existing_radios()
    radio = next((x for x in data['radios'] if x.get('id') == radio_id), None)
    if not radio:
        raise ValueError('Saved radio was not found.')
    credential_dir = Path('/etc/zello-bridge-credentials')
    credential_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(credential_dir, 0o700)
    credential_file = credential_dir / f'{radio_id}.json'
    atomic_json(credential_file, {'username': username, 'password': password}, 0o600)
    radio['gateway_user'] = username
    radio['channel'] = channel
    radio['credentials_file'] = str(credential_file)
    atomic_json(RADIOS_CONFIG, data, 0o640)
    return {'id': radio_id, 'name': radio.get('name', radio_id), 'gateway_user': username}


OPERATOR_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Zello Radio Bridge</title>
<style>:root{--bg:#0b1016;--panel:#141b23;--panel2:#1b2530;--text:#f3f7fb;--muted:#8fa0b2;--line:#2b3947;--green:#38d46a;--amber:#f4b942;--red:#ff6262;--blue:#5eb2ff;--cyan:#3bd6df}*{box-sizing:border-box}body{margin:0;background:linear-gradient(180deg,#091017,#0d131b 320px);color:var(--text);font-family:Segoe UI,Arial,sans-serif}header{padding:18px 24px;background:#05090dee;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}.headright{display:flex;align-items:center;gap:14px}a.btn,button{background:var(--panel2);color:var(--text);border:1px solid var(--line);padding:9px 13px;border-radius:8px;font-weight:650;text-decoration:none;cursor:pointer}a.btn:hover,button:hover{border-color:var(--blue)}h1{font-size:22px;margin:0}.sub{font-size:12px;color:var(--muted);margin-top:4px}.wrap{max-width:1180px;margin:auto;padding:24px}.system-health{padding:14px 16px;margin-bottom:18px;border:1px solid var(--line);background:var(--panel);border-radius:12px}.health-left{display:flex;align-items:center;gap:10px}.health-icon{width:12px;height:12px;border-radius:50%;background:#6e7681}.health-icon.healthy{background:var(--green);box-shadow:0 0 12px #38d46a88}.health-icon.degraded{background:var(--amber)}.health-icon.fault,.health-icon.stopped{background:var(--red)}.health-title{font-weight:700}.health-detail{font-size:12px;color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:18px}.card{border:1px solid var(--line);border-radius:16px;background:var(--panel);overflow:hidden}.cardhead{padding:18px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line)}.name{font-size:24px;font-weight:750}.badge{font-size:12px;font-weight:800;padding:7px 11px;border-radius:999px}.badge.online{background:#38d46a22;color:#85f0a2}.badge.stopped{background:#ff626222;color:#ff9090}.badge.starting{background:#f4b94222;color:#ffd070}.body{padding:16px 20px}.status-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:15px}.chip{background:#0d141c;border:1px solid var(--line);border-radius:10px;padding:11px 9px;text-align:center}.cl{font-size:10px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:5px}.cv{font-size:12px;font-weight:700}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#637180;margin-right:6px}.dot.on{background:var(--green);box-shadow:0 0 8px #38d46aaa}.activity{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:12px 0 14px}.activity-box{min-height:88px;border-radius:12px;border:1px solid var(--line);background:#0c131a;display:flex;flex-direction:column;align-items:center;justify-content:center}.activity-box .t{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px}.activity-box .s{font-size:19px;font-weight:800}.activity-box.rx.active{border-color:#3bd6dfcc;background:#3bd6df1f}.activity-box.rx.active .s{color:#77eef4}.activity-box.tx.active{border-color:#f4b942dd;background:#f4b9421f}.activity-box.tx.active .s{color:#ffd36f}.timing{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px}.metric{padding:9px;background:var(--panel2);border-radius:9px}.ml{font-size:10px;color:var(--muted);text-transform:uppercase}.mv{font-size:12px;font-weight:700;margin-top:4px}.warning{margin:0 20px 14px;padding:10px 12px;border-radius:9px;font-size:12px;color:#ff9a9a;background:#ff626214;border:1px solid #ff626244}.diag{display:flex;gap:16px;color:var(--muted);font-size:11px;padding:0 20px 14px}.actions{padding:0 20px 18px;display:flex;gap:8px}.section{margin-top:22px}details{border:1px solid var(--line);border-radius:12px;background:var(--panel);overflow:hidden}summary{cursor:pointer;padding:13px 16px;font-weight:700}.log-inner{padding:0 14px 14px}.logs{height:300px;overflow:auto;background:#05090d;border:1px solid var(--line);border-radius:9px;padding:12px;white-space:pre-wrap;font:12px/1.45 Consolas,monospace;color:#c9d5df}select{background:var(--panel2);color:var(--text);border:1px solid var(--line);padding:8px;border-radius:8px;margin-bottom:8px}@media(max-width:620px){.wrap{padding:14px}.grid,.status-grid,.timing{grid-template-columns:1fr}.headright .sub{display:none}}</style></head>
<body><header><div><h1 id="title">Zello Radio Bridge</h1><div class="sub">Operator Console</div></div><div class="headright"><a class="btn" href="/setup">Setup Radios</a><div class="sub" id="clock"></div></div></header><div class="wrap"><div id="systemHealth" class="system-health">Checking system…</div><div id="cards" class="grid"></div><div class="section"><details id="logDetails" ontoggle="logToggle()"><summary>Live Log & Diagnostics</summary><div class="log-inner"><select id="logSelect" onchange="logs()"></select><div id="logs" class="logs" onscroll="window._zelloLogAutoFollow=(this.scrollHeight-this.scrollTop-this.clientHeight)<40">Open this section to load logs.</div></div></details></div></div>
<script>function e(s){return String(s??'').replace(/[&<>\"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[m]))}async function api(u,o){let r=await fetch(u,o);if(!r.ok)throw Error(await r.text());return r.json()}function age(ts){if(!ts)return'None yet';let s=Math.max(0,Math.floor(Date.now()/1000-ts));if(s<5)return'Just now';if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';if(s<86400)return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m ago';return Math.floor(s/86400)+'d ago'}function dur(s){s=Number(s)||0;let d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.floor((s%3600)/60);if(d)return d+'d '+h+'h';if(h)return h+'h '+m+'m';if(m)return m+'m';return Math.floor(s)+'s'}function card(s){let warn=s.current_error?`<div class="warning">Current issue: ${e(s.current_error)}</div>`:'';return`<div class="card"><div class="cardhead"><div class="name">${e(s.name)}</div><div class="badge ${e(s.status)}">${s.status==='online'?'ONLINE':e(s.status.toUpperCase())}</div></div><div class="body"><div class="status-grid"><div class="chip"><div class="cl">Zello</div><div class="cv"><i class="dot ${s.zello_online?'on':''}"></i>${s.zello_online?'Connected':'Offline'}</div></div><div class="chip"><div class="cl">Roxi</div><div class="cv"><i class="dot ${s.roxi_connected?'on':''}"></i>${s.roxi_connected?'Connected':'Missing'}</div></div><div class="chip"><div class="cl">Radio</div><div class="cv"><i class="dot ${s.service_active&&s.roxi_connected?'on':''}"></i>${s.service_active&&s.roxi_connected?'Ready':'Not Ready'}</div></div></div><div class="activity"><div class="activity-box rx ${s.rx_active?'active':''}"><div class="t">Receive</div><div class="s">${!s.receive_enabled?'DISABLED':(s.rx_active?'RECEIVING':'IDLE')}</div></div><div class="activity-box tx ${s.tx_active?'active':''}"><div class="t">Transmit / PTT</div><div class="s">${!s.transmit_enabled?'DISABLED':(s.tx_active?e(s.ptt):'IDLE')}</div></div></div><div class="timing"><div class="metric"><div class="ml">Last RX</div><div class="mv">${age(s.last_rx_ts)}</div></div><div class="metric"><div class="ml">Last TX</div><div class="mv">${age(s.last_tx_ts)}</div></div><div class="metric"><div class="ml">Uptime</div><div class="mv">${s.service_active?dur(s.uptime_seconds):'Stopped'}</div></div></div></div>${warn}<div class="diag"><span>Memory ${e(s.memory)}</span><span>Restarts ${e(s.restarts)}</span></div><div class="actions">${s.service_active?`<button onclick="act('${e(s.service)}','restart')">Restart Bridge</button><button onclick="act('${e(s.service)}','stop')">Stop</button>`:`<button onclick="act('${e(s.service)}','start')">Start Bridge</button>`}</div></div>`}function health(d){let rank={healthy:0,stopped:1,degraded:2,fault:3},worst='healthy';for(let x of d.links)if(rank[x.health]>rank[worst])worst=x.health;let good=d.links.filter(x=>x.health==='healthy').length,total=d.links.length,txt=worst==='healthy'?'All systems normal':worst==='fault'?'Attention required':worst==='degraded'?'One or more links degraded':'Bridge stopped';document.getElementById('systemHealth').innerHTML=`<div class="health-left"><span class="health-icon ${worst}"></span><div><div class="health-title">System Health: ${worst==='healthy'?'Healthy':worst.charAt(0).toUpperCase()+worst.slice(1)}</div><div class="health-detail">${e(txt)} • ${good}/${total} link${total===1?'':'s'} healthy</div></div></div>`}async function refresh(){let d=await api('/api/status');document.getElementById('title').textContent=d.title;let cardsEl=document.getElementById('cards'),html=d.links.map(card).join('');function morph(oldNode,newNode){if(!oldNode||!newNode)return;if(oldNode.nodeType!==newNode.nodeType||oldNode.nodeName!==newNode.nodeName){oldNode.replaceWith(newNode.cloneNode(true));return}if(oldNode.nodeType===3){if(oldNode.nodeValue!==newNode.nodeValue)oldNode.nodeValue=newNode.nodeValue;return}for(let a of [...oldNode.attributes])if(!newNode.hasAttribute(a.name))oldNode.removeAttribute(a.name);for(let a of [...newNode.attributes])if(oldNode.getAttribute(a.name)!==a.value)oldNode.setAttribute(a.name,a.value);if(oldNode instanceof HTMLInputElement){oldNode.checked=newNode.checked;oldNode.disabled=newNode.disabled}if(oldNode instanceof HTMLButtonElement)oldNode.disabled=newNode.disabled;let oc=[...oldNode.childNodes],nc=[...newNode.childNodes],n=Math.max(oc.length,nc.length);for(let i=0;i<n;i++){if(!oc[i]&&nc[i])oldNode.appendChild(nc[i].cloneNode(true));else if(oc[i]&&!nc[i])oc[i].remove();else morph(oc[i],nc[i])}}let tmp=document.createElement('div');tmp.innerHTML=html;let newCards=[...tmp.children],oldCards=[...cardsEl.children];if(oldCards.length!==newCards.length){cardsEl.innerHTML=html}else newCards.forEach((n,i)=>morph(oldCards[i],n));health(d);let s=document.getElementById('logSelect'),old=s.value;s.innerHTML=d.links.map(x=>`<option value="${e(x.service)}">${e(x.name)}</option>`).join('');if([...s.options].some(x=>x.value===old))s.value=old}async function act(service,action){if((action==='stop'||action==='restart')&&!confirm(action.toUpperCase()+' '+service+'?'))return;await api('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service,action})});setTimeout(refresh,700)}async function logs(){if(!document.getElementById('logDetails').open)return;let s=document.getElementById('logSelect').value;if(!s)return;let d=await api('/api/logs?service='+encodeURIComponent(s)),b=document.getElementById('logs');b.textContent=d.lines.join('\n');if(window._zelloLogAutoFollow!==false)b.scrollTop=b.scrollHeight}function logToggle(){if(document.getElementById('logDetails').open)logs()}setInterval(()=>document.getElementById('clock').textContent=new Date().toLocaleString(),1000);setInterval(()=>refresh().catch(()=>{}),1000);setInterval(()=>logs().catch(()=>{}),2000);refresh().catch(()=>{})</script></body></html>'''

OPERATOR_HTML = OPERATOR_HTML.replace(
    '</head>',
    '<style>.controls{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:0 0 14px}.toggle{display:flex;align-items:center;justify-content:space-between;gap:10px;background:#0d141c;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:12px;font-weight:700}.toggle input{width:19px;height:19px;accent-color:var(--green);cursor:pointer}.toggle.off{border-color:#ff626255;color:#ff9a9a}@media(max-width:620px){.controls{grid-template-columns:1fr}}</style></head>'
).replace(
    '</body>',
    '''<script>const baseCard=card;card=function(s){let h=baseCard(s);let c=`<div class="controls"><label class="toggle ${s.receive_enabled?'':'off'}"><span>Receive from Radio</span><input type="checkbox" ${s.receive_enabled?'checked':''} onchange="radioControl('${e(s.service)}','receive_enabled',this.checked)"></label><label class="toggle ${s.transmit_enabled?'':'off'}"><span>Transmit / PTT</span><input type="checkbox" ${s.transmit_enabled?'checked':''} onchange="radioControl('${e(s.service)}','transmit_enabled',this.checked)"></label></div>`;h=h.replace('<div class="activity">',c+'<div class="activity">');return h};async function radioControl(service,key,enabled){try{await api('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service,key,enabled})});await refresh()}catch(x){alert('Unable to change radio control: '+x.message);await refresh()}}</script></body>'''
)

OPERATOR_HTML = OPERATOR_HTML.replace(
    '<div id="systemHealth" class="system-health">Checking system…</div>',
    '''<div class="section dispatch-audio"><details open><summary>Dispatch Audio</summary><div class="dispatch-inner"><div class="dispatch-grid"><div><label>Microphone</label><select id="dispatchMic"></select></div><div><label>Speaker / Headset</label><select id="dispatchSpeaker"></select></div><div class="dispatch-buttons"><button onclick="enableDispatchAudio()">Enable / Refresh Audio</button><button onclick="testDispatchSpeaker()">Test Speaker</button></div><div><label>Mic Level</label><div class="meter"><span id="micMeter"></span></div></div></div><div id="dispatchAudioStatus" class="dispatch-status">Choose Enable / Refresh Audio to grant microphone access and list this PC's audio devices.</div></div></details></div><div id="systemHealth" class="system-health">Checking system…</div>'''
).replace(
    '</head>',
    '''<style>.dispatch-audio{margin-top:0;margin-bottom:18px}.dispatch-inner{padding:0 16px 16px}.dispatch-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.dispatch-grid label{display:block;font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:5px}.dispatch-grid select{width:100%;margin:0}.dispatch-buttons{display:flex;align-items:end;gap:8px;flex-wrap:wrap}.dispatch-status{font-size:12px;color:var(--muted);margin-top:10px}.meter{height:12px;border-radius:999px;background:#070b10;border:1px solid var(--line);overflow:hidden;margin-top:9px}.meter span{display:block;height:100%;width:0;background:var(--green);transition:width .08s}.dispatch-console{display:grid;grid-template-columns:1fr 1.35fr;gap:10px;margin:0 0 14px}.listen-toggle{display:flex;align-items:center;justify-content:space-between;background:#0d141c;border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:12px;font-weight:750}.listen-toggle input{width:20px;height:20px;accent-color:var(--cyan)}.ptt-button{border:1px solid #ff626277;background:#301517;color:#ffb0b0;font-size:14px;font-weight:850;touch-action:none;user-select:none}.ptt-button.keyed{background:#8e2026;border-color:#ff7777;color:white;box-shadow:0 0 16px #ff626255}.ptt-button:disabled{opacity:.45;cursor:not-allowed}@media(max-width:620px){.dispatch-grid,.dispatch-console{grid-template-columns:1fr}}</style></head>'''
).replace(
    '</body>',
    r'''<script>
const dispatchListeners={};const activePTT={};let dispatchDeviceStream=null;let micMeterCtx=null;let micMeterTimer=null;
function dispatchStatus(t,bad=false){let x=document.getElementById('dispatchAudioStatus');if(x){x.textContent=t;x.style.color=bad?'#ff9090':'var(--muted)'}}
async function enumerateDispatchDevices(){if(!navigator.mediaDevices||!navigator.mediaDevices.enumerateDevices){dispatchStatus('This browser does not expose audio-device selection.',true);return}let ds=await navigator.mediaDevices.enumerateDevices(),mi=document.getElementById('dispatchMic'),sp=document.getElementById('dispatchSpeaker'),oldm=localStorage.getItem('zelloDispatchMic')||'',olds=localStorage.getItem('zelloDispatchSpeaker')||'';mi.innerHTML='<option value="">System default microphone</option>'+ds.filter(x=>x.kind==='audioinput').map((x,i)=>`<option value="${e(x.deviceId)}">${e(x.label||'Microphone '+(i+1))}</option>`).join('');sp.innerHTML='<option value="">System default speaker</option>'+ds.filter(x=>x.kind==='audiooutput').map((x,i)=>`<option value="${e(x.deviceId)}">${e(x.label||'Speaker '+(i+1))}</option>`).join('');if([...mi.options].some(x=>x.value===oldm))mi.value=oldm;if([...sp.options].some(x=>x.value===olds))sp.value=olds;mi.onchange=()=>localStorage.setItem('zelloDispatchMic',mi.value);sp.onchange=()=>{localStorage.setItem('zelloDispatchSpeaker',sp.value);applySpeakerSink()}}
async function enableDispatchAudio(){try{if(!window.isSecureContext){dispatchStatus('Microphone access requires HTTPS. Listen will still work, but Hold to Talk needs a secure GUI address.',true)}if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia)throw Error('Microphone access is unavailable in this browser.');let id=document.getElementById('dispatchMic')?.value||'';if(dispatchDeviceStream)dispatchDeviceStream.getTracks().forEach(t=>t.stop());dispatchDeviceStream=await navigator.mediaDevices.getUserMedia({audio:id?{deviceId:{exact:id}}:true});await enumerateDispatchDevices();startMicMeter(dispatchDeviceStream);dispatchStatus('Dispatch audio ready on this PC. Device choices are saved in this browser.')}catch(x){dispatchStatus('Audio setup failed: '+x.message,true)}}
function startMicMeter(stream){if(micMeterTimer)cancelAnimationFrame(micMeterTimer);if(micMeterCtx)try{micMeterCtx.close()}catch{};micMeterCtx=new AudioContext();let src=micMeterCtx.createMediaStreamSource(stream),an=micMeterCtx.createAnalyser();an.fftSize=512;src.connect(an);let a=new Uint8Array(an.fftSize);function tick(){an.getByteTimeDomainData(a);let ss=0;for(let v of a){let f=(v-128)/128;ss+=f*f}let rms=Math.sqrt(ss/a.length),m=document.getElementById('micMeter');if(m)m.style.width=Math.min(100,rms*500)+'%';micMeterTimer=requestAnimationFrame(tick)}tick()}
async function makePlaybackContext(){let c=new AudioContext({latencyHint:'interactive'});let sink=document.getElementById('dispatchSpeaker')?.value||'';if(sink&&typeof c.setSinkId==='function')try{await c.setSinkId(sink)}catch{}return c}
async function applySpeakerSink(){for(let x of Object.values(dispatchListeners)){if(x.ctx&&typeof x.ctx.setSinkId==='function')try{await x.ctx.setSinkId(document.getElementById('dispatchSpeaker').value||'')}catch{}}}
async function testDispatchSpeaker(){try{let c=await makePlaybackContext(),o=c.createOscillator(),g=c.createGain();g.gain.value=.08;o.frequency.value=700;o.connect(g).connect(c.destination);o.start();o.stop(c.currentTime+.25);setTimeout(()=>c.close(),500)}catch(x){dispatchStatus('Speaker test failed: '+x.message,true)}}
async function setListen(service,on){localStorage.setItem('zelloListen:'+service,on?'1':'0');if(!on){let st=dispatchListeners[service];if(st){st.abort.abort();try{await st.ctx.close()}catch{};delete dispatchListeners[service]}return}if(dispatchListeners[service])return;let ctx=await makePlaybackContext(),abort=new AbortController(),st={ctx,abort,next:ctx.currentTime+.08,carry:new Uint8Array(0)};dispatchListeners[service]=st;try{let r=await fetch('/api/dispatch/listen?service='+encodeURIComponent(service),{signal:abort.signal});if(!r.ok)throw Error(await r.text());let rd=r.body.getReader();while(true){let z=await rd.read();if(z.done)break;let data=z.value;if(st.carry.length){let n=new Uint8Array(st.carry.length+data.length);n.set(st.carry);n.set(data,st.carry.length);data=n;st.carry=new Uint8Array(0)}if(data.length%2){st.carry=data.slice(data.length-1);data=data.slice(0,-1)}if(!data.length)continue;let samples=data.length/2,b=ctx.createBuffer(1,samples,16000),out=b.getChannelData(0),dv=new DataView(data.buffer,data.byteOffset,data.byteLength);for(let i=0;i<samples;i++)out[i]=dv.getInt16(i*2,true)/32768;let src=ctx.createBufferSource();src.buffer=b;src.connect(ctx.destination);if(st.next<ctx.currentTime-.1||st.next>ctx.currentTime+.5)st.next=ctx.currentTime+.06;src.start(st.next);st.next+=samples/16000}}catch(x){if(x.name!=='AbortError')dispatchStatus('Listen stream failed: '+x.message,true)}finally{if(dispatchListeners[service]===st){delete dispatchListeners[service];try{ctx.close()}catch{}}}}
function resample16k(input,inRate){if(inRate===16000){let o=new Int16Array(input.length);for(let i=0;i<input.length;i++)o[i]=Math.max(-32768,Math.min(32767,input[i]*32767));return o}let ratio=inRate/16000,n=Math.floor(input.length/ratio),o=new Int16Array(n);for(let i=0;i<n;i++){let s=input[Math.floor(i*ratio)];o[i]=Math.max(-32768,Math.min(32767,s*32767))}return o}
async function beginDispatchPTT(service,btn,ev){ev.preventDefault();if(activePTT[service])return;if(!window.isSecureContext){alert('Hold to Talk requires the ZelloBridge GUI to be opened over HTTPS so the browser can use your microphone.');return}try{let id=document.getElementById('dispatchMic')?.value||localStorage.getItem('zelloDispatchMic')||'',stream=await navigator.mediaDevices.getUserMedia({audio:id?{deviceId:{exact:id}}:true}),ctx=new AudioContext({latencyHint:'interactive'}),src=ctx.createMediaStreamSource(stream),proc=ctx.createScriptProcessor(2048,1,1),state={stream,ctx,proc,btn,hb:null,stopping:false};activePTT[service]=state;btn.classList.add('keyed');btn.textContent='TRANSMITTING — RELEASE TO STOP';if(btn.setPointerCapture)try{btn.setPointerCapture(ev.pointerId)}catch{};await api('/api/dispatch/ptt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service,active:true})});state.hb=setInterval(()=>api('/api/dispatch/ptt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service,active:true})}).catch(()=>{}),300);proc.onaudioprocess=x=>{if(state.stopping)return;let pcm=resample16k(x.inputBuffer.getChannelData(0),ctx.sampleRate);fetch('/api/dispatch/audio?service='+encodeURIComponent(service),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:pcm.buffer}).catch(()=>{})};src.connect(proc);proc.connect(ctx.destination)}catch(x){await endDispatchPTT(service);alert('Unable to transmit: '+x.message)}}
async function endDispatchPTT(service){let s=activePTT[service];if(s){s.stopping=true;if(s.hb)clearInterval(s.hb);try{s.proc.disconnect()}catch{};s.stream.getTracks().forEach(t=>t.stop());try{await s.ctx.close()}catch{};if(s.btn){s.btn.classList.remove('keyed');s.btn.textContent='HOLD TO TALK'}delete activePTT[service]}try{await api('/api/dispatch/ptt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({service,active:false})})}catch{}}
function dispatchControls(s){let listen=localStorage.getItem('zelloListen:'+s.service)==='1';return`<div class="dispatch-console"><label class="listen-toggle"><span>Listen on this PC</span><input type="checkbox" ${listen?'checked':''} onchange="setListen('${e(s.service)}',this.checked)"></label><button class="ptt-button" ${s.service_active&&s.roxi_connected&&s.transmit_enabled?'':'disabled'} onpointerdown="beginDispatchPTT('${e(s.service)}',this,event)" onpointerup="endDispatchPTT('${e(s.service)}')" onpointercancel="endDispatchPTT('${e(s.service)}')">HOLD TO TALK</button></div>`}
const dispatchCardBase=card;card=function(s){let h=dispatchCardBase(s);h=h.replace('<div class="activity">',dispatchControls(s)+'<div class="activity">');if(s.saved_binding){let p=h.lastIndexOf('</div></div>');if(p>=0)h=h.slice(0,p)+`<button class="zello-settings" onclick="updateRadioZello('${e(s.radio_id)}','${e(s.gateway_user)}','${e(s.channel)}')">Update Zello Settings</button>`+h.slice(p)}return h};
async function updateRadioZello(id,user,channel){let username=prompt('Zello username',user||'');if(username===null)return;let ch=prompt('Zello channel',channel||'');if(ch===null)return;let password=prompt('Zello password');if(password===null||password==='')return;try{await api('/api/setup/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({radio_id:id,username:username,channel:ch,password:password})});alert('Zello settings saved.');await refresh()}catch(x){alert('Unable to save Zello settings: '+x.message)}}
const dispatchRefreshBase=refresh;refresh=async function(){if(Object.keys(activePTT).length)return;await dispatchRefreshBase();let d=await api('/api/status');d.links.forEach(s=>{if(localStorage.getItem('zelloListen:'+s.service)==='1'&&!dispatchListeners[s.service])setListen(s.service,true).catch(()=>{})})};
window.addEventListener('pointerup',()=>Object.keys(activePTT).forEach(endDispatchPTT));window.addEventListener('blur',()=>Object.keys(activePTT).forEach(endDispatchPTT));document.addEventListener('visibilitychange',()=>{if(document.hidden)Object.keys(activePTT).forEach(endDispatchPTT)});window.addEventListener('beforeunload',()=>Object.keys(activePTT).forEach(s=>{navigator.sendBeacon&&navigator.sendBeacon('/api/dispatch/ptt-stop?service='+encodeURIComponent(s),'')}));enumerateDispatchDevices().catch(()=>{});
</script></body>'''
)

SETUP_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Zello Bridge Setup</title><style>:root{--bg:#0b1016;--panel:#141b23;--panel2:#1b2530;--text:#f3f7fb;--muted:#95a4b5;--line:#2b3947;--green:#38d46a;--amber:#f4b942;--blue:#5eb2ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font-family:Segoe UI,Arial,sans-serif}header{padding:18px 24px;background:#05090d;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}a,button{color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:9px;padding:10px 14px;text-decoration:none;font-weight:650;cursor:pointer}button.primary{border-color:#5eb2ff88;background:#5eb2ff22}.wrap{max-width:900px;margin:auto;padding:24px}.step{border:1px solid var(--line);border-radius:14px;background:var(--panel);padding:20px;margin-bottom:16px}.num{display:inline-flex;width:28px;height:28px;border-radius:50%;align-items:center;justify-content:center;background:#5eb2ff22;color:#8bc8ff;font-weight:800;margin-right:8px}.title{font-size:18px;font-weight:750}.help{color:var(--muted);font-size:13px;line-height:1.5;margin:10px 0 14px}.result{padding:12px;border:1px solid var(--line);border-radius:10px;background:#0c131a;margin-top:10px}.ok{border-color:#38d46a66;background:#38d46a10}.warn{border-color:#f4b94266;background:#f4b94210}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.device{padding:12px;border:1px solid var(--line);border-radius:10px;background:#0c131a;margin:8px 0}.small{font-size:11px;color:var(--muted);word-break:break-all}.check{color:#75e899;font-weight:800}.field{margin:10px 0}.field label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}.field input,.field select{width:100%;padding:10px;border-radius:8px;border:1px solid var(--line);background:#0c131a;color:var(--text)}.bindings{font-size:13px}.hidden{display:none}@media(max-width:650px){.row{grid-template-columns:1fr}.wrap{padding:14px}}</style></head><body><header><div><b>Zello Radio Bridge</b><div style="font-size:12px;color:#95a4b5">Guided Radio Gateway Setup</div></div><a href="/">Back to Console</a></header><div class="wrap"><div class="step"><span class="num">1</span><span class="title">Disconnect the Roxi you want to add</span><div class="help">Leave the other already-configured gateways alone. Disconnect only the new Roxi, then capture what hardware is present without it.</div><button class="primary" onclick="baseline()">Scan Without Roxi</button><div id="baseResult"></div></div><div class="step"><span class="num">2</span><span class="title">Connect that Roxi</span><div class="help">Plug in the Roxi and give Linux a few seconds to create its USB audio and FTDI PTT devices. Then scan again.</div><button class="primary" onclick="rescan()">Scan Again</button><div id="diffResult"></div></div><div class="step" id="pairStep"><span class="num">3</span><span class="title">Confirm the detected hardware pair</span><div class="help">If exactly one FTDI interface and one USB audio device appeared, the wizard will preselect them. This is what prevents gateways from being mixed up later.</div><div class="row"><div class="field"><label>PTT / Serial interface</label><select id="serialSelect"></select></div><div class="field"><label>USB Audio interface</label><select id="audioSelect"></select></div></div></div><div class="step"><span class="num">4</span><span class="title">Name this radio gateway</span><div class="row"><div class="field"><label>Radio name</label><input id="name" placeholder="FIRE"></div><div class="field"><label>Zello channel</label><input id="channel" placeholder="FIRE Dispatch"></div></div><div class="field"><label>Zello gateway username</label><input id="gatewayUser" placeholder="gateway-fire"></div><button class="primary" onclick="saveBinding()">Use This Roxi</button><div id="saveResult"></div></div><div class="step"><span class="title">Saved gateway bindings</span><div class="help">These are stable hardware bindings. They are saved disabled until we validate the multi-radio bridge services.</div><div id="bindings" class="bindings">Loading…</div></div></div><script>function e(s){return String(s??'').replace(/[&<>\"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[m]))}async function api(u,o){let r=await fetch(u,o);let t=await r.text();let d;try{d=JSON.parse(t)}catch{d={error:t}}if(!r.ok)throw Error(d.error||t);return d}function dev(x){return`<div class="device"><div><span class="check">✓</span> ${e(x.label)}</div><div class="small">${e(x.by_id||x.control||x.device||'')}</div><div class="small">Serial: ${e(x.serial||'—')} • Path: ${e(x.id_path||x.sys_path||'—')}</div></div>`}function fill(diff){let ss=document.getElementById('serialSelect'),as=document.getElementById('audioSelect');ss.innerHTML=(diff.new_serial||[]).map(x=>`<option value="${e(x.key)}">${e(x.label)} — ${e(x.serial||x.device)}</option>`).join('');as.innerHTML=(diff.new_audio||[]).map(x=>`<option value="${e(x.key)}">${e(x.label)} — card ${e(x.card)}</option>`).join('');if(diff.auto_pair){ss.value=diff.auto_pair.serial_key;as.value=diff.auto_pair.audio_key}let cls=diff.auto_pair?'result ok':'result warn';document.getElementById('diffResult').innerHTML=`<div class="${cls}"><b>${diff.auto_pair?'Roxi pair detected automatically':'Review detected devices'}</b><div class="row"><div><div class="help">New PTT interfaces</div>${(diff.new_serial||[]).map(dev).join('')||'<div class="small">None detected</div>'}</div><div><div class="help">New audio interfaces</div>${(diff.new_audio||[]).map(dev).join('')||'<div class="small">None detected</div>'}</div></div></div>`}async function baseline(){try{let d=await api('/api/setup/baseline',{method:'POST'});document.getElementById('baseResult').innerHTML=`<div class="result ok"><b>Baseline saved.</b><div class="small">${d.scan.serial.length} serial and ${d.scan.audio.length} audio device(s) currently present.</div></div>`}catch(x){alert(x.message)}}async function rescan(){try{let d=await api('/api/setup/rescan',{method:'POST'});fill(d.diff)}catch(x){alert(x.message)}}async function saveBinding(){try{let body={name:document.getElementById('name').value,channel:document.getElementById('channel').value,gateway_user:document.getElementById('gatewayUser').value,serial_key:document.getElementById('serialSelect').value,audio_key:document.getElementById('audioSelect').value};let d=await api('/api/setup/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});document.getElementById('saveResult').innerHTML=`<div class="result ok"><b>Hardware binding saved:</b> ${e(d.radio.name)}<div class="small">It remains disabled until the multi-radio service is validated.</div></div>`;load()}catch(x){document.getElementById('saveResult').innerHTML=`<div class="result warn">${e(x.message)}</div>`}}async function load(){try{let d=await api('/api/setup/state');if(d.state&&d.state.diff)fill(d.state.diff);let rs=d.radios.radios||[];document.getElementById('bindings').innerHTML=rs.length?rs.map(r=>`<div class="device"><b>${e(r.name)}</b> → ${e(r.channel)} <span class="small">${r.enabled?'enabled':'saved / disabled'}</span><div class="small">FTDI: ${e(r.hardware.serial_by_id||'—')}</div><div class="small">Audio path: ${e(r.hardware.audio_id_path||r.hardware.audio_sys_path||'—')}</div><div style=\"margin-top:8px\"><button onclick=\"updateCredentials('${e(r.id)}','${e(r.name)}','${e(r.gateway_user||'')}','${e(r.channel||'')}')\">Update Zello Settings</button></div></div>`).join(''):'<div class="small">No saved bindings yet.</div>'}catch(x){}}load()</script></body></html>'''

SETUP_HTML = SETUP_HTML.replace(
    '<input id=\"gatewayUser\" placeholder=\"gateway-fire\"></div><button',
    '<input id=\"gatewayUser\" placeholder=\"gateway-fire\" autocomplete=\"username\"></div><div class=\"field\"><label>Zello gateway password</label><input id=\"gatewayPassword\" type=\"password\" autocomplete=\"new-password\"></div><button'
).replace(
    "gateway_user:document.getElementById('gatewayUser').value,serial_key:",
    "gateway_user:document.getElementById('gatewayUser').value,password:document.getElementById('gatewayPassword').value,serial_key:"
)

SETUP_HTML = SETUP_HTML.replace('</body>', r'''<div id="credModal" class="hidden" style="position:fixed;inset:0;background:#0009;z-index:50;align-items:center;justify-content:center"><div style="width:min(460px,92vw);background:#141b23;border:1px solid #2b3947;border-radius:14px;padding:20px"><div class="title">Update Zello Credentials</div><div class="help" id="credRadioName"></div><input type="hidden" id="credRadioId"><div class="field"><label>Zello gateway username</label><input id="credUsername" autocomplete="username"></div><div class="field"><label>Zello channel</label><input id="credChannel"></div><div class="field"><label>Zello gateway password</label><input id="credPassword" type="password" autocomplete="new-password"></div><div style="display:flex;gap:8px;justify-content:flex-end"><button onclick="closeCredentials()">Cancel</button><button class="primary" onclick="saveCredentials()">Save Credentials</button></div><div id="credResult"></div></div></div><script>function updateCredentials(id,name,user,channel){document.getElementById('credRadioId').value=id;document.getElementById('credRadioName').textContent=name;document.getElementById('credUsername').value=user||'';document.getElementById('credChannel').value=channel||'';document.getElementById('credPassword').value='';document.getElementById('credResult').innerHTML='';let m=document.getElementById('credModal');m.classList.remove('hidden');m.style.display='flex';setTimeout(()=>document.getElementById('credPassword').focus(),0)}function closeCredentials(){let m=document.getElementById('credModal');m.style.display='none';m.classList.add('hidden');document.getElementById('credPassword').value=''}async function saveCredentials(){let result=document.getElementById('credResult');try{let body={radio_id:document.getElementById('credRadioId').value,username:document.getElementById('credUsername').value,channel:document.getElementById('credChannel').value,password:document.getElementById('credPassword').value};let d=await api('/api/setup/credentials',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});result.innerHTML='<div class="result ok"><b>Credentials saved.</b></div>';document.getElementById('credPassword').value='';await load();setTimeout(closeCredentials,700)}catch(x){result.innerHTML='<div class="result warn">'+e(x.message)+'</div>'}}</script></body>''')


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type','application/json')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def send_html(self, html):
        data = html.encode()
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def body_json(self):
        try:
            return json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or b'{}')
        except Exception:
            raise ValueError('Invalid JSON request.')

    def do_GET(self):
        path, _, query = self.path.partition('?')
        cfg = load_config()
        if path == '/':
            return self.send_html(OPERATOR_HTML)
        if path == '/setup':
            return self.send_html(SETUP_HTML)
        if path == '/api/status':
            return self.send_json({'title':cfg.get('title','Zello Radio Bridge'),'links':[link_status(x) for x in cfg['links']]})
        if path == '/api/dispatch/listen':
            q = parse_qs(query); service = q.get('service',[''])[0]
            if service not in {x['service'] for x in cfg['links']}:
                return self.send_json({'error':'Unknown service'},404)
            hub = get_monitor_hub(service)
            sub = hub.subscribe()
            self.send_response(200)
            self.send_header('Content-Type','application/octet-stream')
            self.send_header('Cache-Control','no-store')
            self.send_header('Transfer-Encoding','chunked')
            self.end_headers()
            try:
                while True:
                    try:
                        data = sub.get(timeout=5)
                    except queue.Empty:
                        data = b''
                    if data:
                        self.wfile.write(f'{len(data):X}\r\n'.encode('ascii'))
                        self.wfile.write(data)
                        self.wfile.write(b'\r\n')
                        self.wfile.flush()
                    else:
                        self.wfile.write(b'1\r\n\x00\r\n')
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(sub)
            return
        if path == '/api/logs':
            q = parse_qs(query); service = q.get('service',[''])[0]
            if service not in {x['service'] for x in cfg['links']}:
                return self.send_json({'error':'Unknown service'},404)
            lines = []
            for ts, line in journal(service,100):
                stamp = time.strftime('%Y-%m-%d %I:%M:%S %p', time.localtime(ts)) if ts else ''
                lines.append(f'[{stamp}] {line}' if stamp else line)
            return self.send_json({'service':service,'lines':lines})
        if path == '/api/setup/state':
            return self.send_json({'state':load_setup_state(),'radios':existing_radios(),'scan':scan_hardware()})
        return self.send_json({'error':'Not found'},404)

    def do_POST(self):
        path = self.path.partition('?')[0]
        cfg = load_config()
        if path == '/api/dispatch/ptt':
            try: body = self.body_json()
            except ValueError as exc: return self.send_json({'error':str(exc)},400)
            service = body.get('service'); active = body.get('active')
            if service not in {x['service'] for x in cfg['links']}:
                return self.send_json({'error':'Unknown service'},404)
            if not isinstance(active, bool):
                return self.send_json({'error':'Invalid PTT state'},400)
            link = next((x for x in cfg['links'] if x['service'] == service), None)
            status = link_status(link) if link else None
            if active and (not status or not status['service_active'] or not status['roxi_connected'] or not status['transmit_enabled']):
                return self.send_json({'error':'Radio is not ready for dispatch transmit'},409)
            set_dispatch_ptt(service, active)
            return self.send_json({'ok':True,'service':service,'active':active})
        if path == '/api/dispatch/ptt-stop':
            q = parse_qs(self.path.partition('?')[2]); service = q.get('service',[''])[0]
            if service in {x['service'] for x in cfg['links']}:
                set_dispatch_ptt(service, False)
                return self.send_json({'ok':True})
            return self.send_json({'error':'Unknown service'},404)
        if path == '/api/dispatch/audio':
            q = parse_qs(self.path.partition('?')[2]); service = q.get('service',[''])[0]
            if service not in {x['service'] for x in cfg['links']}:
                return self.send_json({'error':'Unknown service'},404)
            try:
                length = int(self.headers.get('Content-Length','0'))
            except ValueError:
                length = 0
            if length <= 0 or length > 131072:
                return self.send_json({'error':'Invalid audio payload'},400)
            data = self.rfile.read(length)
            send_dispatch_audio(service, data)
            return self.send_json({'ok':True})
        if path == '/api/action':
            try: body = self.body_json()
            except ValueError as exc: return self.send_json({'error':str(exc)},400)
            service, action = body.get('service'), body.get('action')
            if service not in {x['service'] for x in cfg['links']} or action not in {'start','stop','restart'}:
                return self.send_json({'error':'Invalid service or action'},400)
            rc, out, err = run(['systemctl',action,service],12)
            return self.send_json({'ok':True,'service':service,'action':action}) if rc == 0 else self.send_json({'error':err or out or 'systemctl failed'},500)
        if path == '/api/control':
            try: body = self.body_json()
            except ValueError as exc: return self.send_json({'error':str(exc)},400)
            service = body.get('service')
            key = body.get('key')
            enabled = body.get('enabled')
            if service not in {x['service'] for x in cfg['links']}:
                return self.send_json({'error':'Unknown service'},404)
            if key not in {'receive_enabled','transmit_enabled'} or not isinstance(enabled, bool):
                return self.send_json({'error':'Invalid radio control'},400)
            controls = set_control(service, key, enabled)
            return self.send_json({'ok':True,'service':service,'controls':controls})
        if path == '/api/setup/baseline':
            scan = scan_hardware(); state = {'baseline':scan,'current':None,'diff':None}; save_setup_state(state)
            return self.send_json({'ok':True,'scan':scan})
        if path == '/api/setup/rescan':
            state = load_setup_state()
            if not state.get('baseline'):
                return self.send_json({'error':'Run Scan Without Roxi first.'},400)
            current = scan_hardware(); diff = diff_scan(state['baseline'],current)
            state.update({'current':current,'diff':diff}); save_setup_state(state)
            return self.send_json({'ok':True,'scan':current,'diff':diff})
        if path == '/api/setup/credentials':
            try:
                radio = update_credentials(self.body_json())
                return self.send_json({'ok':True,'radio':radio})
            except ValueError as exc:
                return self.send_json({'error':str(exc)},400)
            except Exception as exc:
                return self.send_json({'error':f'Unable to update credentials: {exc}'},500)
        if path == '/api/setup/save':
            try:
                radio = save_binding(self.body_json())
                return self.send_json({'ok':True,'radio':radio})
            except ValueError as exc:
                return self.send_json({'error':str(exc)},400)
            except Exception as exc:
                return self.send_json({'error':f'Unable to save binding: {exc}'},500)
        return self.send_json({'error':'Not found'},404)

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    print(f'Zello Bridge GUI listening on http://{HOST}:{PORT}', flush=True)
    ThreadingHTTPServer((HOST,PORT),H).serve_forever()
