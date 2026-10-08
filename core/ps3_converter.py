"""
PS3 → Java Edition Minecraft World Converter
============================================
Converts PS3 Legacy Console Edition saves (GAMEDATA format) to Java Edition .mca region files.

Pipeline:
  1. Parse GAMEDATA index (Big-Endian container)
  2. For each MCR region file in the index:
     a. Read 1024-entry location table (Big-Endian)
     b. For each chunk slot:
        - Parse 12-byte chunk header (Big-Endian)
        - Decompress: zlib(raw deflate, wbits=-15) → then RLE decode if rle_flag
        - Detect payload format:
            * Starts with 0x0a 0x00 → Java NBT already (pass-through)
            * Starts with 0x00 0x0c → LCE version-12 compressed tile storage → decode to NBT
  3. Write output .mca files (Java Region format)
"""

import os
import sys
import struct
import zlib
import gzip
import time
import re
from pathlib import Path
try:
    from .legacy_nbt_writer import chunk_nbt
except ImportError:
    from legacy_nbt_writer import chunk_nbt

try:
    from .nbt_tools import Tag, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY, loads as nbt_loads, dumps as nbt_dumps, child as nbt_child, set_child as nbt_set_child, remove_child as nbt_remove_child
except ImportError:
    from nbt_tools import Tag, BYTE, SHORT, INT, LONG, FLOAT, DOUBLE, BYTE_ARRAY, STRING, LIST, COMPOUND, INT_ARRAY, LONG_ARRAY, loads as nbt_loads, dumps as nbt_dumps, child as nbt_child, set_child as nbt_set_child, remove_child as nbt_remove_child

def _list_compounds_to_tags(items):
    # Convert nbt_tools' raw compound-list values into anonymous Compound Tags
    # before passing them to the NBT writer. This preserves TileEntities/Entities.
    out=[]
    for item in (items or []):
        if isinstance(item, Tag):
            out.append(item)
        elif isinstance(item, list):
            out.append(Tag(COMPOUND, '', item))
    return out


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COMPRESSED_SECTION_HEIGHT = 128
BLOCKS_PER_SECTION = COMPRESSED_SECTION_HEIGHT * 16 * 16   # 32768
NIBBLES_PER_SECTION = BLOCKS_PER_SECTION // 2              # 16384
FULL_CHUNK_BLOCKS   = 256 * 16 * 16                        # 65536
FULL_CHUNK_NIBBLES  = FULL_CHUNK_BLOCKS // 2
WORLD_MIN_COORD = -432
WORLD_MAX_COORD = 432               # 32768

# ---------------------------------------------------------------------------
# RLE decoder (PS3 / LCE style)
# ---------------------------------------------------------------------------
def rle_decode(data: bytes, expected_size: int) -> bytearray:
    output = bytearray(expected_size)
    in_pos = 0
    out_pos = 0
    while in_pos < len(data) and out_pos < expected_size:
        current = data[in_pos]
        in_pos += 1
        if current == 255:
            if in_pos >= len(data):
                break
            count = data[in_pos]
            in_pos += 1
            if count < 3:
                count += 1
                for _ in range(count):
                    if out_pos < expected_size:
                        output[out_pos] = 255
                        out_pos += 1
            else:
                count += 1
                if in_pos >= len(data):
                    break
                value = data[in_pos]
                in_pos += 1
                for _ in range(count):
                    if out_pos < expected_size:
                        output[out_pos] = value
                        out_pos += 1
        else:
            output[out_pos] = current
            out_pos += 1
    return output


# ---------------------------------------------------------------------------
# LCE Sparse Nibble Storage decoder (4-nibble format: Lower/Upper Sky & BlockLight)
# ---------------------------------------------------------------------------
def set_nibble_in_sec(arr: bytearray, index: int, value: int):
    byte_idx = index >> 1
    value &= 0x0F
    if (index & 1) == 0:
        arr[byte_idx] = (arr[byte_idx] & 0xF0) | value
    else:
        arr[byte_idx] = (arr[byte_idx] & 0x0F) | (value << 4)


def set_nibble_in_sec(arr: bytearray, index: int, value: int):
    byte_idx = index >> 1
    value &= 0x0F
    if (index & 1) == 0:
        arr[byte_idx] = (arr[byte_idx] & 0xF0) | value
    else:
        arr[byte_idx] = (arr[byte_idx] & 0x0F) | (value << 4)


def decode_sparse_nibble(count: int, plane_indices: bytes, plane_data: bytes, supports_all_fifteen: bool = False) -> bytearray:
    nibbles = bytearray(NIBBLES_PER_SECTION)
    for y in range(128):
        p_idx = plane_indices[y]
        if p_idx == 128:
            continue
        if supports_all_fifteen and p_idx == 129:
            for xz in range(256):
                pos = (xz << 7) | y
                slot = pos >> 1
                if (pos & 1) == 0:
                    nibbles[slot] = (nibbles[slot] & 0xF0) | 15
                else:
                    nibbles[slot] = (nibbles[slot] & 0x0F) | (15 << 4)
            continue
        p_off = p_idx * 128
        plane = plane_data[p_off : p_off + 128]
        for xz in range(128):
            if xz < len(plane):
                val = plane[xz]
                v0 = val & 0x0F
                v1 = (val >> 4) & 0x0F
                pos0 = ((xz << 1) << 7) | y
                slot0 = pos0 >> 1
                if (pos0 & 1) == 0:
                    nibbles[slot0] = (nibbles[slot0] & 0xF0) | v0
                else:
                    nibbles[slot0] = (nibbles[slot0] & 0x0F) | (v0 << 4)
                pos1 = (((xz << 1) + 1) << 7) | y
                slot1 = pos1 >> 1
                if (pos1 & 1) == 0:
                    nibbles[slot1] = (nibbles[slot1] & 0xF0) | v1
                else:
                    nibbles[slot1] = (nibbles[slot1] & 0x0F) | (v1 << 4)
    return nibbles


# ---------------------------------------------------------------------------
# LCE chunk payload → Java NBT
# ---------------------------------------------------------------------------
def _decode_lce_block_pair(raw: int):
    """Decode one LCE 16-bit block+metadata value.

    LCE stores the block ID in the upper 12 bits and metadata in the low
    nibble.  The on-disk pair is little-endian.  The low 12 bits of the block
    ID are then masked to the 9-bit legacy block-id range used by LCE.
    0xFFFF is the unused/air sentinel found in palettes.
    """
    if raw == 0xFFFF:
        return 0, 0
    block = ((raw >> 4) & 0x0FFF) & 0x1FF
    meta = raw & 0x0F
    return block, meta


def _decode_v12_grid(grid_data: bytes, entry: int, section_index: int, tile_index: int):
    """Decode one LCE v12 4x4x4 grid.

    The format nibble is the high nibble of the second byte of the 16-bit
    grid index.  Formats 2/4/6/8 use 1/2/3/4 bits per block respectively.
    The packed bit planes are *not* a normal little-endian bit stream: LCE
    stores eight bytes per bit-plane and transposes the 64 block indices.
    """
    mode = (entry >> 8) >> 4
    b0 = entry & 0xFF
    b1 = (entry >> 8) & 0xFF
    # offset = (nybble3 << 8 | nybble0 << 4 | nybble1) * 4
    offset_units = ((b1 & 0x0F) << 8) | (b0 & 0xF0) | (b0 & 0x0F)
    # The middle expression above is equivalent to n0<<4 | n1.
    offset = offset_units * 4

    def raw_pair_at(pos):
        if pos + 2 > len(grid_data):
            raise ValueError(f'v12 section {section_index}: grid {tile_index} truncated')
        return struct.unpack_from('<H', grid_data, pos)[0]

    # Format 0: the grid index itself contains the block pair.
    if mode == 0:
        return [_decode_lce_block_pair(entry)] * 64

    # E/F are raw 128-byte grids. F has another 128 bytes of submerged/liquid
    # data which belongs to the second layer and is intentionally ignored here.
    if mode in (0xE, 0xF):
        if offset + 128 > len(grid_data):
            raise ValueError(f'v12 section {section_index}: raw grid out of range')
        return [_decode_lce_block_pair(raw_pair_at(offset + i * 2)) for i in range(64)]

    bits = {2: 1, 4: 2, 6: 3, 8: 4}.get(mode)
    if bits is None:
        raise ValueError(f'v12 section {section_index}: unsupported grid format {mode:X}')

    palette_count = 1 << bits
    palette_bytes = palette_count * 2
    packed_bytes = bits * 8
    end = offset + palette_bytes + packed_bytes
    if end > len(grid_data):
        raise ValueError(f'v12 section {section_index}: grid {tile_index} payload out of range')

    palette = [_decode_lce_block_pair(raw_pair_at(offset + i * 2)) for i in range(palette_count)]
    packed = grid_data[offset + palette_bytes:end]
    values = [None] * 64

    # This is the exact layout documented for LCE compressed grids:
    # for each of the 8 groups, each bit-plane occupies one byte.  The bits
    # inside a byte correspond to the 8 block positions from MSB to LSB.
    for i in range(8):
        for j in range(8):
            palette_index = 0
            mask = 0x80 >> j
            for k in range(bits):
                v = packed[i + k * 8]
                palette_index |= ((v & mask) >> (7 - j)) << k
            if palette_index >= palette_count:
                palette_index = 0
            values[i * 8 + j] = palette[palette_index]

    return values


def _decode_v12_section(sec: bytes, section_index: int = -1):
    """Decode one LCE v12 16x16x16 section.

    The on-disk grid order is YZX (gx*16 + gz*4 + gy), while each 4x4x4
    decoded grid is XZY (lx*16 + lz*4 + ly).  Java 1.12 section arrays are
    YZX (y*256 + z*16 + x).  When numpy is available we decode the palettes
    in Python and perform the 4096-position remap as one tensor transpose;
    this is dramatically faster on large PS3 worlds.  The pure-Python path
    remains as a compatibility fallback.
    """
    if len(sec) < 128:
        return bytearray(4096), bytearray(2048), 0

    indices = [struct.unpack_from('<H', sec, i * 2)[0] for i in range(64)]
    grid_data = sec[128:]

    decoded = []
    for tile_index, entry in enumerate(indices):
        decoded.append(_decode_v12_grid(grid_data, entry, section_index, tile_index))

    try:
        import numpy as np
        ids = np.empty((4, 4, 4, 4, 4, 4), dtype=np.uint8)
        meta = np.empty((4, 4, 4, 4, 4, 4), dtype=np.uint8)
        for ti, vals in enumerate(decoded):
            gx = (ti >> 4) & 3
            gz = (ti >> 2) & 3
            gy = ti & 3
            ids[gx, gz, gy] = np.asarray([v[0] for v in vals], dtype=np.uint8).reshape(4, 4, 4)
            meta[gx, gz, gy] = np.asarray([v[1] for v in vals], dtype=np.uint8).reshape(4, 4, 4)
        # source axes: gx,gz,gy,lx,lz,ly
        # target axes: gy,ly,gz,lz,gx,lx -> y,z,x
        blocks = ids.transpose(2, 5, 1, 4, 0, 3).reshape(4096)
        metas = meta.transpose(2, 5, 1, 4, 0, 3).reshape(4096)
        meta_bytes = (metas[0::2] & 0x0F) | ((metas[1::2] & 0x0F) << 4)
        return bytearray(blocks.tobytes()), bytearray(meta_bytes.tobytes()), int(np.count_nonzero(blocks))
    except Exception:
        pass

    out_blocks = bytearray(4096)
    out_meta = bytearray(2048)
    nonzero = 0
    for tile_index, values in enumerate(decoded):
        tx = (tile_index >> 4) & 3
        tz = (tile_index >> 2) & 3
        ty = tile_index & 3
        base_x, base_z, base_y = tx * 4, tz * 4, ty * 4
        for lx, lz, ly in ((lx, lz, ly) for lx in range(4) for lz in range(4) for ly in range(4)):
            b, m = values[(lx << 4) | (lz << 2) | ly]
            dst = (base_y + ly) * 256 + (base_z + lz) * 16 + base_x + lx
            out_blocks[dst] = b & 0xFF
            if b: nonzero += 1
            di = dst >> 1
            if dst & 1: out_meta[di] = (out_meta[di] & 0x0F) | ((m & 0x0F) << 4)
            else: out_meta[di] = (out_meta[di] & 0xF0) | (m & 0x0F)
    return out_blocks, out_meta, nonzero

def _decode_v12_nibbles(payload: bytes, off: int, count_sections: int = 4):
    """Read the sparse nibble storages following the v12 section blob.

    v12 stores six arrays: lower/upper block data, lower/upper SkyLight,
    lower/upper BlockLight.
    """
    raw = []
    for _ in range(count_sections):
        if off + 4 + 128 > len(payload):
            raise ValueError("Truncated v12 sparse nibble storage")
        count = struct.unpack_from('>i', payload, off)[0]
        off += 4
        plane_indices = payload[off:off + 128]
        off += 128
        size = count * 128
        if count < 0 or off + size > len(payload):
            raise ValueError("Invalid v12 sparse nibble storage size")
        plane_data = payload[off:off + size]
        off += size
        raw.append((count, plane_indices, plane_data))
    return raw, off


def decode_lce_chunk_payload(payload: bytes):
    """Decode LCE chunk payload versions 8/9/12 into Java 1.12.2 NBT."""
    version = struct.unpack_from('>h', payload, 0)[0]
    if version not in (8, 9, 12):
        raise ValueError(f"Unsupported LCE chunk version: {version}")
    chunk_x = struct.unpack_from('>i', payload, 2)[0]
    chunk_z = struct.unpack_from('>i', payload, 6)[0]
    last_update = struct.unpack_from('>q', payload, 10)[0]
    inhabited = struct.unpack_from('>q', payload, 18)[0]
    if version == 12:
        total_pages = struct.unpack_from('>H', payload, 26)[0]
        alloc = total_pages * 256
        # 16 section jump-table entries live at 0x1C..0x3B.  Each entry is
        # an offset in 0x100-byte pages relative to the section blob.
        offsets = [struct.unpack_from('<H', payload, 28 + i * 2)[0] * 256 for i in range(16)]
        section_sizes = list(payload[60:76])
        offsets_with_end = offsets + [alloc]
        if 76 + alloc > len(payload): raise ValueError('Invalid v12 section blob size')
        tile_blob = payload[76:76 + alloc]; off = 76 + alloc
        # v12 PS3 stores four sparse nibble arrays after the section blob:
        # lower/upper SkyLight followed by lower/upper BlockLight.
        nibble_raw, off = _decode_v12_nibbles(payload, off, 4)
        lower_sky = decode_sparse_nibble(*nibble_raw[0], True)
        upper_sky = decode_sparse_nibble(*nibble_raw[1], True)
        lower_blk = decode_sparse_nibble(*nibble_raw[2], True)
        upper_blk = decode_sparse_nibble(*nibble_raw[3], True)
        if off + 514 > len(payload): raise ValueError('Truncated v12 chunk tail')
        height_map = payload[off:off + 256]; off += 256
        off += 2  # terrain populated flag
        biomes = payload[off:off + 256]
        off += 256
        section_count = 16
    else:
        off = 26 + (8 if version >= 9 else 0)
        lower_blocks, off = _read_legacy_cts(payload, off, 128)
        upper_blocks, off = _read_legacy_cts(payload, off, 128)
        nibble_raw=[]
        for _ in range(4):
            count=struct.unpack_from('>i',payload,off)[0];off+=4;idx=payload[off:off+128];off+=128;dat=payload[off:off+count*128];off+=count*128;nibble_raw.append((count,idx,dat))
        lower_sky=decode_sparse_nibble(*nibble_raw[0],True);upper_sky=decode_sparse_nibble(*nibble_raw[1],True)
        lower_blk=decode_sparse_nibble(*nibble_raw[2],False);upper_blk=decode_sparse_nibble(*nibble_raw[3],False)
        height_map=payload[off:off+256];off+=256;off+=2;biomes=payload[off:off+256]
        section_count=16
    # The binary LCE chunk ends with a normal NBT compound containing dynamic
    # data such as TileEntities, Entities and TileTicks. Preserve it instead
    # of silently discarding the player's chests, signs, furnaces, etc.
    entities = None
    tile_entities = None
    tile_ticks = None
    if off + 1 < len(payload):
        try:
            dynamic_root = nbt_loads(payload[off:])
            entities_tag = nbt_child(dynamic_root, 'Entities')
            tile_entities_tag = nbt_child(dynamic_root, 'TileEntities')
            tile_ticks_tag = nbt_child(dynamic_root, 'TileTicks')
            if entities_tag and entities_tag.type == LIST:
                entities = entities_tag.value[1]
            if tile_entities_tag and tile_entities_tag.type == LIST:
                tile_entities = tile_entities_tag.value[1]
            if tile_ticks_tag and tile_ticks_tag.type == LIST:
                tile_ticks = tile_ticks_tag
        except Exception:
            pass

    sections=[]
    for s_idx in range(section_count):
        if version == 12:
            start = offsets[s_idx]
            if start >= alloc:
                continue
            # The one-byte section-size table is authoritative for v12.  The
            # jump table is retained as a fallback because some saves contain
            # zero size entries for empty sections.
            size_bytes = section_sizes[s_idx] * 256
            end = min(alloc, start + size_bytes) if size_bytes else offsets_with_end[s_idx + 1]
            sec=tile_blob[start:end]
            blocks,meta,nonzero=_decode_v12_section(sec,s_idx)
        else:
            source=lower_blocks if s_idx<8 else upper_blocks
            blocks=bytearray(4096);meta=bytearray(2048);base_y=s_idx*16 if s_idx<8 else (s_idx-8)*16
            for x in range(16):
                for z in range(16):
                    for y in range(16):
                        src=(x*16+z)*128+base_y+y;dst=x+z*16+y*256;blocks[dst]=source[src]
            nonzero=sum(1 for b in blocks if b)
        sky_src=lower_sky if s_idx<8 else upper_sky;blk_src=lower_blk if s_idx<8 else upper_blk
        base_y=s_idx*16 if s_idx<8 else (s_idx-8)*16;sky=bytearray(2048);block_light=bytearray(2048)
        for x in range(16):
            for z in range(16):
                xz=x*16+z
                for y in range(16):
                    pos=(xz<<7)+(base_y+y);src=pos>>1;dst=(x+z*16+y*256)>>1
                    sv=(sky_src[src]&15) if not(pos&1) else ((sky_src[src]>>4)&15);bv=(blk_src[src]&15) if not(pos&1) else ((blk_src[src]>>4)&15);di=x+z*16+y*256
                    if di&1:sky[dst]=(sky[dst]&15)|(sv<<4);block_light[dst]=(block_light[dst]&15)|(bv<<4)
                    else:sky[dst]=(sky[dst]&240)|sv;block_light[dst]=(block_light[dst]&240)|bv
        if nonzero or any(sky): sections.append((s_idx,blocks,meta,sky,block_light))
    return chunk_nbt(chunk_x,chunk_z,last_update,inhabited,height_map,biomes,sections,_list_compounds_to_tags(entities),_list_compounds_to_tags(tile_entities),tile_ticks)

def _read_legacy_cts(payload: bytes, off: int, height: int):
    alloc = struct.unpack_from('>i', payload, off)[0]; off += 4
    blob = payload[off:off + alloc]; off += alloc
    blocks = bytearray(height * 256)
    if alloc < 1024:
        return blocks, off
    data = blob[1024:]
    for block in range(512):
        e = struct.unpack_from('<H', blob, block * 2)[0]
        typ = e & 3
        base = ((block & 0x180) << 6) | ((block & 0x060) << 4) | ((block & 0x01F) << 2)
        if typ == 3:
            if e & 4:
                vals = [(e >> 8) & 0xFF] * 64
            else:
                do = (e >> 1) & 0x7FFE; vals = list(data[do:do + 64])
        else:
            bits = 1 << typ; count = 1 << bits; mask = count - 1; shift = 3 - typ; mb = 7 >> typ; mbytes = 62 >> shift
            do = (e >> 1) & 0x7FFE; pal = data[do:do + count]; packed = data[do + count:do + count + (8 << typ)]; vals=[]
            for j in range(64):
                idx = (j >> shift) & mbytes; bit = (j & mb) * bits
                vals.append(pal[(packed[idx] >> bit) & mask])
        for j,v in enumerate(vals):
            tx=(block&0x180)>>7; tz=(block&0x060)>>5; ty=block&0x01F
            lx=(j&0x30)>>4; lz=(j&0x0C)>>2; ly=j&3
            x=tx*4+lx; z=tz*4+lz; y=ty*4+ly
            if y < height: blocks[x*16*height+z*height+y]=v
    return blocks, off


# ---------------------------------------------------------------------------
# Legacy McRegion NBT -> Java Anvil NBT
# ---------------------------------------------------------------------------
def _legacy_mcr_to_anvil(root, local_x, local_z, region_x, region_z):
    """Convert an old 128-high McRegion chunk (Level.Blocks/Data/Light arrays)
    into the Java 1.12.2 Anvil section representation.

    LCE saves can contain a mixture of binary v12 chunks and old NBT chunks.
    Those NBT chunks are already valid NBT, but they are *not* valid Anvil
    chunks because their block arrays live directly under Level.
    """
    level = nbt_child(root, 'Level')
    if level is None:
        return None
    blocks_tag = nbt_child(level, 'Blocks')
    data_tag = nbt_child(level, 'Data')
    sky_tag = nbt_child(level, 'SkyLight')
    blk_tag = nbt_child(level, 'BlockLight')
    if not all(t is not None for t in (blocks_tag, data_tag, sky_tag, blk_tag)):
        return None
    blocks = bytes(blocks_tag.value)
    meta = bytes(data_tag.value)
    sky_src = bytes(sky_tag.value)
    blk_src = bytes(blk_tag.value)
    if len(blocks) < 32768 or len(meta) < 16384:
        return None
    # Old PS3 McRegion chunks found in this save use both 128-high and
    # 256-high layouts. The 256-high form is what accounts for the large
    # legacy-NBT portion of the supplied world.
    height = len(blocks) // 256
    if height not in (128, 256):
        return None
    if len(meta) < (height * 256) // 2:
        return None
    sections = []
    for sy in range(height // 16):
        b = bytearray(4096)
        m = bytearray(2048)
        sl = bytearray(2048)
        bl = bytearray(2048)
        nonzero = False
        for x in range(16):
            for z in range(16):
                base_src = (x * 16 + z) * height + sy * 16
                base_dst = x + z * 16 + sy * 4096
                for ly in range(16):
                    src = base_src + ly
                    dst = x + z * 16 + ly * 256
                    bv = blocks[src]
                    b[dst] = bv
                    if bv:
                        nonzero = True
                    mi = src >> 1
                    di = dst >> 1
                    if (src & 1) == 0:
                        mv = meta[mi] & 0x0F
                    else:
                        mv = (meta[mi] >> 4) & 0x0F
                    if (dst & 1) == 0:
                        m[di] = (m[di] & 0xF0) | mv
                    else:
                        m[di] = (m[di] & 0x0F) | (mv << 4)
                    if src < len(sky_src) * 2:
                        sv = (sky_src[src >> 1] >> (4 if src & 1 else 0)) & 0x0F
                        bvlight = (blk_src[src >> 1] >> (4 if src & 1 else 0)) & 0x0F
                        if dst & 1:
                            sl[di] = (sl[di] & 0x0F) | (sv << 4)
                            bl[di] = (bl[di] & 0x0F) | (bvlight << 4)
                        else:
                            sl[di] = (sl[di] & 0xF0) | sv
                            bl[di] = (bl[di] & 0xF0) | bvlight
        # Legacy 256-high PS3/McRegion chunks can carry light-only bytes above
        # Y=127 even when the original block array is completely air there.
        # Do not emit such an upper section: an older repair pass could turn
        # stale/duplicated upper data into floating terrain in Java.
        if sy >= 8 and not nonzero:
            continue
        if nonzero or any(sl) or any(bl):
            sections.append((sy, b, m, sl, bl))

    def list_child(name):
        t = nbt_child(level, name)
        if t and t.type == LIST:
            return t.value[1]
        return []

    hm = nbt_child(level, 'HeightMap')
    height_map = list(hm.value[:256]) if hm is not None else [0] * 256
    bio = nbt_child(level, 'Biomes')
    biomes = bytes(bio.value[:256]) if bio is not None else bytes([0] * 256)
    lu = nbt_child(level, 'LastUpdate')
    inh = nbt_child(level, 'InhabitedTime')
    return chunk_nbt(
        region_x * 32 + local_x,
        region_z * 32 + local_z,
        int(lu.value) if lu else 0,
        int(inh.value) if inh else 0,
        height_map,
        biomes,
        sections,
        _list_compounds_to_tags(list_child('Entities')),
        _list_compounds_to_tags(list_child('TileEntities')),
        nbt_child(level, 'TileTicks')
    )

# ---------------------------------------------------------------------------
# Java NBT pass-through (for chunks already in NBT format)
# ---------------------------------------------------------------------------
def passthrough_nbt_chunk(payload: bytes, local_x: int, local_z: int, region_x: int, region_z: int) -> bytes:
    try:
        root = nbt_loads(payload)
        # LCE can contain old McRegion NBT chunks. Convert those arrays to
        # Anvil Sections instead of merely copying them through.
        converted = _legacy_mcr_to_anvil(root, local_x, local_z, region_x, region_z)
        if converted is not None:
            return nbt_dumps(converted)

        level = nbt_child(root,'Level')
        if level:
            nbt_set_child(level,Tag(INT,'xPos',region_x*32+local_x)); nbt_set_child(level,Tag(INT,'zPos',region_z*32+local_z))
        if not nbt_child(root,'DataVersion'): nbt_set_child(root,Tag(INT,'DataVersion',1343))
        return nbt_dumps(root)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Java Region File Writer
# ---------------------------------------------------------------------------
class JavaRegionFileWriter:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.buffer   = bytearray(8192)
        self.offsets  = [0] * 1024
        self.timestamps = [0] * 1024
        self.next_sector = 2

    def write_chunk(self, local_x: int, local_z: int, uncompressed_nbt: bytes):
        if not (0 <= local_x < 32 and 0 <= local_z < 32):
            return
        compressed   = zlib.compress(uncompressed_nbt, 6)
        payload_len  = 1 + len(compressed)
        total_len    = 4 + payload_len
        sectors_need = (total_len + 4095) // 4096
        if sectors_need >= 256:
            return
        sector_start = self.next_sector
        self.next_sector += sectors_need
        byte_pos = sector_start * 4096
        needed   = byte_pos + sectors_need * 4096
        if needed > len(self.buffer):
            self.buffer.extend(b'\x00' * (needed - len(self.buffer)))
        struct.pack_into(">I", self.buffer, byte_pos, payload_len)
        self.buffer[byte_pos + 4] = 2  # zlib compression type
        self.buffer[byte_pos + 5: byte_pos + 5 + len(compressed)] = compressed
        idx = local_x + local_z * 32
        self.offsets[idx]    = (sector_start << 8) | sectors_need
        self.timestamps[idx] = int(time.time())

    def save(self):
        for i in range(1024):
            struct.pack_into(">I", self.buffer, i * 4, self.offsets[i])
        for i in range(1024):
            struct.pack_into(">I", self.buffer, 4096 + i * 4, self.timestamps[i])
        os.makedirs(os.path.dirname(os.path.abspath(self.filepath)), exist_ok=True)
        tmp = self.filepath + ".tmp"
        with open(tmp, "wb") as f:
            f.write(self.buffer)
        os.replace(tmp, self.filepath)


# ---------------------------------------------------------------------------
# GAMEDATA index parser
# ---------------------------------------------------------------------------
def parse_gamedata_index(gamedata_bytes: bytes) -> list:
    """Returns list of (name, length, offset) tuples."""
    index_offset = struct.unpack(">I", gamedata_bytes[0:4])[0]
    file_count   = struct.unpack(">I", gamedata_bytes[4:8])[0]
    entries = []
    for i in range(file_count):
        base = index_offset + i * 144
        if base + 144 > len(gamedata_bytes):
            break
        name_bytes = gamedata_bytes[base: base + 80]
        name = name_bytes.decode("utf-16be", errors="ignore").replace("\x00", "")
        length = struct.unpack(">I", gamedata_bytes[base + 128: base + 132])[0]
        offset = struct.unpack(">I", gamedata_bytes[base + 132: base + 136])[0]
        entries.append((name, length, offset))
    return entries


# ---------------------------------------------------------------------------
# Single-region converter
# ---------------------------------------------------------------------------
def convert_ps3_region(region_data: bytes, region_name: str, dest_path: str,
                        queue=None, task_id=None):
    """
    Converts one PS3 MCR region (from GAMEDATA) to a Java .mca file.
    Region name example: "r.-1.-1.mcr" or "DIM-1r.0.0.mcr" or "DIM1/r.0.0.mcr"
    """
    try:
        if queue:
            queue.put((task_id, 'start', region_name))

        # Parse region coords from name
        import re
        m = re.search(r'(?:DIM-1/|DIM1/|DIM-1|DIM1)?r\.(-?\d+)\.(-?\d+)\.mcr', region_name.replace('\\', '/'))
        if not m:
            return
        region_x = int(m.group(1))
        region_z = int(m.group(2))

        writer = JavaRegionFileWriter(dest_path)
        converted = 0

        for slot_idx in range(1024):
            if queue and slot_idx % 64 == 0:
                queue.put((task_id, 'progress', slot_idx))

            # Read location entry (Big-Endian)
            val = struct.unpack_from(">I", region_data, slot_idx * 4)[0]
            sec_off   = (val >> 8) & 0xFFFFFF
            sec_cnt   = val & 0xFF
            if sec_off == 0 or sec_cnt == 0:
                continue

            chunk_start = sec_off * 4096
            if chunk_start + 12 > len(region_data):
                continue

            # Parse 12-byte chunk header (Big-Endian)
            flag_buf    = struct.unpack_from(">I", region_data, chunk_start)[0]
            rle_flag    = bool(flag_buf & 0x80000000)
            comp_size   = flag_buf & 0x3FFFFFFF
            rle_size    = struct.unpack_from(">I", region_data, chunk_start + 4)[0]
            uncomp_size = struct.unpack_from(">I", region_data, chunk_start + 8)[0]

            if comp_size == 0 or chunk_start + 12 + comp_size > len(region_data):
                continue

            compressed_data = region_data[chunk_start + 12: chunk_start + 12 + comp_size]

            try:
                deflated = zlib.decompress(compressed_data, -15)
            except Exception:
                try:
                    deflated = zlib.decompress(compressed_data)
                except Exception:
                    continue

            # Apply RLE if flag is set and sizes are consistent
            if rle_flag and len(deflated) == uncomp_size:
                payload = bytes(rle_decode(deflated, rle_size))
            else:
                payload = deflated

            if len(payload) < 2:
                continue

            local_x = slot_idx % 32
            local_z = slot_idx // 32
            chunk_x = region_x * 32 + local_x
            chunk_z = region_z * 32 + local_z

            # HARD PS3 WORLD BOUNDARY: [-432, 432) blocks.
            # A chunk is either fully inside or fully outside because 432 is
            # exactly divisible by 16. Never emit an Anvil slot outside it.
            if not (WORLD_MIN_COORD // 16 <= chunk_x <= (WORLD_MAX_COORD - 1) // 16 and
                    WORLD_MIN_COORD // 16 <= chunk_z <= (WORLD_MAX_COORD - 1) // 16):
                continue

            # Detect payload format
            first_byte  = payload[0]
            second_byte = payload[1]

            if first_byte == 0x0A:
                # Some PS3 chunks are genuine legacy McRegion NBT rather than
                # LCE v12. Those chunks in this save use a 256-high Blocks
                # array (65536 bytes). Convert them into Anvil Sections instead
                # of merely passing the legacy root through.
                try:
                    legacy_root = nbt_loads(payload)
                    level_tag = nbt_child(legacy_root, 'Level')
                    blocks_tag = nbt_child(level_tag, 'Blocks') if level_tag else None
                    if blocks_tag and len(blocks_tag.value) in (32768, 65536):
                        converted_root = _legacy_mcr_to_anvil(legacy_root, local_x, local_z, region_x, region_z)
                        nbt_bytes = nbt_dumps(converted_root) if converted_root is not None else None
                    else:
                        nbt_bytes = passthrough_nbt_chunk(payload, local_x, local_z, region_x, region_z)
                except Exception:
                    nbt_bytes = passthrough_nbt_chunk(payload, local_x, local_z, region_x, region_z)
                if nbt_bytes:
                    writer.write_chunk(local_x, local_z, nbt_bytes)
                    converted += 1

            elif first_byte == 0x00 and second_byte in (0x08, 0x09, 0x0C):
                # LCE compressed tile storage (version 8, 9, or 12)
                try:
                    java_nbt = decode_lce_chunk_payload(payload)
                    writer.write_chunk(local_x, local_z, nbt_dumps(java_nbt))
                    converted += 1
                except Exception:
                    pass

        if converted > 0:
            writer.save()

    except Exception as e:
        print(f"Error converting region {region_name}: {e}", file=sys.stderr)
    finally:
        if queue:
            queue.put((task_id, 'done', 1024))


# ---------------------------------------------------------------------------
# Main entry point: GAMEDATA → output directory
# ---------------------------------------------------------------------------
def convert_gamedata(gamedata_path: str, output_dir: str, progress_mgr=None):
    print(f"Reading GAMEDATA: {gamedata_path}")
    with open(gamedata_path, "rb") as f:
        gamedata = f.read()

    entries = parse_gamedata_index(gamedata)
    mcr_entries = [(n, l, o) for n, l, o in entries if n.endswith(".mcr")]
    print(f"Found {len(mcr_entries)} MCR region files")

    # Build task list: (region_name, dest_path)
    tasks = []
    for name, length, offset in mcr_entries:
        # Map PS3 region names to Java output paths
        dest_rel = _ps3_region_name_to_java_path(name)
        if not dest_rel:
            continue
        dest_full = os.path.join(output_dir, dest_rel)
        if progress_mgr and progress_mgr.is_file_created(dest_full) and os.path.exists(dest_full):
            print(f"  Skipping (already done): {name}")
            continue
        tasks.append((name, dest_full))

    if not tasks:
        print("All regions already converted or no regions to convert.")
        return

    # Each region is decoded in a fresh Python process. LCE decoding allocates
    # large temporary buffers and some PS3 saves make Python's allocator retain
    # them; process isolation keeps long conversions deterministic.
    import subprocess
    total=len(tasks)
    worker=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'convert_region_worker.py')
    print(f"Converting {total} regions in isolated workers...\n")
    for i, (region_name, dest_path) in enumerate(tasks, 1):
        print(f"\nSTART {region_name}", flush=True)
        
        if getattr(sys, 'frozen', False):
            # Packaged builds re-enter the same executable in worker mode.
            cmd = [sys.executable, '--worker', gamedata_path, region_name, output_dir]
            result=subprocess.run(cmd, cwd=os.path.dirname(os.path.abspath(__file__)))
        else:
            result=subprocess.run([sys.executable, worker, gamedata_path, region_name, output_dir], cwd=os.path.dirname(os.path.abspath(__file__)))
        if result.returncode != 0:
            raise RuntimeError(f"Region worker failed for {region_name} (exit {result.returncode})")
        if progress_mgr and os.path.exists(dest_path): progress_mgr.mark_file_created(dest_path)
        pct=int(i/total*100); bar='#'*int(i/total*30)+'-'*int((1-i/total)*30)
        print(f"\r[{bar}] {i:2d}/{total} ({pct}%) - Done: {region_name:<24}", end='', flush=True)
    print()

    print(f"\n\nRegion conversion complete! Output: {output_dir}")

    # Targeted repair for legacy special blocks. This does not regenerate
    # terrain; it only restores bed/ender-chest block entities and exact
    # legacy metadata where modern Java needs an explicit block entity.
    try:
        if __package__:
            from .repair_special_blocks import main as repair_special_blocks_main
        else:
            from repair_special_blocks import main as repair_special_blocks_main
        repair_special_blocks_main(gamedata_path, output_dir)
    except Exception as e:
        print(f"Warning: special-block repair skipped: {e}")

    # -----------------------------------------------------------------------
    # Extract & convert level.dat, data/*.dat, and playerdata
    # -----------------------------------------------------------------------
    level_entry = [e for e in entries if e[0] == 'level.dat']
    data_entries = [e for e in entries if e[0].startswith('data/')]

    player_tags = []
    player_entries = [e for e in entries if e[0].startswith('P_')]
    for p_name,p_len,p_off in player_entries:
        try:
            player_tags.append((p_name, nbt_loads(gamedata[p_off:p_off+p_len])))
        except Exception as e:
            print(f"Warning: cannot parse player file {p_name}: {e}")

    def offline_uuid(name):
        import hashlib, uuid
        raw=bytearray(hashlib.md5(("OfflinePlayer:"+name).encode('utf-8')).digest())
        raw[6]=(raw[6]&0x0f)|0x30; raw[8]=(raw[8]&0x3f)|0x80
        return str(uuid.UUID(bytes=bytes(raw)))

    player_dir=os.path.join(output_dir,'playerdata'); os.makedirs(player_dir,exist_ok=True)
    first_player=None
    for p_name,root in player_tags:
        u=nbt_child(root,'UUID'); uname=(u.value.strip() if u and isinstance(u.value,str) and u.value.strip() else '')
        if not uname:
            uname='PS3Player'
        # Preserve the original UUID string as a harmless custom tag, while
        # giving Java's 1.12 player loader the conventional UUIDMost/Least pair.
        import uuid
        uid=uuid.UUID(offline_uuid(uname)); n=uid.int; most=(n>>64)&((1<<64)-1); least=n&((1<<64)-1)
        if most >= 1<<63: most-=1<<64
        if least >= 1<<63: least-=1<<64
        nbt_remove_child(root,'UUID')
        nbt_set_child(root,Tag(LONG,'UUIDMost',most)); nbt_set_child(root,Tag(LONG,'UUIDLeast',least))
        dv=nbt_child(root,'DataVersion');
        if dv: dv.value=1343
        else: nbt_set_child(root,Tag(INT,'DataVersion',1343))
        out_p=os.path.join(player_dir,offline_uuid(uname)+'.dat')
        with gzip.open(out_p,'wb') as gz: gz.write(nbt_dumps(root))
        if first_player is None: first_player=root
        print(f"Extracted player '{uname}' -> playerdata/{os.path.basename(out_p)}")

    if level_entry:
        l_name,l_len,l_off=level_entry[0]; l_data=gamedata[l_off:l_off+l_len]
        try:
            level_nbt=nbt_loads(l_data); data_tag=nbt_child(level_nbt,'Data')
            if data_tag:
                ver=nbt_child(data_tag,'version');
                if ver: ver.value=19133
                else: nbt_set_child(data_tag,Tag(INT,'version',19133))
                dv=nbt_child(data_tag,'DataVersion');
                if dv: dv.value=1343
                else: nbt_set_child(data_tag,Tag(INT,'DataVersion',1343))
                nbt_remove_child(data_tag,'Version')
                version_comp=Tag(COMPOUND,'Version',[Tag(INT,'Id',1343),Tag(STRING,'Name','1.12.2'),Tag(BYTE,'Snapshot',0)])
                nbt_set_child(data_tag,version_comp)
                if first_player is not None:
                    # Singleplayer 1.12 stores the active player under Data.Player.
                    nbt_set_child(data_tag,Tag(COMPOUND,'Player',first_player.value))
            level_path=os.path.join(output_dir,'level.dat')
            with gzip.open(level_path,'wb') as gz: gz.write(nbt_dumps(level_nbt))
            print('Generated level.dat (Java 1.12.2 / Anvil / DataVersion 1343)')
        except Exception as e:
            print(f"Warning generating level.dat: {e}")

    if data_entries:
        data_dir = os.path.join(output_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        for d_name, d_len, d_off in data_entries:
            raw = gamedata[d_off : d_off + d_len]
            sub_name = d_name.replace("data/", "").replace("data\\", "")
            dest_file = os.path.join(data_dir, sub_name)
            try:
                d_nbt = nbt_loads(raw)
                with gzip.open(dest_file, "wb") as gz_f:
                    gz_f.write(nbt_dumps(d_nbt))
            except Exception:
                with open(dest_file, "wb") as f:
                    f.write(raw)
        print(f"Successfully extracted {len(data_entries)} data/ files.")

    print(f"\nAll world files successfully converted to Java format at: {output_dir}")


def run_region_worker(gamedata_path: str, region_name: str, output_dir: str):
    """Convert exactly one GAMEDATA region in a fresh process."""
    b = Path(gamedata_path).read_bytes()
    entry = next((x for x in parse_gamedata_index(b) if x[0] == region_name), None)
    if entry is None:
        raise SystemExit('region not found: ' + region_name)
    dest_rel = _ps3_region_name_to_java_path(region_name)
    if not dest_rel:
        raise SystemExit('unsupported region name: ' + region_name)
    dest = Path(output_dir) / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    convert_ps3_region(b[entry[2]:entry[2] + entry[1]], region_name, str(dest), None, None)


def _ps3_region_name_to_java_path(name: str) -> str:
    """Maps PS3 MCR region name to Java output relative path."""
    import re
    name_norm = name.replace("\\", "/")
    # Nether: DIM-1r.X.Z.mcr or DIM-1/r.X.Z.mcr
    m = re.match(r'DIM-1/?r\.(-?\d+)\.(-?\d+)\.mcr', name_norm)
    if m:
        return f"DIM-1/region/r.{m.group(1)}.{m.group(2)}.mca"
    # End: DIM1/r.X.Z.mcr
    m = re.match(r'DIM1/?r\.(-?\d+)\.(-?\d+)\.mcr', name_norm)
    if m:
        return f"DIM1/region/r.{m.group(1)}.{m.group(2)}.mca"
    # Overworld: r.X.Z.mcr
    m = re.match(r'r\.(-?\d+)\.(-?\d+)\.mcr', name_norm)
    if m:
        return f"region/r.{m.group(1)}.{m.group(2)}.mca"
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python ps3_converter.py <GAMEDATA_path> <output_dir>")
        print()
        print("Example:")
        print("  python ps3_converter.py")
        print(r"    C:\PS3\SAVEDATA\<SAVE>\GAMEDATA")
        print(r"    C:\output\MyWorld")
        sys.exit(1)

    gamedata_path = sys.argv[1]
    output_dir    = sys.argv[2]

    if not os.path.isfile(gamedata_path):
        print(f"ERROR: GAMEDATA not found: {gamedata_path}")
        sys.exit(1)

    convert_gamedata(gamedata_path, output_dir)
