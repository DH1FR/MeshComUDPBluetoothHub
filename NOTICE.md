# Notices / Hinweise

This project is an independent reimplementation. It uses protocols and a few small data tables
from the projects below. / Dieses Projekt ist eine unabhängige Neuimplementierung. Es nutzt Protokolle
und einige kleine Datentabellen der folgenden Projekte.

## 1. MeshCom Firmware (MIT)

<https://github.com/icssw-org/MeshCom-Firmware> · © 2024 icssw.org · MIT License

**Used as protocol reference (behaviour re-implemented, no source files copied) /
Als Protokollvorlage genutzt (Verhalten nachgebaut, keine Quelldateien kopiert):**

- BLE phone interface: app-layer hello with PIN hash, time, text and command frames, frame
  framing and trailer bytes (`src/phone_commands.cpp`, `src/ble_phone_frame.h`,
  `src/ble_json_frame.h`, `src/nrf52/nrf52_ble.cpp`)
- Over-the-air frame layout for messages, positions and acknowledgements, and the position
  tag format (`src/aprs_functions.cpp`, `src/ack_attribution.h`)
- ext-UDP JSON datagrams `msg`, `pos`, `tele`, `ack` (`src/extudp_functions.cpp`,
  `src/extern_msg_json.h`, `src/extern_tele_json.h`, `src/udp_frame.h`,
  `docs/ext_udp_telemetry.md`)
- `--mheard` output format (`src/command_functions.cpp`)
- NET console challenge/response protocol (`src/net_console.cpp`)

**Taken over verbatim (small data tables) / Wörtlich übernommen (kleine Tabellen):** the hardware
names and the legacy hardware-ID mapping, and the payload type names (`src/nbr_views.cpp`).

License text of the MeshCom Firmware, as required by its terms:

```
MIT License

Copyright (c) 2024 icssw.org

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 2. MeshcomWebDesk

<https://github.com/DH1FR/MeshcomWebDesk> · © Ralf Altenbrand (DH1FR) · own license

The client this hub is built for. Its documented behaviour was followed (registration packet,
recognition of the node's echo and acknowledgements, `[MH] key=value` parsing). No WebDesk code
is included. / Der Client, für den dieser Hub gebaut wurde. Sein dokumentiertes Verhalten wurde
nachgebildet; es ist kein WebDesk-Code enthalten.

## 3. Dependencies (not bundled) / Abhängigkeiten (nicht mitgeliefert)

- [bleak](https://github.com/hbldh/bleak) – Bluetooth LE client, MIT License
- Python standard library (incl. `tkinter` for the optional window) – PSF License

## 4. Protocols and specifications / Protokolle und Spezifikationen

- Nordic UART Service (BLE UART service UUIDs) – Nordic Semiconductor
- APRS – the MeshCom frame format is APRS-like but binary and defined by the firmware above
- SHA-256 and HMAC-SHA256 (RFC 2104) for the PIN hash and the NET console login

"MeshCom", "WebDesk", "Heltec" and other names belong to their respective owners. This project is
unofficial. / Die Namen gehören ihren jeweiligen Inhabern. Dieses Projekt ist inoffiziell.
