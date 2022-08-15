;----------------------------------------------------------------------------
; X1/turbo LSX-Dodgers SASI Access Program
; usage
;   hdd [hdd drive] [offset] [target drive]
;     hdd drive: 0 - 3(HD0�`HD3)
;     offset: 2MB���Ƃ̃C���f�b�N�X(1��2MB�̈ʒu�A2��4MB�̈ʒu��擪�Ƃ��ď�������)
;     tareget drive: A-G ���蓖�Ă�h���C�u
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
;
; HDD�p��DPB�ɂ��ẮA�e��10MB��HDD��5�������A2MB���g���悤�ȗp�r��z�肵���l�ɂȂ��Ă���A�f�B�X�N�T�C�Y��2048KB�Ɍ�����悤�ɂȂ��Ă��܂��B
; ���A����ɑS�̈�A�N�Z�X�o���邩�m�F�ł��Ă��܂���B
; �����ނ�2MB�̗̈�͉��L�̂悤�Ɏg���܂��B
; 
;  0x0000 -   0x03ff : �\���̈�(1KB)
;  0x0400 -   0x0fff : FAT(3KB)
;  0x1000 -   0x13ff : ��(�\���H1KB)
;  0x1400 -   0x2bff : ���[�g�f�B���N�g���̈�(5KB)
;  0x2c00 - 0x1fffff : �f�[�^�i�[�̈�(2MB - 11KB)
;
; �Ƃ肠�������̃v���O�����ɂ��A�G�~�����[�^�ɂ����ăt�H�[�}�b�g�A�t�@�C���̓ǂݏ������o���鎖��
; �m�F�ςł�(���A�O�q�̂Ƃ���DPB�͂�����x�����Ă�����̂́A�������S�̈悪�g���邩���s��)�B
; �����܂Ō���A�l���łƂȂ�܂��B
;

;----------------------------------------------------------------------------
; BPB���
;----------------------------------------------------------------------------
; VHD(15MB)
SCTSIZ		equ	512			; 1�Z�N�^�̃T�C�Y
CSTSEC		equ	8			; 1�N���X�^�̃Z�N�^��(512*8�Ȃ̂�4096�o�C�g)
RSVSEC		equ	8			; �\���̈�̃Z�N�^��
NUMFAT		equ	2			; FAT�̐�
ROOTCNT		equ	512			; ���[�g�G���g���̃f�B���N�g���G���g���̐�
TOTALSEC	equ	30720			; �{�����[���̑��Z�N�^��
FATSZ		equ	12			; 1��FAT����߂�Z�N�^��
HIDSEC		equ	128			; (VHD��)�B���Z�N�^�T�C�Y

LDRSVSEC	equ	0x82			; LD�̗\��FAT�̈�̗L���Ƙ_���Z�N�^�T�C�Y(�\������A512�o�C�g)

;----------------------------------------------------------------------------
; 2MB offset(1�Z�N�^512�o�C�g�ŁB������)
; SCTSIZ		equ	512			; 1�Z�N�^�̃T�C�Y
; CSTSEC		equ	2			; 1�N���X�^�̃Z�N�^��(512*2��1024�o�C�g)
; RSVSEC		equ	2			; �\���̈�̃Z�N�^��
; NUMFAT		equ	1			; FAT�̐�
; ROOTCNT		equ	512			; ���[�g�G���g���̃f�B���N�g���G���g���̐�
; TOTALSEC	equ	4096		; �{�����[���̑��Z�N�^��
; FATSZ		equ	6			; 1��FAT����߂�Z�N�^��
; HIDSEC		equ	0			; (VHD��)�B���Z�N�^�T�C�Y

; LDRSVSEC	equ	0x04			; LD�̗\��FAT�̈�̗L���Ƙ_���Z�N�^�T�C�Y(�\���Ȃ��A1024�o�C�g)

; LSX-Dodgers�p��BPB��񂩂�v�Z���Ă��
TOTALCST	equ	TOTALSEC/CSTSEC
FATHEAD		equ	HIDSEC+RSVSEC
ROOTHEAD	equ	FATHEAD+FATSZ*NUMFAT
ROOTTAIL	equ	ROOTHEAD+ROOTCNT*32/SCTSIZ
DATAHEAD	equ	ROOTTAIL-2*CSTSEC

;----------------------------------------------------------------------------
;
TOP		equ	0xc700			; ORG�̓�
PROGSZ		equ	0x200			; �擪(0x100)����̔�풓���̃v���O�����T�C�Y
DPBTOP		equ	0xed00			; DPB�擪(A:)

;------------------------------------
		org	TOP+0x0000
;----------------------------------------------------------------------------
start:
	; �풓�`�F�b�N(�蔲���B0x0006��0xc900��������Ă���Ώ풓�ςƂ���c�c)
	ld	hl,0x0006
	ld	a,(hl)
	cp	LDSYS & 0xff
	jr	nz,registhddd
	inc	hl
	ld	a,(hl)
	cp	LDSYS/256
	jr	nz,registhddd
	jp	hdddcmd-TOP+0x100

registhddd:
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

	; SASI�ǂݏ���������TPA�����t��(0xc900)�ɃR�s�[����
	ld	hl,LDSYS-TOP+0x100	; COM��0x100�ɓǂ܂��̂ŁA������LDSYSHDRDC�̐擪�ɂȂ�
	ld	de,0xc900
	ld	bc,endadr-LDSYS
	ldir

	; ����ŏ풓����

	; �R�}���h���C�����
	; hddd [drive(0-3)] [block(2MB�P��)] [target drive(A-G)]
hdddcmd:
	; �R�}���h���C�������̕�����A-G�̏ꍇ�h���C�u���Ƃ��Ĉ����A�Ώۂ�DPB�ʒu����肷��
	; A-G�ł͂Ȃ��ꍇ�̓f�t�H���g H: �ŏ�������
	ld	hl,0x80
	ld	a,(hl)
	add	a,l		; hl + hl + a
	ld	l,a
	ld	a,h
	adc	a,0
	ld	h,a
	ld	a,(hl)
	or	0x20
	cp	'a'
	jr	c,hddcmd1	; A�ȉ���ASCII CODE�̏ꍇ�͖������ăf�t�H���gF:�Ƃ��ď�������(����)
	cp	'h'		; A,B,C,D,E,F,G�̂݋���
	jr	c,hddcmd3ok

	; 2: Target drive error
	ld	c,2
	jp	hdisperr-TOP+0x100
hddcmd3ok:
	and	0xdf
	; �\���p�Ƀh���C�u�������Ă���
	ld	(targetdrv-TOP+0x100),a

	; �w��h���C�u��DPB�A�h���X���Z�o����dpbadr�ɓ����
	sub	'A'
	ld	l,a
	ld	h,0
	add	hl,hl		; 2
	add	hl,hl		; 4
	add	hl,hl		; 8
	add	hl,hl		; 16
	add	hl,hl		; 32
	ld	bc,DPBTOP
	add	hl,bc
	ld	(dpbadr-TOP+0x100),hl

hddcmd1:
	; �C�ӂ̃h���C�u��DPB��HDD�̂��̂ŏ㏑��
	ld	hl,hdddpb-TOP+0x100		; �R�s�[����HDD�pDPB
	ld	de,(dpbadr-TOP+0x100)
	ld	bc,32
	ldir

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
	; Set Unit Number( HDD DRIVE Number 0-3 )
	ld	ix,(dpbadr-TOP+0x100)
	ld	(ix+0x13),a

	push	af

	add	a,0x30		; �h���C�u��������̐������ύX���Ă���(HDD0�`HDD3)
	ld	(ix+0x1f),a

	ld	de,0x006d	; arg2
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
	ld	(ix+HDLBA1),e
	ld	(ix+HDLBA2),d

	ld	de,hdrgmsg-TOP+0x100
	ld	c,0x09
	call	0005h

	; �^�[�Q�b�g�h���C�u��\��
	ld	de,targetdrv-TOP+0x100
	ld	c,0x09
	call	0005h

	; ���蓖�Ă�HDD����\��
	ld	hl,(dpbadr-TOP+0x100)
	ld	bc,0x1c
	add	hl,bc
	ld	b,4
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
cmdend:
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

; ���������Ώۂ�DPB�A�h���X(�f�t�H���g�h���C�u H:)
dpbadr:
	DW	0xede0

; �G���[���b�Z�[�W�Q
hdermsg:
	DW	hder1-TOP+0x100
	DW	hder2-TOP+0x100
	DW	hder3-TOP+0x100
hder1:
	db	7,'Invalid drive number$'
hder2:
	db	7,'Invalid offset number$'
hder3:
	db	7,'Invalid target drive$'

hdrgmsg:
	db	'LD HDD controller v0.06', 0x0d,0x0a, '$'
targetdrv:
	db	'H: $'


; ��VHD(15MB)��BPB���(�Q�l)
; BPB_BytsPerSec �o�C�g�P�ʂ̃Z�N�^�T�C�Y: 512
; BPB_SecPerClus �N���X�^���\������Z�N�^���H: 8 �� 4kb
; BPB_RsvdSecCnt �\��̈�̃Z�N�^��: 8
; BPB_NumFATs FAT�̐�: 2
; BPB_RootEntCnt ���[�g�f�B���N�g���Ɋ܂܂��f�B���N�g���G���g���̐�: 512
; BPB_TotSec16 �{�����[���̑��Z�N�^��: 30720
; BPB_Media: F8
; BPB_FATSz16 1��FAT����߂�Z�N�^��: 12
; BPB_SecPerTrk �g���b�N������̃Z�N�^��: 63
; BPB_NumHeads �w�b�h��: 255
; BPB_HiddSec �X�g���[�W��ł��̃{�����[���̎�O�ɑ��݂���B�ꂽ�����Z�N�^�̐�: 128 ��128*512=64KB�Ԃ�BPB��O�̗̈�
; BPB_TotSec32 �{�����[���̑��Z�N�^��: 0 ��BPB_TotSec16�����鎖

; HDD DPB�̃R�s�[��
hdddpb:
	DB	FATSZ		; +$00 FAT�̈�̃Z�N�^�P�ʂł̃T�C�Y(1byte)
	DB	$F8		; +$01 ���f�B�A�o�C�g(HDD)
	DW	HDRDC		; +$02 HL���������܂�郁�����A�h���X�ADE��1��1kb�Ƃ���HDD�ǂݍ��݈ʒu�H
	DW	HDWTC		; +$04 HL���ǂݍ��݃������A�h���X�ADE��1��1kb�Ƃ���HDD�������݈ʒu�H
	DB	DATAHEAD	; +$06 �f�[�^�i�[�̈�̐擪�_���Z�N�^�ԍ�-2�N���X�^(1byte)
	DB	CSTSEC		; +$07 1�N���X�^�̘_���Z�N�^��(1,2,4,8,16�̂݉�)
	DW	TOTALCST	; +$08 ���N���X�^��
	DB	0		; +$0A �t���b�s�[�f�B�X�N�̃��[�h(1byte)
	DB	ROOTTAIL	; +$0B ���[�g�f�B���N�g���̈�̏I���̘_���Z�N�^�ԍ�+1(1byte)
HDDBL:
HDLBA0		equ	$-hdddpb
	DB	0		; +$0C LBA0 / �t���b�s�[�f�B�X�N�̃V�����_��(1byte)
HDLBA1		equ	$-hdddpb
	DB	0		; +$0D LBA1 / �t���b�s�[�f�B�X�N��1�g���b�N�̃Z�N�^��(1byte)
	DB	FATHEAD		; +$0E FAT�̈�̐擪�_���Z�N�^�ԍ�(1byte)
	DB	LDRSVSEC	; +$0F �\��FAT�̈�Ƙ_���Z�N�^�̃T�C�Y
				;	���1�r�b�g:�\��FAT�̈�
				;		1:�g�p����
				;		0:�g�p���Ȃ�
				;	����4�r�b�g: �_���Z�N�^�̃T�C�Y
				;		2:512�o�C�g
				;		4:1024�o�C�g
	DB	ROOTHEAD	; +$10 ���[�g�f�B���N�g���̈�̐擪�_���Z�N�^�ԍ�(1byte)
HDLBA2		equ	$-hdddpb
	DB	0		; +$11 LBA2 / �t���b�s�[�f�B�X�N�̃Z�N�^�̍ŏ��l(1byte) ��LBA2�Ƃ��ė��p
	DB	9		; +$12 Device Number?? ���H
DPB_UNITNO	equ	$-hdddpb
HDDDV:	DB	3		; +$13 �f�o�C�X�h���C�o���ɂ����郆�j�b�g�ԍ�(1byte)
	DW	0		; +$14
	DW	0		; GNCL�f�t�H���g���g�� ; +$16 FAT�̓��e��ǂݏo�����[�`���̎��s�A�h���X(2bytes)   �� F�h���C�u�̂��̂��c���̂Ŏg��Ȃ�(���A���̒l�Ő������͂�)
	DW	0		; SNCL�f�t�H���g���g�� ; +$18 FAT�Ƀf�[�^���������ރ��[�`���̎��s�A�h���X(2bytes) �� ����
	DW	0		; +$1A �J�����g�f�B���N�g���̃N���X�^�ԍ�(2bytes)
	DB	"HDD0"		; +$1C

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
	ld	a,(ix+DPB_UNITNO)
	call	sasi_set_drive
; de��512�o�C�g�P�ʂ̐��l����sasi_*secs��256�o�C�g�P�ʂȂ̂�2�{����
	ex de,hl
	xor	a
	add hl,hl
	adc	a,a
;�p�[�e�B�V�����I�t�Z�b�g�����Z
	ld	e,(ix+$0C)	;LBA0/ �t���b�s�[�f�B�X�N�̃V�����_��
	ld	d,(ix+$0D)	;LBA1/ �t���b�s�[�f�B�X�N��1�g���b�N�̃Z�N�^��
	add	hl,de
	adc	a,(ix+$11)	;LBA2/ �t���b�s�[�f�B�X�N�̃Z�N�^�̍ŏ��l
;SASI�R�}���h���\�z
	ld	e,a		;EHL = sasi LBA
	ld	c,512/256	;C   = block size
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
	ld	de,512		;DE = trasnfer size
	call	sasi_transfer
	jr	c,sasi_err
;SASI�X�e�[�^�X�A���b�Z�[�W�A�o�X�t���[
	call	sasi_close
	jr	c,sasi_err
	pop hl
	pop de
	inc h		;memory address��i�߂�
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
