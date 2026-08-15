; HOST.COM - S-100 Z80 SBC USB host-link receiver v2
; ---------------------------------------------------
; Companion CP/M program for the Linux S-100 Host Link GTK4 utility.
;
; Hardware (same direct USB path used by the working XMODEM setup):
;   data port   34H
;   status port 36H
;   RX ready    bit 7 active LOW
;   TX ready    bit 6 active LOW
;
; Host-link protocol v2 command block (CRC-protected block 0):
;   bytes 0-7   = "S100HST2"
;   byte  8     = command: 01H PUT, 02H DIR
;   byte  9     = target drive: 0=A ... 15=P, FF=current
;   byte 10     = target user:  0..15, FF=current
;   bytes 11-21 = raw CP/M 8.3 name for PUT (8+3, space padded)
;   bytes 22-25 = original host byte size, little endian (informational)
;
; PUT then uses normal 128-byte CRC/XMODEM-shaped data blocks 1..n + EOT.
; HOST temporarily selects the requested drive/user, creates the file there,
; then restores the drive/user that were active before the request.
;
; DIR returns raw CP/M directory entries to Linux in CRC-protected blocks.
; Each 128-byte reply contains four 32-byte directory entries. Unused slots
; begin with E5H. Linux ACKs each block; HOST finishes with EOT and restores
; the original drive/user. This preserves enough metadata for the GUI to
; merge extents, show CP/M record size, and display file attributes.
;
; The code is intentionally Intel-8080 compatible even though the SBC uses Z80.
; Build: python3 build_host.py HOST.ASM HOST.COM
;

        ORG     0100H

BDOS        EQU     0005H
USBDATA     EQU     034H
USBSTAT     EQU     036H
RXMASK      EQU     080H
TXMASK      EQU     040H

SOH         EQU     001H
EOT         EQU     004H
ACK         EQU     006H
NAK         EQU     015H
CAN         EQU     018H
CRC_REQ     EQU     043H       ; ASCII 'C'
ESC         EQU     01BH

CMD_PUT     EQU     001H
CMD_DIR     EQU     002H
CURRENT     EQU     0FFH

F_CONIN     EQU     001H
F_CONOUT    EQU     002H
F_PRINT     EQU     009H
F_CONST     EQU     00BH
F_SELDSK    EQU     00EH
F_CLOSE     EQU     010H
F_SFIRST    EQU     011H
F_SNEXT     EQU     012H
F_DELETE    EQU     013H
F_WRITE     EQU     015H
F_MAKE      EQU     016H
F_CURDSK    EQU     019H
F_DMA       EQU     01AH
F_USER      EQU     020H

START:
        LXI     D,MSG_BANNER
        CALL    PRINTSTR

READY_RESET:
        XRA     A
        STA     OPENFLAG
        STA     EXPECTED
        STA     CONTEXTFLAG

READY_ADVERTISE:
        LXI     D,MSG_READY
        CALL    PRINTSTR

READY_PULSE:
        MVI     A,CRC_REQ
        CALL    USBPUT
        LXI     B,0FFFFH

READY_WAIT:
        IN      USBSTAT
        ANI     RXMASK
        JZ      READY_GOTBYTE
        DCX     B
        MOV     A,B
        ORA     C
        JNZ     READY_WAIT

        CALL    CHECKQUIT
        ORA     A
        JNZ     EXIT
        JMP     READY_PULSE

READY_GOTBYTE:
        IN      USBDATA
        CPI     SOH
        JZ      COMMAND_PACKET
        CPI     CAN
        JZ      READY_RESET
        JMP     READY_WAIT

COMMAND_PACKET:
        CALL    RECVPACKET
        ORA     A
        JZ      COMMAND_VALID_PACKET
        MVI     A,NAK
        CALL    USBPUT

COMMAND_RETRY_WAIT:
        CALL    USBGET
        CPI     SOH
        JZ      COMMAND_PACKET
        CPI     CAN
        JZ      READY_RESET
        JMP     COMMAND_RETRY_WAIT

COMMAND_VALID_PACKET:
        LDA     RXSEQ
        ORA     A
        JNZ     COMMAND_BAD
        CALL    PARSECOMMAND
        ORA     A
        JNZ     COMMAND_BAD

        MVI     A,001H
        STA     EXPECTED
        MVI     A,ACK
        CALL    USBPUT

        LDA     COMMAND
        CPI     CMD_PUT
        JZ      PUT_BEGIN
        CPI     CMD_DIR
        JZ      DIR_BEGIN
        JMP     COMMAND_BAD

COMMAND_BAD:
        CALL    SENDCAN
        CALL    ABORTFILE
        CALL    RESTORECONTEXT
        LXI     D,MSG_BADHDR
        CALL    PRINTSTR
        JMP     READY_RESET

; ---------------------------------------------------------------------------
; PUT command - receive one file into the selected CP/M drive/user area.
; ---------------------------------------------------------------------------
PUT_BEGIN:
        LXI     D,MSG_RECV
        CALL    PRINTSTR
        CALL    PRINTNAME
        LXI     D,MSG_CRLF
        CALL    PRINTSTR
        JMP     ACTIVE_WAIT

ACTIVE_WAIT:
        CALL    USBGET
        CPI     SOH
        JZ      DATA_PACKET
        CPI     EOT
        JZ      FINISH_FILE
        CPI     CAN
        JZ      REMOTE_CANCEL
        JMP     ACTIVE_WAIT

DATA_PACKET:
        CALL    RECVPACKET
        ORA     A
        JNZ     DATA_NAK

        LDA     EXPECTED
        MOV     B,A
        LDA     RXSEQ
        CMP     B
        JZ      DATA_NEW

        MOV     A,B
        DCR     A
        MOV     B,A
        LDA     RXSEQ
        CMP     B
        JZ      DATA_ACK

DATA_NAK:
        MVI     A,NAK
        CALL    USBPUT
        JMP     ACTIVE_WAIT

DATA_NEW:
        MVI     C,F_DMA
        LXI     D,DMABUF
        CALL    BDOS

        MVI     C,F_WRITE
        LXI     D,FCB
        CALL    BDOS
        ORA     A
        JNZ     DISK_ERROR

        LDA     EXPECTED
        INR     A
        STA     EXPECTED

        MVI     A,'.'
        CALL    CONOUT

DATA_ACK:
        MVI     A,ACK
        CALL    USBPUT
        JMP     ACTIVE_WAIT

FINISH_FILE:
        CALL    CLOSEFILE
        CPI     0FFH
        JZ      CLOSE_ERROR
        MVI     A,ACK
        CALL    USBPUT
        CALL    RESTORECONTEXT
        LXI     D,MSG_DONE
        CALL    PRINTSTR
        JMP     READY_RESET

REMOTE_CANCEL:
        CALL    ABORTFILE
        CALL    RESTORECONTEXT
        LXI     D,MSG_CANCEL
        CALL    PRINTSTR
        JMP     READY_RESET

DISK_ERROR:
        CALL    SENDCAN
        CALL    ABORTFILE
        CALL    RESTORECONTEXT
        LXI     D,MSG_DISKERR
        CALL    PRINTSTR
        JMP     READY_RESET

CLOSE_ERROR:
        CALL    SENDCAN
        CALL    ABORTFILE
        CALL    RESTORECONTEXT
        LXI     D,MSG_CLOSEERR
        CALL    PRINTSTR
        JMP     READY_RESET

; ---------------------------------------------------------------------------
; DIR command - enumerate selected drive/user and return raw directory entries.
; ---------------------------------------------------------------------------
DIR_BEGIN:
        LXI     D,MSG_DIR
        CALL    PRINTSTR
        CALL    SENDDIRECTORY
        ORA     A
        JNZ     DIR_ERROR
        CALL    RESTORECONTEXT
        LXI     D,MSG_DIRDONE
        CALL    PRINTSTR
        JMP     READY_RESET

DIR_ERROR:
        CALL    SENDCAN
        CALL    RESTORECONTEXT
        LXI     D,MSG_DIRERR
        CALL    PRINTSTR
        JMP     READY_RESET

EXIT:
        CALL    ABORTFILE
        CALL    RESTORECONTEXT
        LXI     D,MSG_EXIT
        CALL    PRINTSTR
        RET

; ---------------------------------------------------------------------------
; Receive rest of an XMODEM-shaped packet after SOH has already arrived.
; A=0 valid, A=1 bad. Payload -> DMABUF, sequence -> RXSEQ.
; ---------------------------------------------------------------------------
RECVPACKET:
        CALL    USBGET
        STA     RXSEQ
        CALL    USBGET
        STA     RXCOMP

        LXI     H,DMABUF
        MVI     B,080H
RPK_DATA:
        CALL    USBGET
        MOV     M,A
        INX     H
        DCR     B
        JNZ     RPK_DATA

        CALL    USBGET
        STA     RXCRCHI
        CALL    USBGET
        STA     RXCRCLO

        LDA     RXSEQ
        MOV     B,A
        LDA     RXCOMP
        ADD     B
        INR     A
        JNZ     RPK_BAD

        LXI     D,DMABUF
        MVI     B,080H
        CALL    CRC16

        LDA     RXCRCHI
        CMP     H
        JNZ     RPK_BAD
        LDA     RXCRCLO
        CMP     L
        JNZ     RPK_BAD

        XRA     A
        RET
RPK_BAD:
        MVI     A,001H
        RET

; ---------------------------------------------------------------------------
; CRC-16/XMODEM. DE=buffer, B=length. Output HL=CRC.
; ---------------------------------------------------------------------------
CRC16:
        LXI     H,0000H
CRC_BYTE:
        LDAX    D
        XRA     H
        MOV     H,A
        MVI     C,008H
CRC_BIT:
        DAD     H
        JNC     CRC_NOXOR
        MOV     A,H
        XRI     010H
        MOV     H,A
        MOV     A,L
        XRI     021H
        MOV     L,A
CRC_NOXOR:
        DCR     C
        JNZ     CRC_BIT
        INX     D
        DCR     B
        JNZ     CRC_BYTE
        RET

; ---------------------------------------------------------------------------
; Parse v2 command metadata, save/select drive+user, and prepare PUT if needed.
; A=0 success, A=1 failure.
; ---------------------------------------------------------------------------
PARSECOMMAND:
        LXI     D,DMABUF
        LXI     H,MAGIC
        MVI     B,008H
PC_MAGIC:
        LDAX    D
        CMP     M
        JNZ     PC_FAIL
        INX     D
        INX     H
        DCR     B
        JNZ     PC_MAGIC

        LDA     DMABUF+8
        CPI     CMD_PUT
        JZ      PC_CMDOK
        CPI     CMD_DIR
        JNZ     PC_FAIL
PC_CMDOK:
        STA     COMMAND

        LDA     DMABUF+9
        CPI     CURRENT
        JZ      PC_DRIVEOK
        CPI     010H
        JNC     PC_FAIL
PC_DRIVEOK:
        LDA     DMABUF+10
        CPI     CURRENT
        JZ      PC_USEROK
        CPI     010H
        JNC     PC_FAIL
PC_USEROK:
        CALL    SAVECONTEXT
        CALL    SELECTTARGET

        LDA     COMMAND
        CPI     CMD_DIR
        JZ      PC_SUCCESS

        ; PUT: clear FCB and copy 11-byte raw CP/M filename.
        LXI     H,FCB
        MVI     B,024H
        XRA     A
PC_CLEARFCB:
        MOV     M,A
        INX     H
        DCR     B
        JNZ     PC_CLEARFCB

        LXI     D,DMABUF+11
        LXI     H,FCB+1
        MVI     B,00BH
PC_NAME:
        LDAX    D
        MOV     M,A
        INX     D
        INX     H
        DCR     B
        JNZ     PC_NAME

        LDA     FCB+1
        CPI     ' '
        JZ      PC_FAIL_RESTORE

        MVI     C,F_DELETE
        LXI     D,FCB
        CALL    BDOS

        MVI     C,F_MAKE
        LXI     D,FCB
        CALL    BDOS
        CPI     0FFH
        JZ      PC_FAIL_RESTORE

        MVI     A,001H
        STA     OPENFLAG
PC_SUCCESS:
        XRA     A
        RET

PC_FAIL_RESTORE:
        CALL    RESTORECONTEXT
PC_FAIL:
        MVI     A,001H
        RET

; ---------------------------------------------------------------------------
; Save, select and restore CP/M drive/user context.
; ---------------------------------------------------------------------------
SAVECONTEXT:
        MVI     C,F_CURDSK
        CALL    BDOS
        STA     SAVEDDRIVE

        MVI     E,0FFH
        MVI     C,F_USER
        CALL    BDOS
        STA     SAVEDUSER

        MVI     A,001H
        STA     CONTEXTFLAG
        RET

SELECTTARGET:
        LDA     DMABUF+9
        CPI     CURRENT
        JZ      ST_USER
        MOV     E,A
        MVI     C,F_SELDSK
        CALL    BDOS
ST_USER:
        LDA     DMABUF+10
        CPI     CURRENT
        RZ
        MOV     E,A
        MVI     C,F_USER
        CALL    BDOS
        RET

RESTORECONTEXT:
        LDA     CONTEXTFLAG
        ORA     A
        RZ

        LDA     SAVEDDRIVE
        MOV     E,A
        MVI     C,F_SELDSK
        CALL    BDOS

        LDA     SAVEDUSER
        MOV     E,A
        MVI     C,F_USER
        CALL    BDOS

        XRA     A
        STA     CONTEXTFLAG
        RET

; ---------------------------------------------------------------------------
; Directory sender. Search current selected drive/user with ????????.???.
; Pack four raw 32-byte CP/M directory entries into each 128-byte reply.
; A=0 success, A=1 failure.
; ---------------------------------------------------------------------------
SENDDIRECTORY:
        ; Clear wildcard FCB.
        LXI     H,FCB
        MVI     B,024H
        XRA     A
SD_CLEARFCB:
        MOV     M,A
        INX     H
        DCR     B
        JNZ     SD_CLEARFCB

        LXI     H,FCB+1
        MVI     B,00BH
        MVI     A,'?'
SD_WILD:
        MOV     M,A
        INX     H
        DCR     B
        JNZ     SD_WILD

        XRA     A
        STA     OUTCOUNT
        MVI     A,001H
        STA     TXSEQ
        CALL    CLEAROUTBUF

        MVI     C,F_DMA
        LXI     D,DIRBUF
        CALL    BDOS

        MVI     C,F_SFIRST
        LXI     D,FCB
        CALL    BDOS

SD_RESULT:
        CPI     0FFH
        JZ      SD_SEARCH_DONE
        STA     DIRINDEX
        CALL    COPYDIRENTRY

        LDA     OUTCOUNT
        INR     A
        STA     OUTCOUNT
        CPI     004H
        JNZ     SD_NEXT

        CALL    SENDDIRBLOCK
        ORA     A
        JNZ     SD_FAIL
        XRA     A
        STA     OUTCOUNT
        CALL    CLEAROUTBUF

SD_NEXT:
        MVI     C,F_SNEXT
        LXI     D,FCB
        CALL    BDOS
        JMP     SD_RESULT

SD_SEARCH_DONE:
        LDA     OUTCOUNT
        ORA     A
        JZ      SD_EOT
        CALL    SENDDIRBLOCK
        ORA     A
        JNZ     SD_FAIL

SD_EOT:
        CALL    SENDEOT
        RET
SD_FAIL:
        MVI     A,001H
        RET

CLEAROUTBUF:
        LXI     H,OUTBUF
        MVI     B,080H
        MVI     A,0E5H
COB_LOOP:
        MOV     M,A
        INX     H
        DCR     B
        JNZ     COB_LOOP
        RET

; A source index was saved in DIRINDEX. Destination slot = OUTCOUNT.
COPYDIRENTRY:
        LDA     OUTCOUNT
        ORA     A
        JZ      CDE_SLOT0
        CPI     001H
        JZ      CDE_SLOT1
        CPI     002H
        JZ      CDE_SLOT2
        LXI     H,OUTBUF+060H
        JMP     CDE_DEST
CDE_SLOT0:
        LXI     H,OUTBUF
        JMP     CDE_DEST
CDE_SLOT1:
        LXI     H,OUTBUF+020H
        JMP     CDE_DEST
CDE_SLOT2:
        LXI     H,OUTBUF+040H
CDE_DEST:
        PUSH    H

        LDA     DIRINDEX
        ANI     003H
        ADD     A
        ADD     A
        ADD     A
        ADD     A
        ADD     A
        MOV     E,A
        MVI     D,000H
        LXI     H,DIRBUF
        DAD     D
        XCHG

        POP     H
        MVI     B,020H
CDE_COPY:
        LDAX    D
        MOV     M,A
        INX     D
        INX     H
        DCR     B
        JNZ     CDE_COPY
        RET

; Send OUTBUF as one CRC packet, waiting for ACK and retrying on NAK/noise.
SENDDIRBLOCK:
        LXI     D,OUTBUF
        MVI     B,080H
        CALL    CRC16
        MOV     A,H
        STA     TXCRCHI
        MOV     A,L
        STA     TXCRCLO

        MVI     A,00AH
        STA     TXRETRY
SDB_RETRY:
        MVI     A,SOH
        CALL    USBPUT
        LDA     TXSEQ
        CALL    USBPUT
        LDA     TXSEQ
        CMA
        CALL    USBPUT

        LXI     H,OUTBUF
        MVI     B,080H
SDB_DATA:
        MOV     A,M
        CALL    USBPUT
        INX     H
        DCR     B
        JNZ     SDB_DATA

        LDA     TXCRCHI
        CALL    USBPUT
        LDA     TXCRCLO
        CALL    USBPUT

        CALL    USBGET
        CPI     ACK
        JZ      SDB_OK

        LDA     TXRETRY
        DCR     A
        STA     TXRETRY
        JNZ     SDB_RETRY
        MVI     A,001H
        RET
SDB_OK:
        LDA     TXSEQ
        INR     A
        STA     TXSEQ
        XRA     A
        RET

SENDEOT:
        MVI     A,00AH
        STA     TXRETRY
SEOT_RETRY:
        MVI     A,EOT
        CALL    USBPUT
        CALL    USBGET
        CPI     ACK
        JZ      SEOT_OK
        LDA     TXRETRY
        DCR     A
        STA     TXRETRY
        JNZ     SEOT_RETRY
        MVI     A,001H
        RET
SEOT_OK:
        XRA     A
        RET

; ---------------------------------------------------------------------------
; USB byte I/O. Ready bits are active LOW on the SBC interface.
; ---------------------------------------------------------------------------
USBGET:
UG_WAIT:
        IN      USBSTAT
        ANI     RXMASK
        JNZ     UG_WAIT
        IN      USBDATA
        RET

USBPUT:
        PUSH    PSW
UP_WAIT:
        IN      USBSTAT
        ANI     TXMASK
        JNZ     UP_WAIT
        POP     PSW
        OUT     USBDATA
        RET

SENDCAN:
        MVI     A,CAN
        CALL    USBPUT
        MVI     A,CAN
        CALL    USBPUT
        RET

; ---------------------------------------------------------------------------
; CP/M console helpers.
; ---------------------------------------------------------------------------
PRINTSTR:
        MVI     C,F_PRINT
        CALL    BDOS
        RET

CONOUT:
        PUSH    B
        PUSH    D
        PUSH    H
        MOV     E,A
        MVI     C,F_CONOUT
        CALL    BDOS
        POP     H
        POP     D
        POP     B
        RET

CHECKQUIT:
        PUSH    B
        PUSH    D
        PUSH    H
        MVI     C,F_CONST
        CALL    BDOS
        ORA     A
        JZ      CQ_NO
        MVI     C,F_CONIN
        CALL    BDOS
        CPI     'Q'
        JZ      CQ_YES
        CPI     'q'
        JZ      CQ_YES
        CPI     ESC
        JZ      CQ_YES
CQ_NO:
        XRA     A
        POP     H
        POP     D
        POP     B
        RET
CQ_YES:
        MVI     A,001H
        POP     H
        POP     D
        POP     B
        RET

; ---------------------------------------------------------------------------
; Display FCB filename.
; ---------------------------------------------------------------------------
PRINTNAME:
        LXI     H,FCB+1
        MVI     B,008H
PN_NAME:
        MOV     A,M
        ANI     07FH
        CPI     ' '
        JZ      PN_NAME_SKIP
        CALL    CONOUT
PN_NAME_SKIP:
        INX     H
        DCR     B
        JNZ     PN_NAME

        LDA     FCB+9
        ANI     07FH
        CPI     ' '
        JNZ     PN_EXT
        LDA     FCB+10
        ANI     07FH
        CPI     ' '
        JNZ     PN_EXT
        LDA     FCB+11
        ANI     07FH
        CPI     ' '
        JNZ     PN_EXT
        RET

PN_EXT:
        MVI     A,'.'
        CALL    CONOUT
        LXI     H,FCB+9
        MVI     B,003H
PN_EXT_LOOP:
        MOV     A,M
        ANI     07FH
        CPI     ' '
        JZ      PN_EXT_SKIP
        CALL    CONOUT
PN_EXT_SKIP:
        INX     H
        DCR     B
        JNZ     PN_EXT_LOOP
        RET

; ---------------------------------------------------------------------------
; File cleanup helpers.
; ---------------------------------------------------------------------------
CLOSEFILE:
        LDA     OPENFLAG
        ORA     A
        JZ      CF_NONE
        MVI     C,F_CLOSE
        LXI     D,FCB
        CALL    BDOS
        MOV     B,A
        XRA     A
        STA     OPENFLAG
        MOV     A,B
        RET
CF_NONE:
        XRA     A
        RET

ABORTFILE:
        LDA     OPENFLAG
        ORA     A
        RZ
        CALL    CLOSEFILE
        MVI     C,F_DELETE
        LXI     D,FCB
        CALL    BDOS
        XRA     A
        STA     OPENFLAG
        RET

; ---------------------------------------------------------------------------
; Data
; ---------------------------------------------------------------------------
MAGIC:          DB      'S100HST2'
MSG_BANNER:     DB      0DH,0AH,'S-100 HOST LINK V2.0',0DH,0AH
                DB      'Linux <-> CP/M drive/user host service',0DH,0AH,'$'
MSG_READY:      DB      0DH,0AH,'Ready for host commands. Press Q or ESC to quit.',0DH,0AH,'$'
MSG_RECV:       DB      0DH,0AH,'Receiving: $'
MSG_DONE:       DB      0DH,0AH,'Transfer complete.',0DH,0AH,'$'
MSG_DIR:        DB      0DH,0AH,'Sending directory to host...',0DH,0AH,'$'
MSG_DIRDONE:    DB      'Directory sent.',0DH,0AH,'$'
MSG_DIRERR:     DB      'Directory transfer failed.',0DH,0AH,'$'
MSG_CANCEL:     DB      0DH,0AH,'Operation cancelled by host.',0DH,0AH,'$'
MSG_BADHDR:     DB      0DH,0AH,'Invalid host-link v2 command.',0DH,0AH,'$'
MSG_DISKERR:    DB      0DH,0AH,'Disk write error; partial file removed.',0DH,0AH,'$'
MSG_CLOSEERR:   DB      0DH,0AH,'File close error; partial file removed.',0DH,0AH,'$'
MSG_EXIT:       DB      0DH,0AH,'HOST stopped.',0DH,0AH,'$'
MSG_CRLF:       DB      0DH,0AH,'$'

OPENFLAG:       DB      00H
CONTEXTFLAG:    DB      00H
COMMAND:        DB      00H
EXPECTED:       DB      00H
RXSEQ:          DB      00H
RXCOMP:         DB      00H
RXCRCHI:        DB      00H
RXCRCLO:        DB      00H
SAVEDDRIVE:     DB      00H
SAVEDUSER:      DB      00H
DIRINDEX:       DB      00H
OUTCOUNT:       DB      00H
TXSEQ:          DB      00H
TXRETRY:        DB      00H
TXCRCHI:        DB      00H
TXCRCLO:        DB      00H

FCB:            DS      36
DMABUF:         DS      128
DIRBUF:         DS      128
OUTBUF:         DS      128

        ; End of HOST.COM
