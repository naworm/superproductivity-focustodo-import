#!/usr/bin/env python3
"""Export CSV files from a prepared FocusToDo IndexedDB JSON snapshot.

Usage:
    py export-focustodo-windows-csv.py
    py export-focustodo-windows-csv.py "C:\\path\\to\\FocusToDo_indexeddb.json"
    py export-focustodo-windows-csv.py --out "C:\\path\\to\\exports"
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


DEFAULT_JSON_PATH = Path.cwd() / "FocusToDo_indexeddb.json"


def timestamp_utc(milliseconds) -> str:
    if milliseconds in (None, 0):
        return ""
    try:
        return datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return ""


def read_snapshot(json_file: Path) -> list[dict]:
    with json_file.open("r", encoding="utf-8") as source:
        snapshot = json.load(source)
    supported_formats = {
        "leveldb-indexeddb-prepared-snapshot-v2",
        "leveldb-indexeddb-prepared-snapshot-v1",
        "leveldb-indexeddb-logical-snapshot-v1",
    }
    if not isinstance(snapshot, dict) or snapshot.get("format") not in supported_formats:
        raise ValueError(f"Unsupported FocusToDo JSON snapshot: {json_file}")
    objects = snapshot.get("decoded_objects")
    if not isinstance(objects, list) or any(not isinstance(obj, dict) for obj in objects):
        raise ValueError(f"Snapshot has no valid decoded_objects list: {json_file}")
    return objects


def write_exports(objects: list[dict], output_dir: Path, json_file: Path) -> None:
    all_tasks = [x for x in objects if x.get("objType") == "TASK"]
    deleted_tasks = [x for x in all_tasks if x.get("state") == -1]
    tasks = [x for x in all_tasks if x.get("state") != -1]
    all_project_objects = [x for x in objects if x.get("objType") == "PROJECT" and x.get("type") in (1000, 2000)]
    deleted_projects = [x for x in all_project_objects if x.get("state") == -1]
    project_objects = [x for x in all_project_objects if x.get("state") != -1]
    all_tag_objects = [x for x in objects if x.get("objType") == "PROJECT" and x.get("type") == 3000]
    deleted_tags = [x for x in all_tag_objects if x.get("state") == -1]
    tag_objects = [x for x in all_tag_objects if x.get("state") != -1]
    projects = {x["id"]: x["name"] for x in project_objects if x["name"]}
    project_by_id = {x["id"]: x for x in project_objects}
    project_task_counts: Counter[str] = Counter(x.get("projectId", "") for x in tasks if x.get("projectId"))
    task_tag_refs: Counter[str] = Counter()
    for task in tasks:
        task_tag_refs.update(x for x in task.get("tags", "").split(",") if x)
    tag_by_id = {x["id"]: x for x in all_tag_objects}
    tag_name_by_id = {x["id"]: x["name"] for x in all_tag_objects}
    tag_rows = [
        {
            "id": tag["id"],
            "name": tag["name"],
            "color": tag["color"],
            "order": tag.get("order"),
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
        ids = [x for x in task.get("tags", "").split(",") if x]
        task_rows.append({
            "id": task["id"],
            "name": task.get("name", ""),
            "projectId": task.get("projectId", ""),
            "project_name": projects.get(task.get("projectId", ""), ""),
            "tags": task.get("tags", ""),
            "tag_names": "; ".join(tag_name_by_id.get(x, f"[unknown ID: {x}]") for x in ids),
            "creationDate": task.get("creationDate"),
            "created_at_utc": timestamp_utc(task.get("creationDate")),
            "deadline": task.get("deadline"),
            "deadline_at_utc": timestamp_utc(task.get("deadline")),
            "finishedDate": task.get("finishedDate"),
            "finished_at_utc": timestamp_utc(task.get("finishedDate")),
            "endDate": task.get("endDate"),
            "end_at_utc": timestamp_utc(task.get("endDate")),
            "isFinished": task.get("isFinished"),
            "priority": task.get("priority"),
            "estimatePomoNum": task.get("estimatePomoNum"),
            "estimatedPomoNum": task.get("estimatedPomoNum"),
            "actualPomoNum": task.get("actualPomoNum"),
            "pomodoroInterval": task.get("pomodoroInterval"),
            "reminderDate": task.get("reminderDate"),
            "remark": task.get("remark", ""),
            "rCycle": task.get("rCycle"),
            "rFirstDeadline": task.get("rFirstDeadline"),
            "rUnit": task.get("rUnit", ""),
            "rValue": task.get("rValue", ""),
            "rStartDate": task.get("rStartDate"),
            "rEndDate": task.get("rEndDate"),
            "hasSubtask": task.get("hasSubtask"),
            "isExpanded": task.get("isExpanded"),
            "order": task.get("order"),
        })
    task_rows.sort(key=lambda x: (x["name"].casefold(), x["id"]))
    project_rows = []
    for project in project_objects:
        project_rows.append({
            "id": project["id"],
            "name": project["name"],
            "color": project.get("color", ""),
            "parentId": project.get("parentId", ""),
            "parent_name": project_by_id.get(project.get("parentId", ""), {}).get("name", ""),
            "type": project.get("type"),
            "object_kind": {1000: "PROJECT", 2000: "FOLDER"}.get(project.get("type"), "UNKNOWN"),
            "order": project.get("order"),
            "isDefault": project.get("isDefault"),
            "imageName": project.get("imageName", ""),
            "task_count": project_task_counts.get(project["id"], 0),
        })
    project_rows.sort(key=lambda x: (x["name"].casefold(), x["id"]))
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "FocusToDo_tasks.csv").open("w", encoding="utf-8-sig", newline="") as file:
        task_fields = [
            "id", "name", "projectId", "project_name", "tags", "tag_names",
            "creationDate", "created_at_utc", "deadline", "deadline_at_utc",
            "finishedDate", "finished_at_utc", "endDate", "end_at_utc",
            "isFinished", "priority", "estimatePomoNum", "estimatedPomoNum",
            "actualPomoNum", "pomodoroInterval", "reminderDate", "remark", "rCycle",
            "rFirstDeadline", "rUnit", "rValue", "rStartDate", "rEndDate", "hasSubtask",
            "isExpanded", "order",
        ]
        writer = csv.DictWriter(file, fieldnames=task_fields, delimiter=";")
        writer.writeheader()
        writer.writerows(task_rows)
    with (output_dir / "FocusToDo_tags.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["id", "name", "color", "order", "task_count"], delimiter=";")
        writer.writeheader()
        writer.writerows(tag_rows)
    with (output_dir / "FocusToDo_projects.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["id", "name", "color", "parentId", "parent_name", "type", "object_kind", "order", "isDefault", "imageName", "task_count"], delimiter=";")
        writer.writeheader()
        writer.writerows(project_rows)
    missing_names = [x["id"] for x in tasks if not x["name"]]
    print(f"JSON snapshot: {json_file}")
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Export FocusToDo data as CSV files")
    parser.add_argument("json_file", nargs="?", type=Path, default=DEFAULT_JSON_PATH,
                        help=f"FocusToDo JSON snapshot (default: {DEFAULT_JSON_PATH})")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="Output folder (defaults to the current directory)")
    args = parser.parse_args()

    json_file = args.json_file.expanduser().resolve()
    objects = read_snapshot(json_file)
    write_exports(objects, args.out.expanduser().resolve(), json_file)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
