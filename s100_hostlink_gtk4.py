#!/usr/bin/env python3
"""S-100 Z80 SBC Host Link — GTK4/libadwaita edition (v4.1).

A native-feeling Linux GUI for sending files to the S-100 Z80 SBC over its
onboard USB connection.  The preferred HOST.COM mode sends the CP/M 8.3
filename automatically; classic XMODEM remains available as a fallback.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None


APP_ID = "com.s100computers.HostLink"
APP_NAME = "S-100 Host Link"
CONFIG_DIR = Path.home() / ".config" / "s100-xmodem-sender"
CONFIG_FILE = CONFIG_DIR / "settings.json"

SOH = 0x01
EOT = 0x04
ACK = 0x06
NAK = 0x15
CAN = 0x18
CRC_REQUEST = ord("C")
CPM_EOF = 0x1A
BLOCK_SIZE = 128


class XModemError(RuntimeError):
    pass


def crc16_xmodem(data: bytes) -> int:
    """Return CRC-16/XMODEM for *data*."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def cpm_filename(path: str) -> str:
    """Make a conservative CP/M 8.3 suggestion from a host filename."""
    name = Path(path).name.upper()
    if "." in name:
        stem, ext = name.rsplit(".", 1)
    else:
        stem, ext = name, ""

    stem = re.sub(r"[^A-Z0-9_$#@!%&'()\-{}^~]", "_", stem)[:8] or "FILE"
    ext = re.sub(r"[^A-Z0-9_$#@!%&'()\-{}^~]", "_", ext)[:3]
    return f"{stem}.{ext}" if ext else stem


def cpm_raw_name(remote: str) -> bytes:
    """Return an 11-byte CP/M FCB name (8 name + 3 extension)."""
    remote = remote.upper()
    if "." in remote:
        stem, ext = remote.rsplit(".", 1)
    else:
        stem, ext = remote, ""
    return (stem[:8].ljust(8) + ext[:3].ljust(3)).encode("ascii", "replace")


def load_settings() -> dict:
    defaults = {
        "last_directory": str(Path.home()),
        "last_port": "",
        "baud": 115200,
        "recent_files": [],
        "protocol": "host2",
        "target_drive": "current",
        "target_user": "current",
    }
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update(data)
    except (OSError, json.JSONDecodeError):
        pass

    recent = defaults.get("recent_files", [])
    if not isinstance(recent, list):
        recent = []
    defaults["recent_files"] = [str(item) for item in recent if item][:5]
    return defaults


def save_settings(settings: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except OSError:
        # Settings persistence is a convenience, not a transfer requirement.
        pass


@dataclass
class TransferStats:
    bytes_in_file: int
    blocks_sent: int = 0
    retries: int = 0
    mode: str = ""


@dataclass
class DirectoryFile:
    name: str
    records: int
    attributes: str = ""
    directory_entries: int = 1

    @property
    def size_bytes(self) -> int:
        # CP/M directory size is recorded in 128-byte logical records.
        return self.records * 128


def _decode_cpm_dir_entry(raw: bytes):
    """Decode one raw 32-byte CP/M directory entry.

    Returns None for an unused/deleted slot.  Attribute bits are carried in
    the high bits of the extension characters; the visible filename masks them.
    """
    if len(raw) != 32 or raw[0] == 0xE5 or raw[0] > 0x1F:
        return None
    name_b = bytes(b & 0x7F for b in raw[1:9])
    ext_b = bytes(b & 0x7F for b in raw[9:12])
    stem = name_b.decode("ascii", "replace").rstrip(" ")
    ext = ext_b.decode("ascii", "replace").rstrip(" ")
    if not stem:
        return None
    name = f"{stem}.{ext}" if ext else stem
    attrs = []
    if raw[9] & 0x80:
        attrs.append("R/O")
    if raw[10] & 0x80:
        attrs.append("SYS")
    if raw[11] & 0x80:
        attrs.append("ARC")
    ex = raw[12] & 0x1F
    s2 = raw[14] & 0x3F
    rc = raw[15]
    extent_no = (s2 << 5) | ex
    logical_records = extent_no * 128 + rc
    return name, logical_records, ", ".join(attrs)


def aggregate_directory_entries(raw_entries: list[bytes]) -> list[DirectoryFile]:
    files: dict[str, DirectoryFile] = {}
    for raw in raw_entries:
        decoded = _decode_cpm_dir_entry(raw)
        if decoded is None:
            continue
        name, records, attrs = decoded
        key = name.upper()
        if key not in files:
            files[key] = DirectoryFile(name=name, records=records, attributes=attrs)
        else:
            item = files[key]
            item.records = max(item.records, records)
            item.directory_entries += 1
            if attrs:
                existing = {a.strip() for a in item.attributes.split(",") if a.strip()}
                existing.update(a.strip() for a in attrs.split(",") if a.strip())
                item.attributes = ", ".join(sorted(existing))
    return sorted(files.values(), key=lambda f: f.name.casefold())


class XModemSender:
    def __init__(
        self,
        ser,
        *,
        on_log: Callable[[str], None],
        on_progress: Callable[[int, int, TransferStats], None],
        cancel_event: threading.Event,
        handshake_timeout: float = 30.0,
        response_timeout: float = 10.0,
        max_retries: int = 10,
    ):
        self.ser = ser
        self.on_log = on_log
        self.on_progress = on_progress
        self.cancel_event = cancel_event
        self.handshake_timeout = handshake_timeout
        self.response_timeout = response_timeout
        self.max_retries = max_retries

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise XModemError("Transfer cancelled")

    def _read_one(self, timeout: float) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            b = self.ser.read(1)
            if b:
                return b[0]
            time.sleep(0.01)
        return None

    def _wait_for_receiver(self) -> bool:
        """Wait for NAK (checksum) or C (CRC). Return True for CRC mode."""
        self.on_log("Waiting for the CP/M XMODEM receiver...")
        deadline = time.monotonic() + self.handshake_timeout
        can_count = 0
        while time.monotonic() < deadline:
            self._check_cancel()
            ch = self._read_one(min(1.0, max(0.05, deadline - time.monotonic())))
            if ch is None:
                continue
            if ch == CRC_REQUEST:
                self.on_log("Receiver requested CRC mode.")
                return True
            if ch == NAK:
                self.on_log("Receiver requested checksum mode.")
                return False
            if ch == CAN:
                can_count += 1
                if can_count >= 2:
                    raise XModemError("Receiver cancelled the transfer")
            else:
                can_count = 0
        raise XModemError("Timed out waiting for receiver (expected NAK or 'C')")

    def send_file(self, filename: str) -> TransferStats:
        file_size = os.path.getsize(filename)
        stats = TransferStats(bytes_in_file=file_size)
        use_crc = self._wait_for_receiver()
        stats.mode = "CRC" if use_crc else "checksum"

        sent_file_bytes = 0
        block_no = 1

        with open(filename, "rb") as f:
            while True:
                self._check_cancel()
                data = f.read(BLOCK_SIZE)
                if not data:
                    break
                actual_len = len(data)
                if actual_len < BLOCK_SIZE:
                    data += bytes([CPM_EOF]) * (BLOCK_SIZE - actual_len)

                header = bytes([SOH, block_no & 0xFF, 0xFF - (block_no & 0xFF)])
                if use_crc:
                    crc = crc16_xmodem(data)
                    trailer = bytes([(crc >> 8) & 0xFF, crc & 0xFF])
                else:
                    trailer = bytes([sum(data) & 0xFF])
                packet = header + data + trailer

                accepted = False
                for attempt in range(1, self.max_retries + 1):
                    self._check_cancel()
                    self.ser.write(packet)
                    self.ser.flush()
                    response = self._read_one(self.response_timeout)
                    if response == ACK:
                        accepted = True
                        break
                    if response == NAK or response is None:
                        stats.retries += 1
                        why = "NAK" if response == NAK else "timeout"
                        self.on_log(
                            f"Block {block_no & 0xFF:02X}: {why}; "
                            f"retry {attempt}/{self.max_retries}"
                        )
                        continue
                    if response == CAN:
                        second = self._read_one(1.0)
                        if second == CAN:
                            raise XModemError("Receiver cancelled the transfer")
                        stats.retries += 1
                        self.on_log(
                            f"Block {block_no & 0xFF:02X}: unexpected CAN; "
                            f"retry {attempt}/{self.max_retries}"
                        )
                        continue
                    stats.retries += 1
                    self.on_log(
                        f"Block {block_no & 0xFF:02X}: unexpected response "
                        f"0x{response:02X}; retry {attempt}/{self.max_retries}"
                    )

                if not accepted:
                    self._cancel_remote()
                    raise XModemError(
                        f"Block {block_no & 0xFF:02X} failed after "
                        f"{self.max_retries} retries"
                    )

                stats.blocks_sent += 1
                sent_file_bytes += actual_len
                self.on_progress(min(sent_file_bytes, file_size), file_size, stats)
                block_no = (block_no + 1) & 0xFF

        for attempt in range(1, self.max_retries + 1):
            self._check_cancel()
            self.ser.write(bytes([EOT]))
            self.ser.flush()
            response = self._read_one(self.response_timeout)
            if response == ACK:
                self.on_log("Receiver acknowledged end of transfer.")
                return stats
            if response == NAK:
                self.on_log("Receiver requested a second EOT.")
                continue
            if response == CAN:
                second = self._read_one(1.0)
                if second == CAN:
                    raise XModemError("Receiver cancelled during end-of-transfer")
            stats.retries += 1
            if response is None:
                self.on_log(f"EOT timeout; retry {attempt}/{self.max_retries}")
            else:
                self.on_log(
                    f"Unexpected EOT response 0x{response:02X}; "
                    f"retry {attempt}/{self.max_retries}"
                )

        self._cancel_remote()
        raise XModemError("Receiver did not acknowledge end of transfer")

    def _cancel_remote(self):
        try:
            self.ser.write(bytes([CAN, CAN]))
            self.ser.flush()
        except Exception:
            pass


class HostLinkSenderV1:
    """Sender for the companion CP/M HOST.COM receiver.

    The payload blocks intentionally use XMODEM's proven 128-byte CRC framing,
    but block 0 carries a small S-100 host-link header with the destination
    CP/M filename.  This is not presented as standard YMODEM; both ends are
    under our control and the format is deliberately tiny.
    """

    MAGIC = b"S100HST1"

    def __init__(
        self,
        ser,
        *,
        on_log: Callable[[str], None],
        on_progress: Callable[[int, int, TransferStats], None],
        cancel_event: threading.Event,
        handshake_timeout: float = 30.0,
        response_timeout: float = 10.0,
        max_retries: int = 10,
    ):
        self.ser = ser
        self.on_log = on_log
        self.on_progress = on_progress
        self.cancel_event = cancel_event
        self.handshake_timeout = handshake_timeout
        self.response_timeout = response_timeout
        self.max_retries = max_retries

    def _check_cancel(self):
        if self.cancel_event.is_set():
            self._cancel_remote()
            raise XModemError("Transfer cancelled")

    def _read_one(self, timeout: float) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            b = self.ser.read(1)
            if b:
                return b[0]
            time.sleep(0.01)
        return None

    @staticmethod
    def _packet(block_no: int, payload: bytes) -> bytes:
        if len(payload) != BLOCK_SIZE:
            raise ValueError("host-link payload must be exactly 128 bytes")
        block_no &= 0xFF
        crc = crc16_xmodem(payload)
        return bytes([SOH, block_no, 0xFF - block_no]) + payload + bytes([
            (crc >> 8) & 0xFF, crc & 0xFF
        ])

    def _wait_for_host_receiver(self):
        self.on_log("Waiting for CP/M HOST.COM...")
        deadline = time.monotonic() + self.handshake_timeout
        can_count = 0
        while time.monotonic() < deadline:
            ch = self._read_one(min(1.0, max(0.05, deadline - time.monotonic())))
            if ch is None:
                continue
            if ch == CRC_REQUEST:
                self.on_log("HOST.COM is ready; automatic filename transfer starting.")
                return
            if ch == CAN:
                can_count += 1
                if can_count >= 2:
                    raise XModemError("HOST.COM cancelled before transfer")
            else:
                can_count = 0
        raise XModemError("Timed out waiting for HOST.COM (run HOST on the CP/M console)")

    def _send_packet_with_retry(self, packet: bytes, block_no: int, stats: TransferStats):
        for attempt in range(1, self.max_retries + 1):
            self._check_cancel()
            self.ser.write(packet)
            self.ser.flush()
            deadline = time.monotonic() + self.response_timeout
            while time.monotonic() < deadline:
                response = self._read_one(max(0.05, deadline - time.monotonic()))
                if response is None:
                    break
                if response == ACK:
                    return
                # HOST.COM may have advertised readiness more than once before
                # seeing SOH; queued 'C' bytes are harmless and can be ignored.
                if response == CRC_REQUEST:
                    continue
                if response == NAK:
                    stats.retries += 1
                    self.on_log(
                        f"Block {block_no & 0xFF:02X}: NAK; retry "
                        f"{attempt}/{self.max_retries}"
                    )
                    break
                if response == CAN:
                    second = self._read_one(1.0)
                    if second == CAN:
                        raise XModemError("HOST.COM cancelled the transfer")
                    continue
                stats.retries += 1
                self.on_log(
                    f"Block {block_no & 0xFF:02X}: unexpected response "
                    f"0x{response:02X}; retry {attempt}/{self.max_retries}"
                )
                break
            else:
                pass
            if response is None:
                stats.retries += 1
                self.on_log(
                    f"Block {block_no & 0xFF:02X}: timeout; retry "
                    f"{attempt}/{self.max_retries}"
                )
        self._cancel_remote()
        raise XModemError(
            f"Block {block_no & 0xFF:02X} failed after {self.max_retries} retries"
        )

    def send_file(self, filename: str, remote_name: str) -> TransferStats:
        file_size = os.path.getsize(filename)
        stats = TransferStats(bytes_in_file=file_size, mode="HOST/CRC")
        self._wait_for_host_receiver()

        # Remove any extra ready advertisements already queued before SOH.
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        metadata = bytearray(BLOCK_SIZE)
        metadata[0:8] = self.MAGIC
        metadata[8:19] = cpm_raw_name(remote_name)
        metadata[19:23] = int(file_size).to_bytes(4, "little", signed=False)
        self.on_log(f"Sending filename header for {remote_name} ({file_size:,} bytes).")
        self._send_packet_with_retry(self._packet(0, bytes(metadata)), 0, stats)

        sent_file_bytes = 0
        block_no = 1
        with open(filename, "rb") as f:
            while True:
                self._check_cancel()
                data = f.read(BLOCK_SIZE)
                if not data:
                    break
                actual_len = len(data)
                if actual_len < BLOCK_SIZE:
                    data += bytes([CPM_EOF]) * (BLOCK_SIZE - actual_len)
                self._send_packet_with_retry(self._packet(block_no, data), block_no, stats)
                stats.blocks_sent += 1
                sent_file_bytes += actual_len
                self.on_progress(min(sent_file_bytes, file_size), file_size, stats)
                block_no = (block_no + 1) & 0xFF

        for attempt in range(1, self.max_retries + 1):
            self._check_cancel()
            self.ser.write(bytes([EOT]))
            self.ser.flush()
            response = self._read_one(self.response_timeout)
            if response == ACK:
                self.on_log("HOST.COM acknowledged end of file.")
                return stats
            if response == CAN:
                second = self._read_one(1.0)
                if second == CAN:
                    raise XModemError("HOST.COM reported an error while closing the file")
            stats.retries += 1
            self.on_log(f"EOT not acknowledged; retry {attempt}/{self.max_retries}")

        self._cancel_remote()
        raise XModemError("HOST.COM did not acknowledge end of file")

    def _cancel_remote(self):
        try:
            self.ser.write(bytes([CAN, CAN]))
            self.ser.flush()
        except Exception:
            pass


class HostLinkV2:
    """Version 2 host-link protocol: PUT plus remote CP/M directory browsing."""

    MAGIC = b"S100HST2"
    CMD_PUT = 1
    CMD_DIR = 2
    CURRENT = 0xFF

    def __init__(
        self,
        ser,
        *,
        on_log: Callable[[str], None],
        on_progress: Callable[[int, int, TransferStats], None],
        cancel_event: threading.Event,
        handshake_timeout: float = 30.0,
        response_timeout: float = 10.0,
        max_retries: int = 10,
    ):
        self.ser = ser
        self.on_log = on_log
        self.on_progress = on_progress
        self.cancel_event = cancel_event
        self.handshake_timeout = handshake_timeout
        self.response_timeout = response_timeout
        self.max_retries = max_retries

    def _check_cancel(self):
        if self.cancel_event.is_set():
            self._cancel_remote()
            raise XModemError("Operation cancelled")

    def _read_one(self, timeout: float) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancel()
            b = self.ser.read(1)
            if b:
                return b[0]
            time.sleep(0.01)
        return None

    @staticmethod
    def _packet(block_no: int, payload: bytes) -> bytes:
        if len(payload) != BLOCK_SIZE:
            raise ValueError("host-link payload must be exactly 128 bytes")
        block_no &= 0xFF
        crc = crc16_xmodem(payload)
        return bytes([SOH, block_no, 0xFF - block_no]) + payload + bytes([
            (crc >> 8) & 0xFF, crc & 0xFF
        ])

    def _wait_ready(self):
        self.on_log("Waiting for CP/M HOST.COM v2...")
        deadline = time.monotonic() + self.handshake_timeout
        can_count = 0
        while time.monotonic() < deadline:
            ch = self._read_one(min(1.0, max(0.05, deadline - time.monotonic())))
            if ch is None:
                continue
            if ch == CRC_REQUEST:
                self.on_log("HOST.COM v2 is ready.")
                return
            if ch == CAN:
                can_count += 1
                if can_count >= 2:
                    raise XModemError("HOST.COM cancelled before the operation started")
            else:
                can_count = 0
        raise XModemError("Timed out waiting for HOST.COM v2 (run HOST on the CP/M console)")

    def _begin(self):
        self._wait_ready()
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def _send_packet_with_retry(self, packet: bytes, block_no: int, stats: TransferStats):
        for attempt in range(1, self.max_retries + 1):
            self._check_cancel()
            self.ser.write(packet)
            self.ser.flush()
            deadline = time.monotonic() + self.response_timeout
            response = None
            while time.monotonic() < deadline:
                response = self._read_one(max(0.05, deadline - time.monotonic()))
                if response is None:
                    break
                if response == ACK:
                    return
                if response == CRC_REQUEST:
                    # Stale ready advertisements can remain queued.
                    continue
                if response == NAK:
                    stats.retries += 1
                    self.on_log(
                        f"Block {block_no & 0xFF:02X}: NAK; retry "
                        f"{attempt}/{self.max_retries}"
                    )
                    break
                if response == CAN:
                    second = self._read_one(1.0)
                    if second == CAN:
                        raise XModemError("HOST.COM cancelled the operation")
                    continue
                stats.retries += 1
                self.on_log(
                    f"Block {block_no & 0xFF:02X}: unexpected response "
                    f"0x{response:02X}; retry {attempt}/{self.max_retries}"
                )
                break
            if response is None:
                stats.retries += 1
                self.on_log(
                    f"Block {block_no & 0xFF:02X}: timeout; retry "
                    f"{attempt}/{self.max_retries}"
                )
        self._cancel_remote()
        raise XModemError(
            f"Block {block_no & 0xFF:02X} failed after {self.max_retries} retries"
        )

    @staticmethod
    def _target_byte(value: Optional[int]) -> int:
        return HostLinkV2.CURRENT if value is None else int(value) & 0xFF

    def _command_payload(
        self,
        command: int,
        drive: Optional[int],
        user: Optional[int],
        *,
        remote_name: str = "",
        file_size: int = 0,
    ) -> bytes:
        payload = bytearray(BLOCK_SIZE)
        payload[0:8] = self.MAGIC
        payload[8] = command & 0xFF
        payload[9] = self._target_byte(drive)
        payload[10] = self._target_byte(user)
        if remote_name:
            payload[11:22] = cpm_raw_name(remote_name)
        payload[22:26] = int(file_size).to_bytes(4, "little", signed=False)
        return bytes(payload)

    def send_file(
        self,
        filename: str,
        remote_name: str,
        drive: Optional[int],
        user: Optional[int],
    ) -> TransferStats:
        file_size = os.path.getsize(filename)
        stats = TransferStats(bytes_in_file=file_size, mode="HOST2/CRC")
        self._begin()

        target = self._format_target(drive, user)
        self.on_log(
            f"Sending {remote_name} ({file_size:,} bytes) to CP/M {target}."
        )
        metadata = self._command_payload(
            self.CMD_PUT, drive, user, remote_name=remote_name, file_size=file_size
        )
        self._send_packet_with_retry(self._packet(0, metadata), 0, stats)

        sent_file_bytes = 0
        block_no = 1
        with open(filename, "rb") as f:
            while True:
                self._check_cancel()
                data = f.read(BLOCK_SIZE)
                if not data:
                    break
                actual_len = len(data)
                if actual_len < BLOCK_SIZE:
                    data += bytes([CPM_EOF]) * (BLOCK_SIZE - actual_len)
                self._send_packet_with_retry(self._packet(block_no, data), block_no, stats)
                stats.blocks_sent += 1
                sent_file_bytes += actual_len
                self.on_progress(min(sent_file_bytes, file_size), file_size, stats)
                block_no = (block_no + 1) & 0xFF

        for attempt in range(1, self.max_retries + 1):
            self._check_cancel()
            self.ser.write(bytes([EOT]))
            self.ser.flush()
            response = self._read_one(self.response_timeout)
            if response == ACK:
                self.on_log("HOST.COM acknowledged end of file.")
                return stats
            if response == CAN:
                second = self._read_one(1.0)
                if second == CAN:
                    raise XModemError("HOST.COM reported a CP/M file or disk error")
            stats.retries += 1
            self.on_log(f"EOT not acknowledged; retry {attempt}/{self.max_retries}")

        self._cancel_remote()
        raise XModemError("HOST.COM did not acknowledge end of file")

    def request_directory(
        self, drive: Optional[int], user: Optional[int]
    ) -> list[DirectoryFile]:
        stats = TransferStats(bytes_in_file=0, mode="HOST2/DIR")
        self._begin()
        target = self._format_target(drive, user)
        self.on_log(f"Requesting CP/M directory for {target}.")
        metadata = self._command_payload(self.CMD_DIR, drive, user)
        self._send_packet_with_retry(self._packet(0, metadata), 0, stats)

        raw_entries: list[bytes] = []
        expected = 1
        deadline_reset = self.response_timeout
        while True:
            self._check_cancel()
            ch = self._read_one(deadline_reset)
            if ch is None:
                self._cancel_remote()
                raise XModemError("Timed out while receiving the CP/M directory")
            if ch == EOT:
                self.ser.write(bytes([ACK]))
                self.ser.flush()
                files = aggregate_directory_entries(raw_entries)
                self.on_log(f"Directory received: {len(files)} file(s).")
                return files
            if ch == CAN:
                second = self._read_one(1.0)
                if second == CAN:
                    raise XModemError("HOST.COM could not read that CP/M directory")
                continue
            if ch != SOH:
                # Ignore a stray readiness advertisement or other noise.
                continue

            seq_b = self._read_exact(2, self.response_timeout)
            payload = self._read_exact(BLOCK_SIZE, self.response_timeout)
            crc_b = self._read_exact(2, self.response_timeout)
            seq, comp = seq_b[0], seq_b[1]
            recv_crc = (crc_b[0] << 8) | crc_b[1]
            good = ((seq + comp) & 0xFF) == 0xFF and crc16_xmodem(payload) == recv_crc
            if not good:
                self.ser.write(bytes([NAK]))
                self.ser.flush()
                continue

            if seq == expected:
                for offset in range(0, BLOCK_SIZE, 32):
                    raw_entries.append(payload[offset:offset + 32])
                expected = (expected + 1) & 0xFF
                self.ser.write(bytes([ACK]))
                self.ser.flush()
                continue

            if seq == ((expected - 1) & 0xFF):
                # Sender retried because our ACK was lost; ACK without parsing twice.
                self.ser.write(bytes([ACK]))
                self.ser.flush()
                continue

            self.ser.write(bytes([NAK]))
            self.ser.flush()

    def _read_exact(self, count: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < count and time.monotonic() < deadline:
            self._check_cancel()
            chunk = self.ser.read(count - len(buf))
            if chunk:
                buf.extend(chunk)
            else:
                time.sleep(0.01)
        if len(buf) != count:
            raise XModemError("Timed out receiving a host-link packet")
        return bytes(buf)

    @staticmethod
    def _format_target(drive: Optional[int], user: Optional[int]) -> str:
        d = "current drive" if drive is None else f"{chr(ord('A') + drive)}:"
        u = "current user" if user is None else f"user {user}"
        return f"{d}, {u}"

    def _cancel_remote(self):
        try:
            self.ser.write(bytes([CAN, CAN]))
            self.ser.flush()
        except Exception:
            pass


class MainWindow(Adw.ApplicationWindow):
    BAUD_VALUES = [
        "9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600"
    ]

    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title(APP_NAME)
        # A slightly wider window gives long Linux serial-device descriptions
        # enough room to remain readable instead of being ellipsized.
        self.set_default_size(1000, 860)
        self.set_size_request(840, 640)

        self.settings = load_settings()
        self.selected_file = ""
        self.active_transfer_file = ""
        self.port_devices: list[str] = []
        self.recent_paths: list[str] = []
        self.directory_files = []
        self._updating_recent = False
        self.worker: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()

        self._build_ui()
        self.refresh_ports()
        self._restore_baud()
        self._refresh_recent_files()

        if serial is None:
            GLib.idle_add(
                self.show_toast,
                "pySerial is not installed — run: sudo apt install python3-serial",
                6,
            )

    def _make_string_factory(self) -> Gtk.SignalListItemFactory:
        """Create a non-ellipsizing factory for Gtk.DropDown string items."""
        factory = Gtk.SignalListItemFactory()

        def setup(_factory, list_item):
            label = Gtk.Label(xalign=0)
            label.set_margin_start(8)
            label.set_margin_end(8)
            label.set_margin_top(4)
            label.set_margin_bottom(4)
            list_item.set_child(label)

        def bind(_factory, list_item):
            item = list_item.get_item()
            label = list_item.get_child()
            if item is not None and label is not None:
                label.set_text(item.get_string())

        factory.connect("setup", setup)
        factory.connect("bind", bind)
        return factory

    def _build_ui(self):
        toast_overlay = Adw.ToastOverlay()
        self.toast_overlay = toast_overlay
        self.set_content(toast_overlay)

        toolbar_view = Adw.ToolbarView()
        toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="S-100 Host Link", subtitle="Z80 SBC USB file transfer"))
        toolbar_view.add_top_bar(header)

        refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_button.set_tooltip_text("Refresh serial devices")
        refresh_button.connect("clicked", lambda _b: self.refresh_ports())
        header.pack_end(refresh_button)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scroller)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(920)
        clamp.set_tightening_threshold(720)
        scroller.set_child(clamp)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        main.set_margin_top(18)
        main.set_margin_bottom(24)
        main.set_margin_start(18)
        main.set_margin_end(18)
        clamp.set_child(main)

        # Connection group
        connection_group = Adw.PreferencesGroup(title="Connection")
        main.append(connection_group)

        self.port_model = Gtk.StringList.new([])
        self.port_row = Adw.ActionRow(
            title="USB device",
            subtitle="SBC onboard USB connection",
        )
        self.port_dropdown = Gtk.DropDown()
        self.port_dropdown.set_model(self.port_model)
        self.port_dropdown.set_factory(self._make_string_factory())
        self.port_dropdown.set_list_factory(self._make_string_factory())
        self.port_dropdown.set_enable_search(True)
        self.port_dropdown.set_valign(Gtk.Align.CENTER)
        self.port_dropdown.set_hexpand(True)
        # Keep both the selected value and the pop-up list wide enough for
        # labels such as '/dev/ttyUSB1 — FT232R USB UART'.
        self.port_dropdown.set_size_request(570, -1)
        self.port_dropdown.connect("notify::selected", self._on_port_changed)
        self.port_row.add_suffix(self.port_dropdown)
        self.port_row.set_activatable_widget(self.port_dropdown)
        connection_group.add(self.port_row)

        self.baud_model = Gtk.StringList.new(self.BAUD_VALUES)
        self.baud_row = Adw.ComboRow(title="Baud rate", subtitle="Use the value that works with your current setup")
        self.baud_row.set_model(self.baud_model)
        self.baud_row.connect("notify::selected", self._on_baud_changed)
        connection_group.add(self.baud_row)

        self.protocol_keys = ["host2", "host1", "xmodem"]
        self.protocol_model = Gtk.StringList.new([
            "HOST.COM v2 — drive/user + directory",
            "HOST.COM v1 — legacy automatic filename",
            "XMODEM.COM — manual filename",
        ])
        self.protocol_row = Adw.ComboRow(
            title="CP/M receiver",
            subtitle="HOST.COM v2 enables remote destination selection and directory browsing",
        )
        self.protocol_row.set_model(self.protocol_model)
        self.protocol_row.connect("notify::selected", self._on_protocol_changed)
        connection_group.add(self.protocol_row)

        # File group
        file_group = Adw.PreferencesGroup(title="File")
        main.append(file_group)

        self.file_row = Adw.ActionRow(title="No file selected", subtitle="Choose a file to send to CP/M")
        browse_button = Gtk.Button(label="Browse…")
        browse_button.set_valign(Gtk.Align.CENTER)
        browse_button.add_css_class("suggested-action")
        browse_button.connect("clicked", self.choose_file)
        self.file_row.add_suffix(browse_button)
        self.file_row.set_activatable_widget(browse_button)
        file_group.add(self.file_row)

        self.cpm_row = Adw.ActionRow(title="CP/M filename", subtitle="—")
        file_group.add(self.cpm_row)

        self.recent_model = Gtk.StringList.new([])
        self.recent_row = Adw.ActionRow(
            title="Recent transfers",
            subtitle="Last five successfully transferred files",
        )
        self.recent_dropdown = Gtk.DropDown()
        self.recent_dropdown.set_model(self.recent_model)
        self.recent_dropdown.set_factory(self._make_string_factory())
        self.recent_dropdown.set_list_factory(self._make_string_factory())
        self.recent_dropdown.set_enable_search(True)
        self.recent_dropdown.set_valign(Gtk.Align.CENTER)
        # Do not let a long remembered path consume the whole ActionRow.
        # Recent entries are deliberately compact (filename + parent folder),
        # while the complete path is preserved internally and shown as a tooltip.
        self.recent_dropdown.set_hexpand(False)
        self.recent_dropdown.set_size_request(440, -1)
        self.recent_dropdown.connect("notify::selected", self._on_recent_changed)
        self.recent_row.add_suffix(self.recent_dropdown)
        self.recent_row.set_activatable_widget(self.recent_dropdown)
        file_group.add(self.recent_row)

        # CP/M destination and directory group (HOST.COM v2)
        self.destination_group = Adw.PreferencesGroup(
            title="CP/M destination",
            description="Choose the drive and user area without changing the CP/M console."
        )
        main.append(self.destination_group)

        self.drive_keys = [None] + list(range(16))
        self.drive_model = Gtk.StringList.new(["Current drive"] + [f"{chr(ord('A') + i)}:" for i in range(16)])
        self.drive_row = Adw.ComboRow(title="Drive")
        self.drive_row.set_model(self.drive_model)
        self.drive_row.connect("notify::selected", self._on_target_changed)
        self.destination_group.add(self.drive_row)

        self.user_keys = [None] + list(range(16))
        self.user_model = Gtk.StringList.new(["Current user"] + [f"User {i}" for i in range(16)])
        self.user_row = Adw.ComboRow(title="User area")
        self.user_row.set_model(self.user_model)
        self.user_row.connect("notify::selected", self._on_target_changed)
        self.destination_group.add(self.user_row)

        self.directory_status_row = Adw.ActionRow(
            title="Directory",
            subtitle="Run HOST.COM v2, then press Refresh Directory"
        )
        self.directory_refresh_button = Gtk.Button(label="Refresh Directory")
        self.directory_refresh_button.set_valign(Gtk.Align.CENTER)
        self.directory_refresh_button.connect("clicked", self.refresh_directory)
        self.directory_status_row.add_suffix(self.directory_refresh_button)
        self.directory_status_row.set_activatable_widget(self.directory_refresh_button)
        self.destination_group.add(self.directory_status_row)

        self.directory_frame = Gtk.Frame()
        self.directory_frame.set_size_request(-1, 240)
        self.directory_scroll = Gtk.ScrolledWindow()
        self.directory_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.directory_frame.set_child(self.directory_scroll)
        self.directory_list = Gtk.ListBox()
        self.directory_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.directory_list.add_css_class("boxed-list")
        self.directory_scroll.set_child(self.directory_list)
        self.destination_group.add(self.directory_frame)

        # CP/M command group
        self.command_group = Adw.PreferencesGroup(title="CP/M side")
        main.append(self.command_group)

        command_row = Adw.ActionRow(title="Receive command")
        self.command_entry = Gtk.Entry()
        self.command_entry.set_editable(False)
        self.command_entry.set_hexpand(True)
        self.command_entry.set_width_chars(28)
        self.command_entry.set_text("Choose a file first")
        self.command_entry.add_css_class("monospace")
        command_row.add_suffix(self.command_entry)

        copy_button = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        copy_button.set_tooltip_text("Copy command")
        copy_button.set_valign(Gtk.Align.CENTER)
        copy_button.connect("clicked", self.copy_command)
        command_row.add_suffix(copy_button)
        self.command_group.add(command_row)

        # Transfer group
        transfer_group = Adw.PreferencesGroup(title="Transfer")
        main.append(transfer_group)

        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        status_box.set_margin_top(8)
        status_box.set_margin_bottom(8)
        status_box.set_margin_start(12)
        status_box.set_margin_end(12)

        self.status_label = Gtk.Label(label="Ready", xalign=0)
        self.status_label.add_css_class("dim-label")
        status_box.append(self.status_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_fraction(0.0)
        self.progress.set_show_text(False)
        status_box.append(self.progress)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.send_button = Gtk.Button(label="Send File")
        self.send_button.add_css_class("suggested-action")
        self.send_button.connect("clicked", self.start_transfer)
        button_box.append(self.send_button)

        self.cancel_button = Gtk.Button(label="Cancel")
        self.cancel_button.set_sensitive(False)
        self.cancel_button.connect("clicked", self.cancel_transfer)
        button_box.append(self.cancel_button)
        status_box.append(button_box)

        transfer_group.add(status_box)

        # Log group
        log_group = Adw.PreferencesGroup(title="Transfer log")
        main.append(log_group)

        log_frame = Gtk.Frame()
        log_frame.set_size_request(-1, 220)
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        log_frame.set_child(log_scroll)

        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_monospace(True)
        self.log_view.set_top_margin(8)
        self.log_view.set_bottom_margin(8)
        self.log_view.set_left_margin(8)
        self.log_view.set_right_margin(8)
        log_scroll.set_child(self.log_view)
        log_group.add(log_frame)

        self._restore_protocol()
        self._restore_target()
        self._update_protocol_ui()
        self._render_directory([])

    def show_toast(self, message: str, timeout: int = 3):
        toast = Adw.Toast(title=message)
        toast.set_timeout(timeout)
        self.toast_overlay.add_toast(toast)
        return False

    def log(self, message: str):
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, message + "\n")
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)

    def refresh_ports(self):
        old_device = self.current_port() or str(self.settings.get("last_port", ""))

        entries: list[tuple[str, str]] = []
        if list_ports is not None:
            for p in list_ports.comports():
                label = p.device
                description = (p.description or "").strip()
                if description and description.lower() != "n/a":
                    label = f"{p.device} — {description}"
                entries.append((p.device, label))

        # If the saved device is temporarily absent, still show it so the user
        # can see what the app was previously configured to use.
        if old_device and old_device not in [d for d, _ in entries]:
            entries.append((old_device, f"{old_device} — saved device (not currently detected)"))

        while self.port_model.get_n_items() > 0:
            self.port_model.remove(0)
        self.port_devices = []

        for device, label in entries:
            self.port_model.append(label)
            self.port_devices.append(device)

        selected_index: Optional[int] = None
        if old_device in self.port_devices:
            selected_index = self.port_devices.index(old_device)
        elif self.port_devices:
            selected_index = next(
                (i for i, d in enumerate(self.port_devices) if "ttyUSB" in d),
                next((i for i, d in enumerate(self.port_devices) if "ttyACM" in d), 0),
            )

        if selected_index is not None:
            self.port_dropdown.set_selected(selected_index)
        else:
            self.port_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)

        self.log(f"Found {len(entries)} serial device(s).")

    def current_port(self) -> str:
        idx = self.port_dropdown.get_selected() if hasattr(self, "port_dropdown") else Gtk.INVALID_LIST_POSITION
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.port_devices):
            return ""
        return self.port_devices[idx]

    def _on_port_changed(self, _dropdown, _pspec):
        port = self.current_port()
        if port:
            self.settings["last_port"] = port
            save_settings(self.settings)
            idx = self.port_dropdown.get_selected()
            if idx != Gtk.INVALID_LIST_POSITION and idx < self.port_model.get_n_items():
                item = self.port_model.get_string(idx)
                self.port_dropdown.set_tooltip_text(item)

    def _restore_baud(self):
        saved = str(self.settings.get("baud", 115200))
        try:
            idx = self.BAUD_VALUES.index(saved)
        except ValueError:
            idx = self.BAUD_VALUES.index("115200")
        self.baud_row.set_selected(idx)

    def current_baud(self) -> int:
        idx = self.baud_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.BAUD_VALUES):
            return 115200
        return int(self.BAUD_VALUES[idx])

    def _on_baud_changed(self, _row, _pspec):
        self.settings["baud"] = self.current_baud()
        save_settings(self.settings)

    def _restore_target(self):
        saved_drive = str(self.settings.get("target_drive", "current"))
        saved_user = str(self.settings.get("target_user", "current"))
        try:
            drive_idx = 0 if saved_drive == "current" else int(saved_drive) + 1
        except (TypeError, ValueError):
            drive_idx = 0
        try:
            user_idx = 0 if saved_user == "current" else int(saved_user) + 1
        except (TypeError, ValueError):
            user_idx = 0
        self.drive_row.set_selected(drive_idx if 0 <= drive_idx < len(self.drive_keys) else 0)
        self.user_row.set_selected(user_idx if 0 <= user_idx < len(self.user_keys) else 0)

    def current_drive(self) -> Optional[int]:
        idx = self.drive_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.drive_keys):
            return None
        return self.drive_keys[idx]

    def current_user(self) -> Optional[int]:
        idx = self.user_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.user_keys):
            return None
        return self.user_keys[idx]

    def target_label(self) -> str:
        drive = self.current_drive()
        user = self.current_user()
        d = "current drive" if drive is None else f"{chr(ord('A') + drive)}:"
        u = "current user" if user is None else f"user {user}"
        return f"{d}, {u}"

    def _on_target_changed(self, _row, _pspec):
        drive = self.current_drive()
        user = self.current_user()
        self.settings["target_drive"] = "current" if drive is None else str(drive)
        self.settings["target_user"] = "current" if user is None else str(user)
        save_settings(self.settings)
        if hasattr(self, "directory_status_row"):
            self.directory_status_row.set_subtitle(
                f"{self.target_label()} — press Refresh Directory"
            )
            self._render_directory([])

    def _restore_protocol(self):
        saved = str(self.settings.get("protocol", "host2"))
        try:
            idx = self.protocol_keys.index(saved)
        except ValueError:
            idx = 0
        self.protocol_row.set_selected(idx)

    def current_protocol(self) -> str:
        idx = self.protocol_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.protocol_keys):
            return "host2"
        return self.protocol_keys[idx]

    def _on_protocol_changed(self, _row, _pspec):
        self.settings["protocol"] = self.current_protocol()
        save_settings(self.settings)
        self._update_protocol_ui()

    def _update_protocol_ui(self):
        mode = self.current_protocol() if hasattr(self, "protocol_row") else "host2"
        is_v2 = mode == "host2"
        self.drive_row.set_sensitive(is_v2)
        self.user_row.set_sensitive(is_v2)
        self.directory_refresh_button.set_sensitive(is_v2 and not (self.worker and self.worker.is_alive()))
        self.directory_frame.set_sensitive(is_v2)

        if mode == "host2":
            self.command_group.set_description(
                "Run the new HOST.COM once. The Linux GUI can then choose the CP/M drive/user, "
                "browse its directory, and send files without another CP/M command."
            )
            self.command_entry.set_text("HOST")
        elif mode == "host1":
            self.command_group.set_description(
                "Legacy HOST.COM mode for bootstrapping the new v2 receiver. Files go to the "
                "current CP/M drive/user and directory browsing is unavailable."
            )
            self.command_entry.set_text("HOST")
            self.directory_status_row.set_subtitle("HOST.COM v2 is required for directory browsing")
        else:
            self.command_group.set_description(
                "Start XMODEM on the VGA/keyboard console before each transfer. "
                "Classic XMODEM does not carry the filename."
            )
            if self.selected_file:
                self.command_entry.set_text(f"XMODEM {cpm_filename(self.selected_file)} /R")
            else:
                self.command_entry.set_text("Choose a file first")
            self.directory_status_row.set_subtitle("HOST.COM v2 is required for directory browsing")

    @staticmethod
    def _format_cpm_size(size: int) -> str:
        if size < 1024:
            return f"{size} bytes"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KiB"
        return f"{size / (1024 * 1024):.2f} MiB"

    def _render_directory(self, files: list[DirectoryFile]):
        while True:
            child = self.directory_list.get_first_child()
            if child is None:
                break
            self.directory_list.remove(child)

        self.directory_files = list(files)
        if not files:
            row = Adw.ActionRow(
                title="No directory loaded",
                subtitle="Press Refresh Directory to read the selected CP/M drive/user area."
            )
            row.set_sensitive(False)
            self.directory_list.append(row)
            return

        for item in files:
            subtitle = (
                f"{self._format_cpm_size(item.size_bytes)} • "
                f"{item.records} × 128-byte record{'s' if item.records != 1 else ''}"
            )
            if item.attributes:
                subtitle += f" • {item.attributes}"
            row = Adw.ActionRow(title=item.name, subtitle=subtitle)
            self.directory_list.append(row)

    def refresh_directory(self, _button=None):
        if self.worker and self.worker.is_alive():
            self.show_toast("Another host-link operation is already running.")
            return
        if serial is None:
            self.show_toast("pySerial is required: sudo apt install python3-serial", 6)
            return
        if self.current_protocol() != "host2":
            self.show_toast("Directory browsing requires HOST.COM v2.", 5)
            return
        port = self.current_port()
        if not port:
            self.show_toast("Choose an SBC USB serial device.")
            return

        baud = self.current_baud()
        drive = self.current_drive()
        user = self.current_user()
        target = self.target_label()
        self.cancel_event.clear()
        self.status_label.set_text(f"Reading CP/M directory — {target}…")
        self.directory_status_row.set_subtitle(f"Reading {target}…")
        self.send_button.set_sensitive(False)
        self.directory_refresh_button.set_sensitive(False)
        self.cancel_button.set_sensitive(True)
        self.log(f"Opening {port} at {baud} baud for directory request: {target}")

        self.worker = threading.Thread(
            target=self._directory_worker,
            args=(port, baud, drive, user, target),
            daemon=True,
        )
        self.worker.start()

    def _directory_worker(
        self, port: str, baud: int, drive: Optional[int], user: Optional[int], target: str
    ):
        serial_kwargs = dict(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.15,
            write_timeout=10,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        if os.name == "posix":
            serial_kwargs["exclusive"] = True
        try:
            try:
                ser = serial.Serial(**serial_kwargs)
            except TypeError:
                serial_kwargs.pop("exclusive", None)
                ser = serial.Serial(**serial_kwargs)
            with ser:
                GLib.idle_add(self._ui_port_opened, port, baud, "host2-dir")
                link = HostLinkV2(
                    ser,
                    on_log=lambda text: GLib.idle_add(self._ui_log, text),
                    on_progress=lambda done, total, stats: None,
                    cancel_event=self.cancel_event,
                )
                files = link.request_directory(drive, user)
                GLib.idle_add(self._ui_directory, files, target)
        except Exception as exc:
            GLib.idle_add(self._ui_error, self._friendly_serial_error(port, exc))

    def _ui_directory(self, files: list[DirectoryFile], target: str):
        self._render_directory(files)
        self.directory_status_row.set_subtitle(f"{target} — {len(files)} file(s)")
        self.status_label.set_text("Directory received")
        self.send_button.set_sensitive(True)
        self.cancel_button.set_sensitive(False)
        self.directory_refresh_button.set_sensitive(self.current_protocol() == "host2")
        self.show_toast(f"Directory received: {len(files)} file(s)", 3)
        return False

    def choose_file(self, _button):
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a file to send to CP/M")
        dialog.set_modal(True)

        last_dir = Path(str(self.settings.get("last_directory", Path.home()))).expanduser()
        if last_dir.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(last_dir)))

        dialog.open(self, None, self._on_file_opened)

    def _on_file_opened(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult):
        try:
            file_obj = dialog.open_finish(result)
        except GLib.Error as exc:
            # Gtk.FileDialog reports cancellation as a recoverable dialog error.
            # In normal use this is almost always the user pressing Cancel/Escape.
            # Avoid turning a cancelled file picker into an application error.
            if "dismiss" not in (exc.message or "").lower() and "cancel" not in (exc.message or "").lower():
                self.show_toast(f"Could not open file: {exc.message}", 5)
            return

        path = file_obj.get_path()
        if not path:
            self.show_toast("Please choose a local file.", 4)
            return

        self._select_file(path, update_directory=True)

    def _select_file(self, path: str, *, update_directory: bool = False) -> bool:
        p = Path(path).expanduser()
        if not p.is_file():
            self.show_toast(f"File is no longer available: {p.name}", 5)
            return False

        self.selected_file = str(p)
        if update_directory:
            self.settings["last_directory"] = str(p.parent)
            save_settings(self.settings)

        remote = cpm_filename(str(p))
        try:
            size = p.stat().st_size
        except OSError:
            size = 0

        self.file_row.set_title(p.name)
        self.file_row.set_subtitle(f"{p.parent}  •  {size:,} bytes")
        self.cpm_row.set_subtitle(remote)
        self._update_protocol_ui()
        self.log(f"Selected {p} ({size:,} bytes); suggested CP/M name {remote}")
        return True

    def _refresh_recent_files(self):
        recent = self.settings.get("recent_files", [])
        if not isinstance(recent, list):
            recent = []
        recent = [str(item) for item in recent if item][:5]

        self._updating_recent = True
        try:
            while self.recent_model.get_n_items() > 0:
                self.recent_model.remove(0)
            self.recent_paths = []

            for path in recent:
                p = Path(path)
                # Keep the visible entry compact.  The previous v2 display used
                # the entire path here; GTK quite reasonably gave that suffix all
                # the horizontal space it requested, which could squeeze the
                # ActionRow title down to a few pixels and make "Recent
                # transfers" render one character per line.
                parent_name = p.parent.name or str(p.parent)
                label = f"{p.name} — {parent_name}"
                self.recent_model.append(label)
                self.recent_paths.append(path)

            self.recent_dropdown.set_sensitive(bool(self.recent_paths))
            self.recent_dropdown.set_selected(Gtk.INVALID_LIST_POSITION)
            if self.recent_paths:
                self.recent_dropdown.set_tooltip_text(
                    "Choose one of the last five successfully transferred files"
                )
            else:
                self.recent_dropdown.set_tooltip_text("No successful transfers remembered yet")
        finally:
            self._updating_recent = False

    def _on_recent_changed(self, _dropdown, _pspec):
        if self._updating_recent:
            return
        idx = self.recent_dropdown.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self.recent_paths):
            return
        path = self.recent_paths[idx]
        if self._select_file(path, update_directory=True):
            self.recent_dropdown.set_tooltip_text(path)
        else:
            # Drop stale entries automatically if the file was moved/deleted.
            self._remove_recent_file(path)

    def _remove_recent_file(self, path: str):
        recent = [
            str(item) for item in self.settings.get("recent_files", [])
            if str(item) != str(path)
        ]
        self.settings["recent_files"] = recent[:5]
        save_settings(self.settings)
        self._refresh_recent_files()

    def _remember_recent_file(self, path: str):
        path = str(Path(path).expanduser())
        recent = [
            str(item) for item in self.settings.get("recent_files", [])
            if str(item) != path
        ]
        self.settings["recent_files"] = ([path] + recent)[:5]
        save_settings(self.settings)
        self._refresh_recent_files()

    def copy_command(self, _button):
        text = self.command_entry.get_text()
        if text and text != "Choose a file first":
            clipboard = self.get_clipboard()
            clipboard.set(text)
            self.show_toast("CP/M command copied")

    def start_transfer(self, _button):
        if self.worker and self.worker.is_alive():
            return
        if serial is None:
            self.show_toast("pySerial is required: sudo apt install python3-serial", 6)
            return
        if not self.selected_file or not os.path.isfile(self.selected_file):
            self.show_toast("Choose a file to send first.")
            return

        port = self.current_port()
        if not port:
            self.show_toast("Choose an SBC USB serial device.")
            return

        baud = self.current_baud()
        mode = self.current_protocol()
        remote_name = cpm_filename(self.selected_file)
        drive = self.current_drive() if mode == "host2" else None
        user = self.current_user() if mode == "host2" else None
        self.active_transfer_file = self.selected_file
        self.cancel_event.clear()
        self.progress.set_fraction(0.0)
        self.status_label.set_text("Opening USB device…")
        self.send_button.set_sensitive(False)
        self.directory_refresh_button.set_sensitive(False)
        self.cancel_button.set_sensitive(True)
        self.log(f"Opening {port} at {baud} baud using {mode.upper()} mode")

        self.worker = threading.Thread(
            target=self._transfer_worker,
            args=(port, baud, self.selected_file, mode, remote_name, drive, user),
            daemon=True,
        )
        self.worker.start()

    def cancel_transfer(self, _button):
        self.cancel_event.set()
        self.status_label.set_text("Cancelling…")

    def _transfer_worker(
        self, port: str, baud: int, filename: str, mode: str, remote_name: str,
        drive: Optional[int], user: Optional[int]
    ):
        serial_kwargs = dict(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.15,
            write_timeout=10,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

        # pySerial's POSIX-exclusive mode asks Linux for an exclusive lock.
        # This turns the common "Minicom still owns the port" case into an
        # immediate, understandable open error instead of a confusing read error.
        if os.name == "posix":
            serial_kwargs["exclusive"] = True

        try:
            try:
                ser = serial.Serial(**serial_kwargs)
            except TypeError:
                # Very old pySerial releases may not expose `exclusive`; keep
                # compatibility rather than refusing to run.
                serial_kwargs.pop("exclusive", None)
                ser = serial.Serial(**serial_kwargs)

            with ser:
                GLib.idle_add(self._ui_port_opened, port, baud, mode)
                common = dict(
                    on_log=lambda s: GLib.idle_add(self._ui_log, s),
                    on_progress=lambda done, total, stats: GLib.idle_add(
                        self._ui_progress, done, total, stats
                    ),
                    cancel_event=self.cancel_event,
                )
                if mode == "host2":
                    sender = HostLinkV2(ser, **common)
                    stats = sender.send_file(filename, remote_name, drive, user)
                elif mode == "host1":
                    sender = HostLinkSenderV1(ser, **common)
                    stats = sender.send_file(filename, remote_name)
                else:
                    sender = XModemSender(ser, **common)
                    stats = sender.send_file(filename)
                GLib.idle_add(self._ui_done, stats)
        except Exception as exc:
            GLib.idle_add(self._ui_error, self._friendly_serial_error(port, exc))

    @staticmethod
    def _friendly_serial_error(port: str, exc: Exception) -> str:
        raw = str(exc).strip() or exc.__class__.__name__
        low = raw.lower()

        if "permission denied" in low:
            return (
                f"Permission denied opening {port}. Check serial-device permissions "
                "(typically membership in the dialout group on Ubuntu)."
            )
        if (
            "device or resource busy" in low
            or "resource temporarily unavailable" in low
            or "could not exclusively lock" in low
            or "exclusive" in low and "lock" in low
        ):
            return (
                f"{port} is already in use. Close Minicom or any other program "
                "using the SBC USB port, then try again."
            )
        if "no such file or directory" in low:
            return (
                f"{port} is no longer present. Reconnect the SBC USB cable and "
                "press the refresh button."
            )
        if "device reports readiness to read but returned no data" in low:
            return (
                f"{port} opened, but the USB serial connection stopped returning "
                "data. Another program may still have the port open, or the USB "
                "device may have disconnected."
            )
        if "input/output error" in low or "i/o error" in low:
            return (
                f"I/O error communicating with {port}. Check the USB connection "
                "and make sure no other serial program is using it."
            )
        return raw

    def _ui_port_opened(self, port: str, baud: int, mode: str):
        if mode in ("host2", "host2-dir"):
            receiver = "HOST.COM v2"
        elif mode == "host1":
            receiver = "HOST.COM v1"
        else:
            receiver = "XMODEM receiver"
        self.status_label.set_text(f"USB device open — waiting for CP/M {receiver}…")
        self.log(f"USB device opened successfully: {port} at {baud} baud")
        return False

    def _ui_log(self, text: str):
        self.log(text)
        return False

    def _ui_progress(self, done: int, total: int, stats: TransferStats):
        fraction = 1.0 if total == 0 else min(1.0, done / total)
        self.progress.set_fraction(fraction)
        self.status_label.set_text(
            f"{fraction * 100:5.1f}% — block {stats.blocks_sent} — retries {stats.retries}"
        )
        return False

    def _ui_done(self, stats: TransferStats):
        self.progress.set_fraction(1.0)
        self.status_label.set_text("Transfer complete")
        self.log(
            f"Transfer complete: {stats.bytes_in_file:,} bytes, "
            f"{stats.blocks_sent} blocks, {stats.retries} retries, {stats.mode} mode"
        )
        if self.active_transfer_file:
            self._remember_recent_file(self.active_transfer_file)
        self.send_button.set_sensitive(True)
        self.cancel_button.set_sensitive(False)
        self.directory_refresh_button.set_sensitive(self.current_protocol() == "host2")
        self.show_toast("Transfer complete", 4)
        if self.current_protocol() == "host2":
            # HOST.COM returns to its ready loop after ACKing EOT. Give the
            # worker thread a moment to close the serial device, then refresh.
            GLib.timeout_add(350, self._refresh_directory_after_transfer)
        return False

    def _refresh_directory_after_transfer(self):
        if not (self.worker and self.worker.is_alive()):
            self.refresh_directory()
            return False
        return True

    def _ui_error(self, message: str):
        self.status_label.set_text("Operation failed")
        self.log("ERROR: " + message)
        self.send_button.set_sensitive(True)
        self.cancel_button.set_sensitive(False)
        self.directory_refresh_button.set_sensitive(self.current_protocol() == "host2")
        if self.current_protocol() == "host2":
            self.directory_status_row.set_subtitle(f"{self.target_label()} — refresh failed")
        if "cancelled" in message.lower():
            self.show_toast("Transfer cancelled")
        else:
            self.show_toast(f"Transfer failed: {message}", 6)
        return False


class HostLinkApplication(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        win = self.get_active_window()
        if win is None:
            win = MainWindow(self)
        win.present()


def main():
    Adw.init()
    app = HostLinkApplication()
    raise SystemExit(app.run(None))


if __name__ == "__main__":
    main()
