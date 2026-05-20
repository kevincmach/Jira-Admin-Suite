from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import base64

import requests

from .config import JiraConfig, resolve_auth_credentials


@dataclass
class JiraUser:
    username: Optional[str]
    account_id: Optional[str]
    email: Optional[str]
    display_name: Optional[str]


@dataclass
class JiraProject:
    key: str
    name: str
    project_type: Optional[str]


@dataclass
class JiraRoleActor:
    type: str  # atlassian-user-role-actor / atlassian-group-role-actor
    name: str


class JiraClientError(Exception):
    pass


class JiraClient:
    def __init__(self, config: JiraConfig) -> None:
        self._config = config
        self._auth_info = resolve_auth_credentials(config.auth)
        self._session = requests.Session()

    @property
    def base_url(self) -> str:
        return self._config.base_url

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._auth_info["type"] == "token":
            token = self._auth_info["token"]
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _auth(self) -> Optional[Tuple[str, str]]:
        if self._auth_info["type"] == "basic":
            return (self._auth_info["username"], self._auth_info["password"])
        return None

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        kwargs.setdefault("headers", {}).update(self._headers())
        auth = self._auth()
        if auth is not None:
            kwargs["auth"] = auth
        resp = self._session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            raise JiraClientError(f"Jira API {method} {path} failed: {resp.status_code} {resp.text}")
        if resp.content:
            return resp.json()
        return None

    # --- Projects ---

    def list_projects(self) -> List[JiraProject]:
        data = self._request("GET", "/rest/api/2/project")
        projects: List[JiraProject] = []
        for p in data:
            projects.append(
                JiraProject(
                    key=p.get("key"),
                    name=p.get("name"),
                    project_type=p.get("projectTypeKey"),
                )
            )
        return projects

    # --- Users ---

    def list_all_users(self, start_at: int = 0, max_results: int = 1000) -> List[JiraUser]:
        users: List[JiraUser] = []
        start = start_at
        while True:
            data = self._request(
                "GET",
                "/rest/api/2/user/search",
                params={"startAt": start, "maxResults": max_results, "username": "."},
            )
            if not data:
                break
            for u in data:
                users.append(
                    JiraUser(
                        username=u.get("name") or u.get("key"),
                        account_id=u.get("accountId"),
                        email=u.get("emailAddress"),
                        display_name=u.get("displayName"),
                    )
                )
            if len(data) < max_results:
                break
            start += max_results
        return users

    def find_users(self, query: str, max_results: int = 50) -> List[JiraUser]:
        data = self._request(
            "GET",
            "/rest/api/2/user/search",
            params={"username": query, "maxResults": max_results},
        )
        users: List[JiraUser] = []
        for u in data:
            users.append(
                JiraUser(
                    username=u.get("name") or u.get("key"),
                    account_id=u.get("accountId"),
                    email=u.get("emailAddress"),
                    display_name=u.get("displayName"),
                )
            )
        return users

    # --- Roles ---

    def get_project_roles(self, project_key: str) -> Dict[str, int]:
        data = self._request("GET", f"/rest/api/2/project/{project_key}/role")
        roles: Dict[str, int] = {}
        for name, url in data.items():
            try:
                role_id = int(url.rstrip("/").split("/")[-1])
                roles[name] = role_id
            except ValueError:
                continue
        return roles

    def get_role_actors(self, role_id: int) -> List[JiraRoleActor]:
        data = self._request("GET", f"/rest/api/2/role/{role_id}")
        actors: List[JiraRoleActor] = []
        for a in data.get("actors", []):
            actors.append(JiraRoleActor(type=a.get("type"), name=a.get("name")))
        return actors

    def add_role_actors(self, role_id: int, usernames: List[str]) -> None:
        if not usernames:
            return
        payload = {"user": usernames}
        self._request("POST", f"/rest/api/2/role/{role_id}", json=payload)

    def remove_role_actors(self, role_id: int, usernames: List[str]) -> None:
        for username in usernames:
            self._request(
                "DELETE",
                f"/rest/api/2/role/{role_id}",
                params={"user": username},
            )
