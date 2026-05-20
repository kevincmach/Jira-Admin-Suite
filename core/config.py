from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import os

import yaml


@dataclass
class JiraAuthConfig:
    mode: str
    username_env_var: Optional[str] = None
    password_env_var: Optional[str] = None
    token_env_var: Optional[str] = None


@dataclass
class JiraConfig:
    base_url: str
    auth: JiraAuthConfig


@dataclass
class ProjectFilterConfig:
    include_keys: List[str] = field(default_factory=list)
    exclude_keys: List[str] = field(default_factory=list)
    include_types: List[str] = field(default_factory=list)
    exclude_types: List[str] = field(default_factory=list)
    match_key_regex: Optional[str] = None
    match_name_regex: Optional[str] = None


@dataclass
class UserSourceConfig:
    mode: str
    identifier_type: str
    users: List[str] = field(default_factory=list)
    file_path: Optional[str] = None
    jira_group: Optional[str] = None


@dataclass
class MatchingRulesConfig:
    use_firstname_lastname: bool = False
    email_domain_whitelist: List[str] = field(default_factory=list)


@dataclass
class EnforcementConfig:
    mode: str = "enforce_exact"  # enforce_exact, add_only, remove_only, ensure_group_present, ensure_group_exact
    allow_groups: bool = True
    tolerate_missing_users: bool = True
    max_changes_per_project: int = 10


@dataclass
class PolicyConfig:
    id: str
    name: str
    role_name: str
    project_filters: ProjectFilterConfig
    user_source: UserSourceConfig
    matching_rules: MatchingRulesConfig
    enforcement: EnforcementConfig
    target_group: Optional[str] = None


@dataclass
class AppConfig:
    jira: JiraConfig
    project_filters: ProjectFilterConfig
    policies: List[PolicyConfig]


class ConfigError(Exception):
    """Raised when configuration is invalid."""


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"Config file not found: {path}") from exc


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    raw = _load_yaml(path)

    if "jira" not in raw:
        raise ConfigError("Missing 'jira' section in config")

    jira_raw = raw["jira"]
    base_url = jira_raw.get("base_url")
    if not base_url:
        raise ConfigError("jira.base_url is required")

    auth_raw = jira_raw.get("auth", {})
    auth = JiraAuthConfig(
        mode=auth_raw.get("mode", "basic"),
        username_env_var=auth_raw.get("username_env_var"),
        password_env_var=auth_raw.get("password_env_var"),
        token_env_var=auth_raw.get("token_env_var"),
    )

    jira = JiraConfig(base_url=base_url.rstrip("/"), auth=auth)

    global_filters = _build_project_filters(raw.get("project_filters", {}))

    policies_raw = raw.get("policies", [])
    if not isinstance(policies_raw, list):
        raise ConfigError("policies must be a list")

    policies: List[PolicyConfig] = []
    for p in policies_raw:
        policy = _build_policy_config(p, global_filters)
        policies.append(policy)

    return AppConfig(jira=jira, project_filters=global_filters, policies=policies)


def _build_project_filters(raw: Dict[str, Any]) -> ProjectFilterConfig:
    return ProjectFilterConfig(
        include_keys=list(raw.get("include_keys", []) or []),
        exclude_keys=list(raw.get("exclude_keys", []) or []),
        include_types=list(raw.get("include_types", []) or []),
        exclude_types=list(raw.get("exclude_types", []) or []),
        match_key_regex=raw.get("match_key_regex"),
        match_name_regex=raw.get("match_name_regex"),
    )


_GROUP_MODES = {"ensure_group_present", "ensure_group_exact"}


def _build_policy_config(raw: Dict[str, Any], global_filters: ProjectFilterConfig) -> PolicyConfig:
    required_fields = ["id", "name", "role_name"]
    for field_name in required_fields:
        if field_name not in raw:
            raise ConfigError(f"Policy missing required field: {field_name}")

    enforcement_mode = (raw.get("enforcement") or {}).get("mode", "enforce_exact")
    target_group = raw.get("target_group")

    # user_source is required for user-list policies; for group-based policies
    # it is optional (and only used by the `policy group-audit` flow).
    if enforcement_mode not in _GROUP_MODES and "user_source" not in raw:
        raise ConfigError("Policy missing required field: user_source")

    if enforcement_mode in _GROUP_MODES and not target_group:
        raise ConfigError(
            f"Policy '{raw.get('id')}' uses enforcement.mode={enforcement_mode} "
            f"but no target_group is set."
        )

    pf_raw = raw.get("project_filters", {})
    # Policy filters override global ones when provided, otherwise inherit
    project_filters = ProjectFilterConfig(
        include_keys=pf_raw.get("include_keys", global_filters.include_keys),
        exclude_keys=pf_raw.get("exclude_keys", global_filters.exclude_keys),
        include_types=pf_raw.get("include_types", global_filters.include_types),
        exclude_types=pf_raw.get("exclude_types", global_filters.exclude_types),
        match_key_regex=pf_raw.get("match_key_regex", global_filters.match_key_regex),
        match_name_regex=pf_raw.get("match_name_regex", global_filters.match_name_regex),
    )

    us_raw = raw.get("user_source")
    if us_raw is None:
        # Permitted for group-mode policies; create a stub.
        user_source = UserSourceConfig(mode="static_list", identifier_type="username")
    else:
        if "mode" not in us_raw or "identifier_type" not in us_raw:
            raise ConfigError("user_source.mode and user_source.identifier_type are required")
        user_source = UserSourceConfig(
            mode=us_raw["mode"],
            identifier_type=us_raw["identifier_type"],
            users=list(us_raw.get("users", []) or []),
            file_path=us_raw.get("file_path"),
            jira_group=us_raw.get("jira_group"),
        )

    mr_raw = raw.get("matching_rules", {})
    matching_rules = MatchingRulesConfig(
        use_firstname_lastname=bool(mr_raw.get("use_firstname_lastname", False)),
        email_domain_whitelist=list(mr_raw.get("email_domain_whitelist", []) or []),
    )

    enf_raw = raw.get("enforcement", {})
    enforcement = EnforcementConfig(
        mode=enf_raw.get("mode", "enforce_exact"),
        allow_groups=bool(enf_raw.get("allow_groups", True)),
        tolerate_missing_users=bool(enf_raw.get("tolerate_missing_users", True)),
        max_changes_per_project=int(enf_raw.get("max_changes_per_project", 10)),
    )

    return PolicyConfig(
        id=raw["id"],
        name=raw["name"],
        role_name=raw["role_name"],
        project_filters=project_filters,
        user_source=user_source,
        matching_rules=matching_rules,
        enforcement=enforcement,
        target_group=target_group,
    )


def resolve_auth_credentials(auth: JiraAuthConfig) -> Dict[str, str]:
    """Resolve credentials from environment based on auth config.

    Returns a dict suitable for `requests` auth/headers.
    """

    if auth.mode == "basic":
        if not auth.username_env_var or not auth.password_env_var:
            raise ConfigError("Basic auth requires username_env_var and password_env_var")
        username = os.getenv(auth.username_env_var)
        password = os.getenv(auth.password_env_var)
        if not username or not password:
            raise ConfigError(
                f"Environment variables {auth.username_env_var} and {auth.password_env_var} must be set"
            )
        return {"type": "basic", "username": username, "password": password}

    if auth.mode == "token":
        if not auth.token_env_var:
            raise ConfigError("Token auth requires token_env_var")
        token = os.getenv(auth.token_env_var)
        if not token:
            raise ConfigError(f"Environment variable {auth.token_env_var} must be set")
        return {"type": "token", "token": token}

    raise ConfigError(f"Unsupported auth.mode: {auth.mode}")
