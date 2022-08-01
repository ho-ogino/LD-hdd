;----------------------------------------------------------------------------
;X1/turbo SASI handler
;
;ArdSCSino-stm32を使用する場合の"./scsi-config.txt"の設定
;　４行目(Mode): "1" (X1turbo)
;　５行目(Wait): "0" (0 usec)
;長いウェイトを設定すると動作しません
;----------------------------------------------------------------------------

;----------------------------------------------------------------------------
;X1turbo I/O assign
IO_SASI_DATA		equ	0fd0h
IO_SASI_STS		equ	0fd1h
IO_SASI_SEL_OFF		equ	0fd1h
IO_SASI_RESET		equ	0fd2h
IO_SASI_SEL_ON		equ	0fd3h

;STS reg. bit assign
IO_SASI_STS_MSG		equ	0x10
IO_SASI_STS_CXD		equ	0x08
IO_SASI_STS_IXO		equ	0x04
IO_SASI_STS_BSY		equ	0x02
IO_SASI_STS_REQ		equ	0x01

;----------------------------------------------------------------------------
;work area
;
B_SASI_ID:
	db	00h		;target ID byte
B_SASI_CMD_SIZE:
	db	6		;command length
;command data
B_SASI_CMD6:
	db	08h		;READ 08/WRITE 0A
B_SASI_CMD6_LUN_LAD2:
	db	00h		;LUN[2:0],LAD[20:16]
B_SASI_CMD6_LAD1:
	db	00h		;LAD[15: 8]
B_SASI_CMD6_LAD0:
	db	00h		;LAD[ 7: 0]
B_SASI_CMD6_NOB:
	db	00h		;number of blocks
B_SASI_CMD6_CTRL:
	db	00h		;control
	db	0,0,0,0,0,0,0,0,0,0	;max 16 bytes

;----------------------------------------------------------------------------
;SASI SELECT DRIVE & LUN
;ドライブ番号(ID,LUN)の設定
;in
;	 A : drive number (ID*2 +LUN)
sasi_set_drive:
	ld	b,a
	xor	a			;LUN=0
;drive number -> LUB,SASI-ID
	srl	b
	jr	nc,$+4
	ld	a,0x20		;LUN=1
	ld	(B_SASI_CMD6_LUN_LAD2),a
	ld	a,0x80		;SELECTION BITMASK
	inc	b
SASI_id_l:
	rlca
	djnz	SASI_id_l
;SASI ID set
;	or	0x80		;INITIATOR ID
	ld	(B_SASI_ID),a
	ret

;----------------------------------------------------------------------------
;setup SASI WRITE(6)
;in
;	EHL : LogicalBlockAddress (LBA[20:0])
;	 C  : number of blocks
sasi_setup_write6:
	ld	a,0Ah			;WRITE(6)
	jr	sasi_setup_rw6
;----------------------------------------------------------------------------
;setup SASI READ(6)
;in
;	EHL : LogicalBlockAddress (LBA[20:0])
;	 C  : number of blocks
sasi_setup_read6:
	ld	a,08h			;READ(6)
sasi_setup_rw6:
	ld	(B_SASI_CMD6),a
	ld	b,0x00					;CTRL byte
	ld	(B_SASI_CMD6_NOB),bc	;NOB,CTRL
;
	ld	a,(B_SASI_CMD6_LUN_LAD2);LUN/LAD2
	and	0xe0
	or	e
	ld	(B_SASI_CMD6_LUN_LAD2),a
	ld	a,l						;swap
	ld	l,h
	ld	h,a
	ld	(B_SASI_CMD6_LAD1),hl
;set command size
	ld	a,6
	ld	(B_SASI_CMD_SIZE),a
	ret

;----------------------------------------------------------------------------
;SASI OPEN BUS , send command
;in
;	HL : command data
;	A  : command size
;out
;	CF : 1=timeout error
;	A  : SASI BUS status
sasi_cmd_n_open:
	ld	(B_SASI_CMD_SIZE),a
sasi_cmd6_open:
;selection
	ld	a,(B_SASI_ID)
	ld	bc,IO_SASI_SEL_ON
	out	(c),a
;WAIT BUSY
	ld	de,IO_SASI_STS_BSY*0x0101
	ld	b,3				;タイムアウト短めに
	call	wait_sasi_3
	ld	a,c				;BUS STS
	ret	c
;selection off
	ld	a,high IO_SASI_SEL_OFF
	out	(low IO_SASI_SEL_OFF),a
;0A : CMD 
;0B : CMD & REQ
	ld	d,IO_SASI_STS_CXD | IO_SASI_STS_BSY | IO_SASI_STS_REQ
	call	wait_sasi
	jr	c,sasi_phase_error
;command tx
	ld	hl,B_SASI_CMD6
	ld	a,(B_SASI_CMD_SIZE)
	ld	e,a
	ld	d,0
	call	sasi_tx
	jr	c,sasi_phase_error
;07 : DATA_IN
;03 : DATA_OUT
;0F : STATUS
	ld	de,IO_SASI_STS_REQ*0x0101
	call	wait_sasi_2	;CF=tmeout error
	jr	c,sasi_phase_error
;data phase ok
get_scsi_bus_status:
	ld	a,high IO_SASI_STS
	in	a,(low IO_SASI_STS)
	and	1fh		;CF=0
	ret
;
sasi_phase_error:
	call	get_scsi_bus_status
	scf
	ret

;----------------------------------------------------------------------------
;sasi datain/dataout
;----------------------------------------------------------------------------

;----------------------------------------------------------------------------
;SASI CLOSE BUS , receive STATUS & MESSAGE
;out
;	CF  : 1=error
;	H   : message byte
;	L   : status byte
;	ZF  : 1= STS==MSG==0x00
;
sasi_close:
	ld	d,0Fh			;STATUS IN,REQ
	call	sasi_rx_phase_byte
	ret	c
	ld	l,a
	push	hl
	ld	d,1Fh			;MSG IN,REQ
	call	sasi_rx_phase_byte
	ret	c
	pop	hl
	ld	h,a
	or	l
	ret

;------------------------------------
;wait phase & receive byte
;in:
;	D : STS match byte
;out
;	CF  : 1=timeout
;	A   : SASI receive data
sasi_rx_phase_byte;
	call	wait_sasi
	ret	c
	ld	a,high IO_SASI_DATA
	in	a,(LOW IO_SASI_DATA)
	or	a		;CF=0
	ret

;----------------------------------------------------------------------------
;SASI 条件待ち合わせ
;in
;	D : STS match byte
wait_sasi:
	ld	e,1fh		;mask all bits
;in
;	E : STS mask byte
wait_sasi_2:
;	ld	b,3		;timeout count High
	ld	b,10		;timeout count High
;in
;	B : timeout time
;out
;	C  : BUS STS
;	CF : 1=timeout error,(reset bus)
wait_sasi_3:
	ld	hl,0		;timeout count low
wait_sasi_l:
	ld	a,high IO_SASI_STS
	in	a,(low IO_SASI_STS)
	ld	c,a			;save STS
	and	e			;mask
	cp	d			;comapre
	ret	z			;match
	dec	hl
	ld	a,h
	or	l
	jr	nz,wait_sasi_l
	djnz	wait_sasi_l
;	jr	sasi_reest

;----------------------------------------------------------------------------
;reset SASI BUS
sasi_reest:
	ld	bc,IO_SASI_SEL_OFF
	out	(c),a
	ld	bc,IO_SASI_RESET
	out	(c),a
	ld	b,0
	djnz	$
	scf
	ret
