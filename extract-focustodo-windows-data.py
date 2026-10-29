#!/usr/bin/env python3
"""Extract FocusToDo tasks, tags, and projects from its WebView2 IndexedDB LevelDB store.

Works with Python 3.10+ and the standard library only. Close FocusToDo before running.

Usage on Windows:
    py extract-focustodo-windows-data.py
    py extract-focustodo-windows-data.py "C:\\path\\to\\FocusToDo data folder"

The script writes FocusToDo_tasks.csv, FocusToDo_tags.csv, and FocusToDo_projects.csv
in the current directory.
"""
from __future__ import annotations
import argparse
import base64
import csv
from datetime import datetime, timezone
import json
import math
import os
import re
import struct
import sys
from pathlib import Path
from collections import Counter

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


def timestamp_utc(milliseconds) -> str:
    if milliseconds in (None, 0):
        return ""
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


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
    snapshot = {
        "format": "leveldb-indexeddb-logical-snapshot-v1",
        "source": str(db_dir),
        "encoding": "base64 for key and value bytes",
        "record_count": len(records),
        "decoded_object_count": len(decoded_objects),
        "decoded_objects": decoded_objects,
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
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(snapshot, file, ensure_ascii=False, indent=2)
        file.write("\n")


def write_exports(objects: list[dict], output_dir: Path, db_dir: Path):
    all_tasks = [x for x in objects if x["type"] == "TASK"]
    deleted_tasks = [x for x in all_tasks if x["stateI"] == -1]
    tasks = [x for x in all_tasks if x["stateI"] != -1]
    all_project_objects = [x for x in objects if x["type"] == "PROJECT" and x["typeI"] in (1000, 2000)]
    deleted_projects = [x for x in all_project_objects if x["stateI"] == -1]
    project_objects = [x for x in all_project_objects if x["stateI"] != -1]
    all_tag_objects = [x for x in objects if x["type"] == "PROJECT" and x["typeI"] == 3000]
    deleted_tags = [x for x in all_tag_objects if x["stateI"] == -1]
    tag_objects = [x for x in all_tag_objects if x["stateI"] != -1]
    projects = {x["id"]: x["name"] for x in project_objects if x["name"]}
    project_by_id = {x["id"]: x for x in project_objects}
    project_task_counts: Counter[str] = Counter(x["project_id"] for x in tasks if x["project_id"])
    task_tag_refs: Counter[str] = Counter()
    for task in tasks:
        task_tag_refs.update(x for x in task["tags_raw"].split(",") if x)
    tag_by_id = {x["id"]: x for x in all_tag_objects}
    tag_name_by_id = {x["id"]: x["name"] for x in all_tag_objects}
    tag_rows = [
        {
            "id": tag["id"],
            "name": tag["name"],
            "color": tag["color"],
            "order": tag["orderI"] if tag["orderI"] is not None else tag["orderN"],
            "task_count": task_tag_refs.get(tag["id"], 0),
        }
        for tag in tag_objects
    ]
    for tag_id, task_count in task_tag_refs.items():
        if tag_id not in tag_by_id:
            tag_rows.append({"id": tag_id, "name": "[name not found]", "color": "", "order": None, "task_count": task_count})
    tag_rows.sort(key=lambda x: (x["name"].casefold(), x["id"]))
    task_rows = []
    for task in tasks:
        ids = [x for x in task["tags_raw"].split(",") if x]
        task_rows.append({
            "id": task["id"],
            "name": task["name"],
            "projectId": task["project_id"],
            "project_name": projects.get(task["project_id"], ""),
            "tags": task["tags_raw"],
            "tag_names": "; ".join(tag_name_by_id.get(x, f"[unknown ID: {x}]") for x in ids),
            "creationDate": task["creationDateN_ms"],
            "created_at_utc": timestamp_utc(task["creationDateN_ms"]),
            "deadline": task["deadlineN_ms"],
            "deadline_at_utc": timestamp_utc(task["deadlineN_ms"]),
            "finishedDate": task["finishedDateN_ms"],
            "finished_at_utc": timestamp_utc(task["finishedDateN_ms"]),
            "endDate": task["endDateN_ms"],
            "end_at_utc": timestamp_utc(task["endDateN_ms"]),
            "isFinished": task["isFinished"],
            "priority": task["priorityI"],
            "estimatePomoNum": task["estimatePomoNumI"],
            "estimatedPomoNum": task["estimatedPomoNumI"],
            "actualPomoNum": task["actualPomoNumI"],
            "pomodoroInterval": task["pomodoroIntervalI"],
            "reminderDate": task["reminderDateI"],
            "remark": task["remark"],
            "rCycle": task["rCycleI"],
            "rFirstDeadline": task["rFirstDeadlineI"],
            "rUnit": task["rUnit"],
            "rValue": task["rValue"],
            "rStartDate": task["rStartDateI"],
            "rEndDate": task["rEndDateI"],
            "hasSubtask": task["hasSubtask"],
            "isExpanded": task["isExpanded"],
            "order": task["orderI"] if task["orderI"] is not None else task["orderN"],
        })
    task_rows.sort(key=lambda x: (x["name"].casefold(), x["id"]))
    project_rows = []
    for project in project_objects:
        project_rows.append({
            "id": project["id"],
            "name": project["name"],
            "color": project["color"],
            "parentId": project["parent_id"],
            "parent_name": project_by_id.get(project["parent_id"], {}).get("name", ""),
            "type": project["typeI"],
            "object_kind": {1000: "PROJECT", 2000: "FOLDER"}.get(project["typeI"], "UNKNOWN"),
            "order": project["orderI"] if project["orderI"] is not None else project["orderN"],
            "isDefault": project["is_default"],
            "imageName": project["image_name"],
            "task_count": project_task_counts.get(project["id"], 0),
        })
    project_rows.sort(key=lambda x: (x["name"].casefold(), x["id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "FocusToDo_tasks.csv").open("w", encoding="utf-8-sig", newline="") as f:
        task_fields = [
            "id", "name", "projectId", "project_name", "tags", "tag_names",
            "creationDate", "created_at_utc", "deadline", "deadline_at_utc",
            "finishedDate", "finished_at_utc", "endDate", "end_at_utc",
            "isFinished", "priority", "estimatePomoNum", "estimatedPomoNum",
            "actualPomoNum", "pomodoroInterval", "reminderDate", "remark", "rCycle",
            "rFirstDeadline", "rUnit", "rValue", "rStartDate", "rEndDate", "hasSubtask",
            "isExpanded", "order",
        ]
        writer = csv.DictWriter(f, fieldnames=task_fields, delimiter=";")
        writer.writeheader()
        writer.writerows(task_rows)
    with (output_dir / "FocusToDo_tags.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "color", "order", "task_count"], delimiter=";")
        writer.writeheader()
        writer.writerows(tag_rows)
    with (output_dir / "FocusToDo_projects.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "name", "color", "parentId", "parent_name", "type", "object_kind", "order", "isDefault", "imageName", "task_count"], delimiter=";")
        writer.writeheader()
        writer.writerows(project_rows)
    missing_names = [x["id"] for x in tasks if not x["name"]]
    print(f"IndexedDB base: {db_dir}")
    print(f"Tasks exported: {len(task_rows)}")
    print(f"Deleted tasks excluded: {len(deleted_tasks)}")
    print(f"Deleted tags excluded: {len(deleted_tags)}")
    print(f"Deleted projects/folders excluded: {len(deleted_projects)}")
    print(f"Distinct referenced tags: {len(tag_rows)}")
    print(f"Projects exported: {len(project_rows)}")
    print(f"Tasks with undecoded names: {len(missing_names)}")
    if missing_names:
        print("Affected IDs: " + ", ".join(missing_names))
    print("Files written:")
    print(" - " + str(output_dir / "FocusToDo_tasks.csv"))
    print(" - " + str(output_dir / "FocusToDo_tags.csv"))
    print(" - " + str(output_dir / "FocusToDo_projects.csv"))


def main():
    parser = argparse.ArgumentParser(description="Export FocusToDo data from the WebView2 IndexedDB")
    parser.add_argument("data_path", nargs="?", type=Path, default=DEFAULT_DATA_PATH,
                        help=f"FocusToDo data folder or IndexedDB folder (default: {DEFAULT_DATA_PATH})")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="Output folder (defaults to the current directory)")
    parser.add_argument("--raw-json", action="store_true",
                        help="Write the current LevelDB key/value snapshot as reversible Base64 JSON instead of CSV exports")
    args = parser.parse_args()
    db_dir = locate_db(args.data_path.expanduser().resolve())
    if args.raw_json:
        records = latest_leveldb_records(db_dir)
        output_file = args.out.expanduser().resolve() / "FocusToDo_indexeddb_raw.json"
        write_raw_json(records, output_file, db_dir)
        print(f"IndexedDB base: {db_dir}")
        print(f"Logical records exported: {len(records)}")
        print(f"Raw JSON written: {output_file}")
        return
    objects = extract_objects(db_dir)
    write_exports(objects, args.out.expanduser().resolve(), db_dir)

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
