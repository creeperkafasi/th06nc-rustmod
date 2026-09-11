#!/usr/bin/env python3
"""
pack_bgm.py -- build a th06 / "New Classic" BGM container from any audio file.

Container layout (all little-endian unless noted):

    0x00  u32  magic #1        0x80000001
    0x04  u32  chunk1 size     24
    0x08  u8   reserved        0
    0x09  u8   channels        1 or 2
    0x0a  u16  record size     488
    0x0c  u32  sample rate     48000   (loader enforces this)
    0x10  u32  chunk2 offset   32
    0x14  u32  0
    0x18  u32  0
    0x1c  u16  pre-skip        120 for low-delay CELT
    0x1e  u16  0
    0x20  u32  magic #2        0x80000004
    0x24  u32  payload size    = records * record_size
    0x28  ...  records

Record:  be32 packet length | 4 opaque bytes | Opus packet, zero padded
         so that every record is exactly `record_size` bytes.

Requires ffmpeg with libopus on PATH.

Examples
--------
    ./pack_bgm.py song.flac -o data/bgm/th06_02.opus
    ./pack_bgm.py --bitrate 160k --mono song.wav -o out.opus
    ./pack_bgm.py --info data/bgm/th06_01.opus
    ./pack_bgm.py --to-ogg data/bgm/th06_01.opus -o check.ogg
    ./pack_bgm.py song.flac -o out.opus --verify
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile

MAGIC1 = 0x80000001
MAGIC2 = 0x80000004
SAMPLE_RATE = 48000
SAMPLES_PER_PACKET = 960          # 20 ms @ 48 kHz
DEFAULT_RECORD_SIZE = 488
HEADER_SIZE = 40


# --------------------------------------------------------------------------
# Ogg reading
# --------------------------------------------------------------------------

def iter_ogg_pages(buf):
    """Yield (header_type, granule, serial, seq, segment_table, body)."""
    off = 0
    n = len(buf)
    while off < n:
        if n - off < 27 or buf[off:off + 4] != b'OggS':
            raise ValueError('not an Ogg page at offset %d' % off)
        if buf[off + 4] != 0:
            raise ValueError('unsupported Ogg version %d' % buf[off + 4])
        htype = buf[off + 5]
        granule, = struct.unpack_from('<Q', buf, off + 6)
        serial, seq, _crc = struct.unpack_from('<III', buf, off + 14)
        nseg = buf[off + 26]
        if n - off < 27 + nseg:
            raise ValueError('truncated Ogg segment table')
        segtab = buf[off + 27:off + 27 + nseg]
        body_off = off + 27 + nseg
        body_len = sum(segtab)
        if body_off + body_len > n:
            raise ValueError('truncated Ogg page body')
        yield htype, granule, serial, seq, segtab, buf[body_off:body_off + body_len]
        off = body_off + body_len


def ogg_packets(buf):
    """Reassemble the packets carried by an Ogg stream."""
    cur = bytearray()
    for _htype, _granule, _serial, _seq, segtab, body in iter_ogg_pages(buf):
        p = 0
        for seg in segtab:
            cur += body[p:p + seg]
            p += seg
            if seg < 255:
                yield bytes(cur)
                cur = bytearray()
    if cur:
        raise ValueError('truncated packet at end of Ogg stream')


def split_ogg_opus(data):
    """Return (OpusHead packet, OpusTags packet, [audio packets])."""
    pkts = list(ogg_packets(data))
    if len(pkts) < 2 or not pkts[0].startswith(b'OpusHead'):
        raise ValueError('not an Ogg Opus stream')
    if not pkts[1].startswith(b'OpusTags'):
        raise ValueError('missing OpusTags packet')
    return pkts[0], pkts[1], pkts[2:]


def parse_opus_head(pkt):
    if len(pkt) < 19 or not pkt.startswith(b'OpusHead'):
        raise ValueError('bad OpusHead')
    return {
        'version': pkt[8],
        'channels': pkt[9],
        'preskip': struct.unpack_from('<H', pkt, 10)[0],
        'input_rate': struct.unpack_from('<I', pkt, 12)[0],
        'gain': struct.unpack_from('<h', pkt, 16)[0],
        'mapping': pkt[18],
    }


def make_opus_head(channels, preskip, gain=0, mapping=0):
    h = bytearray(b'OpusHead')
    h += bytes([1, channels])
    h += struct.pack('<H', preskip)
    h += struct.pack('<I', SAMPLE_RATE)
    h += struct.pack('<h', gain)
    h += bytes([mapping])
    return bytes(h)


def make_opus_tags(vendor=b'pack_bgm.py'):
    t = bytearray(b'OpusTags')
    t += struct.pack('<I', len(vendor)) + vendor
    t += struct.pack('<I', 0)          # no user comments
    return bytes(t)


# --------------------------------------------------------------------------
# Ogg writing (only used by --to-ogg / --verify)
# --------------------------------------------------------------------------

_CRC_TABLE = []


def _crc_table():
    if _CRC_TABLE:
        return _CRC_TABLE
    for i in range(256):
        r = i << 24
        for _ in range(8):
            if r & 0x80000000:
                r = ((r << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                r = (r << 1) & 0xFFFFFFFF
        _CRC_TABLE.append(r)
    return _CRC_TABLE


def ogg_crc(data):
    tab = _crc_table()
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ tab[((crc >> 24) & 0xFF) ^ b]
    return crc


def lacing(n):
    segs = [255] * (n // 255) + [n % 255]
    return segs


def make_ogg_page(serial, seq, granule, segtab, body, htype=0):
    page = bytearray()
    page += b'OggS'
    page += bytes([0, htype])
    page += struct.pack('<Q', granule & 0xFFFFFFFFFFFFFFFF)
    page += struct.pack('<I', serial)
    page += struct.pack('<I', seq)
    page += b'\x00\x00\x00\x00'
    page += bytes([len(segtab)])
    page += bytes(segtab)
    page += body
    crc = ogg_crc(page)
    page[22:26] = struct.pack('<I', crc)
    return bytes(page)


def build_ogg(packets, channels, preskip, serial=0x54484F36, tags=None):
    """Rebuild a playable Ogg Opus stream from raw packets."""
    head = make_opus_head(channels, preskip)
    tags = tags or make_opus_tags()

    out = bytearray()
    seq = 0
    out += make_ogg_page(serial, seq, 0, lacing(len(head)), head, htype=0x02)
    seq += 1
    out += make_ogg_page(serial, seq, 0, lacing(len(tags)), tags, htype=0x00)
    seq += 1

    granule = preskip
    last = len(packets) - 1
    for i, pkt in enumerate(packets):
        granule += SAMPLES_PER_PACKET
        htype = 0x04 if i == last else 0x00
        out += make_ogg_page(serial, seq, granule, lacing(len(pkt)), pkt, htype)
        seq += 1
    return bytes(out)


# --------------------------------------------------------------------------
# Opus packet introspection
# --------------------------------------------------------------------------

_FRAME_SAMPLES_48K = [
    480, 960, 1920, 2880,       # SILK NB
    480, 960, 1920, 2880,       # SILK MB
    480, 960, 1920, 2880,       # SILK WB
    480, 960,                   # Hybrid SWB
    480, 960,                   # Hybrid FB
    120, 240, 480, 960,         # CELT NB
    120, 240, 480, 960,         # CELT WB
    120, 240, 480, 960,         # CELT SWB
    120, 240, 480, 960,         # CELT FB
]


def packet_samples(pkt):
    """Samples (at 48 kHz) produced by one Opus packet."""
    if not pkt:
        raise ValueError('empty Opus packet')
    toc = pkt[0]
    frame_samples = _FRAME_SAMPLES_48K[toc >> 3]
    code = toc & 3
    if code == 0:
        frames = 1
    elif code in (1, 2):
        frames = 2
    else:
        if len(pkt) < 2:
            raise ValueError('truncated Opus packet')
        frames = pkt[1] & 0x3F
    return frame_samples * frames


# --------------------------------------------------------------------------
# Container I/O
# --------------------------------------------------------------------------

def build_container(packets, channels, preskip, record_size=DEFAULT_RECORD_SIZE):
    if channels not in (1, 2):
        raise ValueError('channels must be 1 or 2')
    if record_size < 9:
        raise ValueError('record size must be at least 9')
    cap = record_size - 8

    body = bytearray()
    for i, pkt in enumerate(packets):
        if len(pkt) > cap:
            raise ValueError(
                'packet %d is %d bytes, but the record only holds %d '
                '(lower the bitrate or raise --record-size)'
                % (i, len(pkt), cap))
        body += struct.pack('>I', len(pkt))
        body += b'\x00\x00\x00\x00'          # packer bookkeeping, never read
        body += pkt
        body += b'\x00' * (cap - len(pkt))   # CBR padding

    payload_size = len(body)

    hdr = bytearray(HEADER_SIZE)
    struct.pack_into('<I', hdr, 0x00, MAGIC1)
    struct.pack_into('<I', hdr, 0x04, 24)
    hdr[0x08] = 0
    hdr[0x09] = channels
    struct.pack_into('<H', hdr, 0x0a, record_size)
    struct.pack_into('<I', hdr, 0x0c, SAMPLE_RATE)
    struct.pack_into('<I', hdr, 0x10, 32)
    struct.pack_into('<I', hdr, 0x14, 0)
    struct.pack_into('<I', hdr, 0x18, 0)
    struct.pack_into('<H', hdr, 0x1c, preskip)
    struct.pack_into('<H', hdr, 0x1e, 0)
    struct.pack_into('<I', hdr, 0x20, MAGIC2)
    struct.pack_into('<I', hdr, 0x24, payload_size)

    return bytes(hdr) + bytes(body)


def parse_container(data):
    if len(data) < HEADER_SIZE:
        raise ValueError('file is smaller than the 40-byte header')

    magic1, chunk1 = struct.unpack_from('<II', data, 0x00)
    if magic1 != MAGIC1:
        raise ValueError('bad magic #1: 0x%08x' % magic1)
    channels = data[0x09]
    record_size, = struct.unpack_from('<H', data, 0x0a)
    rate, = struct.unpack_from('<I', data, 0x0c)
    chunk2_off, = struct.unpack_from('<I', data, 0x10)
    preskip, = struct.unpack_from('<H', data, 0x1c)
    magic2, payload_size = struct.unpack_from('<II', data, 0x20)
    if magic2 != MAGIC2:
        raise ValueError('bad magic #2: 0x%08x' % magic2)
    if len(data) != HEADER_SIZE + payload_size:
        raise ValueError('file size %d != 40 + payload_size %d'
                         % (len(data), payload_size))
    if record_size < 8 or payload_size % record_size:
        raise ValueError('payload_size is not a multiple of the record size')

    frames = payload_size // record_size
    packets = []
    for i in range(frames):
        base = HEADER_SIZE + i * record_size
        length, = struct.unpack_from('>I', data, base)
        if 8 + length > record_size:
            raise ValueError('record %d declares an out-of-range length' % i)
        packets.append(data[base + 8:base + 8 + length])

    return {
        'channels': channels,
        'record_size': record_size,
        'rate': rate,
        'chunk1_size': chunk1,
        'chunk2_offset': chunk2_off,
        'preskip': preskip,
        'frames': frames,
        'payload_size': payload_size,
        'packets': packets,
    }


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------

def encode_to_ogg_opus(path, bitrate, application, channels, frame_duration):
    exe = shutil.which('ffmpeg')
    if not exe:
        raise SystemExit('ffmpeg not found in PATH')

    fd, tmp = tempfile.mkstemp(suffix='.ogg')
    os.close(fd)
    try:
        cmd = [
            exe, '-hide_banner', '-loglevel', 'error', '-y',
            '-i', path,
            '-map', 'a:0', '-vn',
            '-c:a', 'libopus',
            '-b:a', bitrate,
            '-vbr', 'off',
            '-application', application,
            '-frame_duration', str(frame_duration),
            '-ar', str(SAMPLE_RATE),
            '-ac', str(channels),
            '-f', 'ogg', tmp,
        ]
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr.decode('utf-8', 'replace'))
            raise SystemExit('ffmpeg failed (exit %d)' % proc.returncode)
        with open(tmp, 'rb') as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_pack(args):
    if not args.input:
        raise SystemExit('an input file is required')

    if args.no_reencode:
        with open(args.input, 'rb') as f:
            ogg = f.read()
    else:
        ogg = encode_to_ogg_opus(
            args.input, args.bitrate, args.application,
            args.channels, args.frame_duration)

    head_pkt, _tags, packets = split_ogg_opus(ogg)
    head = parse_opus_head(head_pkt)

    channels = head['channels']
    preskip = head['preskip']

    if channels not in (1, 2):
        raise SystemExit('encoder produced %d channels (must be 1 or 2)'
                         % channels)
    if head['input_rate'] not in (0, SAMPLE_RATE):
        sys.stderr.write('warning: OpusHead input rate is %d, not 48000\n'
                         % head['input_rate'])

    if not packets:
        raise SystemExit('no audio packets were produced')

    odd = 0
    for i, pkt in enumerate(packets):
        try:
            if packet_samples(pkt) != SAMPLES_PER_PACKET:
                odd += 1
        except ValueError:
            odd += 1
    if odd and not args.allow_odd_frame_sizes:
        sys.stderr.write(
            'warning: %d of %d packets are not 20 ms @ 48 kHz; the engine\'s\n'
            '         seek math assumes 960 samples per record.\n'
            % (odd, len(packets)))

    container = build_container(packets, channels, preskip, args.record_size)

    if args.verify:
        check = build_ogg(packets, channels, preskip)
        verify_ogg(check)

    out = args.output
    if not out:
        base, _ = os.path.splitext(args.input)
        out = base + '.opus'
    with open(out, 'wb') as f:
        f.write(container)

    duration = (len(packets) * SAMPLES_PER_PACKET - preskip) / SAMPLE_RATE
    bitrate = (len(container) * 8) / duration / 1000 if duration > 0 else 0
    print('%s: ch=%d rec=%d rate=%d preskip=%d frames=%d duration=%.2fs '
          'bitrate=%.0f kbps'
          % (out, channels, args.record_size, SAMPLE_RATE, preskip,
             len(packets), duration, bitrate))


def verify_ogg(ogg_bytes):
    exe = shutil.which('ffmpeg')
    if not exe:
        sys.stderr.write('warning: ffmpeg not available, skipping --verify\n')
        return
    fd, tmp = tempfile.mkstemp(suffix='.ogg')
    os.close(fd)
    try:
        with open(tmp, 'wb') as f:
            f.write(ogg_bytes)
        proc = subprocess.run(
            [exe, '-hide_banner', '-loglevel', 'error', '-i', tmp,
             '-f', 'null', '-'],
            capture_output=True)
        err = proc.stderr.decode('utf-8', 'replace').strip()
        if proc.returncode != 0 or err:
            sys.stderr.write(err + '\n')
            raise SystemExit('verify: ffmpeg reported decode errors')
        print('verify: ffmpeg decoded the stream cleanly')
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def cmd_info(args):
    with open(args.input, 'rb') as f:
        data = f.read()
    info = parse_container(data)
    duration = (info['frames'] * SAMPLES_PER_PACKET - info['preskip']) / SAMPLE_RATE
    bitrate = (len(data) * 8) / duration / 1000 if duration > 0 else 0
    print('file          : %s' % args.input)
    print('channels      : %d' % info['channels'])
    print('record size   : %d' % info['record_size'])
    print('sample rate   : %d' % info['rate'])
    print('pre-skip      : %d' % info['preskip'])
    print('chunk2 offset : %d' % info['chunk2_offset'])
    print('frames        : %d' % info['frames'])
    print('payload size  : %d' % info['payload_size'])
    print('duration      : %.2f s' % duration)
    print('bitrate       : %.0f kbps' % bitrate)

    lengths = sorted({len(p) for p in info['packets']})
    print('packet lengths: %s' % (lengths if len(lengths) < 8 else
                                  '%d distinct' % len(lengths)))


def cmd_to_ogg(args):
    with open(args.input, 'rb') as f:
        data = f.read()
    info = parse_container(data)
    ogg = build_ogg(info['packets'], info['channels'], info['preskip'])
    out = args.output
    if not out:
        base, _ = os.path.splitext(args.input)
        out = base + '.ogg'
    with open(out, 'wb') as f:
        f.write(ogg)
    print('%s: wrote %d bytes' % (out, len(ogg)))
    if args.verify:
        verify_ogg(ogg)


# --------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', nargs='?',
                    help='input audio file (or container file for '
                         '--info / --to-ogg)')
    ap.add_argument('-o', '--output', help='output file')

    ap.add_argument('--bitrate', default='192k',
                    help='Opus bitrate (default: 192k)')
    ap.add_argument('--application', default='lowdelay',
                    choices=['lowdelay', 'audio', 'voip'],
                    help='libopus application (default: lowdelay, matching '
                         'the shipped files\' 120-sample pre-skip)')
    ap.add_argument('--channels', type=int, default=2, choices=[1, 2],
                    help='output channel count (default: 2)')
    ap.add_argument('--mono', action='store_true',
                    help='shortcut for --channels 1')
    ap.add_argument('--frame-duration', type=int, default=20,
                    choices=[2, 5, 10, 20, 40, 60],
                    help='Opus frame duration in ms (default: 20)')
    ap.add_argument('--record-size', type=int, default=DEFAULT_RECORD_SIZE,
                    help='record size in bytes (default: 488)')
    ap.add_argument('--no-reencode', action='store_true',
                    help='input is already an Ogg Opus file; repack only')
    ap.add_argument('--allow-odd-frame-sizes', action='store_true',
                    help='do not warn about packets that are not 20 ms')

    ap.add_argument('--info', action='store_true',
                    help='print container information and exit')
    ap.add_argument('--to-ogg', action='store_true',
                    help='convert an existing container back to Ogg Opus')
    ap.add_argument('--verify', action='store_true',
                    help='decode the result with ffmpeg as a sanity check')

    args = ap.parse_args(argv)

    if args.mono:
        args.channels = 1

    if args.info:
        cmd_info(args)
    elif args.to_ogg:
        cmd_to_ogg(args)
    else:
        cmd_pack(args)


if __name__ == '__main__':
    main()