"""Task prerequisite validation and queries shared by workspace and agent paths."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Task, TaskDependency


def list_dependencies(db: Session, task_id: str) -> list[Task]:
    return db.scalars(
        select(Task)
        .join(TaskDependency, TaskDependency.depends_on_task_id == Task.id)
        .where(TaskDependency.task_id == task_id)
        .order_by(Task.position, Task.created_at, Task.id)
    ).all()


def unmet_dependency_ids(db: Session, task_id: str) -> list[str]:
    return list(db.scalars(
        select(Task.id)
        .join(TaskDependency, TaskDependency.depends_on_task_id == Task.id)
        .where(TaskDependency.task_id == task_id, Task.status != "done")
    ).all())


def replace_dependencies(db: Session, task: Task, dependency_ids: list[str]) -> None:
    """Replace a task's prerequisites, rejecting foreign-project links and cycles."""
    if len(dependency_ids) != len(set(dependency_ids)):
        raise ValueError("A prerequisite can only be listed once")
    if task.id in dependency_ids:
        raise ValueError("A task cannot depend on itself")
    if dependency_ids:
        matches = set(db.scalars(select(Task.id).where(
            Task.id.in_(dependency_ids), Task.project_id == task.project_id,
        )).all())
        if matches != set(dependency_ids):
            raise ValueError("Prerequisites must be tasks in the same project")

    rows = db.scalars(select(TaskDependency).where(TaskDependency.task_id == task.id)).all()
    graph: dict[str, set[str]] = {}
    for row in db.scalars(select(TaskDependency).where(TaskDependency.task_id != task.id)).all():
        graph.setdefault(row.task_id, set()).add(row.depends_on_task_id)
    graph[task.id] = set(dependency_ids)
    for dependency_id in dependency_ids:
        pending = [dependency_id]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == task.id:
                raise ValueError("These prerequisites would create a dependency cycle")
            if current not in visited:
                visited.add(current)
                pending.extend(graph.get(current, ()))

    for row in rows:
        db.delete(row)
    for dependency_id in dependency_ids:
        db.add(TaskDependency(task_id=task.id, depends_on_task_id=dependency_id))
