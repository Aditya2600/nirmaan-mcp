"""
Smoke test for the Plane API client.

Run:
    python scripts/smoke_test.py

What it does:
    1. Connects to your Plane instance using PLANE_API_KEY.
    2. Lists projects in your workspace (helps you find PLANE_PROJECT_ID).
    3. If PLANE_PROJECT_ID is set, lists states, labels, modules, and the current cycle.
    4. Optionally creates a small test work item (you confirm with Y/n).
"""
import sys
from pathlib import Path

# Make the package importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from nirmaan import PlaneClient, PlaneConfig, PlaneError


console = Console()


def main() -> int:
    try:
        config = PlaneConfig.from_env(require_project=False)
    except RuntimeError as e:
        console.print(f"[red]Config error:[/red] {e}")
        return 1

    console.print(f"[dim]Base URL  :[/dim] {config.base_url}")
    console.print(f"[dim]Workspace :[/dim] {config.workspace_slug}")
    console.print(f"[dim]Project ID:[/dim] {config.project_id or '(not set yet)'}\n")
    console.print(f"[dim]Module ID :[/dim] {config.module_id or '(not set)'}\n")

    with PlaneClient(config) as client:
        # ----- 1. Projects -----
        console.rule("Projects in your workspace")
        try:
            projects = client.list_projects()
        except PlaneError as e:
            console.print(f"[red]Failed:[/red] {e}\n")
            console.print(
                "[yellow]Common causes:[/yellow]\n"
                "  - API key wrong or expired\n"
                "  - Workspace slug wrong (check your URL)\n"
                "  - Endpoint path differs in your Plane version "
                "(see https://docs.plane.so)"
            )
            return 1

        if not projects:
            console.print(
                "[yellow]No projects found. Verify PLANE_WORKSPACE_SLUG.[/yellow]"
            )
            return 1

        t = Table(show_header=True, header_style="bold")
        t.add_column("Name")
        t.add_column("Identifier")
        t.add_column("ID  (paste into PLANE_PROJECT_ID)", style="cyan")
        for p in projects:
            t.add_row(
                str(p.get("name", "?")),
                str(p.get("identifier", "?")),
                str(p.get("id", "?")),
            )
        console.print(t)

        if not config.project_id:
            console.print(
                "\n[yellow]Next:[/yellow] copy the ID for your project (e.g. 'Builder') "
                "into PLANE_PROJECT_ID in .env, then re-run this script."
            )
            return 0

        # ----- 2. States -----
        console.rule("States")
        for s in client.list_states():
            console.print(
                f"  {s.get('name', '?'):<20} "
                f"group={s.get('group', '?'):<12} "
                f"id={s.get('id', '?')}"
            )

        # ----- 3. Labels -----
        console.rule("Labels")
        labels = client.list_labels()
        if labels:
            for label in labels:
                console.print(
                    f"  {label.get('name', '?'):<20} id={label.get('id', '?')}"
                )
        else:
            console.print("  [dim]No labels yet.[/dim]")

        # ----- Modules -----
        console.rule("Modules")
        modules = client.list_modules()
        if modules:
            for m in modules:
                marker = "  ← AgentX" if m.get("name", "").lower() == "agentx" else ""
                console.print(
                    f"  {m.get('name', '?'):<20} id={m.get('id', '?')}{marker}"
                )
        else:
            console.print("  [dim]No modules yet.[/dim]")

        # ----- 5. Current cycle -----
        console.rule("Current cycle")
        cycle = client.get_current_cycle()
        if cycle:
            console.print(
                f"  [green]{cycle.get('name')}[/green] "
                f"({cycle.get('start_date')} → {cycle.get('end_date')}) "
                f"id={cycle.get('id')}"
            )
        else:
            console.print("  [dim]No active cycle right now.[/dim]")

        # ----- 6. Optional: create a test ticket -----
        console.rule("Create test work item?")
        console.print(
            "This will create a work item titled "
            "[bold]'Smoke test — please ignore or delete'[/bold] in your project."
        )
        if config.module_id:
            console.print(
                f"It will also attach the work item to module id "
                f"[cyan]{config.module_id}[/cyan]."
            )
        ans = input("Create it now? [y/N]: ").strip().lower()
        if ans == "y":
            done_state = client.find_state("Done")
            try:
                issue = client.create_issue(
                    name="Smoke test — please ignore or delete",
                    description_html=(
                        "<p>Created by nirmaan-bot smoke_test.py to verify API access. "
                        "Safe to delete.</p>"
                    ),
                    state_id=done_state["id"] if done_state else None,
                    priority="none",
                    module_ids=[config.module_id] if config.module_id else None,
                )
                console.print(f"\n[green]Created:[/green] {issue.get('name')}")
                console.print(f"  ID         : {issue.get('id')}")
                console.print(f"  Sequence   : {issue.get('sequence_id')}")
                if config.module_id:
                    console.print(f"  Module ID  : {config.module_id}")
                console.print(
                    "\nOpen it in Nirmaan to verify it shows up correctly, then delete."
                )
            except PlaneError as e:
                console.print(f"[red]Create failed:[/red] {e}")
                return 1
        else:
            console.print("[dim]Skipped.[/dim]")

    console.rule("[green]Smoke test complete[/green]")
    console.print(
        "\nIf everything above looks right, Phase 1 is working. Ready for Phase 2 (CLI)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
