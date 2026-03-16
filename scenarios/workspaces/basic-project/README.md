# Task Manager

A simple Python task management module.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```python
from main import create_task, list_tasks, complete_task

tasks = []
tasks.append(create_task("My first task", priority="high"))
print(list_tasks(tasks))
```

## Configuration

Edit `config.toml` to change project settings.
