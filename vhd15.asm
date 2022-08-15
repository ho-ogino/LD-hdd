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
; VHD�t�@�C����15MB��p
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
; VHD�t�@�C���̃������}�b�v�͈ȉ���z�肵�Ă��܂��B
;  0x00000 -   0x0ffff : 
;  0x10000 -   0x10fff : BPB��
;  0x11000 -   0x127ff : FAT(6KB)
;  0x12800 -   0x13fff : �\��FAT(6KB)
;  0x14000 -   0x17fff : ���[�g�f�B���N�g���̈�(16KB)
;  0x18000 -           : �f�[�^�i�[�̈�(2MB - 11KB)
;
; �Ƃ肠�������̃v���O�����ɂ��A�G�~�����[�^�ɂ����ăt�H�[�}�b�g�A�t�@�C���̓ǂݏ������o���鎖��
; �m�F�ςł�(���A�O�q�̂Ƃ���DPB�͂�����x�����Ă�����̂́A�������S�̈悪�g���邩���s��)�B
; �����܂Ō���A�l���łƂȂ�܂��B
;


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
;	ld	(ix+HDLBA1),e
;	ld	(ix+HDLBA2),d

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
	db	'LD VHD controller v0.00', 0x0d,0x0a, '$'
targetdrv:
	db	'H: $'


	; HDD DPB�̃R�s�[��
hdddpb:
	DB	12	; +$00 FAT�̈�̃Z�N�^�P�ʂł̃T�C�Y(1byte) ���N���X�^����(1024 or 512(�N���X�^�T�C�Y))�~1.5�̐؂�グ�T�C�Y���K�v�H2048�Ȃ̂�3�ƂȂ�H
	DB	$F8	; +$01 ���f�B�A�o�C�g(HDD)
	DW	HDRDC	; +$02 HL���������܂�郁�����A�h���X�ADE��1��1kb�Ƃ���HDD�ǂݍ��݈ʒu�H
	DW	HDWTC	; +$04 HL���ǂݍ��݃������A�h���X�ADE��1��1kb�Ƃ���HDD�������݈ʒu�H
	DB	32+32-8*2	; +$06 �f�[�^�i�[�̈�̐擪�_���Z�N�^�ԍ�-2(1byte)�A�_���Z�N�^�ƃN���X�^�̊֌W(1byte)��0�H�H ���܂�擪����11KB�̈ʒu����f�[�^���i�[�����
	DB	8	; +$07 1�N���X�^�̘_���Z�N�^��
	DW	3840-6	; +$08 ���N���X�^�� ��(FDD����356�����Ƃ肠����2048�ɂ��Ă݂��c�c)
	DB	0	; +$0A �t���b�s�[�f�B�X�N�̃��[�h(1byte)
	DB	32+32	; +$0B ���[�g�f�B���N�g���̈�̏I���̘_���Z�N�^�ԍ�+1(1byte) ���܂�擪����10KB�ڂ܂ł����[�g�f�B���N�g���̈�ƂȂ�
HDDBL:
HDLBA0		equ	$-hdddpb
	DB	0	; +$0C LBA0 / �t���b�s�[�f�B�X�N�̃V�����_��(1byte)
HDLBA1		equ	$-hdddpb
	DB	1	; +$0D LBA1 / �t���b�s�[�f�B�X�N��1�g���b�N�̃Z�N�^��(1byte)
	DB	8	; +$0E FAT�̈�̐擪�_���Z�N�^�ԍ�(1byte) ������O�͗\��(1KB)
	DB	$82	; +$0F �\��FAT�̈�Ƙ_���Z�N�^�̃T�C�Y
	DB	32	; +$10 ���[�g�f�B���N�g���̈�̐擪�_���Z�N�^�ԍ�(1byte) ���܂�擪����5KB�`10KB�ڂ܂ł����[�g�f�B���N�g���̈�ŁA
			; �̈悪5KB����̂ŁA5KB��32��160�t�@�C���u���鎖�ɂȂ�B���Ȃ݂�FAT/FAT�\���̒���ɂ���Ηǂ����ߗ\���Ȃ��Ȃ�3�ł���(�\������Ȃ�6�̕����������c�c)
HDLBA2		equ	$-hdddpb
	DB	0	; +$11 LBA2 / �t���b�s�[�f�B�X�N�̃Z�N�^�̍ŏ��l(1byte) ��LBA2�Ƃ��ė��p
	DB	9	; +$12 Device Number?? ���H
DPB_UNITNO	equ	$-hdddpb
HDDDV:	DB	3	; +$13 �f�o�C�X�h���C�o���ɂ����郆�j�b�g�ԍ�(1byte)
	DW	0	; +$14
	DW	0	; GNCL�f�t�H���g���g�� ; +$16 FAT�̓��e��ǂݏo�����[�`���̎��s�A�h���X(2bytes)   �� F�h���C�u�̂��̂��c���̂Ŏg��Ȃ�(���A���̒l�Ő������͂�)
	DW	0	; SNCL�f�t�H���g���g�� ; +$18 FAT�Ƀf�[�^���������ރ��[�`���̎��s�A�h���X(2bytes) �� ����
	DW	0	; +$1A �J�����g�f�B���N�g���̃N���X�^�ԍ�(2bytes)
	DB	"HDD0"  ; +$1C

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
; de��512b�P�ʂ̐��l����sasi_*secs��256�o�C�g�P�ʂȂ̂�2�{����
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
