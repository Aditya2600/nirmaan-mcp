"""Nirmaan CLI — fast work item logging for Plane."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .plane_client import PlaneClient, PlaneConfig, PlaneError


app = typer.Typer(
    name="nirmaan",
    help="Nirmaan — fast work item logging for Plane.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@dataclass
class BotDefaults:
    """User-configured defaults for new work items, read from .env."""
    module_id: Optional[str] = None
    label_ids: list[str] = field(default_factory=list)
    estimate: Optional[int] = None
    priority: str = "none"

    @classmethod
    def from_env(cls) -> "BotDefaults":
        load_dotenv()
        labels_raw = os.getenv("PLANE_DEFAULT_LABEL_IDS", "")
        label_ids = [s.strip() for s in labels_raw.split(",") if s.strip()]
        est = os.getenv("PLANE_DEFAULT_ESTIMATE")
        try:
            estimate = int(est) if est else None
        except ValueError:
            estimate = None
        return cls(
            module_id=os.getenv("PLANE_DEFAULT_MODULE_ID") or None,
            label_ids=label_ids,
            estimate=estimate,
            priority=(os.getenv("PLANE_DEFAULT_PRIORITY") or "none").lower(),
        )


# ---------- helpers ----------

def _client() -> PlaneClient:
    try:
        cfg = PlaneConfig.from_env(require_project=True)
    except RuntimeError as e:
        console.print(f"[red]Config error:[/red] {e}")
        raise typer.Exit(1)
    return PlaneClient(cfg)


def _parse_date(s: Optional[str]) -> Optional[str]:
    """today / tomorrow / fri / +3d / 2026-05-15 -> ISO date."""
    if not s:
        return None
    s = s.lower().strip()
    if s == "today":
        return date.today().isoformat()
    if s == "tomorrow":
        return (date.today() + timedelta(days=1)).isoformat()
    if s.startswith("+") and s.endswith("d"):
        try:
            return (date.today() + timedelta(days=int(s[1:-1]))).isoformat()
        except ValueError:
            pass
    weekdays = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    if len(s) >= 3 and s[:3] in weekdays:
        today = date.today()
        days_ahead = (weekdays[s[:3]] - today.weekday()) % 7 or 7
        return (today + timedelta(days=days_ahead)).isoformat()
    return s  # assume ISO


def _to_html(text: Optional[str]) -> Optional[str]:
    """Convert plain text to minimal HTML for Plane's description_html field.
    Passes through if input already looks like HTML."""
    if not text:
        return None
    if "<p>" in text or "<div>" in text or "<br" in text:
        return text  # already HTML, leave alone
    paragraphs = text.split("\n\n")
    return "".join(
        f"<p>{p.replace(chr(10), '<br/>')}</p>" for p in paragraphs if p.strip()
    )


def _resolve_issue(client: PlaneClient, identifier: str) -> dict:
    """Accept a UUID or 'BUILDER-1055' / '1055' and return the issue."""
    if len(identifier) > 30 and identifier.count("-") >= 4:
        return client.get_issue(identifier)
    seq = identifier.split("-")[-1]
    try:
        seq_num = int(seq)
    except ValueError:
        console.print(f"[red]Can't parse identifier:[/red] {identifier}")
        raise typer.Exit(1)
    project_id = client.config.project_id
    for hit in client.search_issues(identifier):
        if hit.get("sequence_id") == seq_num and hit.get("project_id") == project_id:
            return client.get_issue(hit["id"])
    console.print(f"[red]No work item with sequence {seq_num}.[/red]")
    raise typer.Exit(1)


def _create_with_state(
    state_name: str,
    title: str,
    priority: Optional[str],
    label_names: list[str],
    estimate: Optional[int],
    due: Optional[str],
    start: Optional[str],
    module_name: Optional[str],
    no_module: bool,
    parent_id: Optional[str],
    description: Optional[str],
) -> None:
    defaults = BotDefaults.from_env()
    with _client() as c:
        state = c.find_state(state_name)
        if not state:
            available = [s["name"] for s in c.list_states()]
            console.print(f"[red]No state '{state_name}'.[/red] Available: {available}")
            raise typer.Exit(1)

        # Labels: defaults plus any extras passed via -l
        label_ids = list(defaults.label_ids)
        for name in label_names:
            label = c.find_label(name)
            if label and label["id"] not in label_ids:
                label_ids.append(label["id"])
            elif not label:
                console.print(f"[yellow]Warning: label '{name}' not found, skipping.[/yellow]")

        # Module: --module overrides default; --no-module skips entirely
        module_ids: list[str] = []
        if no_module:
            pass
        elif module_name:
            module = c.find_module(module_name)
            if module:
                module_ids.append(module["id"])
            else:
                console.print(f"[yellow]Warning: module '{module_name}' not found, skipping.[/yellow]")
        elif defaults.module_id:
            module_ids.append(defaults.module_id)

        # Smart default: Done items get due=today
        if due is None and state_name.lower() == "done":
            due = "today"

        try:
            issue = c.create_issue(
                name=title,
                description_html=_to_html(description),
                state_id=state["id"],
                priority=priority or defaults.priority,
                label_ids=label_ids or None,
                module_ids=module_ids or None,
                estimate_point=estimate if estimate is not None else defaults.estimate,
                start_date=_parse_date(start),
                target_date=_parse_date(due),
                parent=parent_id,
            )
        except PlaneError as e:
            console.print(f"[red]Failed:[/red] {e}")
            raise typer.Exit(1)

        proj_ident = (
            (issue.get("project_detail") or {}).get("identifier") or "BUILDER"
        )
        seq = issue.get("sequence_id", "?")
        console.print(
            f"[green]✓[/green] Created [cyan]{proj_ident}-{seq}[/cyan]: "
            f"{issue.get('name')}  [dim](state={state_name})[/dim]"
        )


# ---------- commands ----------

@app.command()
def done(
    title: str = typer.Argument(..., help="Title of the work item."),
    desc: Optional[str] = typer.Option(None, "-D", "--desc", help="Description."),
    priority: Optional[str] = typer.Option(None, "-p", "--priority"),
    label: list[str] = typer.Option([], "-l", "--label", help="Add label (repeatable)."),
    est: Optional[int] = typer.Option(None, "-e", "--est", help="Estimate point."),
    due: Optional[str] = typer.Option(None, "-d", "--due", help="today/fri/+3d/ISO."),
    start: Optional[str] = typer.Option(None, "-s", "--start"),
    module: Optional[str] = typer.Option(None, "-m", "--module"),
    no_module: bool = typer.Option(False, "--no-module", help="Skip default module."),
    parent: Optional[str] = typer.Option(None, "--parent"),
):
    """Create a work item in Done state."""
    _create_with_state("Done", title, priority, label, est, due, start, module, no_module, parent, desc)


@app.command()
def wip(
    title: str = typer.Argument(...),
    desc: Optional[str] = typer.Option(None, "-D", "--desc", help="Description."),
    priority: Optional[str] = typer.Option(None, "-p", "--priority"),
    label: list[str] = typer.Option([], "-l", "--label"),
    est: Optional[int] = typer.Option(None, "-e", "--est"),
    due: Optional[str] = typer.Option(None, "-d", "--due"),
    start: Optional[str] = typer.Option(None, "-s", "--start"),
    module: Optional[str] = typer.Option(None, "-m", "--module"),
    no_module: bool = typer.Option(False, "--no-module"),
    parent: Optional[str] = typer.Option(None, "--parent"),
):
    """Create a work item in In Progress state."""
    _create_with_state("In Progress", title, priority, label, est, due, start, module, no_module, parent, desc)


@app.command()
def todo(
    title: str = typer.Argument(...),
    desc: Optional[str] = typer.Option(None, "-D", "--desc", help="Description."),
    priority: Optional[str] = typer.Option(None, "-p", "--priority"),
    label: list[str] = typer.Option([], "-l", "--label"),
    est: Optional[int] = typer.Option(None, "-e", "--est"),
    due: Optional[str] = typer.Option(None, "-d", "--due"),
    start: Optional[str] = typer.Option(None, "-s", "--start"),
    module: Optional[str] = typer.Option(None, "-m", "--module"),
    no_module: bool = typer.Option(False, "--no-module"),
    parent: Optional[str] = typer.Option(None, "--parent"),
):
    """Create a work item in Todo / Backlog state."""
    _create_with_state("Todo", title, priority, label, est, due, start, module, no_module, parent, desc)


@app.command()
def comment(
    identifier: str = typer.Argument(..., help="Issue ID like BUILDER-506."),
    text: str = typer.Argument(..., help="Comment text."),
):
    """Add a comment to a work item."""
    with _client() as c:
        issue = _resolve_issue(c, identifier)
        try:
            c.add_comment(issue["id"], f"<p>{text}</p>")
        except PlaneError as e:
            console.print(f"[red]Failed:[/red] {e}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/green] Comment added to {identifier}.")


@app.command()
def move(
    identifier: str = typer.Argument(..., help="Issue ID like BUILDER-506."),
    state_name: str = typer.Argument(..., help="Target state, e.g. Done."),
):
    """Transition a work item to a new state."""
    with _client() as c:
        issue = _resolve_issue(c, identifier)
        try:
            c.transition_state(issue["id"], state_name)
        except (PlaneError, ValueError) as e:
            console.print(f"[red]Failed:[/red] {e}")
            raise typer.Exit(1)
        console.print(f"[green]✓[/green] {identifier} → {state_name}.")


@app.command(name="list")
def list_cmd(
    state: Optional[str] = typer.Option(None, "-s", "--state"),
    limit: int = typer.Option(20, "-n", "--limit"),
):
    """List work items, optionally filtered by state name."""
    with _client() as c:
        filters = {}
        if state:
            st = c.find_state(state)
            if not st:
                console.print(f"[red]No state '{state}'.[/red]")
                raise typer.Exit(1)
            filters["state"] = st["id"]
        try:
            issues = c.list_issues(**filters)[:limit]
        except PlaneError as e:
            console.print(f"[red]Failed:[/red] {e}")
            raise typer.Exit(1)

        if not issues:
            console.print("[dim]No work items.[/dim]")
            return

        states_by_id = {s["id"]: s["name"] for s in c.list_states()}

        t = Table(show_header=True, header_style="bold")
        t.add_column("ID", style="cyan")
        t.add_column("Title")
        t.add_column("State")
        for i in issues:
            t.add_row(
                f"BUILDER-{i.get('sequence_id', '?')}",
                str(i.get("name", ""))[:60],
                states_by_id.get(i.get("state"), "?"),
            )
        console.print(t)


@app.command()
def show(
    identifier: str = typer.Argument(..., help="Issue ID like BUILDER-506."),
):
    """Show details of a work item."""
    with _client() as c:
        issue = _resolve_issue(c, identifier)
        states_by_id = {s["id"]: s["name"] for s in c.list_states()}
        labels_by_id = {l["id"]: l["name"] for l in c.list_labels()}
        console.print(f"[cyan]{identifier}[/cyan] [bold]{issue.get('name')}[/bold]")
        console.print(f"  State    : {states_by_id.get(issue.get('state'), '?')}")
        console.print(f"  Priority : {issue.get('priority', 'none')}")
        labels = [labels_by_id.get(lid, '?') for lid in (issue.get('labels') or [])]
        console.print(f"  Labels   : {', '.join(labels) if labels else '-'}")
        console.print(f"  Estimate : {issue.get('estimate_point', '-')}")
        console.print(f"  Due      : {issue.get('target_date', '-')}")


@app.command()
def doctor():
    """Verify .env config and connectivity."""
    defaults = BotDefaults.from_env()
    console.print("[bold]Defaults:[/bold]")
    console.print(f"  module_id  = {defaults.module_id or '[yellow]not set[/yellow]'}")
    console.print(f"  label_ids  = {defaults.label_ids or '[dim](none)[/dim]'}")
    console.print(f"  estimate   = {defaults.estimate}")
    console.print(f"  priority   = {defaults.priority}\n")

    with _client() as c:
        try:
            projects = c.list_projects()
            console.print(f"[green]✓[/green] Connected. {len(projects)} project(s) visible.")
            console.print(f"[green]✓[/green] {len(c.list_states())} states.")
            if defaults.module_id:
                modules = c.list_modules()
                m = next((m for m in modules if m.get("id") == defaults.module_id), None)
                if m:
                    console.print(f"[green]✓[/green] Default module: [cyan]{m.get('name')}[/cyan]")
                else:
                    console.print(f"[red]✗[/red] PLANE_DEFAULT_MODULE_ID is set but doesn't match any module.")
        except PlaneError as e:
            console.print(f"[red]✗ Connection failed:[/red] {e}")
            raise typer.Exit(1)


def main():
    app()


if __name__ == "__main__":
    main()
