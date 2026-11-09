# MeshCom UDP Bluetooth Hub

[Deutsche Version → README.de.md](README.de.md)

A small gateway that connects to a [MeshCom](https://github.com/icssw-org/MeshCom-Firmware)
node over **Bluetooth LE** (like the phone app does) and presents itself to
[MeshcomWebDesk](https://github.com/DH1FR/MeshcomWebDesk) as a node with the **ext-UDP interface** and the
**NET console**.

## Why this exists

I own a **Heltec T114** (nRF52840). It has no WiFi and no Ethernet, so the firmware's ext-UDP
interface (UDP 1799) and NET console (TCP 2323) are not available on it. WebDesk talks only
these two network interfaces, so it could not reach the node. The T114 does have Bluetooth,
and the phone app already shows that the node can deliver everything over BLE. This hub bridges
the gap:

```
 MeshcomWebDesk ──UDP/JSON──►  Hub (PC / Raspberry Pi)  ──BLE──►  Node (e.g. Heltec T114)
   (ext-UDP, NET console)  ◄──                            ◄──
```

> Unofficial community project. Not affiliated with or endorsed by the MeshCom project or the
> WebDesk author. Built against MeshCom firmware **4.40a**.

## Features

- Messages in both directions, including the node's echo and delivery acknowledgements
  (the ticks in WebDesk)
- Positions (`pos`) and sensor telemetry (`tele`) of heard stations
- WebDesk **NET console** emulation (HMAC-SHA256 challenge/response, same as the firmware)
  - `--mheard` / `--mh` answered from the node's BLE MH frames, in the firmware's `[MH] key=value` format
  - other `--commands` are sent to the node over BLE (see limitations)
- Optional monitor window (`--gui`): pick the node from a list of nearby nodes, live view of
  messages, positions, acks and MHeard, command line, buttons for *GPS on/off* and *Send position*
- Remembers the node, PIN and WebDesk's address between runs
- Runs on **Windows** (WinRT) and **Linux / Raspberry Pi** (BlueZ) via [bleak](https://github.com/hbldh/bleak)

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (only `bleak`)
- A Bluetooth LE adapter. On Linux/Raspberry Pi BlueZ must be running; for the monitor window
  `sudo apt install python3-tk`
- A MeshCom node with BLE, firmware 4.40a (other 4.x versions are untested)

## Quick start

```
pip install -r requirements.txt
python hub.py --gui
```

Pick your node in the window (names look like `MC-xxxx-CALL`), enter the BLE PIN and press
**Connect**. **Disconnect the phone app first** – the node accepts only one BLE client.

Without the window:

```
python hub.py --name MC-a4c5 --pin 123456
```

(`--name` is a part of the BLE name, `--address` takes the BLE address; both skip the picker.)

### WebDesk settings

Run WebDesk on the same PC (or point it to the hub's IP):

| WebDesk setting | Value |
|---|---|
| Device IP | `127.0.0.1` (or the hub's IP) |
| Device Port | `1798` (hub UDP port) |
| Listen IP / Listen Port | `127.0.0.1` / `1799` |
| Callsign | **the node's callsign** (e.g. `DH1FR-4`) |
| KISS/TCP | off |
| NET Console | enabled, Device IP as above, port `2323` |

Notes:

- WebDesk binds UDP 1799 itself, so the hub uses **1798** by default when both run on one PC.
- The callsign in WebDesk **must match the node**. Otherwise WebDesk does not recognise the
  node's echo of your own messages and shows a warning triangle.
- The hub learns WebDesk's address from its registration packet and stores it. Until then it
  sends to `127.0.0.1:1799` (`--peer` changes that).

## Options

| Option | Meaning |
|---|---|
| `--gui` | monitor window with node selection |
| `--name TEXT` / `--address ADDR` | node by (part of) BLE name / by address |
| `--pin N` | BLE PIN of the node (app-layer hello) |
| `--udp-port N` | hub UDP port (default 1799; use 1798 next to WebDesk on the same PC) |
| `--peer IP:PORT` | where WebDesk listens until it registers |
| `--console-port N` | NET console TCP port (default 2323, `0` = off) |
| `--console-host IP` | bind address (default `127.0.0.1`) |
| `--console-password PW` | NET console password (HMAC); empty = open, local only |

Settings chosen in the window are saved in `hub_config.json` (see `hub_config.example.json`).
That file contains the PIN in plain text and is git-ignored.

## Limitations

- **Console output:** the firmware sends only some command replies over BLE. `--help`, `--info`
  and most text output go to the USB serial port only, so they do not appear in the NET console.
  `--mheard` is answered by the hub from the MH frames; it has **no positions** (`lat/lon/alt = NA`).
- RSSI/SNR of messages and positions come from the last MHeard entry of that station (0 if unknown);
  BLE frames do not carry them.
- Not implemented yet: the node's own position/telemetry as `src_type:"node"`, telemetry from WebDesk to
  the node, a UDP-port field in the window.
- One BLE client at a time (phone app **or** hub).
- Tested with a Heltec T114, firmware 4.40a, Windows 11. Not yet tested on a Raspberry Pi.

## Security

- The NET console listens on `127.0.0.1` only unless you set `--console-host`; a non-local bind
  is **refused without `--console-password`**, because the console can run any command on the node
  (including reboot).
- The UDP port listens on all interfaces; use a firewall if the PC is on an untrusted network.
- ext-UDP itself has no authentication in the firmware either.

## How it works (short)

- BLE: Nordic UART service. The hub sends the app-layer hello (`0x10`, SHA-256 of the 6-digit PIN)
  and the time (`0x20`); the node answers with its configuration, the MH list and later live frames.
- Node → hub: `0x40` frames carry APRS-style messages (`0x3A`), positions (`0x21`) and acks (`0x41`);
  `0x44` frames carry JSON (config, MH).
- Hub → WebDesk: the same JSON the firmware sends on its ext-UDP port (`msg`, `pos`, `tele`, `ack`).
- WebDesk → node: `msg` becomes a BLE text frame (`0xA0`); text starting with `--` runs as a console command.

## Credits

[MeshCom firmware](https://github.com/icssw-org/MeshCom-Firmware) (protocol reference) and
[MeshcomWebDesk](https://github.com/DH1FR/MeshcomWebDesk) (the client this hub was built for).
