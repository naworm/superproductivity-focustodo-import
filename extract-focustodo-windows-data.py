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
import math
import os
import re
import struct
import sys
from pathlib import Path

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


def property_start(value: bytes, property_name: str) -> int | None:
    start = value.find(b"\xff\x15\xfe")
    offset = 0
    if start >= 0:
        value = value[start:]
        offset = start
    marker = b'"' + bytes([len(property_name)]) + property_name.encode("ascii")
    pos = value.find(marker)
    if pos < 0:
        return None
    return offset + pos + len(marker)


def property_string(value: bytes, property_name: str) -> str | None:
    pos = property_start(value, property_name)
    if pos is None:
        return None
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


def property_typed(value: bytes, property_name: str, expected_tag: str):
    pos = property_start(value, property_name)
    if pos is None or pos >= len(value):
        return None
    tag = chr(value[pos])
    pos += 1
    if expected_tag == "B":
        return tag == "T" if tag in ("T", "F") else None
    if tag != expected_tag:
        return None
    if tag == "N":
        if pos + 8 > len(value):
            return None
        number = struct.unpack_from("<d", value, pos)[0]
        return number if math.isfinite(number) else None
    if tag == "I":
        try:
            number, _ = varint(value, pos)
        except ValueError:
            return None
        return (number >> 1) ^ -(number & 1)
    return None


def extract_objects(db_dir: Path, records: dict[bytes, tuple[int, int, bytes]] | None = None):
    if records is None:
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
        obj = {
            "id": obj_id,
            "type": obj_type,
            "name": property_string(value, "name") or "",
            "tags_raw": property_string(value, "tags") or "",
            "project_id": property_string(value, "projectId") or "",
            "sequence": sequence,
        }
        if obj_type == "TASK":
            for field, source, tag in (
                ("creationDateN_ms", "creationDate", "N"),
                ("deadlineN_ms", "deadline", "N"),
                ("finishedDateN_ms", "finishedDate", "N"),
                ("endDateN_ms", "endDate", "N"),
                ("stateI", "state", "I"),
                ("priorityI", "priority", "I"),
                ("estimatePomoNumI", "estimatePomoNum", "I"),
                ("estimatedPomoNumI", "estimatedPomoNum", "I"),
                ("actualPomoNumI", "actualPomoNum", "I"),
                ("pomodoroIntervalI", "pomodoroInterval", "I"),
                ("reminderDateI", "reminderDate", "I"),
                ("rCycleI", "rCycle", "I"),
                ("rFirstDeadlineI", "rFirstDeadline", "I"),
                ("rStartDateI", "rStartDate", "I"),
                ("rEndDateI", "rEndDate", "I"),
                ("orderI", "order", "I"),
                ("orderN", "order", "N"),
            ):
                obj[field] = property_typed(value, source, tag)
            for field, source in (("rUnit", "rUnit"), ("rValue", "rValue"), ("remark", "remark")):
                obj[field] = property_string(value, source) or ""
            for field, source in (("isFinished", "isFinished"), ("hasSubtask", "hasSubtask"), ("isExpanded", "isExpanded")):
                obj[field] = property_typed(value, source, "B")
        elif obj_type == "PROJECT":
            obj.update({
                "color": property_string(value, "color") or "",
                "parent_id": property_string(value, "parentId") or "",
                "typeI": property_typed(value, "type", "I"),
                "stateI": property_typed(value, "state", "I"),
                "orderI": property_typed(value, "order", "I"),
                "orderN": property_typed(value, "order", "N"),
                "is_default": property_typed(value, "isDefault", "B"),
                "image_name": property_string(value, "imageName") or "",
            })
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
        "format": "leveldb-indexeddb-prepared-snapshot-v1",
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
