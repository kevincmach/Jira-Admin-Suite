from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import re

from .jira_client import JiraClient, JiraUser


@dataclass
class UserResolutionResult:
    resolved: Dict[str, JiraUser]
    unresolved: List[str]
    ambiguous: Dict[str, List[JiraUser]]


class UserDiscovery:
    def __init__(self, client: JiraClient):
        self._client = client
        self._user_cache: List[JiraUser] | None = None

    def _ensure_cache(self) -> List[JiraUser]:
        if self._user_cache is None:
            self._user_cache = self._client.list_all_users()
        return self._user_cache

    def resolve_identifiers(
        self,
        identifiers: List[str],
        identifier_type: str,
        email_domains: List[str] | None = None,
    ) -> UserResolutionResult:
        email_domains = email_domains or []
        users = self._ensure_cache()

        by_username = {u.username: u for u in users if u.username}
        by_email = {u.email.lower(): u for u in users if u.email}

        resolved: Dict[str, JiraUser] = {}
        unresolved: List[str] = []
        ambiguous: Dict[str, List[JiraUser]] = {}

        for ident in identifiers:
            if identifier_type == "username":
                user = by_username.get(ident)
                if user:
                    resolved[ident] = user
                else:
                    unresolved.append(ident)
                continue

            if identifier_type == "email":
                user = by_email.get(ident.lower())
                if user:
                    resolved[ident] = user
                else:
                    unresolved.append(ident)
                continue

            # heuristic firstname.lastname
            if identifier_type == "firstname_lastname":
                candidates: List[JiraUser] = []

                # direct username matches
                for candidate_username in self._candidate_usernames(ident):
                    u = by_username.get(candidate_username)
                    if u and u not in candidates:
                        candidates.append(u)

                # email guesses
                local_part = ident.lower()
                for domain in email_domains:
                    email = f"{local_part}@{domain.lower()}"
                    u = by_email.get(email)
                    if u and u not in candidates:
                        candidates.append(u)

                if not candidates:
                    unresolved.append(ident)
                elif len(candidates) == 1:
                    resolved[ident] = candidates[0]
                else:
                    ambiguous[ident] = candidates
                continue

            # unknown identifier type
            unresolved.append(ident)

        return UserResolutionResult(resolved=resolved, unresolved=unresolved, ambiguous=ambiguous)

    @staticmethod
    def _candidate_usernames(ident: str) -> List[str]:
        # normalize
        base = ident.strip()
        base = re.sub(r"\s+", " ", base)
        base = base.replace(" ", ".").lower()
        firstname_lastname = base
        firstname_lastname_us = base.replace(".", "_")
        return [firstname_lastname, firstname_lastname_us]
