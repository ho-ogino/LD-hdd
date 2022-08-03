rem set TGT=scsitest
set TGT=hdd

set TOOL=..\..\tool
set PASMO=%TOOL%\pasmo\pasmo
set AILZ80=%TOOL%\AILZ80ASM

%AILZ80% -f -sym -lst -bin -i %TGT%.asm
if not %errorlevel% == 0 goto ERR
DEL %TGT%.com
ren %TGT%.bin %TGT%.com
goto END

:ERR
echo error!

:END
