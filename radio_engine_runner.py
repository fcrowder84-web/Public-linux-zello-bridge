#!/usr/bin/env python3
"""Run radio_engine.py with the Roxi/C-Media half-duplex audio fix applied.

Some C-Media USB radio interfaces cannot produce valid playback while their
capture endpoint is open. The bridge normally keeps arecord open continuously,
which can result in PTT keying with silent/corrupted transmit audio.

This runner loads the normal engine, patches capture handling so arecord is
closed for radio transmit playback, and then runs the normal engine main().
"""

import asyncio
import importlib.util
from collections import deque
from pathlib import Path

ENGINE_PATH = Path('/opt/zello-bridge/radio_engine.py')

spec = importlib.util.spec_from_file_location('zello_radio_engine', ENGINE_PATH)
engine = importlib.util.module_from_spec(spec)
spec.loader.exec_module(engine)

Bridge = engine.Bridge
_original_init = Bridge.__init__
_original_start_dispatch_tx = Bridge.start_dispatch_tx
_original_stop_dispatch_tx = Bridge.stop_dispatch_tx
_original_start_zello_rx = Bridge.start_zello_rx
_original_stop_zello_rx = Bridge.stop_zello_rx


def patched_init(self):
    _original_init(self)
    self.capture_process = None
    self.capture_paused = False


async def pause_capture(self):
    """Close the Roxi capture endpoint before opening playback."""
    self.capture_paused = True
    process = self.capture_process

    if process and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()

    self.capture_process = None

    # Give the USB audio device a moment to release the capture endpoint.
    await asyncio.sleep(0.10)
    print('*** RADIO RX CAPTURE PAUSED FOR TRANSMIT ***')


def resume_capture(self):
    if self.capture_paused:
        self.capture_paused = False
        print('*** RADIO RX CAPTURE RESUME REQUESTED ***')


async def patched_start_dispatch_tx(self):
    # Match the engine's normal rejection checks before disturbing capture.
    if self.dispatch_tx_active or self.dispatch_tx_starting or not self.transmit_enabled:
        return
    if self.zello_rx_active or self.radio_tx_active:
        return
    if not self.channel_online.is_set():
        return await _original_start_dispatch_tx(self)

    await self.pause_capture()
    try:
        await _original_start_dispatch_tx(self)
    except Exception:
        self.resume_capture()
        raise

    if not self.dispatch_tx_active and not self.dispatch_player:
        self.resume_capture()


async def patched_stop_dispatch_tx(self):
    try:
        await _original_stop_dispatch_tx(self)
    finally:
        self.resume_capture()


async def patched_start_zello_rx(self, data):
    # Preserve all normal engine safety/half-duplex rejection behavior.
    if not self.transmit_enabled or self.radio_tx_active or self.dispatch_tx_active:
        return await _original_start_zello_rx(self, data)

    await self.pause_capture()
    try:
        await _original_start_zello_rx(self, data)
    except Exception:
        self.resume_capture()
        raise

    if not self.zello_rx_active:
        self.resume_capture()


async def patched_stop_zello_rx(self, force=False):
    try:
        await _original_stop_zello_rx(self, force=force)
    finally:
        self.resume_capture()


async def patched_capture_loop(self):
    """Restartable radio capture loop that can be paused for playback."""
    while True:
        while self.capture_paused:
            await asyncio.sleep(0.05)

        process = await asyncio.create_subprocess_exec(
            'arecord',
            '-q',
            '-D', engine.AUDIO_DEV,
            '-t', 'raw',
            '-f', 'S16_LE',
            '-r', str(engine.SAMPLE_RATE),
            '-c', '1',
            stdout=asyncio.subprocess.PIPE,
        )
        self.capture_process = process

        print(
            'Radio RX capture started.',
            f'VOX threshold={engine.VOX_THRESHOLD}'
        )

        prebuffer = deque(maxlen=engine.PREROLL_CHUNKS)
        above_count = 0
        silence_count = 0

        try:
            while True:
                try:
                    data = await process.stdout.readexactly(engine.CHUNK_BYTES)
                except asyncio.IncompleteReadError:
                    break

                if self.capture_paused:
                    break

                try:
                    self.dispatch_rx_socket.sendto(
                        data,
                        ('127.0.0.1', engine.DISPATCH_RX_PORT)
                    )
                except OSError:
                    pass

                rms = engine.rms_level(data)

                if not self.receive_enabled:
                    if (
                        self.radio_tx_active
                        and not (
                            self.dispatch_tx_active
                            or self.dispatch_tx_starting
                        )
                    ):
                        await self.stop_radio_stream()
                    prebuffer.clear()
                    above_count = 0
                    silence_count = 0
                    continue

                if (
                    self.zello_rx_active
                    or self.dispatch_tx_active
                    or self.dispatch_tx_starting
                    or engine.time.monotonic() < self.inhibit_until
                ):
                    prebuffer.clear()
                    above_count = 0
                    silence_count = 0
                    continue

                if not self.radio_tx_active:
                    prebuffer.append(data)

                    if rms >= engine.VOX_THRESHOLD:
                        above_count += 1
                    else:
                        above_count = 0

                    if (
                        above_count >= engine.TRIGGER_CHUNKS
                        and self.channel_online.is_set()
                    ):
                        print('VOX triggered. RMS:', rms)

                        try:
                            await self.start_radio_stream()
                        except Exception as exc:
                            print('Unable to start Zello stream:', repr(exc))
                            above_count = 0
                            prebuffer.clear()
                            continue

                        for buffered in list(prebuffer):
                            await self.send_radio_audio(buffered)

                        prebuffer.clear()
                        silence_count = 0

                    continue

                await self.send_radio_audio(data)

                if rms < engine.VOX_THRESHOLD:
                    silence_count += 1
                else:
                    silence_count = 0

                if silence_count >= engine.RELEASE_CHUNKS:
                    await self.stop_radio_stream()
                    prebuffer.clear()
                    above_count = 0
                    silence_count = 0

        finally:
            if self.radio_tx_active:
                await self.stop_radio_stream()

            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()

            if self.capture_process is process:
                self.capture_process = None

        if not self.capture_paused:
            print('Radio RX capture stopped; restarting.')
            await asyncio.sleep(0.25)


Bridge.__init__ = patched_init
Bridge.pause_capture = pause_capture
Bridge.resume_capture = resume_capture
Bridge.start_dispatch_tx = patched_start_dispatch_tx
Bridge.stop_dispatch_tx = patched_stop_dispatch_tx
Bridge.start_zello_rx = patched_start_zello_rx
Bridge.stop_zello_rx = patched_stop_zello_rx
Bridge.capture_loop = patched_capture_loop


if __name__ == '__main__':
    asyncio.run(engine.main())
