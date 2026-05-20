from dataclasses import dataclass
from typing import Dict, List

from core.config import AppConfig, EnforcementConfig, JiraAuthConfig, JiraConfig, MatchingRulesConfig, PolicyConfig, ProjectFilterConfig, UserSourceConfig
from core.jira_client import JiraProject, JiraRoleActor, JiraUser
from core.policy_engine import PolicyEngine


@dataclass
class DummyClient:
    jira: JiraConfig

    def __init__(self, jira: JiraConfig) -> None:
        self.jira = jira
        self._projects: List[JiraProject] = []
        self._roles_by_project: Dict[str, Dict[str, int]] = {}
        # Per-project role actors: {(project_key, role_id): [actors]}
        self._role_actors: Dict[tuple, List[JiraRoleActor]] = {}
        self._users: List[JiraUser] = []
        self._groups: Dict[str, List[JiraUser]] = {}
        self.added: List[tuple] = []
        self.removed: List[tuple] = []

    def list_projects(self) -> List[JiraProject]:
        return self._projects

    def list_all_users(self) -> List[JiraUser]:
        return self._users

    def get_project_roles(self, project_key: str) -> Dict[str, int]:
        return self._roles_by_project.get(project_key, {})

    def get_role_actors(self, project_key: str, role_id: int) -> List[JiraRoleActor]:
        return list(self._role_actors.get((project_key, role_id), []))

    def get_group_members(self, group_name: str, include_inactive: bool = False, page_size: int = 50) -> List[JiraUser]:
        return list(self._groups.get(group_name, []))

    def add_role_actors(self, project_key: str, role_id: int, usernames: List[str]) -> None:
        self.added.append((project_key, role_id, list(usernames)))
        existing = {
            a.name
            for a in self._role_actors.get((project_key, role_id), [])
            if "group" not in (a.type or "").lower()
        }
        for u in usernames:
            if u not in existing:
                self._role_actors.setdefault((project_key, role_id), []).append(
                    JiraRoleActor(type="atlassian-user-role-actor", name=u)
                )

    def remove_role_actors(self, project_key: str, role_id: int, usernames: List[str]) -> None:
        self.removed.append((project_key, role_id, list(usernames)))
        actors = self._role_actors.get((project_key, role_id), [])
        self._role_actors[(project_key, role_id)] = [a for a in actors if a.name not in usernames]


def _make_engine(dummy: DummyClient, users: List[str], use_firstname_lastname: bool = False) -> PolicyEngine:
    global_filters = ProjectFilterConfig()
    policy = PolicyConfig(
        id="p1",
        name="Policy 1",
        role_name="Role A",
        project_filters=global_filters,
        user_source=UserSourceConfig(mode="static_list", identifier_type="username", users=users),
        matching_rules=MatchingRulesConfig(use_firstname_lastname=use_firstname_lastname),
        enforcement=EnforcementConfig(mode="enforce_exact", max_changes_per_project=10),
    )
    app_cfg = AppConfig(
        jira=dummy.jira,
        project_filters=global_filters,
        policies=[policy],
    )
    return PolicyEngine(app_cfg, dummy)  # type: ignore[arg-type]


def _jira_cfg() -> JiraConfig:
    return JiraConfig(base_url="https://example", auth=JiraAuthConfig(mode="basic"))


def test_evaluate_policy_diff() -> None:
    dummy = DummyClient(_jira_cfg())
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {("P1", 1): [JiraRoleActor(type="atlassian-user-role-actor", name="user1")]}

    engine = _make_engine(dummy, ["user1", "user2"])
    result = engine.evaluate_policy("p1")

    assert result.total_additions == 1
    assert result.total_removals == 0
    diff = result.diffs[0]
    assert diff.project.key == "P1"
    assert diff.to_add == {"user2"}


def test_delta_is_case_insensitive() -> None:
    """A configured user that's already in the role under different casing must
    not show up as both an addition and a removal."""
    dummy = DummyClient(_jira_cfg())
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {
        ("P1", 1): [JiraRoleActor(type="atlassian-user-role-actor", name="Alice.Smith")]
    }

    engine = _make_engine(dummy, ["alice.smith"])
    result = engine.evaluate_policy("p1")

    diff = result.diffs[0]
    assert diff.to_add == set(), f"unexpected add: {diff.to_add}"
    assert diff.to_remove == set(), f"unexpected remove: {diff.to_remove}"


def test_group_members_count_as_covered() -> None:
    """Users present in the role only via group membership should not be
    listed for re-addition, and the group should appear on the diff."""
    dummy = DummyClient(_jira_cfg())
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {
        ("P1", 1): [
            JiraRoleActor(type="atlassian-user-role-actor", name="user1"),
            JiraRoleActor(type="atlassian-group-role-actor", name="developers"),
        ]
    }
    dummy._groups = {
        "developers": [
            JiraUser(username="user2", account_id=None, email=None, display_name=None),
            JiraUser(username="user3", account_id=None, email=None, display_name=None),
        ]
    }

    engine = _make_engine(dummy, ["user1", "user2", "user3"])
    result = engine.evaluate_policy("p1")

    diff = result.diffs[0]
    assert diff.to_add == set()
    assert diff.to_remove == set()
    assert diff.group_actors == {"developers"}
    assert diff.covered_via_group == {"user2", "user3"}


def test_group_member_with_mixed_case_username() -> None:
    """A user in the configured list should be recognised as covered when the
    group reports them under different casing."""
    dummy = DummyClient(_jira_cfg())
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {
        ("P1", 1): [JiraRoleActor(type="atlassian-group-role-actor", name="developers")]
    }
    dummy._groups = {
        "developers": [
            JiraUser(username="Alice.Smith", account_id=None, email=None, display_name=None),
        ]
    }

    engine = _make_engine(dummy, ["alice.smith"])
    result = engine.evaluate_policy("p1")

    diff = result.diffs[0]
    assert diff.to_add == set()
    assert diff.covered_via_group == {"alice.smith"}


def test_individual_user_not_in_config_is_removed_even_when_groups_present() -> None:
    """to_remove must still flag direct individual actors not in the configured
    list, regardless of unrelated group members."""
    dummy = DummyClient(_jira_cfg())
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {
        ("P1", 1): [
            JiraRoleActor(type="atlassian-user-role-actor", name="stale.user"),
            JiraRoleActor(type="atlassian-group-role-actor", name="developers"),
        ]
    }
    dummy._groups = {
        "developers": [
            JiraUser(username="user1", account_id=None, email=None, display_name=None),
        ]
    }

    engine = _make_engine(dummy, ["user1"])
    result = engine.evaluate_policy("p1")

    diff = result.diffs[0]
    assert diff.to_remove == {"stale.user"}
    assert diff.to_add == set()


def test_apply_uses_project_scoped_endpoints() -> None:
    """Mutations must include the project key, not just the role id."""
    dummy = DummyClient(_jira_cfg())
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {("P1", 1): []}

    engine = _make_engine(dummy, ["user1", "user2"])
    engine.apply_policy("p1", force=True)

    assert dummy.added, "expected add_role_actors to be called"
    project_key, role_id, names = dummy.added[0]
    assert project_key == "P1"
    assert role_id == 1
    assert set(names) == {"user1", "user2"}


def test_apply_policy_respects_max_changes() -> None:
    dummy = DummyClient(_jira_cfg())
    dummy._projects = [JiraProject(key="P1", name="Project 1", project_type="software")]
    dummy._roles_by_project = {"P1": {"Role A": 1}}
    dummy._role_actors = {("P1", 1): []}

    engine = _make_engine(dummy, ["user1", "user2"])
    engine._config.policies[0].enforcement.max_changes_per_project = 0

    engine.apply_policy("p1", force=False)
    assert dummy._role_actors[("P1", 1)] == []

    engine.apply_policy("p1", force=True)
    assert any(a.name == "user1" for a in dummy._role_actors[("P1", 1)])
    assert any(a.name == "user2" for a in dummy._role_actors[("P1", 1)])
