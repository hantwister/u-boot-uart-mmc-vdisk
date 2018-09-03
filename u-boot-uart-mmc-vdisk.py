#!/usr/bin/env python

"""

Virtual Disk for MMC Partitions via U-Boot Serial/UART Interface

Copyright (C) 2018 Harrison Neal



This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.



Before running this program, ensure that your U-Boot target is waiting at a
U-Boot shell on your serial/UART interface. This program will try to send mmc
and md commands to the interface you specify, and reads back the results.

Usage: u-boot-uart-mmc-vdisk.py /path/to/mountpoint /dev/serial

The mountpoint will then hopefully contain one "file" for each partition
reported by U-Boot.

You can then try to mount a partition read-only with a command like:

mount -o loop,ro,norecovery /path/to/fuse/mountpoint/8 /path/to/second/mountpoint

If you have problems, it might be a simple case of changing a string/regex/
constant/... used by this program; try running the same commands this program
does in your U-Boot shell, and see if the syntax needs slight adjustment or if
your target doesn't like something else (i.e., the memory address chosen for
staging data, the time this program waits for a response, etc.).

Patience is a virtue - you'll be accessing your data at dial-up speeds. It may
take a few minutes to mount a partition, perform a directory listing of a large
directory, or read a file more than 100KB. This approach may not be your best
option, especially if you need byte-for-byte copies of entire partitions, or
very large amounts of data.

"""

import os, stat, errno, re
from sys import argv
from serial import Serial
from fusepy import FUSE, Operations, FuseOSError
from io import TextIOWrapper, BufferedRWPair
from binascii import unhexlify


class UbootMmc(Operations):
    def __init__(self, serial_device):
        ser = Serial(serial_device, 115200, timeout=0.1)
        self.dev = TextIOWrapper(BufferedRWPair(ser, ser), line_buffering=True)
        self.block_cache = {}
        self.next_fd = 0
        self.read_mmc_partitions()

    # Serial Code

    def read_mmc_partitions(self):
        self.dev.write(unicode('mmc info\n'))
        for line in self.dev.readlines():
            if line.startswith('Rd Block Len:'):
                self.block_size = int(re.sub('[^0-9]', '', line))
                print "MMC Blocksize: " + str(self.block_size)

        if not self.block_size:
            raise RuntimeError('Could not initialize: no blocksize information found')

        self.dev.write(unicode('mmc part\n'))
        ptrn = re.compile('^\s*(?P<number>\d+)\s+(?P<start>\d+)\s+(?P<length>\d+)\s+.*$')
        self.partitions = {}
        for line in self.dev.readlines():
            match = ptrn.match(line)
            if match:
                part = match.groupdict()
                for key in part:
                    part[key] = int(part[key])

                self.partitions[part['number']] = part
                print "Partition: " + str(part)

        if not len(self.partitions):
            raise RuntimeError('Could not initialize: no partition information found')

    def read_mmc_blocks(self, start, length):
        if not length:
            return ''

        print 'Trying to read %u blocks beginning at index %u' % (length, start)

        self.dev.write(unicode('mmc read 90000000 %x %x\n' % (start, length)))
        self.dev.write(unicode('md.b 90000000 %x\n' % (length * self.block_size)))

        ptrn = re.compile('^([0-9a-f]{8}):\s*([0-9a-f ]{47})\s*.{16}$')
        nextaddr = 0x90000000
        blockdata = ''

        for line in self.dev.readlines():
            match = ptrn.match(line)
            if match:
                thisaddr = int(match.group(1), 16)
                if thisaddr != nextaddr:
                    raise RuntimeError('Expected address 0x%x, got address 0x%x' % (nextaddr, thisaddr))

                blockdata += re.sub('\s', '', match.group(2))
                nextaddr += 0x10

        if not blockdata:
            raise RuntimeError('Could not read any memory output')

        return unhexlify(blockdata)

    # Caching Logic

    def read_and_cache_mmc_blocks(self, start, length):
        if not length:
            return ''

        read_data = self.read_mmc_blocks(start, length)

        for read_block in range(start, start + length):
            start_index = (read_block - start) * self.block_size
            end_index = start_index + self.block_size
            self.block_cache[read_block] = read_data[start_index:end_index]

        return read_data

    def get_mmc_blocks(self, start, length):
        if not length:
            return ''

        to_return = ''
        read_start_block = None

        for block in range(start, start + length):
            if block in self.block_cache:
                if read_start_block is not None:
                    toReturn += self.read_and_cache_mmc_blocks(read_start_block, block - read_start_block)
                    read_start_block = None

                to_return += self.block_cache[block]
            else:
                if read_start_block is None:
                    read_start_block = block

        if read_start_block is not None:
            to_return += self.read_and_cache_mmc_blocks(read_start_block, start + length - read_start_block)

        return to_return

    # FUSE Code

    def readdir(self, path, fh=None):
        if path != '/':
            raise FuseOSError(errno.ENOENT)

        to_return = ['.', '..']
        for part in self.partitions:
            to_return.append(str(part))
        return to_return

    def getattr(self, path, fh=None):
        st = dict(
            st_mode=0,
            st_nlink=0,
            st_size=0,
            st_ctime=0,
            st_mtime=0,
            st_atime=0)

        if path == '/':
            st['st_mode'] = stat.S_IFDIR | 0o555
            st['st_nlink'] = 2
        else:
            try:
                part_num = int(path[1:])
                if part_num not in self.partitions:
                    raise FuseOSError(errno.ENOENT)

                st['st_mode'] = stat.S_IFREG | 0o444
                st['st_nlink'] = 1
                st['st_size'] = self.partitions[part_num]['length'] * self.block_size

            except ValueError:
                raise FuseOSError(errno.ENOENT)

        return st

    def open(self, path, flags):
        accmode = os.O_RDONLY | os.O_WRONLY | os.O_RDWR
        if (flags & accmode) != os.O_RDONLY:
            raise FuseOSError(errno.EACCES)

        self.next_fd += 1
        return self.next_fd

    def read(self, path, size, offset, fh=None):
        try:
            part_num = int(path[1:])
            if part_num not in self.partitions:
                raise FuseOSError(errno.ENOENT)

            part = self.partitions[part_num]

            if offset >= (part['length'] * self.block_size):
                return ''

            start_block = (offset // self.block_size) + part['start']
            start_offset_from_block = offset % self.block_size

            end_byte = offset + size
            if end_byte > (part['length'] * self.block_size):
                end_byte = part['length'] * self.block_size

            end_block = (end_byte // self.block_size) + part['start']
            if end_byte % self.block_size > 0:
                end_block += 1

            block_length = end_block - start_block
            byte_length = end_byte - offset

            block_data = self.get_mmc_blocks(start_block, block_length)

            block_data = block_data[start_offset_from_block:]
            block_data = block_data[:byte_length]

            return block_data

        except ValueError:
            raise FuseOSError(errno.ENOENT)


def main(mount_point, serial_device):
    server = UbootMmc(serial_device)
    FUSE(server, mount_point, nothreads=True, foreground=True)


if __name__ == '__main__':
    if len(argv) < 3:
        print "Usage: " + argv[0] + " /path/to/mountpoint /dev/serial"
        exit()

    main(argv[1], argv[2])
