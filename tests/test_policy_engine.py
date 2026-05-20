from dataclasses import dataclass
from typing import List

from core.config import AppConfig, EnforcementConfig, JiraAuthConfig, JiraConfig, MatchingRulesConfig, PolicyConfig, ProjectFilterConfig, UserSourceConfig
from core.jira_client import JiraProject, JiraRoleActor, JiraUser
from core.policy_engine import PolicyEngine


@dataclass
class DummyClient:
    jira: JiraConfig

    def __init__(self, jira: JiraConfig) -> None:
        self.jira = jira
        self._projects: List[JiraProject] = []
        self._roles_by_project: dict[str, dict[str, int]] = {}
        self._role_actors: dict[int, List[JiraRoleActor]] = {}
        self._users: List[JiraUser] = []

    def list_projects(self) -> List[JiraProject]:
        return self._projects

    def list_all_users(self) -> List[JiraUser]:
        return self._users

    def get_project_roles(self, project_key: str) -> dict[str, int]:
        return self._roles_by_project.get(project_key, {})

    def get_role_actors(self, role_id: int) -> List[JiraRoleActor]:
        return self._role_actors.get(role_id, [])

    def add_role_actors(self, role_id: int, usernames: List[str]) -> None:
        existing = {a.name for a in self._role_actors.get(role_id, []) if "group" not in (a.type or "").lower()}
        for u in usernames:
            if u not in existing:
                self._role_actors.setdefault(role_id, []).append(
                    JiraRoleActor(type="atlassian-user-role-actor", name=u)
                )

    def remove_role_actors(self, role_id: int, usernames: List[str]) -> None:
        actors = self._role_actors.get(role_id, [])
        self._role_actors[role_id] = [a for a in actors if a.name not in usernames]


def make_engine(dummy: DummyClient) -> PolicyEngine:
    global_filters = ProjectFilterConfig()
    policy = PolicyConfig(
        id="p1",
        name="Policy 1",
        role_name="Role A",
        project_filters=global_filters,
        user_source=UserSourceConfig(mode="static_list", identifier_type="username", users=["user1", "user2"]),
        matching_rules=MatchingRulesConfig(),
        enforcement=EnforcementConfig(mode="enforce_exact", max_changes_per_project=10),
    )
    app_cfg = AppConfig(
        jira=dummy.jira,
        project_filters=global_filters,
        policies=[policy],
    )

    # type: ignore[arg-type]
    engine = PolicyEngine(app_cfg, dummy)
    return engine


def test_evaluate_policy_diff() -> None:
    jira_cfg = JiraConfig(base_url="https://example", auth=JiraAuthConfig(mode="basic"))
    dummy = DummyClient(jira_cfg)
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {1: [JiraRoleActor(type="atlassian-user-role-actor", name="user1")]}
    dummy._users = [
        JiraUser(username="user1", account_id=None, email="u1@example.com", display_name="U1"),
        JiraUser(username="user2", account_id=None, email="u2@example.com", display_name="U2"),
    ]

    engine = make_engine(dummy)
    result = engine.evaluate_policy("p1")

    assert result.total_additions == 1
    assert result.total_removals == 0
    diff = result.diffs[0]
    assert diff.project.key == "P1"
    assert diff.to_add == {"user2"}


def test_apply_policy_respects_max_changes() -> None:
    jira_cfg = JiraConfig(base_url="https://example", auth=JiraAuthConfig(mode="basic"))
    dummy = DummyClient(jira_cfg)
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {1: []}
    dummy._users = [
        JiraUser(username="user1", account_id=None, email="u1@example.com", display_name="U1"),
        JiraUser(username="user2", account_id=None, email="u2@example.com", display_name="U2"),
    ]

    engine = make_engine(dummy)
    # Set tight limit
    engine._config.policies[0].enforcement.max_changes_per_project = 0

    result = engine.apply_policy("p1", force=False)

    # No actors added because limit exceeded
    assert dummy._role_actors[1] == []

    # Forcing should apply
    result2 = engine.apply_policy("p1", force=True)
    assert any(a.name == "user1" for a in dummy._role_actors[1])
    assert any(a.name == "user2" for a in dummy._role_actors[1])
