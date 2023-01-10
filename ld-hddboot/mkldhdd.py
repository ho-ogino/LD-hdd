#!/usr/bin/env python3

import os

hdd_path = 'HDDBASE.vhd'
ldhd_path = 'ldhdd_boot.bin'

output_path = 'LDHDD.vhd'

if not os.path.exists(hdd_path):
	print('could not found: ' + hdd_path)
	exit(1)

if not os.path.exists(ldhd_path):
	print('could not found: ' + ldhd_path)
	exit(1)

iplsize = os.path.getsize(ldhd_path)
hddsize = os.path.getsize(hdd_path)

print('load ipl')
f = open(ldhd_path, 'rb')
ipldat = f.read(iplsize)
f.close();

print('load hdd')
f = open(hdd_path, 'rb')
f.seek(iplsize, os.SEEK_SET)
hdddat = f.read(hddsize - iplsize)
f.close();

f = open(output_path, 'wb')
f.write(ipldat)
f.write(hdddat)
f.close()

print('done!')


