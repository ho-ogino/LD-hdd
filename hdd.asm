;----------------------------------------------------------------------------
; X1/turbo LSX-Dodgers SASI Access Program
; usage
;   hdd [[drive] [offset]]
;     drive: 0 - 3(HD0�`HD3)
;     offset: 2MB���Ƃ̃C���f�b�N�X(1��2MB�̈ʒu�A2��4MB�̈ʒu��擪�Ƃ��ď�������)
;
;note:
; LSX-Dodgers�ɂ�HDD���h���C�u�Ɋ��蓖�āA�ǂݏ������邽�߂̏풓�v���O�����ł��B
;
; 0x100����ǂݍ��܂��COM�t�@�C���Ƃ��ăo�C�i���o�͂���O��ō���Ă��܂��B
; 
; C900����SASI�h���C�u�ǂݏ����������z�u����ATPA�̖���(0x0006)��C900�ɓ������A
; C900�ɂ�JP 0xCC06(���X0x0006�ɏ�����Ă����A�h���X(LSX-Dodgers�̃V�X�e���̈�J�n�A�h���X)) ���������ŋ����ɏ풓�����Ă��܂��B
; ���������@��m�肽���Ƃ���ł��c�c(�����P�[�^�u���ɂ���̂���ǂ��̂ŌŒ�ɂ��Ă�̂��_���H)�B
;
; �܂��AF�h���C�u���`����DPB(�h���C�u�p�����[�^�u���b�N)��HDD�p�ɏ㏑�����Ă��邽�߁A
; ���̃v���O�������s��AF�h���C�u��HDD�Ƃ��ăA�N�Z�X�o����悤�ɂȂ�܂��B
; ���Ȃ݂� HD3: �����̃h���C�u�������I�ɃA�N�Z�X����悤�ɂ��Ă��܂�(�e�X�g�ł�)
;
; HDD�p��DPB�͌���FDD�p�̂��̂��R�s�[���Ă���A�f�B�X�N�T�C�Y��512KB�Ɍ�����悤�ɂȂ��Ă��܂��B
; ���A����ɑS�̈�A�N�Z�X�o���邩�m�F�ł��Ă��܂���B
; �\�[�X����HDD�pDPB�Ɂu���v��������̂́A���ۂ�HDD�p�ɐ��l��ݒ肵�Ȃ��Ƃ����Ȃ������ł����A
; ���������l���܂����ׂĂ��Ȃ��̂ŁA�b��I��FDD�Ɠ����l�����Ă���܂�(���[��c�c)�B
;
; �Ƃ肠�������̃v���O�����ɂ��A�G�~�����[�^�ɂ����ăt�H�[�}�b�g�A�t�@�C���̓ǂݏ������o���鎖��
; �m�F�ςł�(���A�O�q�̂Ƃ���DPB���e�L�g�[�Ȃ̂Ŏg����̈�͌����A�������g���邩���s��)�B
; �����܂Ō���A�e�X�g�p�v���O�����ƂȂ�܂��B
;




;----------------------------------------------------------------------------
;
TOP	equ	0xc700			; ORG�̓�
PROGSZ	equ	0x200			; �擪(0x100)����̔�풓���̃v���O�����T�C�Y
DPBADR	equ	0xEDA0			; F Drive DPB

;------------------------------------
		org	TOP+0x0000
;----------------------------------------------------------------------------
start:
	; �풓�`�F�b�N(�蔲���B0x0006��c900�ł���Ώ풓�ςƂ���c�c)
	ld	hl,0x0006
	ld	a,(hl)
	cp	LDSYS & 0xff
	jr	nz,registhddd
	inc	hl
	ld	a,(hl)
	cp	LDSYS/256
	jr	nz,registhddd
	jp	hdddcmd-TOP+0x100

registhddd;
;
	; DRIVE F ��DPB��HDD�̂��̂ŏ㏑�����Ă݂�
	ld	hl,hdddpb-TOP+0x100		; �R�s�[����HDD�pDPB
	ld	de,0xEDA0			; F Drive DPB
	ld	bc,22
	ldir

	; FAT�ǂݏ�������(GNCL, SNCL)�͋��ʂ̂��̂��g���悤�Ȃ̂ł�����F�h���C�u�̂��̂��c��
	ld	hl,hdddpb+26-TOP+0x100	; �R�s�[����HDD�pDPB + 26
	ld	de,0xEDA0+26			; F Drive DPB + 26
	ld	bc,6
	ldir

	; SASI�ǂݏ���������TPA�����t��(0xc900)�ɃR�s�[����
	ld	hl,LDSYS-TOP+0x100	; COM��0x100�ɓǂ܂��̂ŁA������LDSYSHDRDC�̐擪�ɂȂ�
	ld	de,0xc900
	ld	bc,endadr-LDSYS
	ldir

	; TPA������0xc900�ɓ�����(�s�C��)
	; �܂��͌���̒l���R�s�[
	ld	de,LDSYS+1
	ld	hl,0x0006
	ldi
	ldi

	; 0x0006�̒��g��0xc900�ɂ���
	ld	hl,0x0006
	ld	a,0
	ld	(hl),a
	inc	hl
	ld	a,0xc9
	ld	(hl),a

	; ����ŏ풓����

	; �R�}���h���C�����
	; hddd [drive(0-3) block(2MB�P��)]
hdddcmd:
	ld	de,0x005d	; arg1
	call	getnum-TOP+0x100
	jr	nc,hddcmd2
	; 0: Drive number error
	ld	c,0
	jp	hdisperr-TOP+0x100
hddcmd2:
	ld	a,l
	; 0-3 check
	cp	4
	jr	c,hddrvok
	; 0: Drive number error
	ld	c,0
	jp	hdisperr-TOP+0x100
hddrvok:
	; Set Unit NUmber( HDD DRIVE Number 0-3 (�͈̓`�F�b�N���ĂȂ��̂Œ���))
	ld	hl,DPBADR+0x13
	ld	(hl),a		; ������ł��������A����DPB�ʒu����������z�肵�Ă���̂�HL�ŏ������Ă���

	add	0x30		; �h���C�u��������̐������ύX���Ă���(HDD0�`HDD3)
	ld	hl,DPBADR+0x1f
	ld	(hl),a

	ld	de,0x006d	; arg2
	push	af
	call	getnum-TOP+0x100
	jr	nc,hddcmd3
	pop	af
	; 1: Offset number error
	ld	c,1
	jp	hdisperr-TOP+0x100
hddcmd3:
	pop	af

	; 0���擪�A1���擪����2MB�̈ʒu�c�c�ƁA�Ȃ�悤��HL�̒l�𒲐����Ă��B1��65536�Ȃ̂ŁA2MB�ɂ���ɂ�32�{���Ă��΂���(5�r�b�g�V�t�g�Ƃ��Ȃ��񂩂�)
	add	hl,hl	; 2
	add	hl,hl	; 4
	add	hl,hl	; 8
	add	hl,hl	; 16
	add	hl,hl	; 32
	ex	de,hl
	ld	hl,DPBADR+HDLBA1
	ld	(hl),e
	ld	hl,DPBADR+HDLBA2
	ld	(hl),d

	ld	de,hdrgmsg-TOP+0x100
	ld	c,0x09
	call	0005h

	ld	b,4
	ld	hl,DPBADR+0x1c
	ld	c,0x02
hddndsp:
	ld	e,(hl)
	call	0x0005
	inc	hl
	dec	b
	jr	nz,hddndsp

	; �I��
	jp	0

getnum:
	ld	hl,0
numloop:
	ld	a,(de)
	cp	0x20
	jr	z,cmdend
	
	sub	'0'
	jr	c,cmderr
	cp	10
	jr	nc,cmderr

;	hl��10�{�ɂ���(���񖳑ʂ����܂�������)
	add hl,hl	; x2
	push hl
	add hl,hl
	add hl,hl	; x8
	pop bc
	add hl,bc	; x10

	ld c,a		; hl + hl + a
	ld b,0
	add hl,bc

;	add a,l		; hl + hl + a	BC�c�u���Ȃ��ŁH�����Ƃ������@�����肻��
;	ld  l,a
;	ld  a,h
;	adc a,0
;	ld  h,a

	inc de
	jr numloop

cmderr:
	scf
cmdend;
	ret

; b = error number
hdisperr:
	ld	b,0
	sla	c	; c = c * 2
	ld	hl,hdermsg-TOP+0x100
	add	hl,bc
	ld	e,(hl)
	inc	hl
	ld	d,(hl)
	ld	c,0x09
	call	0005h
	scf
	jp	0

hdermsg:
	DW	hder1-TOP+0x100
	DW	hder2-TOP+0x100
hder1:
	db	7,'Invalid drive number$'
hder2:
	db	7,'Invalid offset number$'

hdrgmsg:
	db	'LD HDD controller v0.01', 0x0d,0x0a, '$'


	; HDD DPB�̃R�s�[��
hdddpb:
	DB	3	; +$00 FAT�̈�̃Z�N�^�P�ʂł̃T�C�Y(1byte) ���N���X�^����(1024 or 512(�N���X�^�T�C�Y))�~1.5�̐؂�グ�T�C�Y���K�v�H2048�Ȃ̂�3�ƂȂ�H
	DB	$F8	; +$01 ���f�B�A�o�C�g(HDD)
	DW	HDRDC	; +$02 HL���������܂�郁�����A�h���X�ADE��1��1kb�Ƃ���HDD�ǂݍ��݈ʒu�H
	DW	HDWTC	; +$04 HL���ǂݍ��݃������A�h���X�ADE��1��1kb�Ƃ���HDD�������݈ʒu�H
	DW	9	; +$06 �f�[�^�i�[�̈�̐擪�_���Z�N�^�ԍ�-2(1byte)�A�_���Z�N�^�ƃN���X�^�̊֌W(1byte)��0�H�H ���܂�擪����11KB�̈ʒu����f�[�^���i�[�����
	DW	2048	; +$08 ���N���X�^�� ��(FDD����356�����Ƃ肠����2048�ɂ��Ă݂��c�c)
	DB	0	; +$0A �t���b�s�[�f�B�X�N�̃��[�h(1byte)
	DB	11	; +$0B ���[�g�f�B���N�g���̈�̏I���̘_���Z�N�^�ԍ�+1(1byte) ���܂�擪����10KB�ڂ܂ł����[�g�f�B���N�g���̈�ƂȂ�
HDDBL:
HDLBA0		equ	$-hdddpb
	DB	0	; +$0C LBA0 / �t���b�s�[�f�B�X�N�̃V�����_��(1byte)
HDLBA1		equ	$-hdddpb
	DB	0	; +$0D LBA1 / �t���b�s�[�f�B�X�N��1�g���b�N�̃Z�N�^��(1byte)
	DB	1	; +$0E FAT�̈�̐擪�_���Z�N�^�ԍ�(1byte) ������O�͗\��(1KB)
	DB	1	; +$0F ���1�r�b�g0:1�N���X�^1024�o�C�g(1byte)
	DB	5	; +$10 ���[�g�f�B���N�g���̈�̐擪�_���Z�N�^�ԍ�(1byte) ���܂�擪����5KB�`10KB�ڂ܂ł����[�g�f�B���N�g���̈�ŁA
			; �̈悪5KB����̂ŁA5KB��32��160�t�@�C���u���鎖�ɂȂ�B���Ȃ݂�FAT/FAT�\���̒���ɂ���Ηǂ����ߗ\���Ȃ��Ȃ�3�ł���(�\������Ȃ�6�̕����������c�c)
HDLBA2		equ	$-hdddpb
	DB	1	; +$11 LBA2 / �t���b�s�[�f�B�X�N�̃Z�N�^�̍ŏ��l(1byte) �������g�p
	DB	9	; +$12 Device Number?? ���H
DPB_UNITNO	equ	$-hdddpb
HDDDV:	DB	3	; +$13 �f�o�C�X�h���C�o���ɂ����郆�j�b�g�ԍ�(1byte)
	DW	0	; +$14
	DW	0xe5fa ; GNCL ; +$16 FAT�̓��e��ǂݏo�����[�`���̎��s�A�h���X(2bytes)   �� F�h���C�u�̂��̂��c���̂Ŏg��Ȃ�(���A���̒l�Ő������͂�)
	DW	0xe621 ; SNCL ; +$18 FAT�Ƀf�[�^���������ރ��[�`���̎��s�A�h���X(2bytes) �� ����
	DW	0	; +$1A �J�����g�f�B���N�g���̃N���X�^�ԍ�(2bytes)
	DB	"HDD0"  ; +$1C

; �܂Ƃ�
;  0x0000 -   0x03ff : �\���̈�(1KB)
;  0x0400 -   0x0fff : FAT(3KB)
;  0x1000 -   0x1400 : ��(�\���H1KB)
;  0x1400 -   0x2bff : ���[�g�f�B���N�g���̈�(5KB)
;  0x2c00 - 0x1fffff : �f�[�^�i�[�̈�(2MB - 11KB)

;---------------------------------------------------------------------------
;SASI DRIVER
;---------------------------------------------------------------------------
	org	TOP+PROGSZ

; ���̃A�h���X��0x0006�ɏ�����Ă���B�{���̃V�X�e���R�[���A�h���X�ł���0xcc06�������ŌĂ�(�����̂�����)
LDSYS:
	JP	0xcc06		; ���̃A�h���X�͏풓���Ƀc�u�����

;---------------------------------------------------------------------------
;WRITE CRUSTER
;in
;	IX   : DPB
;	DE   : cruster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	DE   : next cruster
;	HL   : next address
;---------------------------------------------------------------------------
HDWTC:
	ld	a,0Ah			;WRITE(6)
;	jr	HDRWC
	db	0x01			;SKIP 2,BC
;---------------------------------------------------------------------------
;READ CRUSTER
;in
;	IX   : DPB
;	DE   : cruster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	DE   : next cruster
;	HL   : next address
;---------------------------------------------------------------------------
HDRDC:
	ld	a,08h			;READ(6)
HDRWC:
	push de
	push hl
	push	af			;SASI CMD
	; �Ƃ肠����HD3
;	ld	a,3
	ld	a,(ix+DPB_UNITNO)
	call	sasi_set_drive
; de��1kb�P�ʂ̐��l����sasi_*secs��256�o�C�g�P�ʂȂ̂�4�{����
	ex de,hl
	xor	a
	add hl,hl
	adc	a,a
	add hl,hl
	adc	a,a
;�p�[�e�B�V�����I�t�Z�b�g�����Z
	ld	e,(ix+$0C)	;LBA0/ �t���b�s�[�f�B�X�N�̃V�����_��
	ld	d,(ix+$0D)	;LBA1/ �t���b�s�[�f�B�X�N��1�g���b�N�̃Z�N�^��
	add	hl,de
	adc	a,(ix+$11)	;LBA2/ �t���b�s�[�f�B�X�N�̃Z�N�^�̍ŏ��l
;SASI�R�}���h���\�z
	ld	e,a		;EHL = sasi LBA
	ld	c,1024/256	;C   = block size
	pop	af		;SASI CMD
;	call	sasi_setup_read6
;	call	sasi_setup_write6
	call	sasi_setup_rw6	;READ(08) OR write(08)
;SASI�Z���N�V�����A�R�}���h�t�F�[�Y
	call	sasi_cmd6_open
	jr	c,sasi_err
;SASI�f�[�^�]��
	pop	hl		;HL = memory address
	push hl
	ld	de,1024		;DE = trasnfer size
	call	sasi_transfer
	jr	c,sasi_err
;SASI�X�e�[�^�X�A���b�Z�[�W�A�o�X�t���[
	call	sasi_close
	jr	c,sasi_err
	pop hl
	pop de
	inc h		;memory address��i�߂�
	inc h
	inc h
	inc h
	inc de		;�Z�N�^�ʒu��i�߂�
	xor a
	ret
;---------------------------------------------------------------------------
sasi_transfer:
	ld	a,(B_SASI_CMD6)
	cp	08h			;READ(6)?
	jp	z,sasi_rx		;READ
	jp	sasi_tx			;WRITE

;---------------------------------------------------------------------------
sasi_err:
	pop	hl
	pop	de
;	scf
	ret

;SASI�h���C�o�{��
	include	"sasi_poll.asm"
	include	"sasi_poll_tx.asm"
	include	"sasi_poll_rx.asm"

endadr:

;---------------------------------------------------------------------------
