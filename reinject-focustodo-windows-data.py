#!/usr/bin/env python3
"""Restore a raw FocusToDo IndexedDB LevelDB snapshot on Windows.

Close FocusToDo before restoring. The existing IndexedDB directory is renamed
to a timestamped backup before the reconstructed database is put in its place.

Usage:
    py reinject-focustodo-windows-data.py --dry-run
    py reinject-focustodo-windows-data.py
    py reinject-focustodo-windows-data.py "C:\\path\\to\\FocusToDo data folder"
    py reinject-focustodo-windows-data.py --snapshot "C:\\path\\to\\snapshot.json"
    py reinject-focustodo-windows-data.py "C:\\path\\to\\FocusToDo data folder" --snapshot "C:\\path\\to\\snapshot.json"
"""
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import shutil
import struct
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

DB_RELATIVE = Path("LocalState") / "EBWebView" / "Default" / "IndexedDB" / "file__0.indexeddb.leveldb"
DEFAULT_DATA_PATH = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Packages" / "52299SuperElement.119436511DDC8_792yctbvabkar"
DEFAULT_SNAPSHOT = Path(__file__).resolve().with_name("FocusToDo_indexeddb_raw.json")
SNAPSHOT_FORMAT = "leveldb-indexeddb-raw-snapshot-v1"
BLOCK_SIZE = 32768


def encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("LevelDB varints cannot encode negative values")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def length_prefixed(data: bytes) -> bytes:
    return encode_varint(len(data)) + data


def crc32c(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


def masked_crc32c(data: bytes) -> int:
    crc = crc32c(data)
    return (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def append_log_record(output: bytearray, payload: bytes) -> None:
    pos = 0
    first = True
    while pos < len(payload):
        block_offset = len(output) % BLOCK_SIZE
        remaining = BLOCK_SIZE - block_offset
        if remaining <= 7:
            output.extend(b"\x00" * remaining)
            continue
        fragment_size = min(len(payload) - pos, remaining - 7, 0xFFFF)
        final = pos + fragment_size == len(payload)
        if first and final:
            record_type = 1  # FULL
        elif first:
            record_type = 2  # FIRST
        elif final:
            record_type = 4  # LAST
        else:
            record_type = 3  # MIDDLE
        fragment = payload[pos:pos + fragment_size]
        checksum_data = bytes((record_type,)) + fragment
        output.extend(struct.pack("<IHB", masked_crc32c(checksum_data), fragment_size, record_type))
        output.extend(fragment)
        pos += fragment_size
        first = False


def load_records(snapshot_path: Path) -> list[tuple[bytes, bytes]]:
    with snapshot_path.open("r", encoding="utf-8") as source:
        snapshot: Any = json.load(source)
    if not isinstance(snapshot, dict) or snapshot.get("format") != SNAPSHOT_FORMAT:
        raise ValueError(f"Unsupported snapshot format: expected {SNAPSHOT_FORMAT!r}")
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise ValueError("The snapshot does not contain a 'records' list.")

    active: list[tuple[bytes, bytes]] = []
    seen: set[bytes] = set()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"Invalid record at index {index}.")
        try:
            key = base64.b64decode(record["key_base64"], validate=True)
            value = base64.b64decode(record["value_base64"], validate=True)
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"Invalid base64 key/value in record {index}.") from exc
        if key in seen:
            raise ValueError(f"Duplicate LevelDB key in record {index}.")
        seen.add(key)
        kind = record.get("kind")
        if kind not in (0, 1):
            raise ValueError(f"Invalid LevelDB record type at index {index}: {kind!r}")
        if bool(record.get("deleted")) != (kind == 0):
            raise ValueError(f"Inconsistent 'deleted' flag in record {index}.")
        if kind == 1:
            active.append((key, value))
    declared_count = snapshot.get("record_count")
    if declared_count != len(records):
        raise ValueError(f"record_count is {declared_count!r}, but {len(records)} records were found.")
    if not active:
        raise ValueError("The snapshot contains no active entries; refusing to restore.")
    return active


def write_database(db_dir: Path, records: list[tuple[bytes, bytes]]) -> None:
    db_dir.mkdir(parents=True)

    batch = bytearray(struct.pack("<QI", 1, len(records)))
    for key, value in records:
        batch.append(1)  # WriteBatch put
        batch.extend(length_prefixed(key))
        batch.extend(length_prefixed(value))

    manifest = bytearray()
    version_edit = bytearray()
    version_edit.extend(encode_varint(1))
    version_edit.extend(length_prefixed(b"idb_cmp1"))
    version_edit.extend(encode_varint(2))
    version_edit.extend(encode_varint(2))  # log number
    version_edit.extend(encode_varint(3))
    version_edit.extend(encode_varint(3))  # next file number
    version_edit.extend(encode_varint(4))
    version_edit.extend(encode_varint(len(records)))
    append_log_record(manifest, bytes(version_edit))

    write_batch_log = bytearray()
    append_log_record(write_batch_log, bytes(batch))
    (db_dir / "CURRENT").write_bytes(b"MANIFEST-000001\n")
    (db_dir / "MANIFEST-000001").write_bytes(manifest)
    (db_dir / "000002.log").write_bytes(write_batch_log)


def verify_database(db_dir: Path, expected: list[tuple[bytes, bytes]]) -> None:
    extractor_path = Path(__file__).resolve().with_name("extract-focustodo-windows-data.py")
    spec = importlib.util.spec_from_file_location("focustodo_extractor", extractor_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load the extractor: {extractor_path}")
    extractor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extractor)
    actual = extractor.latest_leveldb_records(db_dir)
    actual_values = {key: value for key, (sequence, kind, value) in actual.items() if kind == 1}
    expected_values = dict(expected)
    if actual_values != expected_values or len(actual) != len(expected_values):
        raise ValueError("The reconstructed database does not match the snapshot.")


def locate_db(path: Path) -> Path:
    if path.name == "file__0.indexeddb.leveldb":
        return path
    candidate = path / DB_RELATIVE
    if candidate.is_dir():
        return candidate
    if path.is_dir():
        for sub in path.rglob("file__0.indexeddb.leveldb"):
            if sub.is_dir():
                return sub
    return candidate


def restore(snapshot_path: Path, db_dir: Path, dry_run: bool) -> None:
    records = load_records(snapshot_path)
    db_dir = db_dir.expanduser().resolve()
    db_dir.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        with tempfile.TemporaryDirectory(prefix="focustodo-restore-") as temp_dir:
            staged_db = Path(temp_dir) / "file__0.indexeddb.leveldb"
            write_database(staged_db, records)
            verify_database(staged_db, records)
        print(f"Dry run successful: validated {len(records)} active entries.")
        print(f"No application data was changed. Target: {db_dir}")
        return

    staging_db = db_dir.with_name(db_dir.name + ".restore-tmp")
    if staging_db.exists():
        raise FileExistsError(f"Le dossier temporaire existe déjà ; vérifiez-le avant de continuer : {staging_db}")
    write_database(staging_db, records)
    try:
        verify_database(staging_db, records)
    except Exception:
        shutil.rmtree(staging_db)
        raise

    backup_db: Path | None = None
    if db_dir.exists():
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_db = db_dir.with_name(db_dir.name + ".backup-" + timestamp)
        if backup_db.exists():
            shutil.rmtree(staging_db)
            raise FileExistsError(f"Le dossier de sauvegarde existe déjà : {backup_db}")
        db_dir.rename(backup_db)
    try:
        staging_db.rename(db_dir)
    except Exception:
        if backup_db is not None and backup_db.exists() and not db_dir.exists():
            backup_db.rename(db_dir)
        if staging_db.exists():
            shutil.rmtree(staging_db)
        raise

    print(f"Restauration terminée : {len(records)} entrées actives injectées dans {db_dir}")
    if backup_db is not None:
        print(f"Previous database backed up to: {backup_db}")
    print("Restart FocusToDo to load the restored data.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore a raw IndexedDB snapshot into FocusToDo.")
    parser.add_argument("data_path", nargs="?", type=Path, default=DEFAULT_DATA_PATH,
                        help="FocusToDo data folder or IndexedDB folder")
    parser.add_argument("--snapshot", "--input", dest="snapshot", type=Path, default=DEFAULT_SNAPSHOT,
                        help="Source snapshot JSON (default: FocusToDo_indexeddb_raw.json beside this script)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build and verify a temporary database without modifying FocusToDo")
    args = parser.parse_args()

    snapshot_path = args.snapshot.expanduser().resolve()
    db_dir = locate_db(args.data_path.expanduser().resolve())
    if args.dry_run:
        restore(snapshot_path, db_dir, dry_run=True)
        return
    restore(snapshot_path, db_dir, dry_run=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
