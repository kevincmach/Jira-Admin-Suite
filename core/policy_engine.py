from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import re

from .config import AppConfig, PolicyConfig
from .jira_client import JiraClient, JiraProject, JiraRoleActor
from .user_discovery import UserDiscovery, UserResolutionResult


@dataclass
class ProjectRoleDiff:
    project: JiraProject
    role_id: Optional[int]
    to_add: Set[str] = field(default_factory=set)
    to_remove: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)


@dataclass
class PolicyEvaluationResult:
    policy: PolicyConfig
    diffs: List[ProjectRoleDiff]
    unresolved_users: List[str]
    ambiguous_users: Dict[str, int]

    @property
    def total_additions(self) -> int:
        return sum(len(d.to_add) for d in self.diffs)

    @property
    def total_removals(self) -> int:
        return sum(len(d.to_remove) for d in self.diffs)


class PolicyEngine:
    def __init__(self, config: AppConfig, client: JiraClient):
        self._config = config
        self._client = client
        self._user_discovery = UserDiscovery(client)

    # --- Project filtering ---

    def _project_in_scope(self, project: JiraProject, pf) -> bool:
        if pf.include_keys and project.key not in pf.include_keys:
            return False
        if pf.exclude_keys and project.key in pf.exclude_keys:
            return False
        if pf.include_types and project.project_type not in pf.include_types:
            return False
        if pf.exclude_types and project.project_type in pf.exclude_types:
            return False
        if pf.match_key_regex and not re.search(pf.match_key_regex, project.key or ""):
            return False
        if pf.match_name_regex and not re.search(pf.match_name_regex, project.name or ""):
            return False
        return True

    def list_projects_for_policy(self, policy: PolicyConfig) -> List[JiraProject]:
        projects = self._client.list_projects()
        return [p for p in projects if self._project_in_scope(p, policy.project_filters)]

    # --- Policy evaluation ---

    def evaluate_policy(self, policy_id: str, project_key: Optional[str] = None) -> PolicyEvaluationResult:
        policy = next((p for p in self._config.policies if p.id == policy_id), None)
        if not policy:
            raise ValueError(f"Unknown policy id: {policy_id}")

        projects = self.list_projects_for_policy(policy)
        if project_key:
            projects = [p for p in projects if p.key == project_key]

        identifiers = policy.user_source.users
        identifier_type = policy.user_source.identifier_type

        # For policies that specify identifier_type="username", treat the configured
        # values as canonical usernames directly instead of going through
        # UserDiscovery heuristics. This matches environments where Jira role
        # actors and usernames share the same value (e.g. "Firstname.Lastname").
        if identifier_type == "username":
            canonical_usernames: Set[str] = set(identifiers)
            unresolved: List[str] = []
            ambiguous: Dict[str, int] = {}
        else:
            if policy.matching_rules.use_firstname_lastname and identifier_type == "username":
                identifier_type = "firstname_lastname"

            resolution: UserResolutionResult = self._user_discovery.resolve_identifiers(
                identifiers,
                identifier_type=identifier_type,
                email_domains=policy.matching_rules.email_domain_whitelist,
            )

            canonical_usernames = set()
            for ident, user in resolution.resolved.items():
                if user.username:
                    canonical_usernames.add(user.username)

            unresolved = resolution.unresolved
            ambiguous = {k: len(v) for k, v in resolution.ambiguous.items()}

        diffs: List[ProjectRoleDiff] = []
        for project in projects:
            diff = self._evaluate_policy_for_project(policy, project, canonical_usernames)
            diffs.append(diff)

        return PolicyEvaluationResult(
            policy=policy,
            diffs=diffs,
            unresolved_users=unresolved,
            ambiguous_users=ambiguous,
        )

    def _evaluate_policy_for_project(
        self,
        policy: PolicyConfig,
        project: JiraProject,
        canonical_usernames: Set[str],
    ) -> ProjectRoleDiff:
        roles = self._client.get_project_roles(project.key)
        role_id = roles.get(policy.role_name)
        if role_id is None:
            return ProjectRoleDiff(
                project=project,
                role_id=None,
                warnings=[f"Role '{policy.role_name}' not found in project {project.key}"],
            )

        actors: List[JiraRoleActor] = self._client.get_role_actors(role_id)
        individual_users: Set[str] = set()
        group_actors: Set[str] = set()
        for a in actors:
            if "group" in (a.type or "").lower():
                group_actors.add(a.name)
            else:
                individual_users.add(a.name)

        to_add: Set[str] = set()
        to_remove: Set[str] = set()

        mode = policy.enforcement.mode
        if mode == "enforce_exact":
            to_add = canonical_usernames - individual_users
            to_remove = individual_users - canonical_usernames
        elif mode == "add_only":
            to_add = canonical_usernames - individual_users
            to_remove = set()
        elif mode == "remove_only":
            to_add = set()
            to_remove = individual_users - canonical_usernames
        else:
            raise ValueError(f"Unsupported enforcement mode: {mode}")

        max_changes = policy.enforcement.max_changes_per_project
        if max_changes is not None and (len(to_add) + len(to_remove)) > max_changes:
            warnings = [
                f"Change count {len(to_add) + len(to_remove)} exceeds max_changes_per_project={max_changes}.",
                "Use --force or increase the limit to apply.",
            ]
        else:
            warnings = []

        return ProjectRoleDiff(
            project=project,
            role_id=role_id,
            to_add=to_add,
            to_remove=to_remove,
            warnings=warnings,
        )

    # --- Application ---

    def apply_policy(
        self,
        policy_id: str,
        project_key: Optional[str] = None,
        force: bool = False,
    ) -> PolicyEvaluationResult:
        result = self.evaluate_policy(policy_id, project_key=project_key)

        for diff in result.diffs:
            if diff.role_id is None:
                continue
            change_count = len(diff.to_add) + len(diff.to_remove)
            if change_count == 0:
                continue

            max_changes = diff.project and next(
                p.enforcement.max_changes_per_project
                for p in self._config.policies
                if p.id == policy_id
            )
            if not force and max_changes is not None and change_count > max_changes:
                continue

            if diff.to_add:
                self._client.add_role_actors(diff.role_id, sorted(diff.to_add))
            if diff.to_remove:
                self._client.remove_role_actors(diff.role_id, sorted(diff.to_remove))

        return result
