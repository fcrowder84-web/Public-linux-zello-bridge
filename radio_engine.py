#!/usr/bin/env python3

import asyncio
import base64
import ctypes
import json
import math
import os
import signal
import socket
import struct
import subprocess
import time
import zlib
from collections import deque

import serial
import websockets


# ============================================================
# RUNTIME CONFIGURATION
# ============================================================

URL = os.environ["ZELLO_WS_URL"]
USERNAME = os.environ["ZELLO_USERNAME"]
PASSWORD = os.environ["ZELLO_PASSWORD"]
CHANNEL = os.environ["ZELLO_CHANNEL"]
SERIAL_DEV = os.environ["ZELLO_SERIAL_DEV"]

AUDIO_DEV = os.environ["ZELLO_AUDIO_DEV"]

CONTROL_FILE = "/var/lib/zello-bridge/controls.json"
DISPATCH_FILE = "/var/lib/zello-bridge/dispatch.json"
SERVICE_ID = os.environ.get("ZELLO_BRIDGE_SERVICE", "zello-bridge@radio.service")

def dispatch_ports(service):
    slot = zlib.crc32(service.encode("utf-8")) % 1000
    return 41000 + slot, 43000 + slot

DISPATCH_RX_PORT, DISPATCH_TX_PORT = dispatch_ports(SERVICE_ID)

SAMPLE_RATE = 16000
FRAME_MS = 20
SAMPLES_PER_CHUNK = SAMPLE_RATE * FRAME_MS // 1000
CHUNK_BYTES = SAMPLES_PER_CHUNK * 2

# Radio -> Zello VOX
VOX_THRESHOLD = 20
TRIGGER_MS = 200
RELEASE_MS = 700
PREROLL_MS = 300

TRIGGER_CHUNKS = TRIGGER_MS // FRAME_MS
RELEASE_CHUNKS = RELEASE_MS // FRAME_MS
PREROLL_CHUNKS = PREROLL_MS // FRAME_MS

# Zello -> radio timing
PTT_LEAD = 0.200
PTT_TAIL = 0.700

# Prevent Zello audio played into the radio from
# immediately retriggering radio -> Zello.
RX_INHIBIT_AFTER_ZELLO = 1.000

RECONNECT_DELAY = 5


# ============================================================
# OPUS
# ============================================================

opus = ctypes.cdll.LoadLibrary("libopus.so.0")

opus.opus_encoder_create.restype = ctypes.c_void_p
opus.opus_encoder_create.argtypes = [
    ctypes.c_int32,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
]

opus.opus_encode.restype = ctypes.c_int
opus.opus_encode.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_int16),
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_int32,
]

opus.opus_encoder_destroy.argtypes = [
    ctypes.c_void_p
]

opus.opus_decoder_create.restype = ctypes.c_void_p
opus.opus_decoder_create.argtypes = [
    ctypes.c_int32,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int),
]

opus.opus_decode.restype = ctypes.c_int
opus.opus_decode.argtypes = [
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_int32,
    ctypes.POINTER(ctypes.c_int16),
    ctypes.c_int,
    ctypes.c_int,
]

opus.opus_decoder_destroy.argtypes = [
    ctypes.c_void_p
]


def rms_level(data):
    samples = struct.unpack(
        f"<{len(data)//2}h",
        data
    )

    return int(math.sqrt(
        sum(x * x for x in samples)
        / len(samples)
    ))


# ============================================================
# BRIDGE
# ============================================================

class Bridge:

    def __init__(self):

        self.ws = None
        self.seq = 0
        self.pending = {}

        self.send_lock = asyncio.Lock()
        self.channel_online = asyncio.Event()

        # Radio -> Zello
        self.radio_tx_active = False
        self.tx_stream_id = None
        self.tx_encoder = None

        # Zello -> Radio
        self.zello_rx_active = False
        self.rx_stream_id = None
        self.rx_decoder = None
        self.rx_sample_rate = None
        self.player = None

        self.ignore_rx_stream = None

        self.inhibit_until = 0

        # Operator controls. Defaults preserve existing behavior.
        self.receive_enabled = True   # Radio -> Zello
        self.transmit_enabled = True  # Zello -> Radio / PTT

        # Browser dispatch console. Audio stays local to the appliance and
        # is moved between this bridge process and the GUI over localhost UDP.
        self.dispatch_requested = False
        self.dispatch_tx_active = False
        self.dispatch_tx_starting = False
        self.dispatch_zello_buffer = b""
        self.dispatch_player = None
        self.dispatch_rx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dispatch_tx_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dispatch_tx_socket.bind(("127.0.0.1", DISPATCH_TX_PORT))
        self.dispatch_tx_socket.setblocking(False)

        # PTT
        self.ptt = serial.Serial()
        self.ptt.port = SERIAL_DEV
        self.ptt.baudrate = 9600
        self.ptt.timeout = 1

        # Known-good idle state
        self.ptt.rts = False
        self.ptt.dtr = False

        self.ptt.open()
        self.ptt.rts = False

        print("FTDI PTT ready:", SERIAL_DEV)

        self.refresh_controls()

    # --------------------------------------------------------
    # Operator controls
    # --------------------------------------------------------

    def refresh_controls(self):
        try:
            with open(CONTROL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            service = data.get("services", {}).get(SERVICE_ID, {})
            self.receive_enabled = bool(service.get("receive_enabled", True))
            self.transmit_enabled = bool(service.get("transmit_enabled", True))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            self.receive_enabled = True
            self.transmit_enabled = True

    def refresh_dispatch_request(self):
        try:
            with open(DISPATCH_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            entry = data.get("services", {}).get(SERVICE_ID, {})
            self.dispatch_requested = float(entry.get("ptt_until", 0)) > time.time()
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            self.dispatch_requested = False

    async def start_dispatch_tx(self):
        if self.dispatch_tx_active or self.dispatch_tx_starting or not self.transmit_enabled:
            return
        if self.zello_rx_active or self.radio_tx_active:
            return
        if not self.channel_online.is_set():
            print("Dispatch PTT rejected: Zello channel is not online.")
            return
        self.dispatch_tx_starting = True
        self.dispatch_zello_buffer = b""
        self.dispatch_player = subprocess.Popen([
            "aplay", "-q", "-D", AUDIO_DEV,
            "-t", "raw", "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", "1"
        ], stdin=subprocess.PIPE, bufsize=0)
        try:
            await self.start_radio_stream()
        except Exception:
            try:
                self.dispatch_player.stdin.close()
            except Exception:
                pass
            try:
                self.dispatch_player.terminate()
            except Exception:
                pass
            self.dispatch_player = None
            self.dispatch_tx_starting = False
            self.dispatch_zello_buffer = b""
            raise
        self.ptt.rts = True
        await asyncio.sleep(PTT_LEAD)
        self.dispatch_tx_active = True
        self.dispatch_tx_starting = False
        print("*** DISPATCH CONSOLE -> RADIO START ***")
        print("*** DISPATCH CONSOLE -> ZELLO START ***")

    async def stop_dispatch_tx(self):
        if not self.dispatch_tx_active and not self.dispatch_tx_starting and not self.dispatch_player:
            self.ptt.rts = False
            return
        self.dispatch_tx_active = False
        self.dispatch_tx_starting = False
        if self.dispatch_player:
            try:
                self.dispatch_player.stdin.close()
            except Exception:
                pass
            try:
                self.dispatch_player.wait(timeout=1)
            except Exception:
                try:
                    self.dispatch_player.terminate()
                except Exception:
                    pass
        self.dispatch_player = None
        self.dispatch_zello_buffer = b""
        if self.radio_tx_active:
            try:
                await self.stop_radio_stream()
            except Exception as exc:
                print("Dispatch Zello stop error:", repr(exc))
        await asyncio.sleep(0.15)
        self.ptt.rts = False
        self.inhibit_until = time.monotonic() + RX_INHIBIT_AFTER_ZELLO
        print("*** DISPATCH CONSOLE -> ZELLO STOP ***")
        print("*** DISPATCH CONSOLE -> RADIO STOP ***")

    async def dispatch_audio_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.sock_recv(self.dispatch_tx_socket, 8192)
            if self.dispatch_tx_active and self.dispatch_player and self.dispatch_player.stdin:
                try:
                    self.dispatch_player.stdin.write(data)
                    if self.radio_tx_active:
                        self.dispatch_zello_buffer += data
                        while len(self.dispatch_zello_buffer) >= CHUNK_BYTES:
                            chunk = self.dispatch_zello_buffer[:CHUNK_BYTES]
                            self.dispatch_zello_buffer = self.dispatch_zello_buffer[CHUNK_BYTES:]
                            await self.send_radio_audio(chunk)
                except (BrokenPipeError, OSError):
                    await self.stop_dispatch_tx()

    async def control_watcher(self):
        while True:
            old_receive = self.receive_enabled
            old_transmit = self.transmit_enabled
            self.refresh_controls()
            self.refresh_dispatch_request()

            if old_transmit and not self.transmit_enabled:
                if self.zello_rx_active:
                    print("*** TRANSMIT DISABLED BY OPERATOR; RELEASING PTT ***")
                    await self.stop_zello_rx(force=True)
                if self.dispatch_tx_active:
                    await self.stop_dispatch_tx()

            if old_receive and not self.receive_enabled and self.radio_tx_active:
                print("*** RECEIVE DISABLED BY OPERATOR; STOPPING RADIO STREAM ***")
                await self.stop_radio_stream()

            if self.dispatch_requested and not self.dispatch_tx_active:
                await self.start_dispatch_tx()
            elif not self.dispatch_requested and self.dispatch_tx_active:
                await self.stop_dispatch_tx()

            await asyncio.sleep(0.10)

    # --------------------------------------------------------
    # websocket helpers
    # --------------------------------------------------------

    async def send(self, payload):

        async with self.send_lock:
            await self.ws.send(payload)

    async def command(self, payload, timeout=20):

        self.seq += 1
        seq = self.seq

        payload["seq"] = seq

        loop = asyncio.get_running_loop()
        future = loop.create_future()

        self.pending[seq] = future

        await self.send(json.dumps(payload))

        try:
            return await asyncio.wait_for(
                future,
                timeout=timeout
            )

        finally:
            self.pending.pop(seq, None)

    # --------------------------------------------------------
    # Opus TX
    # --------------------------------------------------------

    def create_tx_encoder(self):

        err = ctypes.c_int()

        # OPUS_APPLICATION_VOIP = 2048
        encoder = opus.opus_encoder_create(
            SAMPLE_RATE,
            1,
            2048,
            ctypes.byref(err)
        )

        if not encoder or err.value != 0:
            raise RuntimeError(
                f"Opus encoder creation failed: {err.value}"
            )

        return encoder

    async def start_radio_stream(self):

        codec_header = base64.b64encode(
            struct.pack(
                "<HBB",
                SAMPLE_RATE,
                1,
                FRAME_MS
            )
        ).decode()

        response = await self.command({
            "command": "start_stream",
            "channel": CHANNEL,
            "type": "audio",
            "codec": "opus",
            "codec_header": codec_header,
            "packet_duration": FRAME_MS
        })

        if not response.get("success"):
            raise RuntimeError(
                f"start_stream failed: {response}"
            )

        self.tx_stream_id = response["stream_id"]
        self.tx_encoder = self.create_tx_encoder()
        self.radio_tx_active = True

        print(
            "*** RADIO -> ZELLO START",
            self.tx_stream_id,
            "***"
        )

    async def send_radio_audio(self, pcm):

        if not self.tx_encoder:
            return

        samples = (
            ctypes.c_int16 * SAMPLES_PER_CHUNK
        ).from_buffer_copy(pcm)

        encoded_buffer = (
            ctypes.c_ubyte * 4000
        )()

        encoded_len = opus.opus_encode(
            self.tx_encoder,
            samples,
            SAMPLES_PER_CHUNK,
            encoded_buffer,
            len(encoded_buffer)
        )

        if encoded_len < 0:
            raise RuntimeError(
                f"Opus encode error: {encoded_len}"
            )

        packet = (
            struct.pack(
                ">BII",
                1,
                self.tx_stream_id,
                0
            )
            + bytes(
                encoded_buffer[:encoded_len]
            )
        )

        await self.send(packet)

    async def stop_radio_stream(self):

        if not self.radio_tx_active:
            return

        stream_id = self.tx_stream_id

        try:
            response = await self.command({
                "command": "stop_stream",
                "stream_id": stream_id,
                "channel": CHANNEL
            })

            if not response.get("success"):
                print(
                    "stop_stream response:",
                    response
                )

        except Exception as e:
            print(
                "stop_stream error:",
                repr(e)
            )

        finally:

            if self.tx_encoder:
                opus.opus_encoder_destroy(
                    self.tx_encoder
                )

            self.tx_encoder = None
            self.tx_stream_id = None
            self.radio_tx_active = False

            print(
                "*** RADIO -> ZELLO STOP ***"
            )

    # --------------------------------------------------------
    # Radio capture / VOX
    # --------------------------------------------------------

    async def capture_loop(self):

        process = await asyncio.create_subprocess_exec(
            "arecord",
            "-q",
            "-D", AUDIO_DEV,
            "-t", "raw",
            "-f", "S16_LE",
            "-r", str(SAMPLE_RATE),
            "-c", "1",
            stdout=asyncio.subprocess.PIPE
        )

        print(
            "Radio RX capture started.",
            f"VOX threshold={VOX_THRESHOLD}"
        )

        prebuffer = deque(
            maxlen=PREROLL_CHUNKS
        )

        above_count = 0
        silence_count = 0

        try:

            while True:

                try:
                    data = await process.stdout.readexactly(
                        CHUNK_BYTES
                    )
                except asyncio.IncompleteReadError:
                    break

                # Always mirror radio receive audio to the local dispatch GUI.
                # This is independent of whether Radio -> Zello forwarding is enabled.
                try:
                    self.dispatch_rx_socket.sendto(data, ("127.0.0.1", DISPATCH_RX_PORT))
                except OSError:
                    pass

                rms = rms_level(data)

                # Operator Receive control: when disabled, keep capture open
                # but do not VOX or forward any radio audio to Zello.
                if not self.receive_enabled:
                    if self.radio_tx_active and not (self.dispatch_tx_active or self.dispatch_tx_starting):
                        await self.stop_radio_stream()
                    prebuffer.clear()
                    above_count = 0
                    silence_count = 0
                    continue

                # ------------------------------------------------
                # We are currently receiving FROM Zello.
                # Never send that audio straight back to Zello.
                # ------------------------------------------------

                if (
                    self.zello_rx_active
                    or self.dispatch_tx_active
                    or self.dispatch_tx_starting
                    or time.monotonic()
                    < self.inhibit_until
                ):

                    prebuffer.clear()
                    above_count = 0
                    silence_count = 0
                    continue

                # ------------------------------------------------
                # No radio transmission active yet
                # ------------------------------------------------

                if not self.radio_tx_active:

                    prebuffer.append(data)

                    if rms >= VOX_THRESHOLD:
                        above_count += 1
                    else:
                        above_count = 0

                    if (
                        above_count
                        >= TRIGGER_CHUNKS
                        and self.channel_online.is_set()
                    ):

                        print(
                            "VOX triggered. RMS:",
                            rms
                        )

                        try:
                            await self.start_radio_stream()

                        except Exception as e:
                            print(
                                "Unable to start Zello stream:",
                                repr(e)
                            )

                            above_count = 0
                            prebuffer.clear()
                            continue

                        # Send pre-roll so the beginning
                        # of the transmission is preserved.
                        for buffered in list(prebuffer):
                            await self.send_radio_audio(
                                buffered
                            )

                        prebuffer.clear()
                        silence_count = 0

                    continue

                # ------------------------------------------------
                # Radio -> Zello transmission active
                # ------------------------------------------------

                await self.send_radio_audio(data)

                if rms < VOX_THRESHOLD:
                    silence_count += 1
                else:
                    silence_count = 0

                if silence_count >= RELEASE_CHUNKS:

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
                await asyncio.wait_for(
                    process.wait(),
                    timeout=2
                )
            except asyncio.TimeoutError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()

    # --------------------------------------------------------
    # Incoming Zello -> Radio
    # --------------------------------------------------------

    def create_rx_decoder(
        self,
        codec_header
    ):

        raw = base64.b64decode(
            codec_header
        )

        if len(raw) != 4:
            raise RuntimeError(
                "Invalid Zello codec header"
            )

        rate, frames_per_packet, frame_ms = (
            struct.unpack(
                "<HBB",
                raw
            )
        )

        print(
            "Incoming Opus:",
            rate,
            "Hz,",
            frames_per_packet,
            "frame(s),",
            frame_ms,
            "ms"
        )

        err = ctypes.c_int()

        decoder = opus.opus_decoder_create(
            rate,
            1,
            ctypes.byref(err)
        )

        if not decoder or err.value != 0:
            raise RuntimeError(
                f"Opus decoder creation failed: {err.value}"
            )

        return decoder, rate

    async def start_zello_rx(self, data):

        # Operator Transmit control: do not play incoming Zello audio into
        # the radio and, critically, never assert PTT while disabled.
        if not self.transmit_enabled:
            self.ignore_rx_stream = data["stream_id"]
            self.ptt.rts = False
            print(
                "Ignoring incoming Zello stream because radio transmit "
                "is disabled by operator."
            )
            return

        # Natural half duplex: do not key the radio for incoming Zello
        # while the physical radio or browser dispatch console is transmitting.
        if self.radio_tx_active or self.dispatch_tx_active:

            self.ignore_rx_stream = data[
                "stream_id"
            ]

            print(
                "Ignoring incoming Zello stream "
                "while radio is transmitting."
            )

            return

        self.zello_rx_active = True
        self.rx_stream_id = data["stream_id"]

        self.rx_decoder, self.rx_sample_rate = (
            self.create_rx_decoder(
                data["codec_header"]
            )
        )

        self.player = subprocess.Popen([
            "aplay",
            "-q",
            "-D", AUDIO_DEV,
            "-t", "raw",
            "-f", "S16_LE",
            "-r", str(self.rx_sample_rate),
            "-c", "1"
        ],
            stdin=subprocess.PIPE,
            bufsize=0
        )

        print(
            "*** ZELLO -> RADIO START FROM",
            data.get("from"),
            "***"
        )

        self.ptt.rts = True

        # Match existing bridge lead time
        await asyncio.sleep(PTT_LEAD)

    def decode_zello_packet(self, payload):

        if (
            not self.rx_decoder
            or not self.player
        ):
            return

        # Opus permits up to 120 ms in one packet.
        max_samples = int(
            self.rx_sample_rate * 0.120
        )

        pcm = (
            ctypes.c_int16 * max_samples
        )()

        encoded = (
            ctypes.c_ubyte * len(payload)
        ).from_buffer_copy(payload)

        samples = opus.opus_decode(
            self.rx_decoder,
            encoded,
            len(payload),
            pcm,
            max_samples,
            0
        )

        if samples < 0:
            print(
                "Opus decode error:",
                samples
            )
            return

        audio = ctypes.string_at(
            ctypes.addressof(pcm),
            samples * 2
        )

        self.player.stdin.write(audio)

    async def stop_zello_rx(self, force=False):

        if not self.zello_rx_active:
            return

        if self.player:

            try:
                self.player.stdin.close()
            except Exception:
                pass

            try:
                self.player.wait(
                    timeout=3
                )
            except Exception:
                self.player.terminate()

        # Match existing bridge tail during normal traffic. Disabling
        # transmit is a safety action, so release PTT without the tail.
        if not force:
            await asyncio.sleep(
                PTT_TAIL
            )

        self.ptt.rts = False

        if self.rx_decoder:
            opus.opus_decoder_destroy(
                self.rx_decoder
            )

        self.rx_decoder = None
        self.rx_stream_id = None
        self.rx_sample_rate = None
        self.player = None

        self.zello_rx_active = False

        # Existing bridge had a pause period.
        self.inhibit_until = (
            time.monotonic()
            + RX_INHIBIT_AFTER_ZELLO
        )

        print(
            "*** ZELLO -> RADIO STOP ***"
        )

    # --------------------------------------------------------
    # Websocket reader
    # --------------------------------------------------------

    async def reader_loop(self):

        async for message in self.ws:

            # ----------------------------------------------------
            # Binary voice packet
            # ----------------------------------------------------

            if isinstance(message, bytes):

                if len(message) < 9:
                    continue

                packet_type, stream_id, packet_id = (
                    struct.unpack(
                        ">BII",
                        message[:9]
                    )
                )

                if packet_type != 1:
                    continue

                if (
                    self.ignore_rx_stream
                    == stream_id
                ):
                    continue

                if (
                    self.zello_rx_active
                    and stream_id
                    == self.rx_stream_id
                ):
                    self.decode_zello_packet(
                        message[9:]
                    )

                continue

            # ----------------------------------------------------
            # JSON
            # ----------------------------------------------------

            data = json.loads(message)

            seq = data.get("seq")

            if seq in self.pending:

                future = self.pending[
                    seq
                ]

                if not future.done():
                    future.set_result(
                        data
                    )

            command = data.get(
                "command"
            )

            if command == "on_channel_status":

                if data.get("channel") != CHANNEL:
                    continue

                status = data.get(
                    "status"
                )

                print(
                    "Channel:",
                    CHANNEL,
                    status
                )

                if status == "online":
                    self.channel_online.set()
                else:
                    self.channel_online.clear()

            elif command == "on_stream_start":

                if (
                    data.get("channel")
                    != CHANNEL
                ):
                    continue

                if data.get("type") != "audio":
                    continue

                await self.start_zello_rx(
                    data
                )

            elif command == "on_stream_stop":

                stream_id = data.get(
                    "stream_id"
                )

                if (
                    stream_id
                    == self.ignore_rx_stream
                ):
                    self.ignore_rx_stream = None
                    continue

                if (
                    stream_id
                    == self.rx_stream_id
                ):
                    await self.stop_zello_rx()

            elif command == "on_error":

                print(
                    "Zello error:",
                    data
                )

    # --------------------------------------------------------
    # One websocket session
    # --------------------------------------------------------

    async def session(self):

        self.channel_online.clear()

        async with websockets.connect(
            URL,
            ping_interval=20,
            ping_timeout=20
        ) as ws:

            self.ws = ws

            reader = asyncio.create_task(
                self.reader_loop()
            )

            try:

                response = await self.command({
                    "command": "logon",
                    "username": USERNAME,
                    "password": PASSWORD,
                    "channels": [CHANNEL],
                    "version": "linux-zello-bridge-test-0.5",
                    "platform_type": "linux",
                    "platform_name": "Linux Roxi Gateway"
                })

                if not response.get(
                    "success"
                ):
                    raise RuntimeError(
                        f"Zello login failed: {response}"
                    )

                print(
                    "*** ZELLO LOGIN SUCCESSFUL ***"
                )

                await asyncio.wait_for(
                    self.channel_online.wait(),
                    timeout=20
                )

                print(
                    "*** ZELLO BRIDGE ONLINE ***"
                )

                capture = asyncio.create_task(
                    self.capture_loop()
                )
                controls = asyncio.create_task(
                    self.control_watcher()
                )
                dispatch_audio = asyncio.create_task(
                    self.dispatch_audio_loop()
                )

                done, pending = (
                    await asyncio.wait(
                        {reader, capture, controls, dispatch_audio},
                        return_when=asyncio.FIRST_COMPLETED
                    )
                )

                for task in pending:
                    task.cancel()

                if pending:
                    await asyncio.gather(
                        *pending,
                        return_exceptions=True
                    )

                for task in done:
                    if task.cancelled():
                        continue

                    exc = task.exception()
                    if exc:
                        raise exc

            finally:

                reader.cancel()

                if self.radio_tx_active:
                    try:
                        await self.stop_radio_stream()
                    except Exception:
                        pass

                if self.zello_rx_active:
                    try:
                        await self.stop_zello_rx()
                    except Exception:
                        pass

                if self.dispatch_tx_active or self.dispatch_player:
                    try:
                        await self.stop_dispatch_tx()
                    except Exception:
                        pass

                self.ptt.rts = False

    # --------------------------------------------------------
    # Main reconnect loop
    # --------------------------------------------------------

    async def run(self):

        try:

            while True:

                try:

                    await self.session()

                except asyncio.CancelledError:
                    raise

                except KeyboardInterrupt:
                    raise

                except Exception as e:

                    self.ptt.rts = False

                    print(
                        "*** CONNECTION ERROR:",
                        repr(e),
                        "***"
                    )

                    print(
                        f"Retrying in {RECONNECT_DELAY} seconds..."
                    )

                    await asyncio.sleep(
                        RECONNECT_DELAY
                    )

        finally:

            self.ptt.rts = False

            try:
                self.ptt.close()
            except Exception:
                pass
            for sock in (self.dispatch_rx_socket, self.dispatch_tx_socket):
                try:
                    sock.close()
                except Exception:
                    pass

            print(
                "Gateway stopped; PTT released."
            )


async def main():

    bridge = Bridge()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            stop_event.set
        )

    bridge_task = asyncio.create_task(
        bridge.run()
    )

    stop_task = asyncio.create_task(
        stop_event.wait()
    )

    done, pending = await asyncio.wait(
        {bridge_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED
    )

    if stop_task in done:
        print("Shutdown requested...")
        bridge_task.cancel()

    for task in pending:
        task.cancel()

    await asyncio.gather(
        bridge_task,
        stop_task,
        return_exceptions=True
    )


if __name__ == "__main__":
    asyncio.run(main())
