rem set TGT=scsitest
set TGT=hdd

set TOOL=..\..\tool
set PASMO=%TOOL%\pasmo\pasmo
set AASM=%TOOL%\aasm\aasm

%AASM% -t10 -l -Ilib -Idrawv2 -Ifmpcm -Isasi -IDRV_OPM8 -s %TGT%.asm
rem %TOOL%\hex2bin -s0x100 -e com -c %TGT%.hex
%TOOL%\hex2bin -s 0xc600 -e com -c %TGT%.hex

