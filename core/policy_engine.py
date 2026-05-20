from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import re

from .config import AppConfig, PolicyConfig
from .jira_client import JiraClient, JiraClientError, JiraProject, JiraRoleActor
from .user_discovery import UserDiscovery, UserResolutionResult


_USER_ACTOR_TYPES = {"atlassian-user-role-actor"}
_GROUP_ACTOR_TYPES = {"atlassian-group-role-actor"}


def _is_group_actor(actor_type: str) -> bool:
    t = (actor_type or "").lower()
    if t in _GROUP_ACTOR_TYPES:
        return True
    return "group-role-actor" in t


def _is_user_actor(actor_type: str) -> bool:
    t = (actor_type or "").lower()
    if t in _USER_ACTOR_TYPES:
        return True
    return "user-role-actor" in t


_GROUP_MODES = {"ensure_group_present", "ensure_group_exact"}


@dataclass
class ProjectRoleDiff:
    project: JiraProject
    role_id: Optional[int]
    # Before snapshot (what is currently on the role in this project)
    current_users: Set[str] = field(default_factory=set)
    current_groups: Set[str] = field(default_factory=set)
    # User-level plan
    to_add: Set[str] = field(default_factory=set)
    to_remove: Set[str] = field(default_factory=set)
    already_present: Set[str] = field(default_factory=set)
    covered_via_group: Set[str] = field(default_factory=set)
    # Group-level plan (used by ensure_group_* modes)
    groups_to_add: Set[str] = field(default_factory=set)
    groups_to_remove: Set[str] = field(default_factory=set)
    warnings: List[str] = field(default_factory=list)

    @property
    def group_actors(self) -> Set[str]:
        # Backwards-compatible alias.
        return self.current_groups

    @property
    def planned_users(self) -> Set[str]:
        return (self.current_users - self.to_remove) | self.to_add

    @property
    def planned_groups(self) -> Set[str]:
        return (self.current_groups - self.groups_to_remove) | self.groups_to_add


@dataclass
class GroupMembershipAudit:
    policy: "PolicyConfig"
    group_name: str
    current_members: Set[str]
    expected_users: Set[str]
    to_add: Set[str]
    to_remove: Set[str]
    already_present: Set[str]
    unresolved_users: List[str] = field(default_factory=list)
    ambiguous_users: Dict[str, int] = field(default_factory=dict)


@dataclass
class PolicyEvaluationResult:
    policy: PolicyConfig
    diffs: List[ProjectRoleDiff]
    unresolved_users: List[str]
    ambiguous_users: Dict[str, int]

    @property
    def total_additions(self) -> int:
        return sum(len(d.to_add) + len(d.groups_to_add) for d in self.diffs)

    @property
    def total_removals(self) -> int:
        return sum(len(d.to_remove) + len(d.groups_to_remove) for d in self.diffs)


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
        # If the policy asks for firstname.lastname matching, route to UserDiscovery
        # even when the identifier_type is "username" (the configured strings are
        # treated as "firstname.lastname" handles to resolve into Jira usernames).
        if identifier_type == "username" and policy.matching_rules.use_firstname_lastname:
            identifier_type = "firstname_lastname"

        if identifier_type == "username":
            canonical_usernames: Set[str] = set(identifiers)
            unresolved: List[str] = []
            ambiguous: Dict[str, int] = {}
        else:
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

        actors: List[JiraRoleActor] = self._client.get_role_actors(project.key, role_id)
        individual_users: Set[str] = set()
        group_actors: Set[str] = set()
        for a in actors:
            if _is_group_actor(a.type):
                group_actors.add(a.name)
            elif _is_user_actor(a.type):
                individual_users.add(a.name)
            else:
                # Unknown actor type — assume user so we never silently drop a member.
                individual_users.add(a.name)

        warnings: List[str] = []
        mode = policy.enforcement.mode

        # --- Group-based enforcement modes -----------------------------------
        if mode in _GROUP_MODES:
            target_group = policy.target_group
            if not target_group:
                raise ValueError(
                    f"Policy {policy.id} mode={mode} requires target_group"
                )

            def _gnorm(s: str) -> str:
                return s.strip().lower()

            groups_by_lc: Dict[str, str] = {}
            for g in group_actors:
                groups_by_lc.setdefault(_gnorm(g), g)

            target_lc = _gnorm(target_group)
            groups_to_add: Set[str] = set()
            groups_to_remove: Set[str] = set()
            if target_lc not in groups_by_lc:
                groups_to_add.add(target_group)

            if mode == "ensure_group_exact":
                # Remove every other group actor that is not the target.
                for lc, original in groups_by_lc.items():
                    if lc != target_lc:
                        groups_to_remove.add(original)
                # Also remove any individual user actors — the role is meant
                # to be governed exclusively by the target group.
                to_remove_users = set(individual_users)
            else:
                to_remove_users = set()

            change_count = (
                len(groups_to_add) + len(groups_to_remove) + len(to_remove_users)
            )
            max_changes = policy.enforcement.max_changes_per_project
            if max_changes is not None and change_count > max_changes:
                warnings.append(
                    f"Change count {change_count} exceeds "
                    f"max_changes_per_project={max_changes}. Use --force or "
                    f"increase the limit to apply."
                )

            return ProjectRoleDiff(
                project=project,
                role_id=role_id,
                current_users=individual_users,
                current_groups=group_actors,
                to_remove=to_remove_users,
                groups_to_add=groups_to_add,
                groups_to_remove=groups_to_remove,
                warnings=warnings,
            )

        # --- User-list enforcement modes (legacy behavior) -------------------
        # Expand any group actors into their members so that a user already
        # covered by a group is not treated as "missing" from the role.
        group_member_usernames: Set[str] = set()
        if group_actors:
            for group_name in group_actors:
                try:
                    members = self._client.get_group_members(group_name)
                except JiraClientError as exc:
                    warnings.append(
                        f"Could not expand group '{group_name}': {exc}"
                    )
                    continue
                for m in members:
                    if m.username:
                        group_member_usernames.add(m.username)

        # Case-insensitive comparison while preserving the original casing
        # for API calls and reporting.
        def _norm(s: str) -> str:
            return s.strip().lower()

        configured_by_lc: Dict[str, str] = {}
        for u in canonical_usernames:
            configured_by_lc.setdefault(_norm(u), u)

        individual_by_lc: Dict[str, str] = {}
        for u in individual_users:
            individual_by_lc.setdefault(_norm(u), u)

        group_lc: Set[str] = {_norm(u) for u in group_member_usernames}
        configured_lc: Set[str] = set(configured_by_lc)
        individual_lc: Set[str] = set(individual_by_lc)
        covered_lc: Set[str] = individual_lc | group_lc

        to_add_lc: Set[str] = set()
        to_remove_lc: Set[str] = set()

        if mode == "enforce_exact":
            to_add_lc = configured_lc - covered_lc
            to_remove_lc = individual_lc - configured_lc
        elif mode == "add_only":
            to_add_lc = configured_lc - covered_lc
        elif mode == "remove_only":
            to_remove_lc = individual_lc - configured_lc
        else:
            raise ValueError(f"Unsupported enforcement mode: {mode}")

        # Map back to original casings. For additions, prefer the configured
        # casing (that's what the user typed). For removals, use the casing
        # Jira reported, because that's what the DELETE call needs.
        to_add: Set[str] = {configured_by_lc[lc] for lc in to_add_lc}
        to_remove: Set[str] = {individual_by_lc[lc] for lc in to_remove_lc}

        # Users in the configured list who are only "in" via group membership.
        covered_via_group: Set[str] = {
            configured_by_lc[lc]
            for lc in (configured_lc & group_lc) - individual_lc
        }

        # Configured users who are already directly in the role (the "no
        # action needed" bucket).
        already_present: Set[str] = {
            configured_by_lc[lc] for lc in configured_lc & individual_lc
        }

        # Surface configured users who are present in groups in the role but
        # also present individually under a different casing — that's harmless
        # but worth noting so the operator can clean up duplicates.
        if mode == "enforce_exact":
            unmanaged_group_only_lc = group_lc - configured_lc - individual_lc
            if unmanaged_group_only_lc:
                warnings.append(
                    f"{len(unmanaged_group_only_lc)} user(s) present via group "
                    f"membership are not in the configured list and cannot be "
                    f"removed by this tool (groups: {sorted(group_actors)})."
                )

        max_changes = policy.enforcement.max_changes_per_project
        if max_changes is not None and (len(to_add) + len(to_remove)) > max_changes:
            warnings.append(
                f"Change count {len(to_add) + len(to_remove)} exceeds "
                f"max_changes_per_project={max_changes}. Use --force or "
                f"increase the limit to apply."
            )

        return ProjectRoleDiff(
            project=project,
            role_id=role_id,
            current_users=individual_users,
            current_groups=group_actors,
            to_add=to_add,
            to_remove=to_remove,
            warnings=warnings,
            covered_via_group=covered_via_group,
            already_present=already_present,
        )

    # --- Group membership audit ---

    def audit_group_membership(self, policy_id: str) -> "GroupMembershipAudit":
        """Compare a policy's configured user list against the actual members
        of its target_group. Read-only — does not mutate Jira."""
        policy = next((p for p in self._config.policies if p.id == policy_id), None)
        if policy is None:
            raise ValueError(f"Unknown policy id: {policy_id}")
        if not policy.target_group:
            raise ValueError(
                f"Policy {policy_id} has no target_group; group-audit requires one."
            )

        identifiers = policy.user_source.users
        identifier_type = policy.user_source.identifier_type
        if identifier_type == "username" and policy.matching_rules.use_firstname_lastname:
            identifier_type = "firstname_lastname"

        if identifier_type == "username":
            expected = set(identifiers)
            unresolved: List[str] = []
            ambiguous: Dict[str, int] = {}
        else:
            resolution = self._user_discovery.resolve_identifiers(
                identifiers,
                identifier_type=identifier_type,
                email_domains=policy.matching_rules.email_domain_whitelist,
            )
            expected = {u.username for u in resolution.resolved.values() if u.username}
            unresolved = resolution.unresolved
            ambiguous = {k: len(v) for k, v in resolution.ambiguous.items()}

        members = self._client.get_group_members(policy.target_group)
        current = {m.username for m in members if m.username}

        def _norm(s: str) -> str:
            return s.strip().lower()

        expected_by_lc = {_norm(u): u for u in expected}
        current_by_lc = {_norm(u): u for u in current}
        expected_lc = set(expected_by_lc)
        current_lc = set(current_by_lc)

        to_add = {expected_by_lc[lc] for lc in expected_lc - current_lc}
        to_remove = {current_by_lc[lc] for lc in current_lc - expected_lc}
        already_present = {current_by_lc[lc] for lc in expected_lc & current_lc}

        return GroupMembershipAudit(
            policy=policy,
            group_name=policy.target_group,
            current_members=current,
            expected_users=expected,
            to_add=to_add,
            to_remove=to_remove,
            already_present=already_present,
            unresolved_users=unresolved,
            ambiguous_users=ambiguous,
        )

    # --- Application ---

    def apply_policy(
        self,
        policy_id: str,
        project_key: Optional[str] = None,
        force: bool = False,
    ) -> PolicyEvaluationResult:
        result = self.evaluate_policy(policy_id, project_key=project_key)

        policy = next(p for p in self._config.policies if p.id == policy_id)
        max_changes = policy.enforcement.max_changes_per_project

        for diff in result.diffs:
            if diff.role_id is None:
                continue
            change_count = (
                len(diff.to_add)
                + len(diff.to_remove)
                + len(diff.groups_to_add)
                + len(diff.groups_to_remove)
            )
            if change_count == 0:
                continue

            if not force and max_changes is not None and change_count > max_changes:
                continue

            if diff.to_add:
                self._client.add_role_actors(
                    diff.project.key, diff.role_id, sorted(diff.to_add)
                )
            if diff.to_remove:
                self._client.remove_role_actors(
                    diff.project.key, diff.role_id, sorted(diff.to_remove)
                )
            if diff.groups_to_add:
                self._client.add_role_group_actors(
                    diff.project.key, diff.role_id, sorted(diff.groups_to_add)
                )
            if diff.groups_to_remove:
                self._client.remove_role_group_actors(
                    diff.project.key, diff.role_id, sorted(diff.groups_to_remove)
                )

        return result
