#!/usr/bin/env python3
"""MeshCom BLE <-> ext-UDP hub.

Connects to a MeshCom 4.x node (e.g. Heltec T114, nRF52) over BLE like the
phone app does and emulates the node's ext-UDP interface (port 1799, JSON)
so that MeshcomWebDesk can talk to it. Also emulates the NET console (TCP 2323).

Runs on Windows (WinRT) and Raspberry Pi / Linux (BlueZ) via `bleak`.

Protocol facts (from MeshCom-Firmware src/phone_commands.cpp, nrf52_ble.cpp):
  phone -> node frames: [len][type][payload...]   (len = whole frame length)
    0x10 hello : [36][0x10][0x20][0x30][sha256("%06d" % pin)]  (32 bytes)
    0x20 time  : [6][0x20][uint32 LE unix UTC]
    0xA0 text  : [2+n][0xA0][text]   ("--cmd" = command, else sent as ":"+text)
  node -> phone notifications: one frame per notification
    0x40 + APRS payload (0x3A msg / 0x21 pos ...), 0x44 + JSON, 0x91 MH list
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import struct
import queue
import re
import sys
import threading
import time

from bleak import BleakClient, BleakScanner

# Nordic UART Service (Bluefruit BLEUart)
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # phone -> node (write)
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # node -> phone (notify)


ui_queue = None  # set when --gui is used


def emit(kind: str, text: str):
    """Log to console and (optionally) to the GUI window."""
    print(f"[{kind}] {text}", flush=True)
    if ui_queue is not None:
        ui_queue.put((kind, time.strftime("%H:%M:%S"), text))


def hello_frame(pin: int) -> bytes:
    h = hashlib.sha256(("%06d" % pin).encode()).digest()
    body = bytes([0x10, 0x20, 0x30]) + h
    return bytes([len(body) + 1]) + body


def time_frame() -> bytes:
    return bytes([6, 0x20]) + struct.pack("<I", int(time.time()))


def text_frame(text: str) -> bytes:
    data = text.encode("utf-8")
    return bytes([len(data) + 2, 0xA0]) + data


def log_frame(data: bytes):
    kind = {0x40: "APRS", 0x44: "JSON", 0x91: "MH"}.get(data[0], "?") if data else "-"
    printable = data[1:].decode("utf-8", "replace") if data and data[0] == 0x44 else ""
    emit("RAW", f"{kind:4} len={len(data):3} {data.hex(' ')}  {printable}")


HELP_LINES = [
    # WebDesk's help popup shows only lines that start with "--", are empty or contain
    # "commands" - keep every line in that shape.
    "-- MeshCom BLE hub: the node does not send its --help over Bluetooth (USB serial only).",
    "-- Available commands handled by the hub itself:",
    "--help           this text",
    "--mheard, --mh   heard list from the node's BLE MH frames (no lat/lon/alt)",
    "-- Every other --command is sent to the node over BLE, e.g.:",
    "--gps on|off     GPS on / off",
    "--sendpos        send position now",
    "--btcode NNNNNN  set BLE PIN (0 = off)",
    "-- Debug commands (sent to the node; its debug output goes to USB serial, only the echo comes back here):",
    "--loradebug on|off  LoRa RX frame debug",
    "--debug on|off      general debug output",
    "--bledebug on|off   BLE debug",
    "--all / --msg       display mode: all frames / messages only",
    "-- The node answers only commands it replies to over BLE; others run silently,",
    "-- output like --info is visible on USB only.",
    "-- Full list of commands: https://github.com/icssw-org/MeshCom-Firmware",
]


class UdpSide(asyncio.DatagramProtocol):
    """ext-UDP emulation. Stage 1: remember WebDesk peer, forward msg to BLE."""

    def __init__(self, hub):
        self.hub = hub
        self.transport = None
        self.peer = hub.default_peer  # WebDesk address; learned from its packets
        self.last_rx = None           # time.monotonic() of last datagram from WebDesk
        self.rx_count = 0

    def connection_made(self, transport):
        self.transport = transport

    def error_received(self, exc):
        pass  # e.g. WebDesk not running: ignore

    def datagram_received(self, data, addr):
        try:
            j = json.loads(data)
        except ValueError:
            emit("UDP", f"invalid JSON from {addr}")
            return
        self.last_rx = time.monotonic()
        self.rx_count += 1
        if self.peer != addr:
            self.peer = addr
            self.hub.remember_peer(addr)
        t = j.get("type")
        emit("UDP", f"<- {addr[0]}:{addr[1]} {j}")
        if t == "info":
            return  # registration only
        if t == "msg":
            dst, msg = j.get("dst", "*"), j.get("msg", "")
            if msg.startswith("--"):  # console command: sent as is, no {dst}
                asyncio.ensure_future(self.hub.command(msg))
            else:
                asyncio.ensure_future(self.hub.send(text_frame("{%s}%s" % (dst, msg))))
        # "tele" etc.: not implemented yet

    def send_json(self, obj):
        if self.peer and self.transport:
            self.transport.sendto(json.dumps(obj).encode(), self.peer)


class Hub:
    def __init__(self, args):
        self.args = args
        self.target = getattr(args, 'address', None)  # BLE address to connect to
        self.pin = getattr(args, 'pin', 0)
        self.loop = None
        self.cfg = {}
        self.console_writer = None
        self.mh_full = {}     # call -> (monotonic time received, MH json from the node)
        p = getattr(args, 'peer', None)
        self.default_peer = (p.rsplit(':', 1)[0], int(p.rsplit(':', 1)[1])) if p else None
        self.client = None
        self.udp = None
        self.mh = {}          # call -> (rssi, snr) from MH frames
        self.own_call = ""
        self.fw_str = ""
        self.batt = 0

    async def send(self, frame: bytes):
        if self.client and self.client.is_connected:
            emit("RAW", f"TX {frame.hex(' ')}")
            await self.client.write_gatt_char(NUS_RX, frame, response=False)
        else:
            emit("SYS", "not connected - message not sent to node")

    def on_notify(self, _char, data: bytearray):
        data = bytes(data)
        log_frame(data)
        try:
            out = self.decode(data)
        except Exception as e:  # never let a bad frame kill the link
            emit("SYS", f"decode error {e!r}")
            return
        for o in (out if isinstance(out, list) else [out] if out else []):
            emit("UDP", f"-> {o}" if self.udp.peer else f"(no WebDesk peer yet) {o}")
            self.udp.send_json(o)

    def decode(self, d: bytes):
        """BLE notification -> ext-UDP JSON dict (or None)."""
        if not d:
            return None
        if d[0] == 0x44:  # JSON; strip firmware's NUL padding
            j = json.loads(d[1:].rstrip(b"\x00"))
            typ = j.get("TYP")
            if typ == "MH":  # remember RSSI/SNR per station
                self.mh[j.get("CALL", "")] = (j.get("RSSI", 0), j.get("SNR", 0))
                self.mh_full[j.get("CALL", "")] = (time.monotonic(), j)
                emit("MH", f"{j.get('CALL')} rssi={j.get('RSSI')} snr={j.get('SNR')} "
                           f"age={j.get('AGE')}min role={j.get('ROLE')} nb={j.get('NB')}")
            elif typ == "I":
                self.own_call = j.get("CALL", self.own_call)
                self.fw_str = str(j.get("FWVER", "")).split(" ")[0]
                self.batt = j.get("BATP", 0)
                emit("SYS", f"Node {j.get('CALL')} FW {j.get('FWVER')} batt {j.get('BATP')}%")
            elif typ == "CONFFIN":
                emit("SYS", "config burst complete")
            return None
        if d[0] != 0x40 or len(d) < 2:
            return None  # 0x91 MH list etc.: not needed for ext-UDP
        f = d[1:]  # strip BLE text tag 0x40
        if f[0] == 0x41 and len(f) >= 7:  # ack: 41 id(4 LE) status n call
            msg_id = struct.unpack_from("<I", f, 1)[0]
            n = f[6]
            emit("ACK", f"msg_id={msg_id:08X} status={f[5]} from={f[7:7 + n].decode('ascii', 'replace')}")
            return {"type": "ack", "msg_id": "%08X" % msg_id, "status": f[5],
                    "from": f[7:7 + n].decode("ascii", "replace"), "via": "udp"}
        if f[0] in (0x3A, 0x21) and len(f) > 16:
            return self.decode_msg(f)
        # 0x40/0x21 (position) and 0x91: TODO once real frames are captured
        return None

    def decode_msg(self, f: bytes):
        """LoRa APRS msg frame: 3A id(4 LE) flags SRC>DST:payload 00 hw mod fcs(2) fw hw2 sub"""
        msg_id = struct.unpack_from("<I", f, 1)[0]
        max_hop = f[5] & 0x0F
        gt = f.index(b">", 6)
        src = f[6:gt].decode("ascii")
        colon = f.index(bytes([f[0]]), gt)  # 0x3A ':' (msg) or 0x21 '!' (position)
        dst = f[gt + 1:colon].decode("ascii")
        end = f.index(b"\x00", colon + 1)
        msg = f[colon + 1:end].decode("utf-8", "replace")
        hw, mod = f[end + 1], f[end + 2]
        fw = f[end + 5] if len(f) > end + 5 else 0
        sub = chr(f[end + 7]) if len(f) > end + 7 and f[end + 7] not in (0, 0x7E) else "#"
        first = src.split(",")[0]
        rssi, snr = self.mh.get(first, (0, 0))
        if f[0] == 0x21:
            return self.build_pos(src, msg, msg_id, max_hop, fw, sub, hw, mod, rssi, snr)
        if src == "response":  # console output of a "--command" sent over BLE
            for line in msg.splitlines():
                if line.strip():
                    emit("CMD", line)
                    self.console_write(line)
            return None
        emit("MSG", f"{src} > {dst}: {msg}   (id {msg_id:08X}, hop {max_hop}, rssi {rssi})")
        out = {"src_type": "lora", "type": "msg", "src": src, "dst": dst, "msg": msg,
               "msg_id": "%08X" % msg_id, "firmware": fw, "fw_sub": sub,
               "rssi": rssi, "snr": snr, "hw_id": hw, "lora_mod": mod & 0x0F,
               "max_hop": max_hop}
        if src == self.own_call:
            # Echo of a message sent by this node: the firmware reports it as
            # src_type "node" with its firmware string and battery (WebDesk relies on it).
            out["src_type"] = "node"
            out["firmware"] = self.fw_str or fw
            out["batt"] = self.batt
        return out

    def build_pos(self, src, payload, msg_id, max_hop, fw, sub, hw, mod, rssi, snr):
        """Position payload: ddmm.mmN/dddmm.mmE#text#name/B=..(tags). -> pos (+ tele) JSON."""
        m = re.match(r"\s*([\d.]+)([NS])(.)([\d.]+)([EW])(.)", payload)
        if not m:
            emit("SYS", f"unparsable position: {payload!r}")
            return None

        def deg(v):  # ddmm.mm -> decimal degrees, truncated to 4 digits like the firmware
            v = float(v)
            return int((int(v / 100) + (v % 100) / 60) * 10000) / 10000.0

        tags = {k: v for k, v in re.findall(r"/([A-Z])=([^/ ]*)", payload)}

        def num(k, cast=float, default=0):
            try:
                return cast(tags[k])
            except (KeyError, ValueError):
                return default

        sym = m.group(6)
        grp = m.group(3)
        own = src == self.own_call
        pos = {"src_type": "node" if own else "lora", "type": "pos", "src": src, "msg": "",
               "lat": deg(m.group(1)), "lat_dir": m.group(2),
               "long": deg(m.group(4)), "long_dir": m.group(5),
               "aprs_symbol": sym * 2 if sym == "\\" else sym,
               "aprs_symbol_group": grp * 2 if grp == "\\" else grp,
               "hw_id": hw, "msg_id": "%08X" % msg_id, "alt": num("A", int),
               "lora_mod": mod & 0x0F, "max_hop": max_hop,
               "batt": self.batt if own else num("B", int),
               "firmware": (self.fw_str or fw) if own else fw, "fw_sub": sub,
               "rssi": rssi, "snr": snr}
        emit("POS", f"{src}: {pos['lat']}{pos['lat_dir']} {pos['long']}{pos['long_dir']} "
                    f"alt {pos['alt']} batt {pos['batt']}  [{payload.split('/')[0][-40:]}]")
        out = [pos]
        if own:
            return out  # own telemetry would come from the node's sensors (none on a T114)
        if any(k in tags for k in "THPOQFGC"):
            out.append({"src_type": "lora", "type": "tele", "src": src, "batt": pos["batt"],
                        "temp1": num("T"), "temp2": num("O"), "hum": num("H"),
                        "qfe": num("P"), "qnh": num("Q"), "pressure_alt": num("F", int),
                        "gas": num("G"), "co2": num("C")})
        return out

    # ---- connection control (used by CLI and GUI) -------------------------

    HW_NAMES = ["no info", "TLORA_V2", "TLORA_V1", "TLORA_V2_1_1p6", "TBEAM", "TBEAM_1268",
                "TBEAM_0p7", "T_ECHO", "TDECK", "RAK4631", "HELTEC_V2_1", "HELTEC_V1",
                "TBEAM_AXP2101", "EBYTE_E22", "HELTEC_V3", "HELTEC_E290", "TBEAM_1262",
                "TDECK_PLUS", "TBEAM_SUPREME", "ESP_S3_E22", "TRACK_V3", "STICK_V3", "T5_EPAPER",
                "TPAGER", "TDECKpro", "TBEAM_1W", "HELTEC_V4", "T_ETH_ELITE", "HELTEC_T114",
                "T3_S3_V13", "T_CON_PRO", "WIRELESS_PAPER", "HELTEC_E213", "ESP32_LORAPRS_E22",
                "ESP32_LORAPRS_RA01", "T_WATCH_S3"]
    HW_LEGACY = [13, 22, 20, 21, 14, 15, 16, 17, 18, 19, 23, 24,
                 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35]  # firmware ids 39..61

    def mheard_lines(self):
        """Firmware 4.40a '--mheard' output, built from the MH frames the node sent."""
        now = time.monotonic()
        rows = []
        for call, (t, j) in self.mh_full.items():
            age = int(j.get("AGE", 0) + (now - t) / 60)
            if age <= 720:
                rows.append((age, call, j))
        rows.sort(key=lambda r: r[0])
        out = [f"[MH] window=720min rows={len(rows)}"]
        for age, call, j in rows:
            hw = j.get("HW", 0)
            if 39 <= hw <= 61:
                hw = self.HW_LEGACY[hw - 39]
            hw_name = self.HW_NAMES[hw] if 0 <= hw < len(self.HW_NAMES) else self.HW_NAMES[0]
            typ = {58: "TXT", 33: "POS", 64: "HEY"}.get(j.get("PLT"), "???")
            mod = j.get("MOD", 0)

            def opt(k, unit=""):
                v = j.get(k)
                return "NA" if v is None else f"{v}{unit}"

            dist = j.get("DIST", -1)
            dist_s = "NA" if dist is None or dist < 0 else f"{dist:.1f}"
            date = str(j.get("DATE", "")).replace("-", ".")
            out.append(
                f"[MH] call={call} {date} {j.get('TIME', '')} typ={typ} hw={hw_name} "
                f"mod={mod >> 4:X}/{mod & 15:X} rssi={opt('RSSI')}dBm snr={opt('SNR')}dB "
                f"dist={dist_s}km ncnt={j.get('NCNT', 0)} age={age}min hm={opt('HM')}dB "
                f"role={j.get('ROLE') or '-'} ex={j.get('EX', 0)} nb={j.get('NB', 0)} "
                f"gw={'Y' if j.get('GW') else 'N'} lat=NA lon=NA alt=NAm")
        return out

    async def command(self, text: str):
        """Run a console command on the node (BLE text starting with '--')."""
        if text.strip().lower() in ("--mheard", "--mh"):
            # The node prints this list only on USB; the phone-app MH frames carry the
            # same data, so answer from them (lat/lon/alt are not in the BLE frames).
            emit("CMD", f"> {text}")
            for line in self.mheard_lines():
                emit("CMD", line)
                self.console_write(line)
            return
        if text.strip().lower() in ("--help", "--h", "--?"):
            # The firmware prints its help on USB only, so answer here with a hint.
            emit("CMD", f"> {text}")
            for line in HELP_LINES:
                emit("CMD", line)
                self.console_write(line)
            return
        if not (self.client and self.client.is_connected):
            emit("SYS", "not connected - command not sent")
            return
        emit("CMD", f"> {text}")
        await self.send(text_frame(text))

    # ---- NET console emulation (WebDesk "NET Console", firmware net_console.cpp) ----
    # TCP, single client:  <- "NONCE: <32 hex>\r\n"  -> "<64 hex HMAC-SHA256(pw, nonce)>\r\n"
    #                      <- "OK\r\n<banner>" or "FAIL\r\n"; then plain text lines.
    # Each received line is run as a "--command" on the node over BLE; the node's
    # replies (BLE frames from "response") are written back to the client.

    async def start_console(self):
        host, port, pw = self.args.console_host, self.args.console_port, self.args.console_password
        if port == 0:
            return
        if host not in ("127.0.0.1", "localhost", "::1") and not pw:
            emit("SYS", "NET console NOT started: non-local --console-host needs --console-password")
            return
        try:
            await asyncio.start_server(self.console_client, host, port)
            emit("SYS", f"NET console on {host}:{port} ({'password' if pw else 'open, local only'})")
        except OSError as e:
            emit("SYS", f"NET console not started: {e}")

    def console_write(self, text: str):
        w = self.console_writer
        if w is not None and not w.is_closing():
            w.write((text + "\r\n").encode("utf-8", "replace"))

    async def console_client(self, reader, writer):
        pw = self.args.console_password
        peer = writer.get_extra_info("peername")
        try:
            if pw:
                nonce = os.urandom(16)
                writer.write(b"NONCE: " + nonce.hex().encode() + b"\r\n")
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), 10)
                expected = hmac.new(pw.encode(), nonce, hashlib.sha256).hexdigest()
                if not hmac.compare_digest(line.strip().decode("ascii", "replace").lower(), expected):
                    writer.write(b"FAIL\r\n")
                    await writer.drain()
                    emit("SYS", f"NET console: authentication failed from {peer}")
                    return
            if self.console_writer is not None:  # a new client takes over, like the firmware
                self.console_writer.close()
            self.console_writer = writer
            emit("SYS", f"NET console client connected {peer}")
            writer.write(b"OK\r\nMeshCom Console (BLE hub)\r\nType --help for commands\r\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                if not text.startswith("--"):
                    text = "--" + text
                if text.lower() not in ("--mheard", "--mh") and not (
                        self.client and self.client.is_connected):
                    self.console_write("[hub] node not connected")
                    continue
                await self.command(text)
        except (asyncio.TimeoutError, ConnectionError):
            pass
        finally:
            if self.console_writer is writer:
                self.console_writer = None
                emit("SYS", "NET console client disconnected")
            writer.close()

    def remember_peer(self, addr):
        emit("SYS", f"WebDesk peer: {addr[0]}:{addr[1]}")
        self.cfg["peer"] = f"{addr[0]}:{addr[1]}"
        save_config(self.cfg)

    def set_state(self, text: str):
        if ui_queue is not None:
            ui_queue.put(("STATE", "", text))

    def set_target(self, address):
        """Connect to this BLE address (None = disconnect). Safe from any thread."""
        self.target = address

    async def scan(self, timeout: float = 8.0):
        """Return [(name, address, rssi)] of nearby MeshCom nodes (names 'MC-...')."""
        emit("SYS", "Scanning ...")
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        devs = []
        for addr, (d, ad) in found.items():
            name = d.name or ad.local_name or ""
            if name.startswith("MC-"):
                devs.append((name, addr, ad.rssi))
        devs.sort(key=lambda x: -x[2])
        emit("SYS", "Scan done: " + (", ".join(f"{n} ({r} dBm)" for n, _, r in devs) or "no MeshCom node found"))
        return devs

    async def find_by_name(self, name):
        emit("SYS", f"Scanning for BLE device with name containing '{name}' ...")
        dev = await BleakScanner.find_device_by_filter(
            lambda d, ad: name.lower() in ((d.name or ad.local_name or "").lower()),
            timeout=20)
        if not dev:
            emit("SYS", "Device not found (is the phone app disconnected?)")
            return None
        emit("SYS", f"Found {dev.name} {dev.address}")
        return dev.address

    async def run(self):
        self.loop = asyncio.get_running_loop()
        _, self.udp = await self.loop.create_datagram_endpoint(
            lambda: UdpSide(self), local_addr=("0.0.0.0", self.args.udp_port))
        emit("SYS", f"UDP listening on :{self.args.udp_port}")
        if sys.platform == "win32":
            # Windows reports ICMP "port unreachable" (WebDesk not running) as WinError 10054 on the
            # receive side and the proactor loop then stops receiving; switch that off.
            try:
                import socket
                sock = self.udp.transport.get_extra_info("socket")
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except Exception as e:
                emit("SYS", f"could not disable UDP connreset: {e!r}")
        await self.start_console()
        if self.target is None and self.args.name and not self.args.gui:
            self.target = await self.find_by_name(self.args.name)
            if self.target is None:
                raise SystemExit(1)
        while True:
            addr = self.target
            if not addr:
                self.set_state("disconnected")
                await asyncio.sleep(0.3)
                continue
            self.set_state("connecting")
            try:
                async with BleakClient(addr) as client:
                    emit("SYS", "BLE connected")
                    self.set_state("connected")
                    self.client = client
                    await client.start_notify(NUS_TX, self.on_notify)
                    await self.send(hello_frame(self.pin))
                    await asyncio.sleep(0.3)
                    await self.send(time_frame())
                    while client.is_connected and self.target == addr:
                        await asyncio.sleep(0.5)
            except Exception as e:  # reconnect loop
                emit("SYS", f"BLE {e!r}")
            self.client = None
            if self.target == addr:  # lost, not user-initiated: retry
                self.set_state("retry in 5 s")
                for _ in range(10):
                    if self.target != addr:
                        break
                    await asyncio.sleep(0.5)
            else:
                emit("SYS", "BLE disconnected")


COLORS = {"CMD": "#00838f", "POS": "#c2185b", "MSG": "#0a7d28", "ACK": "#0b5cad", "MH": "#666666", "UDP": "#8a4fb3",
          "SYS": "#b36b00", "RAW": "#999999"}


def run_gui(hub: "Hub", cfg: dict):
    """Optional monitor window (tkinter, stdlib): choose the node, see what it
    receives, independent of WebDesk."""
    import tkinter as tk
    from tkinter import ttk, scrolledtext

    root = tk.Tk()
    root.title("MeshCom BLE <-> UDP Hub")
    root.geometry("960x560")

    # --- connection bar
    top = ttk.Frame(root, padding=4)
    top.pack(fill="x")
    ttk.Label(top, text="Node:").pack(side="left")
    devs = {}  # label -> address
    if cfg.get("address"):
        devs[cfg.get("label") or cfg["address"]] = cfg["address"]
    combo = ttk.Combobox(top, width=34, state="readonly", values=list(devs))
    combo.pack(side="left", padx=4)
    if devs:
        combo.current(0)
    scan_btn = ttk.Button(top, text="Scan")
    scan_btn.pack(side="left")
    ttk.Label(top, text="PIN:").pack(side="left", padx=(10, 2))
    pin_var = tk.StringVar(value=str(hub.pin))
    ttk.Entry(top, textvariable=pin_var, width=8, show="*").pack(side="left")
    conn_btn = ttk.Button(top, text="Connect")
    conn_btn.pack(side="left", padx=8)
    state_lbl = ttk.Label(top, text="disconnected", foreground="#b00020")
    state_lbl.pack(side="left", padx=4)
    wd_lbl = ttk.Label(top, text="WebDesk: no contact", foreground="#b00020")
    wd_lbl.pack(side="left", padx=12)

    # --- filters
    bar = ttk.Frame(root, padding=(4, 0))
    bar.pack(fill="x")
    show = {}
    for k, default in (("CMD", True), ("MSG", True), ("POS", True), ("ACK", True), ("MH", True), ("SYS", True),
                       ("UDP", False), ("RAW", False)):
        v = tk.BooleanVar(value=default)
        show[k] = v
        tk.Checkbutton(bar, text=k, variable=v, fg=COLORS[k]).pack(side="left")
    cmdbar = ttk.Frame(root, padding=4)
    cmdbar.pack(side="bottom", fill="x")
    box = scrolledtext.ScrolledText(root, font=("Consolas", 10), wrap="word")
    box.pack(fill="both", expand=True)
    for k, c in COLORS.items():
        box.tag_config(k, foreground=c)
    history = []
    state = {"connected": False}

    def render(item):
        kind, ts, text = item
        if show[kind].get():
            box.insert("end", f"{ts} {kind:3} {text}\n", kind)

    def refilter(*_):
        box.delete("1.0", "end")
        for it in history:
            render(it)
        box.see("end")

    for v in show.values():
        v.trace_add("write", refilter)

    def save_cfg():
        label = combo.get()
        cfg.update(address=devs.get(label), label=label, pin=hub.pin)
        save_config(cfg)

    def do_scan():
        if hub.loop is None:
            return
        scan_btn.state(["disabled"])

        def work():
            try:
                res = asyncio.run_coroutine_threadsafe(hub.scan(), hub.loop).result()
            except Exception as e:
                emit("SYS", f"scan failed: {e!r}")
                res = []
            ui_queue.put(("SCAN", "", res))

        threading.Thread(target=work, daemon=True).start()

    def do_connect():
        if state["connected"] or hub.target:
            hub.set_target(None)
            conn_btn.config(text="Connect")
            return
        label = combo.get()
        if label not in devs:
            emit("SYS", "Select a node first (Scan)")
            return
        try:
            hub.pin = int(pin_var.get() or 0)
        except ValueError:
            emit("SYS", "PIN must be a number")
            return
        save_cfg()
        hub.set_target(devs[label])
        conn_btn.config(text="Disconnect")

    def run_cmd(text):
        text = text.strip()
        if not text:
            return
        if not text.startswith("--"):
            text = "--" + text
        if hub.loop is None or not state["connected"]:
            emit("SYS", "not connected - command not sent")
            return
        asyncio.run_coroutine_threadsafe(hub.command(text), hub.loop)

    ttk.Label(cmdbar, text="Command:").pack(side="left")
    cmd_var = tk.StringVar()
    cmd_entry = ttk.Entry(cmdbar, textvariable=cmd_var)
    cmd_entry.pack(side="left", fill="x", expand=True, padx=4)

    def send_entry(_e=None):
        run_cmd(cmd_var.get())
        cmd_var.set("")

    cmd_entry.bind("<Return>", send_entry)
    ttk.Button(cmdbar, text="Send", command=send_entry).pack(side="left")
    for label, c in (("GPS on", "--gps on"), ("GPS off", "--gps off"),
                     ("Send position", "--sendpos"), ("Info", "--info")):
        ttk.Button(cmdbar, text=label, command=lambda c=c: run_cmd(c)).pack(side="left", padx=2)

    scan_btn.config(command=do_scan)
    conn_btn.config(command=do_connect)

    def on_scan(res):
        keep = combo.get()
        for name, addr, rssi in res:
            devs[f"{name}  ({rssi} dBm)"] = addr
            # drop stale label for same address
            for k in [k for k, a in devs.items() if a == addr and not k.startswith(name)]:
                del devs[k]
        combo["values"] = list(devs)
        if not combo.get() and devs:
            combo.current(0)
        elif keep in devs:
            combo.set(keep)
        else:
            for k, a in devs.items():  # same address, new label (rssi changed)
                if a == cfg.get("address"):
                    combo.set(k)
        scan_btn.state(["!disabled"])

    def poll():
        try:
            while True:
                it = ui_queue.get_nowait()
                kind = it[0]
                if kind == "SCAN":
                    on_scan(it[2])
                elif kind == "STATE":
                    state["connected"] = it[2] == "connected"
                    state_lbl.config(text=it[2],
                                     foreground="#0a7d28" if state["connected"] else "#b00020")
                    if not hub.target:
                        conn_btn.config(text="Connect")
                else:
                    history.append(it)
                    del history[:-5000]
                    render(it)
                    box.see("end")
        except queue.Empty:
            pass
        u = hub.udp
        if u is not None and u.last_rx is not None:
            age = int(time.monotonic() - u.last_rx)
            con = " + NET console" if hub.console_writer else ""
            wd_lbl.config(text=f"WebDesk: UDP {age}s ago ({u.rx_count} pkts){con}",
                          foreground="#0a7d28" if age < 120 else "#b36b00")
        elif hub.console_writer:
            wd_lbl.config(text="WebDesk: NET console only", foreground="#b36b00")
        root.after(100, poll)

    threading.Thread(target=lambda: asyncio.run(hub.run()), daemon=True).start()
    poll()
    do_scan_later = lambda: do_scan()
    root.after(800, do_scan_later)          # list nodes nearby right away
    if cfg.get("address") and cfg.get("autoconnect", True):
        root.after(900, do_connect)         # reconnect to last node
    root.mainloop()


CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub_config.json")


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        emit("SYS", f"cannot save config: {e}")


def main():
    cfg = load_config()
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--name", help="BLE name substring (CLI mode), e.g. MC-a4c5 or DH1FR-4")
    p.add_argument("--address", help="BLE MAC (Linux) / address (Windows), skips scan")
    p.add_argument("--pin", type=int, default=cfg.get("pin", 0), help="node BLE PIN (bt_code)")
    p.add_argument("--udp-port", type=int, default=cfg.get("udp_port", 1799),
                   help="hub UDP port; use 1798 if WebDesk runs on the same PC (it binds 1799 itself)")
    p.add_argument("--peer", default=cfg.get("peer", "127.0.0.1:1799"),
                   help="where WebDesk listens (used until it registers itself); default 127.0.0.1:1799")
    p.add_argument("--console-port", type=int, default=cfg.get("console_port", 2323),
                   help="NET console (WebDesk) TCP port, 0 = off; default 2323")
    p.add_argument("--console-host", default=cfg.get("console_host", "127.0.0.1"),
                   help="bind address of the NET console; non-local needs a password")
    p.add_argument("--console-password", default=cfg.get("console_password", ""),
                   help="NET console password (HMAC auth); empty = open, local only")
    p.add_argument("--gui", action="store_true", help="monitor window with node selection")
    args = p.parse_args()
    hub = Hub(args)
    hub.cfg = cfg
    if args.gui:
        global ui_queue
        ui_queue = queue.Queue()
        cfg["udp_port"] = args.udp_port
        save_config(cfg)
        run_gui(hub, cfg)
    else:
        if not (args.name or args.address):
            p.error("give --name or --address (or use --gui to pick a node)")
        asyncio.run(hub.run())


if __name__ == "__main__":
    main()
