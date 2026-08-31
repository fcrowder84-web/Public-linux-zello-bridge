# Changelog

All notable changes to the Public Linux Zello Bridge are documented here.

## 2026-08-31

### Fixed

- Fixed Zello-to-radio transmit audio on Roxi-style/C-Media USB radio interfaces where PTT keyed correctly but the physical radio transmitted silence, pops, or unusable audio.
- Fixed browser **Hold to Talk** audio reaching Zello but not reaching the physical radio on the same affected USB interfaces.
- The bridge now treats the USB radio interface as half-duplex at the ALSA device level: continuous `arecord` capture is paused before opening `aplay` for radio transmit, then capture is automatically restarted after transmit completes.
- The capture loop is restartable so an intentional capture pause does not terminate or reconnect the bridge service.
- Added a short delay after closing capture so the C-Media USB playback endpoint can be opened cleanly.
- Fixed the operator GUI incorrectly showing `arecord: pcm_read:2272: read error: Interrupted system call` as a **Current issue** when that message is produced by the intentional capture pause. Real ALSA, process, and bridge errors continue to be reported.

### Technical notes

The failure was reproduced independently of Zello by holding the C-Media capture endpoint open with `arecord` while playing a known-good WAV through the same device. Playback failed while capture was open and worked immediately after capture was closed. This isolated the issue from Opus decoding, Zello packet handling, PTT control, ALSA format conversion, and the radio hardware itself.

The public runtime now starts saved-radio bridges through `radio_engine_runner.py`, which applies the tested half-duplex capture/playback handling to `radio_engine.py`. The web console service starts through `gui_runner.py`, which filters only the known benign intentional-capture interruption from health reporting.

## Initial public release

- Multi-radio Linux Zello Work bridge.
- USB audio capture/playback with FTDI RTS PTT control.
- Radio-to-Zello VOX forwarding with pre-roll and release timing.
- Zello-to-radio automatic PTT and audio playback.
- Browser operator console with per-radio status and controls.
- Browser radio monitoring and Hold-to-Talk dispatch audio.
- Guided Roxi-style USB gateway setup with stable physical USB-path binding.
- Per-radio receive/transmit safety controls.
- systemd service support and automatic Zello reconnect.
