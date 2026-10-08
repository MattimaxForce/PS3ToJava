"""Targeted post-conversion repair for PS3/LCE beds and ender chests.

This deliberately does NOT regenerate terrain. It reads the original GAMEDATA,
finds only chunks containing legacy block IDs 26 (bed) and 130 (ender chest),
and patches those exact block bytes/metadata into an already-converted Java
world. It also restores the block entities required by Java's modern DataFixer
path for beds and ender chests.
"""
from __future__ import annotations
import os, re, struct, time, zlib, shutil, sys
from pathlib import Path

if __package__:
    from .ps3_converter import parse_gamedata_index, rle_decode, decode_lce_chunk_payload, nbt_dumps, nbt_child
    from .nbt_tools import Tag, COMPOUND, LIST, BYTE, INT, STRING
else:
    from ps3_converter import parse_gamedata_index, rle_decode, decode_lce_chunk_payload, nbt_dumps, nbt_child
    from nbt_tools import Tag, COMPOUND, LIST, BYTE, INT, STRING


def as_compound(v):
    return Tag(COMPOUND, '', v) if isinstance(v, list) else v


def find_children(comp, name):
    t = nbt_child(comp, name)
    if t is None or t.type != LIST or t.value[0] != COMPOUND:
        return []
    return t.value[1]


def region_output_name(region_name: str) -> tuple[str, str]:
    n = region_name.replace('\\', '/')
    if n.startswith('DIM-1/') or n.startswith('DIM-1'):
        return 'DIM-1', n.split('/')[-1].replace('.mcr','.mca')
    if n.startswith('DIM1/') or n.startswith('DIM1'):
        return 'DIM1', n.split('/')[-1].replace('.mcr','.mca')
    return '', n.split('/')[-1].replace('.mcr','.mca')


def chunk_payloads(gamedata: bytes):
    entries = {name: (length, off) for name, length, off in parse_gamedata_index(gamedata)}
    for name, (length, off) in entries.items():
        if not name.endswith('.mcr'):
            continue
        rd = gamedata[off:off + length]
        m = re.search(r'(?:DIM-1/|DIM1/|DIM-1|DIM1)?r\.(-?\d+)\.(-?\d+)\.mcr', name.replace('\\', '/'))
        if not m:
            continue
        rx, rz = map(int, m.groups())
        for slot in range(1024):
            val = struct.unpack_from('>I', rd, slot * 4)[0]
            sec_off = (val >> 8) & 0xFFFFFF
            if not sec_off:
                continue
            st = sec_off * 4096
            if st + 12 > len(rd):
                continue
            flag = struct.unpack_from('>I', rd, st)[0]
            comp_size = flag & 0x3FFFFFFF
            if not comp_size or st + 12 + comp_size > len(rd):
                continue
            try:
                payload = zlib.decompress(rd[st + 12:st + 12 + comp_size], -15)
            except Exception:
                try:
                    payload = zlib.decompress(rd[st + 12:st + 12 + comp_size])
                except Exception:
                    continue
            if flag & 0x80000000:
                rle_size = struct.unpack_from('>I', rd, st + 4)[0]
                try:
                    payload = bytes(rle_decode(payload, rle_size))
                except Exception:
                    continue
            if len(payload) >= 2 and payload[:2] == b'\x00\x0c':
                yield name, rx, rz, slot, payload


def palette_may_contain(payload: bytes, wanted: set[int]) -> bool:
    if len(payload) < 76:
        return False
    alloc = struct.unpack_from('>H', payload, 26)[0] * 256
    if 76 + alloc > len(payload):
        return False
    blob = payload[76:76 + alloc]
    offs = [struct.unpack_from('<H', payload, 28 + i * 2)[0] * 256 for i in range(16)]
    sizes = list(payload[60:76])
    for sy in range(16):
        st = offs[sy]
        sz = sizes[sy] * 256
        if not sz or st >= alloc or st + 128 > alloc:
            continue
        sec = blob[st:min(alloc, st + sz)]
        for ti in range(64):
            e = struct.unpack_from('<H', sec, ti * 2)[0]
            mode = (e >> 12) & 0xF
            if mode == 0:
                vals = [(e >> 4) & 0x1FF]
            elif mode in (0xE, 0xF):
                off = (((e >> 8) & 0xF) << 8 | (e & 0xF0) | (e & 0x0F)) * 4
                base = 128 + off
                if base + 128 > len(sec):
                    continue
                vals = [(struct.unpack_from('<H', sec, base + j * 2)[0] >> 4) & 0x1FF for j in range(64)]
            else:
                bits = {2: 1, 4: 2, 6: 3, 8: 4}.get(mode)
                if not bits:
                    continue
                off = (((e >> 8) & 0xF) << 8 | (e & 0xF0) | (e & 0x0F)) * 4
                count = 1 << bits
                base = 128 + off
                if base + count * 2 > len(sec):
                    continue
                vals = [(struct.unpack_from('<H', sec, base + j * 2)[0] >> 4) & 0x1FF for j in range(count)]
            if wanted.intersection(vals):
                return True
    return False


def extract_special_positions(root):
    level = nbt_child(root, 'Level')
    sections = nbt_child(level, 'Sections') if level else None
    if not sections or sections.type != LIST:
        return [], []
    beds, enders = [], []
    for sec_raw in sections.value[1]:
        sec = as_compound(sec_raw)
        sy = nbt_child(sec, 'Y').value
        blocks = nbt_child(sec, 'Blocks').value
        meta = nbt_child(sec, 'Data').value
        for i, bid in enumerate(blocks):
            if bid not in (26, 130):
                continue
            mi = i >> 1
            mv = ((meta[mi] >> 4) & 0xF) if (i & 1) else (meta[mi] & 0xF)
            x = i & 15
            z = (i >> 4) & 15
            y = sy * 16 + (i >> 8)
            pos = (x, y, z, mv)
            (beds if bid == 26 else enders).append(pos)
    return beds, enders


def set_nibble(arr: bytearray, index: int, value: int):
    p = index >> 1
    if index & 1:
        arr[p] = (arr[p] & 0x0F) | ((value & 15) << 4)
    else:
        arr[p] = (arr[p] & 0xF0) | (value & 15)


def patch_chunk_nbt(raw: bytes, bed_positions, ender_positions, cx, cz):
    if __package__:
        from .nbt_tools import loads
    else:
        from nbt_tools import loads
    root = loads(raw)
    level = nbt_child(root, 'Level')
    sections = nbt_child(level, 'Sections')
    secmap = {int(nbt_child(as_compound(s), 'Y').value): as_compound(s) for s in sections.value[1]}

    # Restore exact legacy block IDs + metadata only at special-block positions.
    for x, y, z, meta in bed_positions + ender_positions:
        sy = y // 16
        sec = secmap.get(sy)
        if sec is None:
            continue
        blocks = bytearray(nbt_child(sec, 'Blocks').value)
        metas = bytearray(nbt_child(sec, 'Data').value)
        idx = (y & 15) * 256 + z * 16 + x
        blocks[idx] = 26 if (x, y, z, meta) in bed_positions else 130
        set_nibble(metas, idx, meta)
        nbt_child(sec, 'Blocks').value = bytes(blocks)
        nbt_child(sec, 'Data').value = bytes(metas)

    te_tag = nbt_child(level, 'TileEntities')
    if te_tag is None or te_tag.type != LIST:
        te_tag = Tag(LIST, 'TileEntities', (COMPOUND, []))
        level.value.append(te_tag)
    existing = []
    for item in te_tag.value[1]:
        c = as_compound(item)
        xt, yt, zt = nbt_child(c, 'x'), nbt_child(c, 'y'), nbt_child(c, 'z')
        if xt and yt and zt:
            existing.append((int(xt.value), int(yt.value), int(zt.value)))

    # Java 1.12 stores beds as TileEntityBed and ender chests as EnderChest.
    # Keeping these block entities is important because modern DataFixer only
    # creates missing bed entities for worlds older than the 1.12 bed fix.
    for x, y, z, meta in bed_positions:
        gx, gy, gz = cx * 16 + x, y, cz * 16 + z
        if (gx, gy, gz) not in existing:
            te_tag.value[1].append(Tag(COMPOUND, '', [
                Tag(STRING, 'id', 'bed'), Tag(INT, 'x', gx), Tag(INT, 'y', gy), Tag(INT, 'z', gz)
            ]))
            existing.append((gx, gy, gz))
    for x, y, z, meta in ender_positions:
        gx, gy, gz = cx * 16 + x, y, cz * 16 + z
        if (gx, gy, gz) not in existing:
            te_tag.value[1].append(Tag(COMPOUND, '', [
                Tag(STRING, 'id', 'ender_chest'), Tag(INT, 'x', gx), Tag(INT, 'y', gy), Tag(INT, 'z', gz)
            ]))
            existing.append((gx, gy, gz))
    return nbt_dumps(root)


def patch_region_file(path: Path, local_x: int, local_z: int, nbt_bytes: bytes):
    data = bytearray(path.read_bytes())
    if len(data) < 8192:
        data.extend(b'\0' * (8192 - len(data)))
    idx = local_x + local_z * 32
    old = struct.unpack_from('>I', data, idx * 4)[0]
    old_sector = old >> 8
    old_count = old & 0xFF
    comp = zlib.compress(nbt_bytes, 6)
    payload_len = 1 + len(comp)
    total = 4 + payload_len
    need = (total + 4095) // 4096
    if need >= 256:
        raise ValueError('patched chunk is too large for Anvil location entry')
    if old_sector and old_count >= need:
        sector = old_sector
    else:
        sector = (len(data) + 4095) // 4096
        data.extend(b'\0' * (need * 4096))
    pos = sector * 4096
    if pos + need * 4096 > len(data):
        data.extend(b'\0' * (pos + need * 4096 - len(data)))
    struct.pack_into('>I', data, pos, payload_len)
    data[pos + 4] = 2
    data[pos + 5:pos + 5 + len(comp)] = comp
    # Clear unused bytes in reused sectors so stale data cannot confuse tools.
    end = pos + need * 4096
    if pos + 5 + len(comp) < end:
        data[pos + 5 + len(comp):end] = b'\0' * (end - (pos + 5 + len(comp)))
    struct.pack_into('>I', data, idx * 4, (sector << 8) | need)
    struct.pack_into('>I', data, 4096 + idx * 4, int(time.time()))
    path.write_bytes(data)


def main(gamedata_path, world_dir):
    gamedata = Path(gamedata_path).read_bytes()
    world = Path(world_dir)
    candidates = []
    for name, rx, rz, slot, payload in chunk_payloads(gamedata):
        if palette_may_contain(payload, {26, 130}):
            candidates.append((name, rx, rz, slot, payload))
    patched = 0; beds = 0; enders = 0
    for name, rx, rz, slot, payload in candidates:
        root = decode_lce_chunk_payload(payload)
        bp, ep = extract_special_positions(root)
        if not bp and not ep:
            continue
        dim, rname = region_output_name(name)
        region_path = world / dim / 'region' / rname if dim else world / 'region' / rname
        if not region_path.exists():
            continue
        local_x, local_z = slot % 32, slot // 32
        # Read the existing Java chunk; preserve every unrelated block/entity.
        data = region_path.read_bytes(); idx = local_x + local_z * 32
        ent = struct.unpack_from('>I', data, idx * 4)[0]
        if ent:
            st = (ent >> 8) * 4096; ln = struct.unpack_from('>I', data, st)[0]
            old_raw = zlib.decompress(data[st + 5:st + 4 + ln])
        else:
            # A few special-block chunks were missing from the older build.
            # Recreate ONLY those chunks from the already-correct LCE decoder;
            # no other chunk is touched.
            old_raw = nbt_dumps(root)
        new_raw = patch_chunk_nbt(old_raw, bp, ep, rx * 32 + local_x, rz * 32 + local_z)
        patch_region_file(region_path, local_x, local_z, new_raw)
        patched += 1; beds += len(bp); enders += len(ep)
    print(f'candidate chunks: {len(candidates)}')
    print(f'patched chunks: {patched}')
    print(f'bed blocks repaired: {beds}')
    print(f'ender chest blocks repaired: {enders}')

if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('usage: python repair_special_blocks.py GAMEDATA ConvertedWorld')
    main(sys.argv[1], sys.argv[2])
