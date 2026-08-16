#!/usr/bin/env python3
"""Launch the dual-pane UI with a GVFS/SMB-safe CP/M GET implementation."""
from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import Optional

import s100_hostlink_dualpane as ui


def _receive_file_safe(
    self,
    remote_name: str,
    local_filename: str,
    drive: Optional[int],
    user: Optional[int],
    expected_size: int = 0,
):
    """Receive a CP/M file safely on local, GVFS, and SMB-backed folders.

    The older GET path unconditionally unlinked a nonexistent .part file and
    then used an atomic replace at completion. Some GVFS/SMB FUSE mounts return
    EOPNOTSUPP for one or both operations. Create the .part file exclusively
    before starting the remote GET, then fall back to copy+delete if rename is
    unsupported.
    """
    stats = ui.TransferStats(bytes_in_file=0, mode="HOST2/GET")
    destination = Path(local_filename)
    partial = destination.with_name(destination.name + ".part")

    if destination.exists():
        raise ui.XModemError(f"Destination already exists: {destination}")
    if partial.exists():
        raise ui.XModemError(
            f"Temporary receive file already exists: {partial}. "
            "Rename or remove it before retrying."
        )

    received = 0
    expected = 1
    remote_started = False
    eot_seen = False

    try:
        # Open the destination-side temporary file before telling HOST.COM to
        # start GET. This catches permissions/GVFS problems without stranding
        # the CP/M sender waiting for ACKs.
        with open(partial, "xb") as f:
            self._begin()
            target = self._format_target(drive, user)
            self.on_log(f"Receiving {remote_name} from CP/M {target}.")
            metadata = self._command_payload(
                self.CMD_GET,
                drive,
                user,
                remote_name=remote_name,
                file_size=expected_size,
            )
            self._send_packet_with_retry(self._packet(0, metadata), 0, stats)
            remote_started = True

            while True:
                self._check_cancel()
                ch = self._read_one(self.response_timeout)
                if ch is None:
                    self._cancel_remote()
                    raise ui.XModemError("Timed out while receiving the CP/M file")

                if ch == ui.EOT:
                    # Finish the CP/M transaction first. If local finalization
                    # later fails, the complete .part file is retained.
                    self.ser.write(bytes([ui.ACK]))
                    self.ser.flush()
                    eot_seen = True
                    stats.bytes_in_file = received
                    break

                if ch == ui.CAN:
                    second = self._read_one(1.0)
                    if second == ui.CAN:
                        raise ui.XModemError(
                            "HOST.COM could not read or send the requested CP/M file"
                        )
                    continue

                if ch != ui.SOH:
                    continue

                seq_b = self._read_exact(2, self.response_timeout)
                payload = self._read_exact(ui.BLOCK_SIZE, self.response_timeout)
                crc_b = self._read_exact(2, self.response_timeout)
                seq, comp = seq_b[0], seq_b[1]
                recv_crc = (crc_b[0] << 8) | crc_b[1]
                good = (
                    ((seq + comp) & 0xFF) == 0xFF
                    and ui.crc16_xmodem(payload) == recv_crc
                )

                if not good:
                    self.ser.write(bytes([ui.NAK]))
                    self.ser.flush()
                    stats.retries += 1
                    continue

                if seq == expected:
                    f.write(payload)
                    received += len(payload)
                    stats.blocks_sent += 1
                    expected = (expected + 1) & 0xFF
                    self.ser.write(bytes([ui.ACK]))
                    self.ser.flush()
                    total = expected_size if expected_size > 0 else received
                    self.on_progress(received, total, stats)
                    continue

                if seq == ((expected - 1) & 0xFF):
                    self.ser.write(bytes([ui.ACK]))
                    self.ser.flush()
                    stats.retries += 1
                    continue

                self.ser.write(bytes([ui.NAK]))
                self.ser.flush()
                stats.retries += 1

        # Normal filesystems: use a cheap same-directory rename.
        try:
            os.replace(partial, destination)
        except OSError as exc:
            fallback_errnos = {
                errno.EXDEV,
                errno.EPERM,
                errno.EACCES,
                getattr(errno, "EOPNOTSUPP", 95),
                getattr(errno, "ENOTSUP", 95),
            }
            if exc.errno not in fallback_errnos:
                raise

            self.on_log(
                "Destination filesystem does not support atomic rename; "
                "finishing with copy+delete instead."
            )
            created_destination = False
            try:
                with open(partial, "rb") as src, open(destination, "xb") as dst:
                    created_destination = True
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            except Exception:
                if created_destination:
                    try:
                        destination.unlink()
                    except OSError:
                        pass
                raise

            try:
                partial.unlink()
            except OSError as cleanup_exc:
                # The destination is valid; a stale .part file is annoying but
                # should not turn a successful transfer into a failure.
                self.on_log(
                    f"Receive completed, but temporary file cleanup failed: "
                    f"{cleanup_exc}"
                )

        self.on_log(
            f"Received {remote_name}: {received:,} bytes "
            f"({stats.blocks_sent} CP/M record(s)) -> {destination}."
        )
        return stats

    except Exception as exc:
        if remote_started and not eot_seen:
            try:
                self._cancel_remote()
            except Exception:
                pass
        if not eot_seen:
            try:
                partial.unlink()
            except OSError:
                pass
        elif partial.exists():
            # We have the whole file; preserve it and make the error actionable.
            raise ui.XModemError(
                f"CP/M transfer completed, but Linux could not finalize the file. "
                f"The complete receive is preserved as {partial}: {exc}"
            ) from exc
        raise


ui.HostLinkV2.CMD_GET = 3
ui.HostLinkV2.receive_file = _receive_file_safe

if __name__ == "__main__":
    ui.main()
