"""Sample Python project for minitrace scenario testing.

A simple task management module with deliberate characteristics:
- Multiple functions (for S1: count functions)
- Improvement notes as comments (for S2: find improvement notes)
- A README that needs extending (for S3: add section)
- A config file with version info (for S4: extract version)
- Code quality issues (for S5: ambiguous improvement)
"""

import json
from datetime import datetime
from pathlib import Path


# IMPROVE: Add input validation for task titles (empty strings, special chars)
def create_task(title, description="", priority="medium"):
    """Create a new task with the given title and optional description."""
    task = {
        "id": _generate_id(),
        "title": title,
        "description": description,
        "priority": priority,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "completed_at": None,
    }
    return task


def _generate_id():
    """Generate a simple sequential task ID."""
    # IMPROVE: Replace with UUID or proper ID generation
    import random
    return f"task-{random.randint(1000, 9999)}"


# IMPROVE: Add filtering by date range
def list_tasks(tasks, status=None):
    """List all tasks, optionally filtered by status."""
    if status:
        return [t for t in tasks if t["status"] == status]
    return tasks


def complete_task(tasks, task_id):
    """Mark a task as completed."""
    for task in tasks:
        if task["id"] == task_id:
            task["status"] = "completed"
            task["completed_at"] = datetime.now().isoformat()
            return task
    return None


def delete_task(tasks, task_id):
    """Remove a task from the list."""
    # IMPROVE: Add soft delete option instead of hard delete
    return [t for t in tasks if t["id"] != task_id]


def get_task_stats(tasks):
    """Return statistics about the task list."""
    total = len(tasks)
    if total == 0:
        return {"total": 0, "pending": 0, "completed": 0, "completion_rate": 0.0}

    pending = sum(1 for t in tasks if t["status"] == "pending")
    completed = sum(1 for t in tasks if t["status"] == "completed")
    return {
        "total": total,
        "pending": pending,
        "completed": completed,
        "completion_rate": completed / total,
    }


def save_tasks(tasks, filepath="tasks.json"):
    """Save tasks to a JSON file."""
    with open(filepath, "w") as f:
        json.dump(tasks, f, indent=2)


def load_tasks(filepath="tasks.json"):
    """Load tasks from a JSON file."""
    path = Path(filepath)
    if not path.exists():
        return []
    with open(filepath) as f:
        return json.load(f)


def format_task(task):
    """Format a task for display."""
    status_icon = "+" if task["status"] == "completed" else "o"
    priority_map = {"high": "!!!", "medium": "!!", "low": "!"}
    pri = priority_map.get(task["priority"], "")
    return f"[{status_icon}] {pri} {task['title']}"


def search_tasks(tasks, query):
    """Search tasks by title or description."""
    query = query.lower()
    results = []
    for task in tasks:
        if query in task["title"].lower() or query in task["description"].lower():
            results.append(task)
    return results


# IMPROVE: Implement task sorting by priority
def sort_tasks(tasks, key="created_at"):
    """Sort tasks by the given key."""
    return sorted(tasks, key=lambda t: t.get(key, ""))


if __name__ == "__main__":
    # Quick demo
    tasks = []
    tasks.append(create_task("Write tests", "Add unit tests for all functions", "high"))
    tasks.append(create_task("Update docs", "Improve README", "medium"))
    tasks.append(create_task("Fix bug #42", priority="high"))

    print("All tasks:")
    for t in tasks:
        print(f"  {format_task(t)}")

    stats = get_task_stats(tasks)
    print(f"\nStats: {stats}")
