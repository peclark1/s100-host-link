#!/usr/bin/env python3
"""Build the GET-capable HOST v2.2 with CP/M file-management commands.

The repository's tested v2.1 PUT/DIR/GET source is retained as
HOST_SERIALIO_GET.ASM.gz.  This script expands that known-good source, applies a
small deterministic protocol patch for DELETE/RENAME/ATTR, writes readable
HOST22.ASM, and assembles HOST22.COM with the existing build_host.py assembler.

Protocol additions:
  command 04H DELETE  old 8.3 name at command bytes 11..21
  command 05H RENAME  old name at 11..21, new raw 8.3 name at 26..36
  command 06H ATTR    name at 11..21, flags at byte 37
                       bit 0 R/O, bit 1 SYS, bit 2 ARC

Metadata-only commands send the normal command-block ACK, then EOT on success
(or CAN CAN on a CP/M/BDOS failure).  The Linux side ACKs the EOT.
"""
from __future__ import annotations

import argparse
import gzip
import re
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
DEFAULT_SOURCE = HERE / "HOST_SERIALIO_GET.ASM.gz"
DEFAULT_ASM = HERE / "HOST22.ASM"
DEFAULT_COM = HERE / "HOST22.COM"


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected exactly one source match, found {count}")
    return text.replace(old, new, 1)


def regex_once(text: str, pattern: str, replacement: str, label: str, *, flags=0) -> str:
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise PatchError(f"{label}: expected exactly one source match, found {count}")
    return new_text


def patch_source(text: str) -> str:
    if "CMD_GET" not in text or "GET_BEGIN" not in text:
        raise PatchError(
            "Input is not the tested GET-capable HOST source; expected CMD_GET and GET_BEGIN."
        )

    # Make the tested build visibly distinguishable at the CP/M console.
    text = text.replace("v2.1", "v2.2")

    if "CMD_DELETE" not in text:
        text = regex_once(
            text,
            r"(^CMD_GET\s+EQU\s+003H[^\n]*\n)",
            r"\1CMD_DELETE  EQU     004H\nCMD_RENAME  EQU     005H\nCMD_ATTR    EQU     006H\n",
            "command constants",
            flags=re.MULTILINE,
        )

    if "F_RENAME" not in text:
        text = regex_once(
            text,
            r"(^F_DELETE\s+EQU\s+013H[^\n]*\n)",
            r"\1F_RENAME    EQU     017H\n",
            "BDOS rename constant",
            flags=re.MULTILINE,
        )
    if "F_ATTRIB" not in text:
        text = regex_once(
            text,
            r"(^F_USER\s+EQU\s+020H[^\n]*\n)",
            r"F_ATTRIB    EQU     01EH\n\1",
            "BDOS attribute constant",
            flags=re.MULTILINE,
        )

    # Dispatch new commands after GET and before the existing bad-command path.
    if "JZ      DELETE_BEGIN" not in text:
        text = regex_once(
            text,
            r"(\s+CPI\s+CMD_GET\s*\n\s+JZ\s+GET_BEGIN\s*\n)(\s+JMP\s+COMMAND_BAD)",
            r"\1        CPI     CMD_DELETE\n        JZ      DELETE_BEGIN\n        CPI     CMD_RENAME\n        JZ      RENAME_BEGIN\n        CPI     CMD_ATTR\n        JZ      ATTR_BEGIN\n\2",
            "command dispatch",
        )

    # Accept commands 01H through 06H.  Keep the original PC_CMDOK body intact.
    parse_pattern = (
        r"(?ms)(^\s*LDA\s+DMABUF\+8\s*\n)"
        r".*?"
        r"(^PC_CMDOK:\s*\n)"
    )
    parse_replacement = (
        "        LDA     DMABUF+8\n"
        "        CPI     CMD_PUT\n"
        "        JZ      PC_CMDOK\n"
        "        CPI     CMD_DIR\n"
        "        JZ      PC_CMDOK\n"
        "        CPI     CMD_GET\n"
        "        JZ      PC_CMDOK\n"
        "        CPI     CMD_DELETE\n"
        "        JZ      PC_CMDOK\n"
        "        CPI     CMD_RENAME\n"
        "        JZ      PC_CMDOK\n"
        "        CPI     CMD_ATTR\n"
        "        JNZ     PC_FAIL\n"
        "PC_CMDOK:\n"
    )
    text = regex_once(text, parse_pattern, parse_replacement, "PARSECOMMAND validation")

    # The GET-capable source already prepares an FCB for GET and PUT.  New file
    # operations also need that old-name FCB, but only PUT may delete/create the
    # destination before data transfer.  Guard the existing PUT make sequence.
    put_guard = (
        "        LDA     COMMAND\n"
        "        CPI     CMD_PUT\n"
        "        JNZ     PC_SUCCESS\n\n"
    )
    if put_guard not in text:
        text = regex_once(
            text,
            r"(^\s*MVI\s+C,F_DELETE\s*\n\s*LXI\s+D,FCB\s*\n\s*CALL\s+BDOS\s*\n)",
            put_guard + r"\1",
            "PUT-only create guard",
            flags=re.MULTILINE,
        )

    handlers = r"""
; ---------------------------------------------------------------------------
; Metadata-only file management commands.  PARSECOMMAND has already selected
; the requested drive/user and built FCB from command bytes 11..21.
; Success is reported with EOT (Linux ACKs it); BDOS failure is CAN CAN.
; ---------------------------------------------------------------------------
DELETE_BEGIN:
        MVI     C,F_DELETE
        LXI     D,FCB
        CALL    BDOS
        CPI     0FFH
        JZ      FILEOP_ERROR
        JMP     FILEOP_SUCCESS

RENAME_BEGIN:
        ; Rename FCB second filename starts at offset 16; drive byte remains 0.
        XRA     A
        STA     FCB+16
        LXI     D,DMABUF+26
        LXI     H,FCB+17
        MVI     B,00BH
RN_COPY:
        LDAX    D
        MOV     M,A
        INX     D
        INX     H
        DCR     B
        JNZ     RN_COPY

        LDA     FCB+17
        CPI     ' '
        JZ      FILEOP_ERROR
        MVI     C,F_RENAME
        LXI     D,FCB
        CALL    BDOS
        CPI     0FFH
        JZ      FILEOP_ERROR
        JMP     FILEOP_SUCCESS

ATTR_BEGIN:
        ; t1' (extension byte 1 high bit) = R/O.
        LDA     DMABUF+37
        ANI     001H
        JZ      AT_RO_CLEAR
        LDA     FCB+9
        ORI     080H
        STA     FCB+9
        JMP     AT_SYS
AT_RO_CLEAR:
        LDA     FCB+9
        ANI     07FH
        STA     FCB+9

AT_SYS:
        ; t2' = SYS.
        LDA     DMABUF+37
        ANI     002H
        JZ      AT_SYS_CLEAR
        LDA     FCB+10
        ORI     080H
        STA     FCB+10
        JMP     AT_ARC
AT_SYS_CLEAR:
        LDA     FCB+10
        ANI     07FH
        STA     FCB+10

AT_ARC:
        ; t3' = ARC under CP/M 3.  CP/M 2.2 formally reserves this bit.
        LDA     DMABUF+37
        ANI     004H
        JZ      AT_ARC_CLEAR
        LDA     FCB+11
        ORI     080H
        STA     FCB+11
        JMP     AT_APPLY
AT_ARC_CLEAR:
        LDA     FCB+11
        ANI     07FH
        STA     FCB+11

AT_APPLY:
        MVI     C,F_ATTRIB
        LXI     D,FCB
        CALL    BDOS
        CPI     0FFH
        JZ      FILEOP_ERROR

FILEOP_SUCCESS:
        CALL    SENDEOT
        ORA     A
        JNZ     FILEOP_LINK_ERROR
        CALL    RESTORECONTEXT
        JMP     READY_RESET

FILEOP_ERROR:
        CALL    SENDCAN
FILEOP_LINK_ERROR:
        CALL    RESTORECONTEXT
        JMP     READY_RESET

"""
    if "DELETE_BEGIN:" not in text:
        text = regex_once(
            text,
            r"(?m)^EXIT:\s*$",
            handlers + "EXIT:",
            "file-operation handlers",
        )

    return text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--asm", type=Path, default=DEFAULT_ASM)
    parser.add_argument("--com", type=Path, default=DEFAULT_COM)
    parser.add_argument("--no-build", action="store_true", help="write HOST22.ASM only")
    args = parser.parse_args()

    try:
        with gzip.open(args.source, "rt", encoding="ascii", errors="strict") as src:
            original = src.read()
        patched = patch_source(original)
    except (OSError, UnicodeError, PatchError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    args.asm.write_text(patched, encoding="ascii")
    print(f"Wrote {args.asm}")

    if args.no_build:
        return 0

    command = [
        sys.executable,
        str(HERE / "build_host.py"),
        str(args.asm),
        str(args.com),
    ]
    completed = subprocess.run(command, cwd=HERE)
    if completed.returncode:
        return completed.returncode
    print(f"Built {args.com}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
