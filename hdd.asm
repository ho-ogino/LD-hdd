;----------------------------------------------------------------------------
; X1/turbo LSX-Dodgers SASI Access Program
; usage
;   hdd [hdd drive] [offset] [target drive]
;     hdd drive: 0 - 3(HD0�`HD3)
;     offset: 2MB���Ƃ̃C���f�b�N�X(1��2MB�̈ʒu�A2��4MB�̈ʒu��擪�Ƃ��ď�������BVHD�̏ꍇ��0�Œ�ɂ��鎖)
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
; ������DPB(�h���C�u�p�����[�^�u���b�N)��HDD�p�ɏ㏑�����A
; ���̃v���O�������s��A�C�ӂ̎w��h���C�u��HDD�Ƃ��ăA�N�Z�X�o����悤�ɂȂ�܂��B
;
; HDD�p��DPB�ɂ��ẮAVHD�`���̃t�@�C������BPB(BIOS Parameter Block)����ǂݍ���œK�؂Ȓl��ݒ肵�܂��B
; BPB��������Ȃ��ꍇ�́A�f�t�H���g�ݒ�Ƃ���2MB������HDD����؂��Ďg����Ԃ�DPB���ݒ肳��܂��B
; 
; �����_�ŁA����ɑS�̈�A�N�Z�X�o���邩�m�F�ł��Ă��܂���B
; �����܂Ō���A�l���łƂȂ�܂��B
;

;----------------------------------------------------------------------------
; �f�t�H���gBPB���(BPB��񂪑��݂���ꍇ�͏㏑������邪�ABPB���ǂ߂Ȃ��ꍇ�͂��̒l���g����)
;----------------------------------------------------------------------------
; VHD(15MB)
; SCTSIZ		equ	512			; 1�Z�N�^�̃T�C�Y
; CSTSEC		equ	8			; 1�N���X�^�̃Z�N�^��(512*8�Ȃ̂�4096�o�C�g)
; RSVSEC		equ	8			; �\���̈�̃Z�N�^��
; NUMFAT		equ	2			; FAT�̐�
; ROOTCNT		equ	512			; ���[�g�G���g���̃f�B���N�g���G���g���̐�
; TOTALSEC	equ	30720			; �{�����[���̑��Z�N�^��
; FATSZ		equ	12			; 1��FAT����߂�Z�N�^��
; HIDSEC		equ	128			; (VHD��)�B���Z�N�^�T�C�Y
; 
; LDRSVSEC	equ	0x82			; LD�̗\��FAT�̈�̗L���Ƙ_���Z�N�^�T�C�Y(�\������A512�o�C�g)

;----------------------------------------------------------------------------
; 2MB offset(1�Z�N�^512�o�C�g��)
SCTSIZ		equ	512			; 1�Z�N�^�̃T�C�Y
CSTSEC		equ	2			; 1�N���X�^�̃Z�N�^��(512*2��1024�o�C�g)
RSVSEC		equ	2			; �\���̈�̃Z�N�^��
NUMFAT		equ	1			; FAT�̐�
ROOTCNT		equ	160			; ���[�g�G���g���̃f�B���N�g���G���g���̐�
TOTALSEC	equ	4096			; �{�����[���̑��Z�N�^��
FATSZ		equ	8			; 1��FAT����߂�Z�N�^��
HIDSEC		equ	0			; (VHD��)�B���Z�N�^�T�C�Y

LDRSVSEC	equ	0x02			; LD�̗\��FAT�̈�̗L���Ƙ_���Z�N�^�T�C�Y(�\���Ȃ��A512�o�C�g)

; LSX-Dodgers�p��BPB��񂩂�v�Z���Ă��
TOTALCST	equ	TOTALSEC/CSTSEC
FATHEAD		equ	HIDSEC+RSVSEC
ROOTHEAD	equ	FATHEAD+FATSZ*NUMFAT
ROOTSCNT	equ	ROOTCNT*32/SCTSIZ
DATAHEAD	equ	ROOTHEAD+ROOTSCNT-2	; �����̌v�Z�኱������(VHD�ł��Ɠ����Ȃ��Ǝv����̂�BPB����̌v�Z�̕��𐳂Ƃ��鎖)

; LSX-Dodgers�����R�[��
_BPB2DPB	equ	0xecf1

;----------------------------------------------------------------------------
;
TOP		equ	0xc600			; ORG�̓�
PROGSZ		equ	0x300			; �擪(0x100)����̔�풓���̃v���O�����T�C�Y
DPBTOP		equ	0xed00			; DPB�擪(A:)

;------------------------------------
		org	0x0100
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
	jp	hdddcmd

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
	ld	hl,0x0100+PROGSZ	; COM��0x100�ɓǂ܂��̂ŁA������LDSYSHDRDC�̐擪�ɂȂ�
	ld	de,0xc900
	ld	bc,endadr-LDSYS
	ldir

	ld	c,0x6f		;MSX-DOS �̃o�[�W�����ԍ��̊l��(_DOSVER)
	call	0x0005
	ld	hl,0x0138
	and	a
	sbc	hl,de
	jr	nc,hdddcmd

	ld	a,(0x000b)
	ld	(BPB2DPB+2),a
	ld	(b2dp1+2),a
	ld	(b2dp2+2),a
b2dp1:	ld	hl,(_BPB2DPB+1)
	ld	(BPB2DPB+1),hl
	ld	hl,bpb2dpb1
b2dp2:	ld	(_BPB2DPB+1),hl
	; ����ŏ풓����

	; �R�}���h���C�����
	; hdd [drive(0-3)] [block(2MB�P��)] [target drive(A-G)]
hdddcmd:
	; �R�}���h���C�������̕�����A-G�̏ꍇ�h���C�u���Ƃ��Ĉ����A�Ώۂ�DPB�ʒu����肷��
	; A-G�ł͂Ȃ��ꍇ�̓f�t�H���g H: �ŏ�������
	ld	hl,0x80
	ld	a,(hl)
	add	a,l		; hl + hl + a
	ld	l,a
	adc	a,h
	sub	l
	ld	h,a
	ld	a,(hl)
	or	0x20
	cp	'a'
	jr	c,hddcmd1	; A�ȉ���ASCII CODE�̏ꍇ�͖������ăf�t�H���gF:�Ƃ��ď�������(����)
	cp	'i'		; A,B,C,D,E,F,G,H�̂݋���
	jr	c,hddcmd3ok

	; 2: Target drive error
	ld	c,2
	jp	hdisperr
hddcmd3ok:
	and	0xdf
	; �\���p�Ƀh���C�u�������Ă���
	ld	(targetdrv),a

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
	ld	(dpbadr),hl

hddcmd1:
	; �C�ӂ̃h���C�u��DPB��HDD�̂��̂ŏ㏑��
	ld	hl,hdddpb		; �R�s�[����HDD�pDPB
	ld	de,(dpbadr)
	ld	bc,32
	ldir

	ld	de,0x005d	; arg1
	call	getnum
	jr	nc,hddcmd2
	; 0: Drive number error
	ld	c,0
	jp	hdisperr
hddcmd2:
	ld	a,l
	; 0-3 check
	cp	4
	jr	c,hddrvok
	; 0: Drive number error
	ld	c,0
	jp	hdisperr
hddrvok:
	; Set Unit Number( HDD DRIVE Number 0-3 )
	ld	ix,(dpbadr)
	ld	(ix+0x13),a

	push	af

	add	a,0x30		; �h���C�u��������̐������ύX���Ă���(HDD0�`HDD3)
	ld	(ix+0x1f),a

	ld	de,0x006d	; arg2
	call	getnum
	jr	nc,hddcmd3
	pop	af
	; 1: Offset number error
	ld	c,1
	jp	hdisperr
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

	ld	de,hdrgmsg
	ld	c,0x09
	call	0005h

	; �^�[�Q�b�g�h���C�u��\��
	ld	de,targetdrv
	ld	c,0x09
	call	0005h

	; ���蓖�Ă�HDD����\��
	ld	hl,(dpbadr)
	ld	bc,0x1c
	add	hl,bc
	ld	b,4
	ld	c,0x06
hddndsp:
	ld	e,(hl)
	call	0x0005
	inc	hl
	dec	b
	jr	nz,hddndsp

; BPB����DPB��ݒ肷��
; ���̎��_�łƂ肠����SASI�h���C�o�̏풓�͂���ł���̂ŁA���ڃh���C�o�̓ǂݏo���������g����BPB��ǂݍ���
	ld	a,(ix+DPB_UNITNO)
	call	sasi_set_drive

	ld	hl,0x0000		; IMG-�擪����ǂ�(BPB�擪)
	call	check_bpb
	jp	nc,0

	ld	hl,0x0100		; VHD-0x10000�o�C�g����ǂ�(BPB�擪�B�������擪64KB���B���Z�N�^�Œ�(�����̂���))
	call	check_bpb
	jp	nc,0

	ld	hl,0x007e		; IMG-0x07e00�o�C�g����ǂ�(MBR�t��)
	call	check_bpb
	jp	nc,0

	ld	hl,0x0098		; HDI-0x09800�o�C�g����ǂ�
	call	check_bpb
	jp	nc,0

	ld	hl,0x0120		; HDI-0x12000�o�C�g����ǂ�
	call	check_bpb
	jp	nc,0

	ld	hl,0x0202		; NHD-0x20200�o�C�g����ǂ�
	call	check_bpb
	jp	nc,0

	; BPB���I�t�ɂ���
	res	4,(ix+$12)	;DPB_01_DEVICE
	jp	0

check_bpb:
	push	hl
	ld	a,08h			;READ
	ld	e,0
	ld	c,1			; C   = block size(BPB��1�u���b�N=256�o�C�g�ȓ��ɓ���)
	call	sasi_setup_rw6
	call	sasi_cmd6_open
	jr	c,bpbrerr
;SASI�f�[�^�]��
	ld	hl,0x4000		; �e�L�g�[��0x4000����ǂ�(�󂢂Ă�͂�)
	ld	de,256			; DE = trasnfer size
	call	sasi_transfer
	jr	c,bpbrerr
;SASI�X�e�[�^�X�A���b�Z�[�W�A�o�X�t���[
	call	sasi_close
	jr	c,bpbrerr
	pop	hl

;	0x4000����BPB���ǂ܂�Ă���(�͂�)
bpbtodpb:
	ld	iy,$4000	; BPB

	ld	a,(iy+0)	;BS_JmpBoot
	cp	$eb
	jr	z,bpbeb
	cp	$e9
	jr	z,bpbok
	cp	$60		;X68K
	jr	z,bpbeb
notbpb:
	scf
	sbc	a,a
	ret

bpbrerr:
	pop	hl
	ei
	ld	c,3
	jp	hdisperr

bpbeb:
	ld	a,(iy+2)
	cp	0x90
	jr	nz,notbpb
bpbok:
	ld	a,(iy+12)	;BPB_BytsPerSec
	or	a
	jr	z,notbpb
	ld	b,a
	dec	b
	and	b
	jr	nz,notbpb

	ld	a,(iy+13)	;BPB_SecPerClus
	or	a
	jr	z,notbpb
	ld	b,a
	dec	b
	and	b
	jr	nz,notbpb

	set	5,(ix+$12)	;DPB_01_DEVICE
	; �B���Z�N�^�Ԃ��LBA�ɑ����Ă����Ă��
	ld	e,(ix+HDLBA0)
	ld	d,(ix+HDLBA1)
	xor	a
	add	hl,de
	adc	a,(ix+HDLBA2)
	ld	(ix+HDLBA0),l
	ld	(ix+HDLBA1),h
	ld	(ix+HDLBA2),a
	xor	a
	ret

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
	ld	hl,hdermsg
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
	DW	hder1
	DW	hder2
	DW	hder3
	DW	hder4
hder1:
	db	7,'Invalid drive number$'
hder2:
	db	7,'Invalid offset number$'
hder3:
	db	7,'Invalid target drive$'
hder4:
	db	7,0x0d,0x0a,'HDD BPB read error$'

hdrgmsg:
	db	'LD HDD controller v0.07', 0x0d,0x0a, '$'
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
	DW	FATSZ		; +$00 FAT�̈�̃Z�N�^�P�ʂł̃T�C�Y(1byte)
	DW	HDRDC		; +$02 HL���������܂�郁�����A�h���X�ADE��1��1kb�Ƃ���HDD�ǂݍ��݈ʒu�H
	DW	HDWTC		; +$04 HL���ǂݍ��݃������A�h���X�ADE��1��1kb�Ƃ���HDD�������݈ʒu�H
	DB	$F8		; +$06 ���f�B�A�o�C�g(HDD)
	DB	CSTSEC		; +$07 1�N���X�^�̘_���Z�N�^��(1,2,4,8,16,32,64,128�̂݉�) 0x08
	DW	TOTALCST	; +$08 ���N���X�^�� 0xefa
	DB	0		; +$0A �t���b�s�[�f�B�X�N�̃��[�h(1byte)
	DB	ROOTSCNT	; +$0B ���[�g�f�B���N�g���̈�̏I���̘_���Z�N�^�ԍ�+1(1byte) 0x40
HDDBL:
HDLBA0		equ	$-hdddpb
	DB	0		; +$0C LBA0 / �t���b�s�[�f�B�X�N�̃V�����_��(1byte)
HDLBA1		equ	$-hdddpb
	DB	0		; +$0D LBA1 / �t���b�s�[�f�B�X�N��1�g���b�N�̃Z�N�^��(1byte) �B���Z�N�^64KB���I�t�Z�b�g���Ă��
	DB	FATHEAD		; +$0E FAT�̈�̐擪�_���Z�N�^�ԍ�(1byte) 0x08
	DB	LDRSVSEC	; +$0F �\��FAT�̈�Ƙ_���Z�N�^�̃T�C�Y 0x82
				;	���1�r�b�g:�\��FAT�̈�
				;		1:�g�p����
				;		0:�g�p���Ȃ�
				;	����3�r�b�g: �_���Z�N�^�̃T�C�Y
				;		1:256�o�C�g
				;		2:512�o�C�g
				;		4:1024�o�C�g
	DW	ROOTHEAD	; +$10 ���[�g�f�B���N�g���̈�̐擪�_���Z�N�^�ԍ�(1byte) 0x20
	DB	$29		; +$12 Device Number?? ��
DPB_UNITNO	equ	$-hdddpb
HDDDV:	DB	3		; +$13 �f�o�C�X�h���C�o���ɂ����郆�j�b�g�ԍ�(1byte)
	DW	DATAHEAD	; +$14 �f�[�^�i�[�̈�̐擪�_���Z�N�^�ԍ�-2�N���X�^(1byte) 0x30
	DW	0		; 
HDLBA2		equ	$-hdddpb
	DW	0		; +$11 -> +$18 LBA2 / �t���b�s�[�f�B�X�N�̃Z�N�^�̍ŏ��l(1byte) ��LBA2�Ƃ��ė��p
	DW	0		; +$1A �J�����g�f�B���N�g���̃N���X�^�ԍ�(2bytes)
	DB	"HDD0"		; +$1C

;---------------------------------------------------------------------------
;SASI DRIVER
;---------------------------------------------------------------------------
	org	TOP+PROGSZ,PROGSZ

; ���̃A�h���X��0x0006�ɏ�����Ă���B�{���̃V�X�e���R�[���A�h���X�ł���0xcc06�������ŌĂ�(�����̂�����)
LDSYS:
	JP	0xcc06		; ���̃A�h���X�͏풓���Ƀc�u�����
BPB2DPB:
	JP	0

;---------------------------------------------------------------------------
;WRITE CLUSTER
;in
;	IX   : DPB
;	C DE : cluster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	C DE : next cluster
;	HL   : next address
;---------------------------------------------------------------------------
HDWTC:
	ld	a,0Ah			;WRITE(6)
	jr	HDRWC
;	db	0x01			;SKIP 2,BC ��BC���c�u���Ă͂����Ȃ��Ȃ����̂�jr���Ă���
;---------------------------------------------------------------------------
;READ CLUSTER
;in
;	IX   : DPB
;	C DE : cluster number
;	HL   : memory address
;out
;	CF   : 0=no error,1=error
;	C DE : next cluster
;	HL   : next address
;---------------------------------------------------------------------------
HDRDC:
	ld	a,08h			;READ(6)
HDRWC:
	push bc
	push de
	push hl
	push	af			;SASI CMD
	ld	a,(ix+DPB_UNITNO)
	push bc
	call	sasi_set_drive
; de�̒P�ʂɍ��킹��256�o�C�g�P�ʂ�sasi_*secs��n(1,2,4)�{����
	ex de,hl
	ld	a,c
	LD	c,(IX+00FH)	;DPB_0F_BPS(bit0-2:1:256�o�C�g 2:512�o�C�g 4:1024�o�C�g)
mulsec1:
	rr	c
	jr	c,mulsec2
	add hl,hl
	adc	a,a
	jr	mulsec1
mulsec2:
	pop bc
;�p�[�e�B�V�����I�t�Z�b�g�����Z
	ld	e,(ix+$0C)	;LBA0/ �t���b�s�[�f�B�X�N�̃V�����_��
	ld	d,(ix+$0D)	;LBA1/ �t���b�s�[�f�B�X�N��1�g���b�N�̃Z�N�^��
	add	hl,de
	adc	a,(ix+$18)	;LBA2/ �t���b�s�[�f�B�X�N�̃Z�N�^�̍ŏ��l
	jr	c,sasi_err2
;SASI�R�}���h���\�z
	ld	e,a		;EHL = sasi LBA
	add	a,$100-$e0	;SASI�̗��_�A�h���X��21�r�b�g�Ȃ̂Œ�������G���[
	jr	c,sasi_err2
	ld	a,(ix+0x0f)	;DPB_0F_BPS(bit0-2:1:256�o�C�g 2:512�o�C�g 4:1024�o�C�g)
	and	7
	ld	c,a		;C   = block size
	xor	a
mulblock:
	add	a,c
	djnz	mulblock
	ld	c,a
	ld	(tspat+2),a
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
tspat:	ld	de,0x200	;DE = trasnfer size
	call	sasi_transfer
	jr	c,sasi_err
;SASI�X�e�[�^�X�A���b�Z�[�W�A�o�X�t���[
	call	sasi_close
	jr	c,sasi_err
	pop hl
	pop de
	pop bc
	ld	a,(tspat+2)
	add	a,h		;memory address��i�߂�
	ld	h,a
	ld	a,e		;�Z�N�^�ʒu��i�߂�
	add	a,b
	ld	e,a
	ld	a,d
	adc	a,0
	ld	d,a
	adc	a,c
	sub	d
	ld	c,a
	xor a
	ret
;---------------------------------------------------------------------------
sasi_transfer:
	ld	a,(B_SASI_CMD6)
	cp	08h			;READ(6)?
	jp	z,sasi_rx		;READ
	jp	sasi_tx			;WRITE

;---------------------------------------------------------------------------
sasi_err2:
	pop	af
sasi_err:
	pop	hl
	pop	de
	pop	bc
;	scf
	ret


bpb2dpb1:
	call	BPB2DPB
	ret	nc
	LD	A,(IY+0)	;BS_JmpBoot
	CP	0EBH		;Short jump
	JR	Z,bpb2k
	CP	0E9H		;Near jump
	SCF
	RET
bpb2k:				;1�Z�N�^2KB�΍�
	LD	A,(IY+12)	;BPB_BytsPerSec
	cp	8		;1�Z�N�^2KB
	scf
	ret	nz
	LD	A,(IX+00FH)	;DPB_0F_BPS
	xor	0x0c
	LD	(IX+00FH),A	;DPB_0F_BPS

	sla	(IX+7)		;DPB_07_SECPCL

	sla	(IX+0)		;DPB_00_FATLN
	rl	(IX+1)		;DPB_00_FATLN

	sla	(IX+00EH)	;DPB_0E_FATPS

	sla	(IX+010H)	;DPB_10_DIRPS
	rl	(IX+011H)

	sla	(IX+00BH)	;DPB_0B_DIRSCNT

	sla	(IX+014H)	;DPB_14_ADDCL16
	rl	(IX+015H)
	ret


;SASI�h���C�o�{��
	include	"sasi_poll.asm"
	include	"sasi_poll_tx.asm"
	include	"sasi_poll_rx.asm"

endadr:

;---------------------------------------------------------------------------