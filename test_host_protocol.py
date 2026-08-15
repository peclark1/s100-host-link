#!/usr/bin/env python3
"""Protocol-only tests for S-100 Host Link v4 (no GTK dependency)."""
from __future__ import annotations
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = (HERE / 's100_hostlink_gtk4.py').read_text(encoding='utf-8')
# Execute only the protocol portion, removing GTK imports so this runs on a
# headless build host as well as on the Ubuntu desktop.
prefix = src.split('class MainWindow(', 1)[0]
prefix = prefix.replace('import gi\n\ngi.require_version("Gtk", "4.0")\ngi.require_version("Adw", "1")\nfrom gi.repository import Adw, Gio, GLib, Gtk\n\n', '')
# Remove pySerial import block; protocol classes accept an already-open serial object.
start = prefix.find('try:\n    import serial')
end = prefix.find('\n\nAPP_ID', start)
if start >= 0 and end >= 0:
    prefix = prefix[:start] + prefix[end+2:]
ns = {}
exec(compile(prefix, str(HERE/'s100_hostlink_gtk4.py'), 'exec'), ns)

HostLinkV2 = ns['HostLinkV2']
TransferStats = ns['TransferStats']
crc16_xmodem = ns['crc16_xmodem']
SOH, EOT, ACK, CRC_REQUEST = ns['SOH'], ns['EOT'], ns['ACK'], ns['CRC_REQUEST']

class FakeSerial:
    def __init__(self, incoming: bytes):
        self.incoming = bytearray(incoming)
        self.writes = bytearray()
    def read(self, n=1):
        if not self.incoming:
            return b''
        n = min(n, len(self.incoming))
        out = bytes(self.incoming[:n])
        del self.incoming[:n]
        return out
    def write(self, data):
        self.writes.extend(data)
        return len(data)
    def flush(self):
        pass
    def reset_input_buffer(self):
        # No-op: test input represents future receiver responses as well.
        pass

def mk_dirent(user, stem, ext, ex=0, s2=0, rc=1, ro=False, sys=False, arc=False):
    e = bytearray(32)
    e[0] = user
    e[1:9] = stem.upper().encode().ljust(8, b' ')[:8]
    xb = bytearray(ext.upper().encode().ljust(3, b' ')[:3])
    if ro: xb[0] |= 0x80
    if sys: xb[1] |= 0x80
    if arc: xb[2] |= 0x80
    e[9:12] = xb
    e[12], e[14], e[15] = ex, s2, rc
    return bytes(e)

def test_put():
    with tempfile.TemporaryDirectory() as td:
        f = Path(td)/'hello.com'
        f.write_bytes(bytes(range(256)) + b'abc')
        # Ready C, metadata ACK, 3 data ACKs, EOT ACK.
        ser = FakeSerial(bytes([CRC_REQUEST, ACK, ACK, ACK, ACK, ACK]))
        link = HostLinkV2(ser, on_log=lambda s: None, on_progress=lambda *a: None,
                          cancel_event=__import__('threading').Event())
        st = link.send_file(str(f), 'HELLO.COM', 4, 7)
        assert st.bytes_in_file == 259 and st.blocks_sent == 3
        w = bytes(ser.writes)
        assert w[0] == SOH and w[1] == 0 and w[2] == 0xFF
        meta = w[3:131]
        assert meta[:8] == b'S100HST2'
        assert meta[8] == HostLinkV2.CMD_PUT
        assert meta[9] == 4 and meta[10] == 7
        assert meta[11:22] == b'HELLO   COM'
        assert int.from_bytes(meta[22:26], 'little') == 259

def test_dir():
    # HELLO.COM spans logical extents 0 and 1; max formula should give 138 records.
    entries = [
        mk_dirent(3, 'HELLO', 'COM', ex=0, rc=128, ro=True),
        mk_dirent(3, 'HELLO', 'COM', ex=1, rc=10, ro=True),
        mk_dirent(3, 'README', 'TXT', ex=0, rc=5, arc=True),
    ]
    payload = bytearray([0xE5] * 128)
    for i, e in enumerate(entries): payload[i*32:(i+1)*32] = e
    pkt = HostLinkV2._packet(1, bytes(payload))
    # C + ACK command + directory packet + EOT
    ser = FakeSerial(bytes([CRC_REQUEST, ACK]) + pkt + bytes([EOT]))
    link = HostLinkV2(ser, on_log=lambda s: None, on_progress=lambda *a: None,
                      cancel_event=__import__('threading').Event())
    files = link.request_directory(0, 3)
    assert [f.name for f in files] == ['HELLO.COM', 'README.TXT']
    hello = files[0]
    assert hello.records == 138 and hello.size_bytes == 138*128
    assert 'R/O' in hello.attributes
    readme = files[1]
    assert readme.records == 5 and 'ARC' in readme.attributes
    # Linux must ACK the returned directory block and EOT.
    assert bytes(ser.writes).endswith(bytes([ACK, ACK]))

def main():
    test_put(); test_dir()
    print('Host-link v2 protocol tests: PASS')

if __name__ == '__main__':
    main()
