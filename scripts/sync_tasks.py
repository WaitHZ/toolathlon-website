#!/usr/bin/env python3
"""Generate the 108 public task pages from a pinned Toolathlon revision.

The upstream repository owns executable task facts (instruction, tools, and
versioned fixtures).  This website owns presentation metadata (title,
description, category, route, and legacy trajectory links).  The registry
joins those two sources explicitly so task pages can be regenerated and
checked without relying on the old ``*_.mdx`` copy chain.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "task-pages.json"
DEFAULT_UPSTREAM_REF = "d57361c0f1582cf9a0675c0753315bb6b004bd0e"
UPSTREAM_WEB_ROOT = "https://github.com/hkust-nlp/Toolathlon/tree"
EXPECTED_TASK_COUNT = 108
TASK_CATEGORIES = {"aca", "campus", "daily", "finance", "office", "shopping", "tech"}

ICON_ALIASES = {
    "handle_overlong_tool_outputs": "overlong_tool_output",
    "python_execute": "python-execute",
}

LEGACY_LINE_RE = re.compile(
    r"^\s*-\s*([✅❌➖])\s*\[([^]]+)\]\((https://toolathlon-traj\.xyz/[^)]+)\)"
)


class SyncError(RuntimeError):
    """A deterministic task-page validation error."""


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_OBJECT_DIRECTORY"):
        env.pop(name, None)
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SyncError(f"git {' '.join(args)} failed: {detail}")
    return result


def git_show(repo: Path, ref: str, path: str) -> str:
    return run_git(repo, "show", f"{ref}:{path}").stdout


def git_path_exists(repo: Path, ref: str, path: str) -> bool:
    return run_git(repo, "cat-file", "-e", f"{ref}:{path}", check=False).returncode == 0


def git_tree_files(repo: Path, ref: str, path: str) -> list[str]:
    result = run_git(
        repo,
        "-c",
        "core.quotePath=false",
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        f"{ref}:{path}",
        check=False,
    )
    if result.returncode:
        return []
    return [name for name in result.stdout.split("\0") if name]


def parse_json_string(value: str) -> str:
    value = value.strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise SyncError(f"frontmatter value is not a JSON-compatible string: {value}") from exc
    if not isinstance(parsed, str) or not parsed.strip():
        raise SyncError(f"frontmatter value must be a non-empty string: {value}")
    return parsed


def read_frontmatter(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
    if not match:
        raise SyncError(f"missing YAML frontmatter: {path}")
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    if "title" not in fields or "description" not in fields:
        raise SyncError(f"frontmatter needs title and description: {path}")
    return parse_json_string(fields["title"]), parse_json_string(fields["description"])


def read_legacy_trajectories(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    heading = re.search(
        r"^\s*## (?:Model Trajectory|Legacy Trajectories)\s*$",
        text,
        flags=re.MULTILINE,
    )
    if not heading:
        return []
    entries: list[dict[str, Any]] = []
    for line in text[heading.end() :].splitlines():
        match = LEGACY_LINE_RE.match(line)
        if not match:
            continue
        icon, model, url = match.groups()
        status = True if icon == "✅" else False if icon == "❌" else None
        entries.append({"model": model, "url": url, "passed": status})
    return entries


def load_classification() -> dict[str, str]:
    namespace = runpy.run_path(str(ROOT / "classification.py"))
    mapping = namespace.get("task_classification_mapping")
    if not isinstance(mapping, dict):
        raise SyncError("classification.py does not define task_classification_mapping")
    return {str(key).lower(): str(value) for key, value in mapping.items()}


def load_map() -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for line_number, raw in enumerate((ROOT / "map.txt").read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        parts = raw.split()
        if len(parts) != 3:
            raise SyncError(f"map.txt:{line_number} must contain slug, id, and category")
        slug, task_id, category = parts
        rows.append((slug.lower(), int(task_id), category))
    return rows


def task_routes_from_docs_config() -> set[str]:
    config = json.loads((ROOT / "docs.json").read_text(encoding="utf-8"))
    routes: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, str) and value.startswith("docs/tasks/"):
            routes.add(value)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            for item in value.values():
                visit(item)

    visit(config.get("navigation", {}))
    return routes


def validate_redirects(tasks: list[dict[str, Any]]) -> None:
    config = json.loads((ROOT / "docs.json").read_text(encoding="utf-8"))
    redirects = config.get("redirects")
    if not isinstance(redirects, list):
        raise SyncError("docs.json must define task redirects")
    expected = {
        (
            f"/docs/tasks/{task['category']}/{task['id']}_",
            f"/docs/tasks/{task['category']}/{task['id']}",
        )
        for task in tasks
    }
    actual: list[tuple[str, str]] = []
    for redirect in redirects:
        if not isinstance(redirect, dict):
            raise SyncError(f"invalid redirect entry: {redirect!r}")
        source = redirect.get("source")
        destination = redirect.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise SyncError(f"redirect source and destination must be strings: {redirect!r}")
        if source.startswith("/docs/tasks/"):
            if redirect.get("permanent") is not True:
                raise SyncError(f"task redirect must be permanent: {redirect!r}")
            actual.append((source, destination))
    if len(actual) != len(expected) or set(actual) != expected:
        raise SyncError(
            "task redirect mismatch; "
            f"missing={sorted(expected - set(actual))}, "
            f"extra={sorted(set(actual) - expected)}, "
            f"duplicates={len(actual) - len(set(actual))}"
        )


def bootstrap_registry(path: Path) -> None:
    if path.exists():
        raise SyncError(f"refusing to overwrite existing registry: {path}")
    classification = load_classification()
    tasks: list[dict[str, Any]] = []
    for slug, task_id, category in load_map():
        page = ROOT / "docs" / "tasks" / category / f"{task_id}.mdx"
        title, description = read_frontmatter(page)
        classification_value = classification.get(slug)
        if not classification_value or " > " not in classification_value:
            raise SyncError(f"missing group/subgroup classification for {slug}")
        group, subgroup = classification_value.split(" > ", 1)
        tasks.append(
            {
                "slug": slug,
                "id": task_id,
                "category": category,
                "group": group,
                "subgroup": subgroup,
                "title": title,
                "description": description,
                "legacy_trajectories": read_legacy_trajectories(page),
            }
        )

    registry = {
        "schema_version": 1,
        "benchmark": {
            "label": "Toolathlon-Verified",
            "repository": "https://github.com/hkust-nlp/Toolathlon",
            "commit": DEFAULT_UPSTREAM_REF,
        },
        "tasks": tasks,
    }
    try:
        display_path = path.resolve().relative_to(ROOT)
    except ValueError:
        display_path = path.resolve()
    atomic_write(path, json.dumps(registry, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {display_path} with {len(tasks)} tasks")


def load_registry(path: Path) -> dict[str, Any]:
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncError(f"registry not found: {path}; run --bootstrap-registry first") from exc
    if registry.get("schema_version") != 1:
        raise SyncError("unsupported task registry schema")
    return registry


def validate_registry(registry: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = registry.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != EXPECTED_TASK_COUNT:
        raise SyncError(f"registry must contain exactly {EXPECTED_TASK_COUNT} tasks")

    slugs: set[str] = set()
    routes: set[str] = set()
    for task in tasks:
        required = {"slug", "id", "category", "group", "subgroup", "title", "description"}
        missing = required - set(task)
        if missing:
            raise SyncError(f"registry task is missing {sorted(missing)}: {task}")
        slug = str(task["slug"])
        if slug != slug.lower() or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise SyncError(f"slug must be lowercase kebab-case: {slug}")
        if slug in slugs:
            raise SyncError(f"duplicate slug: {slug}")
        slugs.add(slug)
        category = task["category"]
        task_id = task["id"]
        if category not in TASK_CATEGORIES:
            raise SyncError(f"invalid task category for {slug}: {category!r}")
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0:
            raise SyncError(f"task id must be a positive integer for {slug}: {task_id!r}")
        route = f"docs/tasks/{category}/{task_id}"
        if route in routes:
            raise SyncError(f"duplicate route: {route}")
        routes.add(route)
        if not str(task["title"]).strip() or not str(task["description"]).strip():
            raise SyncError(f"title and description must be non-empty: {slug}")
        for trajectory in task.get("legacy_trajectories", []):
            passed = trajectory.get("passed")
            if passed is not True and passed is not False and passed is not None:
                raise SyncError(f"invalid legacy trajectory state for {slug}")
            model = trajectory.get("model")
            if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", model):
                raise SyncError(f"invalid legacy trajectory model for {slug}: {model!r}")
            url = str(trajectory.get("url", ""))
            if not re.fullmatch(
                rf"https://toolathlon-traj\.xyz/[A-Za-z0-9._+-]+_{re.escape(slug)}",
                url,
            ):
                raise SyncError(f"unexpected legacy trajectory URL for {slug}: {url}")

    nav_routes = task_routes_from_docs_config()
    if nav_routes != routes:
        missing_nav = sorted(routes - nav_routes)
        extra_nav = sorted(nav_routes - routes)
        raise SyncError(f"task navigation mismatch; missing={missing_nav}, extra={extra_nav}")
    validate_redirects(tasks)
    return tasks


def validate_upstream(repo: Path, ref: str, tasks: list[dict[str, Any]]) -> None:
    resolved = run_git(repo, "rev-parse", f"{ref}^{{commit}}").stdout.strip()
    if resolved != ref:
        raise SyncError(f"upstream ref resolved to {resolved}, expected pinned commit {ref}")
    result = run_git(repo, "ls-tree", "-d", "--name-only", f"{ref}:tasks/finalpool")
    upstream_slugs = {line for line in result.stdout.splitlines() if line}
    registry_slugs = {str(task["slug"]) for task in tasks}
    if upstream_slugs != registry_slugs:
        raise SyncError(
            "upstream task roster mismatch; "
            f"missing={sorted(registry_slugs - upstream_slugs)}, "
            f"extra={sorted(upstream_slugs - registry_slugs)}"
        )
    for slug in sorted(registry_slugs):
        for relative in ("docs/task.md", "task_config.json"):
            path = f"tasks/finalpool/{slug}/{relative}"
            if not git_path_exists(repo, ref, path):
                raise SyncError(f"upstream task is missing {path}")


def load_icon_map() -> dict[str, str]:
    namespace = runpy.run_path(str(ROOT / "icon2.py"))
    mapping = namespace.get("icon_map_new")
    if not isinstance(mapping, dict):
        raise SyncError("icon2.py does not define icon_map_new")
    result = {str(key): str(value) for key, value in mapping.items()}
    for alias, target in ICON_ALIASES.items():
        if target not in result:
            raise SyncError(f"icon alias target does not exist: {target}")
        result[alias] = result[target]
    for name, markup in result.items():
        for source in re.findall(r'<img[^>]+src="([^"]+)"', markup):
            if source.startswith("/icons/") and not (ROOT / source.lstrip("/")).is_file():
                raise SyncError(f"icon asset for {name} does not exist: {source}")
    return result


def accessible_icon(markup: str) -> str:
    if markup.lstrip().startswith("<img") and " alt=" not in markup:
        return markup.replace("<img ", '<img alt="" aria-hidden="true" ', 1)
    return markup


def render_tool_item(name: str, icon_map: dict[str, str], css_class: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        raise SyncError(f"invalid tool name: {name!r}")
    if name not in icon_map:
        raise SyncError(f"no icon mapping for tool: {name}")
    label = html.escape(name).replace("{", "&#123;").replace("}", "&#125;")
    return "\n".join(
        [
            f'<div className="{css_class}-item">',
            accessible_icon(icon_map[name]),
            f'<span className="{css_class}-name">{label}</span>',
            "</div>",
        ]
    )


def render_tools(servers: Iterable[str], local_tools: Iterable[str], icon_map: dict[str, str]) -> str:
    server_items = "\n".join(render_tool_item(name, icon_map, "mcp-server") for name in servers)
    local_items = "\n".join(render_tool_item(name, icon_map, "local-tool") for name in local_tools)
    return f"""<Card>
<div className="tools-container">
<div className="mcp-servers-container">
<div className="mcp-servers-title">
MCP Servers
</div>
<div className="mcp-servers-grid">
{server_items}
</div>
</div>
<div className="local-tools-container">
<div className="mcp-servers-title">
Local Tools
</div>
<div className="local-tools-grid">
{local_items}
</div>
</div>
</div>
</Card>"""


def escape_mdx_prose_line(line: str) -> str:
    """Escape MDX expressions in prose while preserving inline code spans."""
    output: list[str] = []
    index = 0
    while index < len(line):
        if line[index] == "`":
            run_length = 1
            while index + run_length < len(line) and line[index + run_length] == "`":
                run_length += 1
            marker = "`" * run_length
            end = line.find(marker, index + run_length)
            if end != -1:
                output.append(line[index : end + run_length])
                index = end + run_length
                continue
        char = line[index]
        if char == "{":
            output.append("&#123;")
        elif char == "}":
            output.append("&#125;")
        elif char == "<" and index + 1 < len(line) and line[index + 1] in "!/>ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz":
            output.append("&lt;")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def escape_instruction_for_mdx(instruction: str) -> str:
    lines = instruction.strip().splitlines()
    output: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in lines:
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line) if not fence_char else None
        closing = (
            re.match(rf"^ {{0,3}}({re.escape(fence_char)}{{{fence_length},}})[ \t]*$", line)
            if fence_char
            else None
        )
        if opening:
            marker = opening.group(1)
            fence_char = marker[0]
            fence_length = len(marker)
            output.append(line)
        elif closing:
            fence_char = ""
            fence_length = 0
            output.append(line)
        elif fence_char:
            output.append(line)
        else:
            trailing_spaces = len(line) - len(line.rstrip(" "))
            line = line.rstrip(" \t")
            if trailing_spaces >= 2:
                line += "\\"
            if re.match(r"^ {0,3}(?:import|export)(?:\s|\{|\*)", line):
                stripped = line.lstrip()
                indentation = line[: len(line) - len(stripped)]
                line = f"{indentation}&#{ord(stripped[0])};{stripped[1:]}"
            output.append(escape_mdx_prose_line(line))
    if fence_char:
        raise SyncError("instruction contains an unclosed fenced code block")
    return "\n".join(output)


def build_tree(paths: Iterable[str]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path in paths:
        cursor = root
        for part in Path(path).parts:
            cursor = cursor.setdefault(part, {})
    return root


def safe_tree_label(value: str) -> str:
    return html.escape(value, quote=False).replace("{", "&#123;").replace("}", "&#125;")


def tree_lines(tree: dict[str, Any], prefix: str = "") -> list[str]:
    names = sorted(tree, key=lambda name: (not bool(tree[name]), name.casefold()))
    lines: list[str] = []
    for index, name in enumerate(names):
        child = tree[name]
        last = index == len(names) - 1
        connector = "└── " if last else "├── "
        suffix = "/" if child else ""
        lines.append(f"{prefix}{connector}{safe_tree_label(name)}{suffix}")
        if child:
            lines.extend(tree_lines(child, prefix + ("    " if last else "│   ")))
    return lines


def render_initial_state(repo: Path, ref: str, slug: str) -> str:
    task_root = f"tasks/finalpool/{slug}"
    workspace_path = f"{task_root}/initial_workspace"
    preprocess_path = f"{task_root}/preprocess"
    files = git_tree_files(repo, ref, workspace_path)
    has_preprocess = git_path_exists(repo, ref, preprocess_path)
    sections: list[str] = []

    if files:
        url = f"{UPSTREAM_WEB_ROOT}/{ref}/{workspace_path}"
        rendered_tree = "\n".join(tree_lines(build_tree(files)))
        sections.append(
            f"""### Local Workspace

<div className="file-tree">
<a href="{url}">workspace</a>/
{rendered_tree}
</div>"""
        )

    if has_preprocess:
        url = f"{UPSTREAM_WEB_ROOT}/{ref}/{preprocess_path}"
        sections.append(
            """### Runtime Setup

This task initializes application or service state during preprocessing. """
            f"[Review the pinned setup source]({url})."
        )

    if not sections:
        sections.append(
            "This task does not include a versioned local workspace or a public preprocessing fixture."
        )
    return "\n\n".join(sections)


def render_legacy_trajectories(task: dict[str, Any]) -> str:
    trajectories = task.get("legacy_trajectories", [])
    if not trajectories:
        return ""
    lines = [
        "## Legacy Trajectories",
        "",
        "<Warning>",
        "These replays were produced on the original Toolathlon release. They are retained for historical inspection and are not Toolathlon-Verified results.",
        "</Warning>",
        "",
    ]
    for trajectory in trajectories:
        passed = trajectory.get("passed")
        icon = "✅" if passed is True else "❌" if passed is False else "➖"
        suffix = " — evaluation status unavailable" if passed is None else ""
        lines.append(f"- {icon} [{trajectory['model']}]({trajectory['url']}){suffix}")
    return "\n".join(lines)


def render_page(
    task: dict[str, Any],
    repo: Path,
    ref: str,
    icon_map: dict[str, str],
) -> str:
    slug = str(task["slug"])
    task_root = f"tasks/finalpool/{slug}"
    instruction = git_show(repo, ref, f"{task_root}/docs/task.md")
    config = json.loads(git_show(repo, ref, f"{task_root}/task_config.json"))
    servers = config.get("needed_mcp_servers")
    local_tools = config.get("needed_local_tools")
    if not isinstance(servers, list) or not all(isinstance(item, str) for item in servers):
        raise SyncError(f"needed_mcp_servers must be a string list: {slug}")
    if not isinstance(local_tools, list) or not all(isinstance(item, str) for item in local_tools):
        raise SyncError(f"needed_local_tools must be a string list: {slug}")

    source_url = f"{UPSTREAM_WEB_ROOT}/{ref}/{task_root}"
    source_hash = hashlib.sha256(instruction.encode("utf-8")).hexdigest()
    legacy = render_legacy_trajectories(task)
    page = f"""---
title: {json.dumps(task['title'], ensure_ascii=False)}
description: {json.dumps(task['description'], ensure_ascii=False)}
---

> **Toolathlon-Verified** · Website task ID `{task['id']}` · Canonical task `{slug}`
>
> [View the task source at `{ref[:8]}`]({source_url})

## Required Tools

{render_tools(servers, local_tools, icon_map)}

## Instruction

{{/* toolathlon-task-sha256: {source_hash} */}}
{escape_instruction_for_mdx(instruction)}

## Initial State

{render_initial_state(repo, ref, slug)}
"""
    if legacy:
        page += f"\n\n{legacy}\n"
    return page.rstrip() + "\n"


def generated_pages(
    tasks: list[dict[str, Any]], repo: Path, ref: str
) -> dict[Path, str]:
    icon_map = load_icon_map()
    pages: dict[Path, str] = {}
    for task in tasks:
        path = ROOT / "docs" / "tasks" / str(task["category"]) / f"{task['id']}.mdx"
        if path in pages:
            raise SyncError(f"multiple tasks generate {path.relative_to(ROOT)}")
        pages[path] = render_page(task, repo, ref, icon_map)
    if len(pages) != EXPECTED_TASK_COUNT:
        raise SyncError(f"expected {EXPECTED_TASK_COUNT} generated pages, got {len(pages)}")
    return pages


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o644
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(content)
            temp_name = handle.name
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name is not None:
            Path(temp_name).unlink(missing_ok=True)


def write_pages(pages: dict[Path, str]) -> None:
    for path, content in pages.items():
        atomic_write(path, content)
    print(f"wrote {len(pages)} canonical task pages")


def check_pages(pages: dict[Path, str]) -> None:
    drifted: list[str] = []
    for path, expected in pages.items():
        actual = path.read_text(encoding="utf-8") if path.exists() else ""
        if actual != expected:
            drifted.append(str(path.relative_to(ROOT)))
    underscore_pages = sorted((ROOT / "docs" / "tasks").glob("**/*_.mdx"))
    if underscore_pages:
        drifted.append(f"{len(underscore_pages)} routable underscore task pages remain")
    actual_pages = set((ROOT / "docs" / "tasks").glob("*/*.mdx"))
    extra_pages = sorted(actual_pages - set(pages))
    if extra_pages:
        drifted.extend(
            f"unexpected canonical task page: {path.relative_to(ROOT)}"
            for path in extra_pages
        )
    if drifted:
        raise SyncError("task page drift detected:\n  " + "\n  ".join(drifted))
    print(f"checked {len(pages)} task pages: no drift")


def remove_underscore_pages(tasks: list[dict[str, Any]]) -> None:
    pages = sorted(
        ROOT / "docs" / "tasks" / str(task["category"]) / f"{task['id']}_.mdx"
        for task in tasks
    )
    pages = [path for path in pages if path.is_file()]
    for path in pages:
        path.unlink()
    print(f"removed {len(pages)} underscore task pages")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--upstream-repo", type=Path)
    parser.add_argument("--upstream-ref", default=DEFAULT_UPSTREAM_REF)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--bootstrap-registry", action="store_true")
    action.add_argument("--write", action="store_true")
    action.add_argument("--check", action="store_true")
    action.add_argument("--remove-underscore-pages", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.bootstrap_registry:
            bootstrap_registry(args.registry)
            return 0
        if not args.upstream_repo:
            raise SyncError("--upstream-repo is required for generation and checks")

        registry = load_registry(args.registry)
        pinned_ref = str(registry.get("benchmark", {}).get("commit", ""))
        if args.upstream_ref != pinned_ref:
            raise SyncError(
                f"requested upstream ref {args.upstream_ref} does not match registry commit {pinned_ref}"
            )
        tasks = validate_registry(registry)
        validate_upstream(args.upstream_repo, args.upstream_ref, tasks)
        pages = generated_pages(tasks, args.upstream_repo, args.upstream_ref)

        if args.write:
            write_pages(pages)
        if args.remove_underscore_pages:
            remove_underscore_pages(tasks)
        if args.check:
            check_pages(pages)
        return 0
    except (OSError, ValueError, SyncError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
