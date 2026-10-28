#!/usr/bin/env python3
"""Convert FocusToDo CSV exports into a Super Productivity backup JSON."""
from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

INBOX_PROJECT_ID = "INBOX_PROJECT"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source, delimiter=";"))


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


def task_tag_ids(value: str, valid_ids: set[str]) -> list[str]:
    return [tag_id.strip() for tag_id in value.replace(";", ",").split(",") if tag_id.strip() in valid_ids]


def add_aliases(rows: list[dict[str, str]], aliases: dict[str, str]) -> list[dict[str, str]]:
    normalized_rows = []
    for row in rows:
        normalized = dict(row)
        for target, source in aliases.items():
            if target not in normalized and source in row:
                normalized[target] = row[source]
        normalized_rows.append(normalized)
    return normalized_rows


def make_task(row: dict[str, str], project_id: str, tag_ids: set[str], template_task: dict[str, Any]) -> dict[str, Any]:
    interval_ms = max(0, number(row.get("pomodoroInterval"))) * 1000
    created = timestamp(row.get("creationDate"))
    is_done = row.get("isFinished", "").strip().lower() == "true"
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
    folders = {row["id"]: row for row in project_rows if row.get("type") == "2000"}
    projects = [row for row in project_rows if row.get("type") == "1000"]
    nodes: dict[str, dict[str, Any]] = {
        folder_id: {
            "k": "f",
            "id": folder_id,
            "name": row.get("name", ""),
            "isExpanded": row.get("expanded", "").strip().lower() == "true",
            "children": [],
        }
        for folder_id, row in folders.items()
    }
    for row in projects:
        nodes[row["id"]] = {"k": "p", "id": row["id"]}

    roots: list[tuple[float, dict[str, Any]]] = []
    for row in project_rows:
        node = nodes.get(row.get("id", ""))
        if node is None:
            continue
        parent_id = row.get("parentId", "")
        parent = nodes.get(parent_id) if parent_id in folders else None
        ordered_node = (number(row.get("order")), node)
        if parent is None:
            roots.append(ordered_node)
        else:
            parent["children"].append(ordered_node)

    def unpack(items: list[tuple[float, dict[str, Any]]]) -> list[dict[str, Any]]:
        result = []
        for _, node in sorted(items, key=lambda item: item[0]):
            if node["k"] == "f":
                node["children"] = unpack(node["children"])
            result.append(node)
        return result

    return unpack(roots)


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
        raise ValueError("Le modèle ne contient pas d'objet 'data' Super Productivity.")

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
        raise ValueError("Le modèle doit contenir le projet INBOX_PROJECT.")
    template_tag = next(iter(tag_entities.values()), {"theme": {}})

    task_rows = add_aliases(task_rows, {
        "id": "task_id", "name": "task_name", "projectId": "project_id",
        "creationDate": "creationDateN_ms", "deadline": "deadlineN_ms",
        "finishedDate": "finishedDateN_ms", "actualPomoNum": "actualPomoNumI",
        "estimatePomoNum": "estimatePomoNumI", "pomodoroInterval": "pomodoroIntervalI",
        "order": "orderI", "isFinished": "isFinishedT",
    })
    tag_rows = add_aliases(tag_rows, {"id": "tag_id", "name": "tag_name"})
    project_rows = add_aliases(project_rows, {
        "id": "project_id", "name": "project_name", "parentId": "parent_id",
        "isDefault": "is_default",
    })

    imported_projects = [row for row in project_rows if row.get("type") == "1000"]
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
        entity = copy.deepcopy(template_tag)
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
            raise ValueError(f"Identifiant de tâche dupliqué : {task_id}")
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
    parser = argparse.ArgumentParser(description="Convertit les exports CSV FocusToDo en backup JSON Super Productivity.")
    parser.add_argument("--template", type=Path, required=True, help="Backup JSON exemple de Super Productivity")
    parser.add_argument("--tasks", type=Path, default=Path("FocusToDo_tasks.csv"), help="Export CSV des tâches")
    parser.add_argument("--tags", type=Path, default=Path("FocusToDo_tags.csv"), help="Export CSV des tags")
    parser.add_argument("--projects", type=Path, default=Path("FocusToDo_projects.csv"), help="Export CSV des projets")
    parser.add_argument("--out", type=Path, default=Path("super-productivity-import.json"), help="Fichier JSON à créer")
    args = parser.parse_args()

    with args.template.expanduser().open("r", encoding="utf-8-sig") as source:
        template = json.load(source)
    result = convert(
        template,
        read_csv(args.tasks.expanduser()),
        read_csv(args.tags.expanduser()),
        read_csv(args.projects.expanduser()),
    )
    output = args.out.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as destination:
        json.dump(result, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    print(f"JSON Super Productivity créé : {output}")
    print(f"Tâches : {len(result['data']['task']['ids'])}")
    print(f"Projets importés : {len([project_id for project_id in result['data']['project']['ids'] if project_id != INBOX_PROJECT_ID])}")
    print(f"Tags importés : {len(read_csv(args.tags.expanduser()))}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
