from __future__ import annotations

from pathlib import Path
from typing import Optional

import csv
import json

import click

from core.config import AppConfig, ConfigError, load_config
from core.jira_client import JiraClient
from core.policy_engine import PolicyEngine


def _load_app_config(config_path: str) -> AppConfig:
    try:
        return load_config(config_path)
    except ConfigError as e:
        raise click.ClickException(str(e)) from e


def _build_engine(config_path: str) -> PolicyEngine:
    app_config = _load_app_config(config_path)
    client = JiraClient(app_config.jira)
    return PolicyEngine(app_config, client)


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    default="config/config.yaml",
    show_default=True,
    help="Path to config YAML file.",
)
@click.pass_context
def main(ctx: click.Context, config_path: str) -> None:
    """Jira Data Center admin suite for project roles and permissions."""

    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


@main.group()
def config() -> None:  # type: ignore[override]
    """Configuration commands."""


@config.command("validate")
@click.pass_context
def config_validate(ctx: click.Context) -> None:
    """Validate configuration and Jira connectivity."""

    config_path = ctx.obj["config_path"]
    app_config = _load_app_config(config_path)
    client = JiraClient(app_config.jira)

    # Simple connectivity check: list projects
    try:
        projects = client.list_projects()
    except Exception as e:  # pragma: no cover - network errors
        raise click.ClickException(f"Failed to connect to Jira: {e}") from e

    click.echo(f"Config OK. Jira base_url={app_config.jira.base_url}, projects={len(projects)}")


@main.group()
def projects() -> None:  # type: ignore[override]
    """Project-related commands."""


@projects.command("list")
@click.option("--policy", "policy_id", type=str, help="Filter by policy id.")
@click.pass_context
def projects_list(ctx: click.Context, policy_id: Optional[str]) -> None:
    """List projects, optionally scoped to a policy."""

    engine = _build_engine(ctx.obj["config_path"])
    if policy_id:
        policy = next((p for p in engine._config.policies if p.id == policy_id), None)
        if not policy:
            raise click.ClickException(f"Unknown policy id: {policy_id}")
        projects = engine.list_projects_for_policy(policy)
    else:
        client = JiraClient(engine._config.jira)
        projects = client.list_projects()

    for p in projects:
        click.echo(f"{p.key}\t{p.name}\t{p.project_type}")


@main.group()
def policies() -> None:  # type: ignore[override]
    """Policy commands."""


@policies.command("list")
@click.pass_context
def policies_list(ctx: click.Context) -> None:
    """List policies defined in the config."""

    app_config = _load_app_config(ctx.obj["config_path"])
    for p in app_config.policies:
        click.echo(f"{p.id}\t{p.role_name}\t{p.enforcement.mode}")


@main.group()
def policy() -> None:  # type: ignore[override]
    """Single-policy operations."""


@policy.command("dry-run")
@click.argument("policy_id", type=str)
@click.option("--project", "project_key", type=str, help="Limit to a specific project key.")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def policy_dry_run(
    ctx: click.Context,
    policy_id: str,
    project_key: Optional[str],
    as_json: bool,
) -> None:
    """Show planned adds/removes for a policy."""

    engine = _build_engine(ctx.obj["config_path"])
    result = engine.evaluate_policy(policy_id, project_key=project_key)

    if as_json:
        payload = {
            "policy": result.policy.id,
            "total_additions": result.total_additions,
            "total_removals": result.total_removals,
            "diffs": [
                {
                    "project": d.project.key,
                    "role_id": d.role_id,
                    "before": {
                        "users": sorted(d.current_users),
                        "groups": sorted(d.current_groups),
                    },
                    "after": {
                        "users": sorted(d.planned_users),
                        "groups": sorted(d.planned_groups),
                    },
                    "to_add": sorted(d.to_add),
                    "to_remove": sorted(d.to_remove),
                    "groups_to_add": sorted(d.groups_to_add),
                    "groups_to_remove": sorted(d.groups_to_remove),
                    "already_present": sorted(d.already_present),
                    "covered_via_group": sorted(d.covered_via_group),
                    "warnings": d.warnings,
                }
                for d in result.diffs
            ],
            "unresolved_users": result.unresolved_users,
            "ambiguous_users": result.ambiguous_users,
        }
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Policy: {result.policy.id} / {result.policy.name}")
    click.echo(
        f"Total additions: {result.total_additions}, removals: {result.total_removals}"
    )
    if result.unresolved_users:
        click.echo("Unresolved users: " + ", ".join(result.unresolved_users))
    if result.ambiguous_users:
        click.echo("Ambiguous users:")
        for ident, count in result.ambiguous_users.items():
            click.echo(f"  {ident}: {count} candidates")

    for d in result.diffs:
        click.echo("")
        click.echo(f"Project {d.project.key} ({d.project.name}) role_id={d.role_id}")
        click.echo(
            "  Before: users=["
            + ", ".join(sorted(d.current_users))
            + "] groups=["
            + ", ".join(sorted(d.current_groups))
            + "]"
        )
        click.echo(
            "  After:  users=["
            + ", ".join(sorted(d.planned_users))
            + "] groups=["
            + ", ".join(sorted(d.planned_groups))
            + "]"
        )
        if d.to_add:
            click.echo("  Users to add:      " + ", ".join(sorted(d.to_add)))
        if d.to_remove:
            click.echo("  Users to remove:   " + ", ".join(sorted(d.to_remove)))
        if d.groups_to_add:
            click.echo("  Groups to add:     " + ", ".join(sorted(d.groups_to_add)))
        if d.groups_to_remove:
            click.echo("  Groups to remove:  " + ", ".join(sorted(d.groups_to_remove)))
        if d.already_present:
            click.echo("  Already in role:   " + ", ".join(sorted(d.already_present)))
        if d.covered_via_group:
            click.echo("  Covered via group: " + ", ".join(sorted(d.covered_via_group)))
        for w in d.warnings:
            click.echo(f"  WARNING: {w}")


@policy.command("group-audit")
@click.argument("policy_id", type=str)
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.pass_context
def policy_group_audit(ctx: click.Context, policy_id: str, as_json: bool) -> None:
    """Compare a policy's configured user list against its target_group members."""

    engine = _build_engine(ctx.obj["config_path"])
    audit = engine.audit_group_membership(policy_id)

    if as_json:
        payload = {
            "policy": audit.policy.id,
            "group": audit.group_name,
            "expected_users": sorted(audit.expected_users),
            "current_members": sorted(audit.current_members),
            "to_add": sorted(audit.to_add),
            "to_remove": sorted(audit.to_remove),
            "already_present": sorted(audit.already_present),
            "unresolved_users": audit.unresolved_users,
            "ambiguous_users": audit.ambiguous_users,
        }
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Policy: {audit.policy.id} / {audit.policy.name}")
    click.echo(f"Target group: {audit.group_name}")
    click.echo(
        f"Expected: {len(audit.expected_users)} user(s), "
        f"Current: {len(audit.current_members)} member(s)"
    )
    if audit.unresolved_users:
        click.echo("Unresolved users: " + ", ".join(audit.unresolved_users))
    if audit.ambiguous_users:
        click.echo("Ambiguous users:")
        for ident, count in audit.ambiguous_users.items():
            click.echo(f"  {ident}: {count} candidates")
    if audit.to_add:
        click.echo("Add to group:       " + ", ".join(sorted(audit.to_add)))
    if audit.to_remove:
        click.echo("Remove from group:  " + ", ".join(sorted(audit.to_remove)))
    if audit.already_present:
        click.echo("Already in group:   " + ", ".join(sorted(audit.already_present)))
    if not audit.to_add and not audit.to_remove:
        click.echo("Group membership matches the configured user list.")


@policy.command("apply")
@click.argument("policy_id", type=str)
@click.option("--project", "project_key", type=str, help="Limit to a specific project key.")
@click.option("--yes", is_flag=True, help="Apply without interactive confirmation.")
@click.option("--force", is_flag=True, help="Bypass max_changes_per_project guardrail.")
@click.pass_context
def policy_apply(
    ctx: click.Context,
    policy_id: str,
    project_key: Optional[str],
    yes: bool,
    force: bool,
) -> None:
    """Apply changes for a policy after confirmation."""

    engine = _build_engine(ctx.obj["config_path"])
    result = engine.evaluate_policy(policy_id, project_key=project_key)

    total_add = result.total_additions
    total_remove = result.total_removals
    if total_add == 0 and total_remove == 0:
        click.echo("No changes to apply.")
        return

    if not yes:
        msg = f"Apply policy {policy_id} with {total_add} additions and {total_remove} removals?"
        if not click.confirm(msg):
            click.echo("Aborted.")
            return

    result = engine.apply_policy(policy_id, project_key=project_key, force=force)

    click.echo(
        f"Applied policy {policy_id}. Additions={result.total_additions}, removals={result.total_removals}",
    )


@main.group()
def users() -> None:  # type: ignore[override]
    """User-related commands."""


@users.command("inventory")
@click.option("--output", type=click.Path(dir_okay=False, writable=True, path_type=str))
@click.pass_context
def users_inventory(ctx: click.Context, output: Optional[str]) -> None:
    """Export all users (username, key, email, displayName)."""

    app_config = _load_app_config(ctx.obj["config_path"])
    client = JiraClient(app_config.jira)
    users = client.list_all_users()

    records = [
        {
            "username": u.username,
            "account_id": u.account_id,
            "email": u.email,
            "display_name": u.display_name,
        }
        for u in users
    ]

    if output:
        path = Path(output)
        path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        click.echo(f"Wrote {len(records)} users to {path}")
    else:
        for r in records:
            click.echo(f"{r['username']}\t{r['email']}\t{r['display_name']}")


@users.command("find")
@click.option("--pattern", required=True, type=str, help="Pattern like firstname.lastname")
@click.pass_context
def users_find(ctx: click.Context, pattern: str) -> None:
    """Heuristic user lookup for firstname.lastname pattern."""

    app_config = _load_app_config(ctx.obj["config_path"])
    client = JiraClient(app_config.jira)
    from core.user_discovery import UserDiscovery

    discovery = UserDiscovery(client)

    # Treat as firstname_lastname identifier type
    result = discovery.resolve_identifiers(
        [pattern],
        identifier_type="firstname_lastname",
        email_domains=["example.com"],  # could be overridden later via config
    )

    if result.resolved:
        click.echo("Resolved:")
        for ident, user in result.resolved.items():
            click.echo(f"  {ident} -> {user.username} ({user.email})")
    if result.ambiguous:
        click.echo("Ambiguous:")
        for ident, users in result.ambiguous.items():
            click.echo(f"  {ident}:")
            for u in users:
                click.echo(f"    - {u.username} ({u.email})")
    if result.unresolved:
        click.echo("Unresolved: " + ", ".join(result.unresolved))


@main.group()
def roles() -> None:  # type: ignore[override]
    """Role-related commands."""


@roles.command("list")
@click.option("--csv", "as_csv", is_flag=True, help="Output CSV instead of tab-delimited text.")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.option("--markdown", "as_markdown", is_flag=True, help="Output Markdown table instead of text.")
@click.pass_context
def roles_list(ctx: click.Context, as_csv: bool, as_json: bool, as_markdown: bool) -> None:
    """List all distinct roles across projects."""

    selected_modes = sum(1 for flag in (as_csv, as_json, as_markdown) if flag)
    if selected_modes > 1:
        raise click.ClickException("Choose only one of --csv, --json, or --markdown.")

    app_config = _load_app_config(ctx.obj["config_path"])
    client = JiraClient(app_config.jira)

    projects = client.list_projects()

    role_projects: dict[tuple[str, int], set[str]] = {}
    for p in projects:
        roles = client.get_project_roles(p.key)
        for name, role_id in roles.items():
            key = (name, role_id)
            if key not in role_projects:
                role_projects[key] = set()
            role_projects[key].add(p.key)

    # Normalize to a list of dicts
    records = []
    for (name, role_id), proj_keys in sorted(role_projects.items(), key=lambda x: x[0][0].lower()):
        records.append(
            {
                "role_id": role_id,
                "role_name": name,
                "projects": ",".join(sorted(proj_keys)),
            }
        )

    if as_json:
        click.echo(json.dumps(records, indent=2))
        return

    if as_csv:
        writer = csv.writer(click.get_text_stream("stdout"))
        writer.writerow(["role_id", "role_name", "projects"])
        for r in records:
            writer.writerow([r["role_id"], r["role_name"], r["projects"]])
        return

    if as_markdown:
        click.echo("| role_id | role_name | projects |")
        click.echo("| --- | --- | --- |")
        for r in records:
            click.echo(f"| {r['role_id']} | {r['role_name']} | {r['projects']} |")
        return

    # Default: tab-delimited text
    for r in records:
        click.echo(f"{r['role_id']}\t{r['role_name']}\t{r['projects']}")


@roles.command("for-project")
@click.argument("project_key", type=str)
@click.option("--csv", "as_csv", is_flag=True, help="Output CSV instead of tab-delimited text.")
@click.option("--json", "as_json", is_flag=True, help="Output JSON instead of text.")
@click.option("--markdown", "as_markdown", is_flag=True, help="Output Markdown table instead of text.")
@click.pass_context
def roles_for_project(ctx: click.Context, project_key: str, as_csv: bool, as_json: bool, as_markdown: bool) -> None:
    """List roles defined for a single project."""

    selected_modes = sum(1 for flag in (as_csv, as_json, as_markdown) if flag)
    if selected_modes > 1:
        raise click.ClickException("Choose only one of --csv, --json, or --markdown.")

    app_config = _load_app_config(ctx.obj["config_path"])
    client = JiraClient(app_config.jira)

    roles = client.get_project_roles(project_key)
    if not roles:
        click.echo(f"No roles found for project {project_key}")
        return

    records = [
        {"role_id": role_id, "role_name": name}
        for name, role_id in sorted(roles.items(), key=lambda x: x[0].lower())
    ]

    if as_json:
        click.echo(json.dumps(records, indent=2))
        return

    if as_csv:
        writer = csv.writer(click.get_text_stream("stdout"))
        writer.writerow(["role_id", "role_name"])
        for r in records:
            writer.writerow([r["role_id"], r["role_name"]])
        return

    if as_markdown:
        click.echo("| role_id | role_name |")
        click.echo("| --- | --- |")
        for r in records:
            click.echo(f"| {r['role_id']} | {r['role_name']} |")
        return

    # Default: tab-delimited text
    for r in records:
        click.echo(f"{r['role_id']}\t{r['role_name']}")


if __name__ == "__main__":  # pragma: no cover
    main()
