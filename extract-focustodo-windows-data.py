#!/usr/bin/env python3
"""Extract FocusToDo tasks, tags, and projects from its WebView2 IndexedDB LevelDB store.

Works with Python 3.10+ and the standard library only. Close FocusToDo before running.

Usage on Windows:
    py extract-focustodo-windows-data.py
    py extract-focustodo-windows-data.py "C:\\path\\to\\FocusToDo data folder"
    py extract-focustodo-windows-data.py --out "C:\\path\\to\\exports"

The positional path can be the FocusToDo data folder or the IndexedDB folder.
By default, the script writes FocusToDo_indexeddb_raw.json and
FocusToDo_indexeddb.json in the current directory. The raw file preserves
LevelDB keys and values as base64; the prepared file contains decoded objects.
Use --out to choose another output directory. To export CSV files, run
export-focustodo-windows-csv.py.
"""
from __future__ import annotations
import argparse
import base64
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any

DB_RELATIVE = Path("LocalState") / "EBWebView" / "Default" / "IndexedDB" / "file__0.indexeddb.leveldb"
DEFAULT_DATA_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Packages" / "52299SuperElement.119436511DDC8_792yctbvabkar"


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


def serialized_string(data: bytes, pos: int) -> tuple[str, int]:
    tag = data[pos]
    pos += 1
    if tag not in (0x22, 0x63, 0x76, 0x56):
        raise ValueError(f"Unsupported IndexedDB property key encoding: {data[pos - 1:pos + 12].hex()}")
    length, pos = varint(data, pos)
    if pos + length > len(data):
        raise ValueError("Truncated IndexedDB string")
    raw = data[pos:pos + length]
    if tag == 0x22:
        return raw.decode("latin-1"), pos + length
    return raw.decode("utf-16le", errors="replace"), pos + length


def serialized_value(data: bytes, pos: int) -> tuple[Any, int]:
    while pos < len(data) and data[pos] == 0:
        pos += 1
    if pos >= len(data):
        raise ValueError("Missing IndexedDB value tag")
    tag = data[pos]
    pos += 1
    if tag in (0x22, 0x63, 0x76, 0x56):
        return serialized_string(data, pos - 1)
    if tag == ord("T"):
        return True, pos
    if tag == ord("F"):
        return False, pos
    if tag == ord("0"):
        return None, pos
    if tag == ord("I"):
        number, pos = varint(data, pos)
        return (number >> 1) ^ -(number & 1), pos
    if tag == ord("N"):
        if pos + 8 > len(data):
            raise ValueError("Truncated IndexedDB number")
        return struct.unpack_from("<d", data, pos)[0], pos + 8
    raise ValueError(f"Unsupported IndexedDB value tag: {tag:#x}")


def serialized_object(value: bytes) -> dict[str, Any] | None:
    headers = [value.find(marker) for marker in (b"\xff\x0f", b"\xff\x10")]
    header = min((position for position in headers if position >= 0), default=-1)
    if header < 0 or header + 2 >= len(value) or value[header + 2] != ord("o"):
        return None
    pos = header + 3
    obj: dict[str, Any] = {}
    while pos < len(value) and value[pos] != ord("{"):
        key, pos = serialized_string(value, pos)
        obj[key], pos = serialized_value(value, pos)
    if pos >= len(value) or value[pos] != ord("{"):
        raise ValueError("Unterminated IndexedDB object")
    return obj


def extract_objects(db_dir: Path, records: dict[bytes, tuple[int, int, bytes]] | None = None):
    if records is None:
        records = latest_leveldb_records(db_dir)
    objects = []
    for _, (sequence, kind, value) in records.items():
        if kind != 1:
            continue
        obj = serialized_object(value)
        if obj is None or obj.get("objType") not in ("TASK", "PROJECT", "TAG"):
            continue
        if not obj.get("id"):
            continue
        objects.append(obj)
    return objects


def write_raw_json(records: dict[bytes, tuple[int, int, bytes]], output_file: Path, db_dir: Path) -> None:
    decoded_objects = extract_objects(db_dir, records)
    raw_snapshot = {
        "format": "leveldb-indexeddb-raw-snapshot-v1",
        "source": str(db_dir),
        "encoding": "base64 for key and value bytes",
        "record_count": len(records),
        "records": [
            {
                "key_base64": base64.b64encode(key).decode("ascii"),
                "sequence": sequence,
                "kind": kind,
                "deleted": kind == 0,
                "value_base64": base64.b64encode(value).decode("ascii"),
            }
            for key, (sequence, kind, value) in sorted(records.items())
        ],
    }
    prepared_snapshot = {
        "format": "leveldb-indexeddb-prepared-snapshot-v2",
        "source": str(db_dir),
        "decoded_object_count": len(decoded_objects),
        "decoded_objects": decoded_objects,
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    prepared_file = output_file.with_name("FocusToDo_indexeddb.json")
    for path, snapshot in ((output_file, raw_snapshot), (prepared_file, prepared_snapshot)):
        with path.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(snapshot, file, ensure_ascii=False, indent=2)
            file.write("\n")


def main():
    parser = argparse.ArgumentParser(description="Export FocusToDo data from the WebView2 IndexedDB")
    parser.add_argument("data_path", nargs="?", type=Path, default=DEFAULT_DATA_PATH,
                        help=f"FocusToDo data folder or IndexedDB folder (default: {DEFAULT_DATA_PATH})")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="Output folder (defaults to the current directory)")
    args = parser.parse_args()
    db_dir = locate_db(args.data_path.expanduser().resolve())
    records = latest_leveldb_records(db_dir)
    output_file = args.out.expanduser().resolve() / "FocusToDo_indexeddb_raw.json"
    write_raw_json(records, output_file, db_dir)
    prepared_file = output_file.with_name("FocusToDo_indexeddb.json")
    print(f"IndexedDB base: {db_dir}")
    print(f"Logical records exported: {len(records)}")
    print(f"Raw JSON written: {output_file}")
    print(f"Prepared JSON written: {prepared_file}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
