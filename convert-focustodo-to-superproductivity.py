#!/usr/bin/env python3
"""Convert FocusToDo JSON snapshot exports into a Super Productivity backup JSON.

Usage:
    python convert-focustodo-to-superproductivity.py
    python convert-focustodo-to-superproductivity.py --json FocusToDo_indexeddb.json \
        --template sp-backup_2026-10-30_004811.json --out import.json

By default, the script reads the FocusToDo IndexedDB snapshot JSON generated in
this folder, uses the real Super Productivity backup model from this workspace,
and writes super-productivity-import.json.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

INBOX_PROJECT_ID = "INBOX_PROJECT"
DEFAULT_TEMPLATE = Path(__file__).resolve().with_name("super-productivity-template.json")


def read_json_snapshot(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig") as source:
        payload = json.load(source)
    if not isinstance(payload, dict):
        raise ValueError(f"The file {path} does not contain a JSON object.")
    decoded_objects = payload.get("decoded_objects", [])
    if not isinstance(decoded_objects, list):
        raise ValueError(f"The JSON file {path} does not contain a 'decoded_objects' list.")

    task_rows: list[dict[str, Any]] = []
    project_rows: list[dict[str, Any]] = []
    tag_rows: list[dict[str, Any]] = []
    for row in decoded_objects:
        if not isinstance(row, dict):
            continue
        state_value = row.get("state")
        if state_value not in (0, "0", None):
            try:
                if int(state_value) != 0:
                    continue
            except (TypeError, ValueError):
                continue
        obj_type = str(row.get("objType", "")).upper()
        if obj_type == "TASK":
            task_rows.append(row)
        elif obj_type == "PROJECT":
            project_type = normalize_type(row.get("type"))
            if project_type == "3000":
                tag_rows.append(row)
            elif not is_system_project(row):
                project_rows.append(row)
    return task_rows, project_rows, tag_rows


def normalize_type(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)
    return str(value).strip()


def is_system_project(row: dict[str, Any]) -> bool:
    project_id = str(row.get("id", ""))
    name = str(row.get("name", ""))
    if project_id.startswith("id-") or name.startswith("PRJ_"):
        return True
    return normalize_type(row.get("type")) in {
        "4000", "4001", "4002", "4003", "4004", "4005", "4006", "4007",
        "5001", "5002", "5003", "7000", "7001", "7002", "7003", "7004", "7005"
    }


def number(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def timestamp(value: Any) -> int:
    return int(number(value))


def day_from_timestamp(value: Any) -> str | None:
    milliseconds = number(value)
    if milliseconds <= 0:
        return None
    return datetime.fromtimestamp(milliseconds / 1000).date().isoformat()


def task_tag_ids(value: Any, valid_ids: set[str]) -> list[str]:
    if isinstance(value, list):
        raw_value = ",".join(str(item) for item in value)
    else:
        raw_value = str(value)
    return [tag_id.strip() for tag_id in raw_value.replace(";", ",").split(",") if tag_id.strip() in valid_ids]


def make_task(row: dict[str, str], project_id: str, tag_ids: set[str], template_task: dict[str, Any]) -> dict[str, Any]:
    interval_ms = max(0, number(row.get("pomodoroInterval"))) * 1000
    created = timestamp(row.get("creationDate"))
    is_finished = row.get("isFinished", "")
    is_done = is_finished if isinstance(is_finished, bool) else str(is_finished).strip().lower() == "true"
    finished = timestamp(row.get("finishedDate"))
    done_at = finished if is_done and finished > 0 else created

    task = copy.deepcopy(template_task)
    task.update({
        "id": row["id"],
        "subTaskIds": [],
        "timeSpentOnDay": {},
        "timeSpent": int(max(0, number(row.get("actualPomoNum"))) * interval_ms),
        "timeEstimate": int(max(0, number(row.get("estimatePomoNum"))) * interval_ms),
        "isDone": is_done,
        "title": row.get("name", ""),
        "tagIds": task_tag_ids(row.get("tags", ""), tag_ids),
        "created": created,
        "attachments": [],
        "projectId": project_id,
        "dueDay": day_from_timestamp(row.get("deadline")),
        "modified": done_at,
    })
    return task


def project_tree(project_rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    projects = [
        row for row in project_rows
        if not is_system_project(row) and normalize_type(row.get("type")) == "1000"
    ]
    roots: list[tuple[float, dict[str, Any]]] = []
    for row in projects:
        roots.append((number(row.get("order")), {"k": "p", "id": row["id"]}))
    return [{"k": "p", "id": row["id"]} for _, row in sorted(roots, key=lambda item: item[0])]


def collect_tree_ids(nodes: list[dict[str, Any]]) -> set[str]:
    ids: set[str] = set()
    for node in nodes:
        if "id" in node:
            ids.add(node["id"])
        ids.update(collect_tree_ids(node.get("children", [])))
    return ids


def convert(template: dict[str, Any], task_rows: list[dict[str, str]], tag_rows: list[dict[str, str]], project_rows: list[dict[str, str]]) -> dict[str, Any]:
    data = copy.deepcopy(template)
    source = data.get("data")
    if not isinstance(source, dict):
        raise ValueError("The template does not contain a Super Productivity 'data' object.")

    task_data = source.setdefault("task", {})
    project_data = source.setdefault("project", {})
    tag_data = source.setdefault("tag", {})
    task_entities = task_data.get("entities", {})
    project_entities = project_data.get("entities", {})
    tag_entities = tag_data.get("entities", {})
    template_task = next(iter(task_entities.values()), {
        "subTaskIds": [], "timeSpentOnDay": {}, "attachments": [],
    })
    inbox_template = project_entities.get(INBOX_PROJECT_ID)
    if inbox_template is None:
        raise ValueError("The template must contain the INBOX_PROJECT project.")
    template_tag = next(iter(tag_entities.values()), {"theme": {}})

    imported_projects = [
        row for row in project_rows
        if not is_system_project(row) and normalize_type(row.get("type")) == "1000"
    ]
    project_ids = {row["id"] for row in imported_projects}
    project_entities_out = {INBOX_PROJECT_ID: copy.deepcopy(inbox_template)}
    project_entities_out[INBOX_PROJECT_ID]["taskIds"] = []
    for row in imported_projects:
        entity = copy.deepcopy(inbox_template)
        entity.update({
            "id": row["id"],
            "title": row.get("name", ""),
            "taskIds": [],
            "backlogTaskIds": [],
            "isArchived": False,
            "isDone": False,
            "doneOn": None,
            "icon": None,
        })
        color = row.get("color", "").strip().lstrip("#")
        if len(color) == 6 and all(char in "0123456789abcdefABCDEF" for char in color):
            entity.setdefault("theme", {})["primary"] = "#" + color
        project_entities_out[row["id"]] = entity

    tag_entities_out = copy.deepcopy(tag_entities)
    for row in tag_rows:
        tag_id = row.get("id", "")
        if not tag_id:
            continue
        entity: dict[str, Any] = copy.deepcopy(template_tag)
        color = row.get("color", "").strip().lstrip("#")
        entity.update({"id": tag_id, "title": row.get("name", ""), "color": "#" + color if color else None})
        if len(color) == 6 and all(char in "0123456789abcdefABCDEF" for char in color):
            entity.setdefault("theme", {})["primary"] = "#" + color
        tag_entities_out[tag_id] = entity

    valid_tag_ids = set(tag_entities_out)
    task_entities_out: dict[str, dict[str, Any]] = {}
    sorted_tasks = sorted(task_rows, key=lambda row: number(row.get("order")))
    for row in sorted_tasks:
        task_id = row.get("id", "")
        if not task_id:
            continue
        if task_id in task_entities_out:
            raise ValueError(f"Duplicate task ID: {task_id}")
        requested_project = row.get("projectId", "")
        target_project = requested_project if requested_project in project_ids else INBOX_PROJECT_ID
        task_entities_out[task_id] = make_task(row, target_project, valid_tag_ids, template_task)
        project_entities_out[target_project].setdefault("taskIds", []).append(task_id)

    task_data["ids"] = list(task_entities_out)
    task_data["entities"] = task_entities_out
    for field in ("currentTaskId", "selectedTaskId", "taskDetailTargetPanel", "lastCurrentTaskId"):
        if field in task_data:
            task_data[field] = None
    project_data["ids"] = list(project_entities_out)
    project_data["entities"] = project_entities_out
    tag_data["ids"] = list(tag_entities_out)
    tag_data["entities"] = tag_entities_out

    menu_tree = source.setdefault("menuTree", {})
    menu_tree["projectTree"] = project_tree(project_rows)
    tag_tree = menu_tree.setdefault("tagTree", [])
    existing_tag_ids = collect_tree_ids(tag_tree)
    for row in tag_rows:
        tag_id = row.get("id", "")
        if tag_id and tag_id not in existing_tag_ids:
            tag_tree.append({"k": "t", "id": tag_id})
            existing_tag_ids.add(tag_id)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert FocusToDo JSON snapshots into a Super Productivity backup JSON.")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="Template backup JSON used as Super Productivity model")
    parser.add_argument("--json", "--input", dest="json_input", type=Path, default=Path("FocusToDo_indexeddb.json"), help="FocusToDo IndexedDB prepared snapshot JSON")
    parser.add_argument("--out", type=Path, default=Path("super-productivity-import.json"), help="Output JSON file to create")
    args = parser.parse_args()

    if not args.json_input.expanduser().exists():
        raise FileNotFoundError(f"The FocusToDo snapshot JSON was not found: {args.json_input.expanduser()}")

    with args.template.expanduser().open("r", encoding="utf-8-sig") as source:
        template = json.load(source)

    task_rows, project_rows, tag_rows = read_json_snapshot(args.json_input.expanduser())
    result = convert(template, task_rows, tag_rows, project_rows)
    output = args.out.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as destination:
        json.dump(result, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(f"Super Productivity JSON created: {output}")
    print(f"Tasks: {len(result['data']['task']['ids'])}")
    print(f"Projects imported: {len([project_id for project_id in result['data']['project']['ids'] if project_id != INBOX_PROJECT_ID])}")
    print(f"Tags imported: {len(result['data']['tag']['ids'])}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
