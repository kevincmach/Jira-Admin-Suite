from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Flask, render_template, request, redirect, url_for, flash

from core.config import AppConfig, ConfigError, JiraConfig, PolicyConfig, load_config
from core.jira_client import JiraClient
from core.policy_engine import PolicyEngine


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
HISTORY_LOG_PATH = BASE_DIR / "webapp" / "history.log.jsonl"

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


def create_app() -> Flask:
    app = Flask(__name__)
    # For local dev only; override via FLASK_SECRET_KEY env var or similar in prod.
    app.secret_key = "change-me-for-production"

    @app.context_processor
    def inject_globals() -> dict[str, Any]:  # type: ignore[override]
        return {
            "app_title": "Jira Admin Suite",
        }

    def get_policy_engine() -> PolicyEngine:
        try:
            config: AppConfig = load_config(CONFIG_PATH)
        except ConfigError as exc:  # noqa: BLE001
            logger.error("Failed to load config: %s", exc)
            raise
        client = JiraClient(config.jira)
        return PolicyEngine(config, client)

    def log_execution(
        *,
        action: str,
        policy_id: Optional[str],
        project_key: Optional[str],
        result: Optional[Any],
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Append a structured execution record to the history log.

        Uses a simple JSON-lines file for now so we avoid introducing
        additional dependencies; can be swapped for SQLite later.
        """

        record: dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "action": action,
            "policy_id": policy_id,
            "project_key": project_key,
            "status": status,
        }

        if result is not None:
            try:
                record.update(
                    {
                        "total_additions": getattr(result, "total_additions", None),
                        "total_removals": getattr(result, "total_removals", None),
                    }
                )
            except Exception:  # noqa: BLE001
                # Best-effort; don't break UI if structure changes.
                pass

        if error is not None:
            record["error"] = error

        try:
            HISTORY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with HISTORY_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:  # noqa: BLE001
            logger.warning("Failed to write history log: %s", exc)

    def read_history(limit: int = 50) -> list[dict[str, Any]]:
        """Read last N execution records from the history log.

        This is intentionally simple and optimized for small to
        moderate log sizes; can be replaced with database-backed
        storage later without changing callers.
        """

        if not HISTORY_LOG_PATH.exists():
            return []

        lines: list[str] = []
        try:
            with HISTORY_LOG_PATH.open("r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError as exc:  # noqa: BLE001
            logger.warning("Failed to read history log: %s", exc)
            return []

        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            if len(records) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def find_policy(config: AppConfig, policy_id: str) -> Optional[PolicyConfig]:
        for policy in config.policies:
            if policy.id == policy_id:
                return policy
        return None

    @app.route("/", methods=["GET"])
    def dashboard() -> str:
        """Dashboard: show high-level summary and recent executions."""

        engine = get_policy_engine()
        config = engine._config

        policies = config.policies
        policy_count = len(policies)

        history_records = read_history(limit=20)

        return render_template(
            "dashboard.html",
            policy_count=policy_count,
            policies=policies,
            history_records=history_records,
        )

    @app.route("/run", methods=["GET", "POST"])
    def run_policy() -> str:
        """Run a policy: dry-run or apply, with results view."""

        engine = get_policy_engine()
        config = engine._config

        selected_policy_id: Optional[str] = None
        project_key: str = ""
        plan_result: Optional[Any] = None

        if request.method == "POST":
            form_action = request.form.get("action") or "plan"
            selected_policy_id = request.form.get("policy_id") or None
            project_key = (request.form.get("project_key") or "").strip()

            if not selected_policy_id:
                flash("Please select a policy.", "error")
            else:
                try:
                    if form_action == "apply":
                        result = engine.apply_policy(
                            selected_policy_id,
                            project_key=project_key or None,
                            force=False,
                        )
                        plan_result = result
                        log_execution(
                            action="apply",
                            policy_id=selected_policy_id,
                            project_key=project_key or None,
                            result=result,
                            status="success",
                        )
                        flash(
                            f"Applied policy {result.policy.id}: "
                            f"additions={result.total_additions}, removals={result.total_removals}",
                            "success",
                        )
                    else:
                        result = engine.evaluate_policy(
                            selected_policy_id,
                            project_key=project_key or None,
                        )
                        plan_result = result
                        log_execution(
                            action="plan",
                            policy_id=selected_policy_id,
                            project_key=project_key or None,
                            result=result,
                            status="success",
                        )
                        flash(
                            f"Planned policy {result.policy.id}: "
                            f"additions={result.total_additions}, removals={result.total_removals}",
                            "info",
                        )
                except ValueError as exc:  # invalid policy id or similar
                    logger.warning("Policy error: %s", exc)
                    log_execution(
                        action=form_action,
                        policy_id=selected_policy_id,
                        project_key=project_key or None,
                        result=None,
                        status="error",
                        error=str(exc),
                    )
                    flash(str(exc), "error")
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Unexpected error running policy")
                    log_execution(
                        action=form_action,
                        policy_id=selected_policy_id,
                        project_key=project_key or None,
                        result=None,
                        status="error",
                        error=str(exc),
                    )
                    flash(f"Unexpected error: {exc}", "error")

        policies = config.policies

        return render_template(
            "index.html",
            policies=policies,
            selected_policy_id=selected_policy_id,
            project_key=project_key,
            plan_result=plan_result,
        )

    @app.route("/policies/<policy_id>", methods=["GET"])
    def policy_detail(policy_id: str) -> str:
        """View a single policy's configuration and scope."""

        engine = get_policy_engine()
        config = engine._config

        policy = find_policy(config, policy_id)
        if policy is None:
            flash(f"Unknown policy id: {policy_id}", "error")
            return redirect(url_for("dashboard"))

        projects = engine.list_projects_for_policy(policy)

        return render_template(
            "policy_detail.html",
            policy=policy,
            projects=projects,
        )

    @app.route("/config", methods=["GET"])
    def config_view() -> str:
        """Read-only view of Jira config and roles mapping."""

        try:
            config: AppConfig = load_config(CONFIG_PATH)
        except ConfigError as exc:  # noqa: BLE001
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))

        jira: JiraConfig = config.jira
        jira_info = {
            "base_url": jira.base_url,
            "auth_mode": jira.auth.mode,
            # Do not expose actual env var values, only names.
            "username_env_var": jira.auth.username_env_var,
            "password_env_var": jira.auth.password_env_var,
            "token_env_var": jira.auth.token_env_var,
        }

        # For now, roles map is driven by policies (role_name field).
        unique_roles = sorted({p.role_name for p in config.policies})

        roles_mapping = [
            {
                "role_name": role_name,
                "policies": [p for p in config.policies if p.role_name == role_name],
            }
            for role_name in unique_roles
        ]

        return render_template(
            "config_view.html",
            jira_info=jira_info,
            roles_mapping=roles_mapping,
        )

    @app.route("/history", methods=["GET"])
    def history_view() -> str:
        """Display recent execution history with basic filters."""

        # Simple in-memory filtering over JSONL records.
        all_records = read_history(limit=200)

        policy_filter = request.args.get("policy_id") or None
        project_filter = request.args.get("project_key") or None
        action_filter = request.args.get("action") or None

        filtered = []
        for rec in all_records:
            if policy_filter and rec.get("policy_id") != policy_filter:
                continue
            if project_filter and rec.get("project_key") != project_filter:
                continue
            if action_filter and rec.get("action") != action_filter:
                continue
            filtered.append(rec)

        return render_template(
            "history.html",
            records=filtered,
            raw_records=all_records,
            policy_filter=policy_filter or "",
            project_filter=project_filter or "",
            action_filter=action_filter or "",
        )

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    # Local development entrypoint
    app.run(host="127.0.0.1", port=5000, debug=True)
