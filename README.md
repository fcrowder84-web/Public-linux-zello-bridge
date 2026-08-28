# Public Linux Zello Bridge

A Linux-based bridge between physical two-way radios and Zello Work.

This project was built to replace a Windows radio-gateway bridge with a native Linux service that can run multiple radio interfaces, expose a browser-based operator console, and provide fail-safe physical PTT control.

## Features

- Multiple independent radio-to-Zello bridges on one Linux host
- USB audio capture/playback for each radio interface
- FTDI serial RTS PTT control
- Radio -> Zello VOX forwarding
- Zello -> physical-radio audio with automatic PTT
- Browser operator console
- Listen to individual radios from the operator PC
- Browser microphone Hold-to-Talk
- Operator mic audio sent to the physical radio and Zello simultaneously
- Guided USB gateway discovery and stable physical-path binding
- Per-radio receive/transmit safety switches
- Automatic reconnect to Zello
- systemd service support
- Dispatch PTT software lease so PTT releases if the browser or network disappears

## Status

This is an early public release based on a working multi-radio deployment. It should be considered experimental until you have fully tested it with your own radios and interface hardware.

**Never connect this software to an operational transmitter until you have verified PTT polarity, audio levels, isolation, and fail-safe release behavior on a bench setup.**

## Hardware

The current implementation was developed with Roxi-style USB radio gateways containing:

- a USB audio interface
- an FTDI serial interface used for PTT
- isolated radio audio paths
- a relay/transistor PTT output

The bridge expects the PTT interface to be controlled through FTDI RTS. The tested configuration uses RTS active-high and releases RTS on startup, shutdown, reconnect, and error cleanup.

Other USB radio interfaces can work if they provide equivalent Linux ALSA audio devices and a serial-controlled PTT interface. Adaptation may be required.

## Linux requirements

Recommended base system:

- Debian 12/13 or Ubuntu Server
- Python 3.11+
- ALSA
- libopus
- systemd

Install system packages on Debian/Ubuntu:

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv alsa-utils libopus0 git
```

Install Python dependencies:

```bash
python3 -m pip install --break-system-packages -r requirements.txt
```

A virtual environment can be used instead if preferred; update the systemd service paths accordingly.

## Installation

Clone the repository:

```bash
git clone https://github.com/fcrowder84-web/Public-linux-zello-bridge.git
sudo mkdir -p /opt/zello-bridge
sudo cp -a Public-linux-zello-bridge/. /opt/zello-bridge/
```

Create runtime directories:

```bash
sudo mkdir -p /var/lib/zello-bridge
sudo mkdir -p /etc/zello-bridge-credentials
sudo chmod 700 /etc/zello-bridge-credentials
```

Create the global Zello configuration:

```bash
sudo cp /opt/zello-bridge/config.example.json /etc/zello-bridge.json
sudo nano /etc/zello-bridge.json
```

Set your Zello Work websocket URL:

```json
{
  "zello_ws_url": "wss://zellowork.io/ws/YOUR_ZELLO_WORK_NETWORK"
}
```

Your Zello Work network identifier is installation-specific. Do not use the example value literally.

Install the systemd services:

```bash
sudo cp /opt/zello-bridge/zello-bridge@.service /etc/systemd/system/
sudo cp /opt/zello-bridge/zello-bridge-gui.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zello-bridge-gui.service
```

The web console listens on port `8810` by default.

## Adding a radio

Open the web console and choose **Setup Radios**.

The guided setup process is designed to avoid mixing up identical USB gateways:

1. Disconnect the new gateway.
2. Click **Scan Without Roxi** to capture a baseline.
3. Connect the gateway to the USB port you intend to keep using.
4. Click **Scan Again**.
5. Confirm the newly detected FTDI PTT interface and USB audio interface.
6. Enter a radio name, Zello channel, and Zello username.
7. Save the hardware binding.
8. Use **Update Zello Settings** on the radio card to store the Zello password and update channel/username later without rescanning the hardware.

The saved radio database is stored at:

```text
/etc/zello-bridge-radios.json
```

Credentials are stored separately under:

```text
/etc/zello-bridge-credentials/
```

Credential files should remain mode `0600` and must never be committed to Git.

## Starting a radio bridge

A saved radio with ID `fire` is run by:

```bash
sudo systemctl enable --now zello-bridge@fire.service
```

Check its logs with:

```bash
journalctl -u zello-bridge@fire.service -f
```

Each bridge process resolves its saved USB hardware binding, reads its credentials, connects to Zello, and starts radio audio capture.

## Operator console

The console provides per-radio status for:

- Zello connection
- USB radio interface presence
- Receive activity
- Transmit/PTT activity
- Last RX and TX timestamps
- service uptime
- restart count
- receive/transmit enable controls

### Listen on this PC

Enabling **Listen on this PC** streams raw 16 kHz mono radio audio from the Linux bridge to the browser.

This listen function is independent of the Radio -> Zello bridge enable switch.

### Hold to Talk

The browser dispatch control is a momentary press-and-hold PTT button.

While held:

- the selected browser microphone is captured
- the physical radio PTT is asserted
- microphone audio is played into the physical radio interface
- the same microphone audio is sent into the Zello channel

Releasing the button releases PTT.

A short server-side lease is refreshed while PTT is held. If the browser disappears, the network connection fails, or the heartbeat stops, the bridge automatically releases PTT.

## HTTPS is required for browser microphone access

Modern browsers require a secure context for microphone capture. Opening the console through plain HTTP by IP address will normally allow viewing and basic controls, but browser Hold-to-Talk will not have microphone access.

Use a trusted HTTPS reverse proxy, Tailscale Serve, or another secure HTTPS endpoint for the console.

Do not disable browser security protections as a normal deployment method.

## Stable USB mapping

Identical FTDI and USB audio interfaces may have duplicate or unreliable serial identities. This project therefore supports binding gateways by their physical USB/sysfs topology.

For reliable operation:

- use a powered USB hub when needed
- assign one permanent physical USB port to each radio gateway
- do not casually move gateways between ports after setup
- verify all radio mappings after hardware maintenance

## Audio format

The bridge currently operates at:

- 16 kHz
- mono
- signed 16-bit PCM
- 20 ms audio frames
- Opus for Zello audio transport

Radio -> Zello uses VOX with trigger, pre-roll, and release timing defined in `radio_engine.py`.

## Safety behavior

The bridge is designed to fail toward PTT released:

- RTS is released during startup
- RTS is released on normal shutdown
- RTS is released during reconnect/error cleanup
- browser dispatch PTT uses a short software lease
- newly configured radio bridge directions default disabled until intentionally enabled

For critical/public-safety use, an independent hardware PTT watchdog is strongly recommended in addition to the software lease.

## Troubleshooting

### Zello login fails

Check:

- `zello_ws_url` in `/etc/zello-bridge.json`
- Zello username/password
- exact channel name
- whether the account has the channel in its contact list

### Radio gateway missing

Check:

```bash
ls -l /dev/serial/by-path/
arecord -l
aplay -l
```

Then verify the saved physical USB path in `/etc/zello-bridge-radios.json`.

### No browser microphone

Confirm the console is being opened through HTTPS and that the browser has microphone permission.

### PTT works but no audio

Verify ALSA capture/playback device selection, radio accessory-port levels, cable pinout, grounding, and isolation.

## Security notes

The current web console is intended for a trusted LAN or private overlay network. Before exposing it to an untrusted network, add strong authentication/authorization and appropriate CSRF/session protections.

Do not expose credential files or `/etc/zello-bridge-credentials` through a web server.

## Zello

Zello and Zello Work are products/services of Zello Inc. This project is an independent open-source integration and is **not affiliated with, endorsed by, or supported by Zello Inc.**

You are responsible for complying with Zello's terms, your radio licenses, local laws, and your organization's communications policies.

## License

MIT License. See [LICENSE](LICENSE).

## Contributing

Issues and pull requests are welcome. Hardware varies widely, so reports that include Linux version, USB interface type, radio model, sanitized logs, and expected behavior are especially useful.
