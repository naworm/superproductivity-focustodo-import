#!/usr/bin/env python3
"""Extract FocusToDo task names and tag names from its WebView2 IndexedDB LevelDB store.

Works with Python 3.10+ and the standard library only. Close FocusToDo before running.

Usage on Windows:
  py extract_focustodo.py "C:\\Users\\<user>\\AppData\\Local\\Packages\\52299SuperElement.119436511DDC8_792yctbvabkar"

The script writes FocusToDo_tasks.csv and FocusToDo_tags.csv in the current directory.
"""
from __future__ import annotations
import argparse
import csv
import os
import re
import struct
import sys
from pathlib import Path
from collections import Counter

DB_RELATIVE = Path("LocalState") / "EBWebView" / "Default" / "IndexedDB" / "file__0.indexeddb.leveldb"


def varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, pos
        shift += 7
        if shift > 70:
            break
    raise ValueError("Invalid or truncated varint")


def snappy_decompress(data: bytes) -> bytes:
    """Decode a raw Snappy block (the format used by LevelDB SSTables)."""
    expected, pos = varint(data, 0)
    out = bytearray()
    while pos < len(data):
        tag = data[pos]
        pos += 1
        kind = tag & 0x03
        if kind == 0:  # literal
            code = tag >> 2
            if code < 60:
                length = code + 1
            else:
                nbytes = code - 59
                if pos + nbytes > len(data):
                    raise ValueError("Truncated Snappy literal length")
                length = int.from_bytes(data[pos:pos+nbytes], "little") + 1
                pos += nbytes
            if pos + length > len(data):
                raise ValueError("Truncated Snappy literal")
            out.extend(data[pos:pos+length])
            pos += length
        elif kind == 1:  # copy with one-byte offset
            if pos >= len(data):
                raise ValueError("Truncated Snappy copy")
            length = 4 + ((tag >> 2) & 0x07)
            offset = ((tag & 0xE0) << 3) | data[pos]
            pos += 1
            _copy_from_history(out, offset, length)
        elif kind == 2:  # copy with two-byte offset
            if pos + 2 > len(data):
                raise ValueError("Truncated Snappy copy")
            length = 1 + (tag >> 2)
            offset = int.from_bytes(data[pos:pos+2], "little")
            pos += 2
            _copy_from_history(out, offset, length)
        else:  # copy with four-byte offset
            if pos + 4 > len(data):
                raise ValueError("Truncated Snappy copy")
            length = 1 + (tag >> 2)
            offset = int.from_bytes(data[pos:pos+4], "little")
            pos += 4
            _copy_from_history(out, offset, length)
    if len(out) != expected:
        raise ValueError(f"Snappy length mismatch: expected {expected}, got {len(out)}")
    return bytes(out)


def _copy_from_history(out: bytearray, offset: int, length: int) -> None:
    if offset <= 0 or offset > len(out):
        raise ValueError("Invalid Snappy back-reference")
    for _ in range(length):
        out.append(out[-offset])


def read_block(data: bytes, offset: int, size: int) -> bytes:
    # A LevelDB block is followed by a 1-byte compression type and a 4-byte checksum.
    block = data[offset:offset + size + 5]
    if len(block) != size + 5:
        raise ValueError("Truncated LevelDB block")
    payload, compression = block[:-5], block[-5]
    if compression == 0:
        return payload
    if compression == 1:
        return snappy_decompress(payload)
    raise ValueError(f"Unsupported LevelDB compression type {compression}")


def block_entries(block: bytes) -> list[tuple[bytes, bytes]]:
    if len(block) < 4:
        return []
    restart_count = struct.unpack_from("<I", block, len(block)-4)[0]
    restart_start = len(block) - 4 - 4 * restart_count
    if restart_start < 0:
        raise ValueError("Malformed LevelDB restart array")
    pos = 0
    previous_key = b""
    entries: list[tuple[bytes, bytes]] = []
    while pos < restart_start:
        shared, pos = varint(block, pos)
        nonshared, pos = varint(block, pos)
        value_length, pos = varint(block, pos)
        if shared > len(previous_key) or pos + nonshared + value_length > restart_start:
            raise ValueError("Malformed LevelDB block entry")
        key = previous_key[:shared] + block[pos:pos+nonshared]
        pos += nonshared
        value = block[pos:pos+value_length]
        pos += value_length
        entries.append((key, value))
        previous_key = key
    return entries


def table_entries(path: Path) -> list[tuple[bytes, bytes]]:
    data = path.read_bytes()
    if len(data) < 48 or data[-8:] != bytes.fromhex("57fb808b247547db"):
        raise ValueError(f"Not a supported LevelDB table: {path.name}")
    footer = data[-48:-8]
    _, pos = varint(footer, 0)  # metaindex block offset
    _, pos = varint(footer, pos)  # metaindex block size
    index_offset, pos = varint(footer, pos)
    index_size, pos = varint(footer, pos)
    index_block = read_block(data, index_offset, index_size)
    output: list[tuple[bytes, bytes]] = []
    for _, handle in block_entries(index_block):
        block_offset, p = varint(handle, 0)
        block_size, _ = varint(handle, p)
        output.extend(block_entries(read_block(data, block_offset, block_size)))
    return output


def log_fragments(path: Path) -> list[bytes]:
    data = path.read_bytes()
    pos = 0
    fragment = b""
    output: list[bytes] = []
    block_size = 32768
    while pos + 7 <= len(data):
        block_offset = pos % block_size
        if block_size - block_offset < 7:
            pos += block_size - block_offset
            continue
        _, length, kind = struct.unpack_from("<IHB", data, pos)
        pos += 7
        if length == 0 and kind == 0:
            continue
        payload = data[pos:pos+length]
        if len(payload) != length:
            break
        pos += length
        if kind == 1:  # FULL
            output.append(payload)
            fragment = b""
        elif kind == 2:  # FIRST
            fragment = payload
        elif kind == 3:  # MIDDLE
            fragment += payload
        elif kind == 4:  # LAST
            fragment += payload
            output.append(fragment)
            fragment = b""
    return output


def parse_write_batch(batch: bytes):
    if len(batch) < 12:
        return
    sequence, count = struct.unpack_from("<QI", batch, 0)
    pos = 12
    for index in range(count):
        if pos >= len(batch):
            return
        kind = batch[pos]
        pos += 1
        key_length, pos = varint(batch, pos)
        key = batch[pos:pos+key_length]
        pos += key_length
        if kind == 1:
            value_length, pos = varint(batch, pos)
            value = batch[pos:pos+value_length]
            pos += value_length
        elif kind == 0:
            value = b""
        else:
            return
        yield sequence + index, kind, key, value


def locate_db(path: Path) -> Path:
    if path.is_dir() and any(path.glob("*.ldb")):
        return path
    candidate = path / DB_RELATIVE
    if candidate.is_dir():
        return candidate
    for sub in path.rglob("file__0.indexeddb.leveldb"):
        if sub.is_dir() and any(sub.glob("*.ldb")):
            return sub
    raise FileNotFoundError("Impossible de trouver le dossier IndexedDB file__0.indexeddb.leveldb sous : " + str(path))


def latest_leveldb_records(db_dir: Path) -> dict[bytes, tuple[int, int, bytes]]:
    current: dict[bytes, tuple[int, int, bytes]] = {}
    for table in db_dir.glob("*.ldb"):
        for internal_key, value in table_entries(table):
            if len(internal_key) < 8:
                continue
            packed = struct.unpack("<Q", internal_key[-8:])[0]
            sequence, kind = packed >> 8, packed & 0xFF
            user_key = internal_key[:-8]
            previous = current.get(user_key)
            if previous is None or sequence > previous[0]:
                current[user_key] = (sequence, kind, value)
    for log in db_dir.iterdir():
        if not re.fullmatch(r"\d+\.log", log.name):
            continue
        for fragment in log_fragments(log):
            for sequence, kind, user_key, value in parse_write_batch(fragment):
                previous = current.get(user_key)
                if previous is None or sequence > previous[0]:
                    current[user_key] = (sequence, kind, value)
    return current


def property_string(value: bytes, property_name: str) -> str | None:
    start = value.find(b"\xff\x15\xfe")
    if start >= 0:
        value = value[start:]
    marker = b'"' + bytes([len(property_name)]) + property_name.encode("ascii")
    pos = value.find(marker)
    if pos < 0:
        return None
    pos += len(marker)
    if pos >= len(value):
        return None
    tag = value[pos]
    pos += 1
    # Some two-byte strings have a zero marker before the V8 'c' string tag.
    if tag == 0 and pos < len(value) and value[pos] in (0x63, 0x76, 0x56):
        tag = value[pos]
        pos += 1
    if tag not in (0x22, 0x63, 0x76, 0x56):
        return None
    try:
        length, pos = varint(value, pos)
    except ValueError:
        return None
    if pos + length > len(value):
        return None
    raw = value[pos:pos+length]
    if tag == 0x22:
        # V8 one-byte strings represent Latin-1 code points, not UTF-8 bytes.
        return raw.decode("latin-1")
    return raw.decode("utf-16le", errors="replace")


def extract_objects(db_dir: Path):
    records = latest_leveldb_records(db_dir)
    objects = []
    for _, (sequence, kind, value) in records.items():
        if kind != 1 or b"objType" not in value:
            continue
        obj_type = property_string(value, "objType")
        if obj_type not in ("TASK", "PROJECT", "TAG"):
            continue
        obj_id = property_string(value, "id")
        if not obj_id:
            continue
        objects.append({
            "id": obj_id,
            "type": obj_type,
            "name": property_string(value, "name") or "",
            "tags_raw": property_string(value, "tags") or "",
            "project_id": property_string(value, "projectId") or "",
            "sequence": sequence,
        })
    return objects


def write_exports(objects: list[dict], output_dir: Path, db_dir: Path):
    tasks = [x for x in objects if x["type"] == "TASK"]
    projects = {x["id"]: x["name"] for x in objects if x["type"] == "PROJECT" and x["name"]}
    task_tag_refs: Counter[str] = Counter()
    for task in tasks:
        task_tag_refs.update(x for x in task["tags_raw"].split(",") if x)
    tag_rows = []
    for tag_id, task_count in sorted(task_tag_refs.items(), key=lambda item: (projects.get(item[0], "").casefold(), item[0])):
        tag_rows.append({"tag_id": tag_id, "tag_name": projects.get(tag_id, "[nom introuvable]"), "task_count": task_count})
    tag_names = {x["tag_id"]: x["tag_name"] for x in tag_rows}
    task_rows = []
    for task in tasks:
        ids = [x for x in task["tags_raw"].split(",") if x]
        task_rows.append({
            "task_id": task["id"],
            "task_name": task["name"],
            "tags": "; ".join(tag_names.get(x, f"[ID inconnu: {x}]") for x in ids),
            "tag_ids": "; ".join(ids),
        })
    task_rows.sort(key=lambda x: (x["task_name"].casefold(), x["task_id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "FocusToDo_tasks.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["task_id", "task_name", "tags", "tag_ids"], delimiter=";")
        writer.writeheader()
        writer.writerows(task_rows)
    with (output_dir / "FocusToDo_tags.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["tag_id", "tag_name", "task_count"], delimiter=";")
        writer.writeheader()
        writer.writerows(tag_rows)
    missing_names = [x["id"] for x in tasks if not x["name"]]
    print(f"Base IndexedDB : {db_dir}")
    print(f"Tâches exportées : {len(task_rows)}")
    print(f"Tags distincts référencés : {len(tag_rows)}")
    print(f"Tâches sans nom décodé : {len(missing_names)}")
    if missing_names:
        print("Identifiants concernés : " + ", ".join(missing_names))
    print("Fichiers écrits :")
    print(" - " + str(output_dir / "FocusToDo_tasks.csv"))
    print(" - " + str(output_dir / "FocusToDo_tags.csv"))


def main():
    parser = argparse.ArgumentParser(description="Export des tâches et tags FocusToDo à partir de l'IndexedDB WebView2")
    parser.add_argument("data_path", type=Path, help="Dossier des données FocusToDo ou dossier IndexedDB")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="Dossier de sortie (défaut : répertoire courant)")
    args = parser.parse_args()
    db_dir = locate_db(args.data_path.expanduser().resolve())
    objects = extract_objects(db_dir)
    write_exports(objects, args.out.expanduser().resolve(), db_dir)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
