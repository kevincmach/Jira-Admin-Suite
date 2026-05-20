from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Header
from pydantic import BaseModel

from core.config import AppConfig, ConfigError, load_config
from core.jira_client import JiraClient
from core.policy_engine import PolicyEngine


CONFIG_PATH = Path("config/config.yaml")
API_KEY_ENV_VAR = "JIRA_ROLE_SYNC_API_KEY"


def get_app_config() -> AppConfig:
    try:
        return load_config(CONFIG_PATH)
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))


def get_policy_engine(config: AppConfig = Depends(get_app_config)) -> PolicyEngine:
    client = JiraClient(config.jira)
    return PolicyEngine(config, client)


def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    import os

    configured = os.getenv(API_KEY_ENV_VAR)
    if not configured:
        # If no key is set, allow all (for local dev)
        return
    if not x_api_key or x_api_key != configured:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


app = FastAPI(title="Jira Role Sync API")


class ProjectOut(BaseModel):
    key: str
    name: str
    project_type: Optional[str]


class PolicyOut(BaseModel):
    id: str
    name: str
    role_name: str
    enforcement_mode: str


class DiffOut(BaseModel):
    project: str
    role_id: int | None
    to_add: list[str]
    to_remove: list[str]
    warnings: list[str]


class PolicyPlanOut(BaseModel):
    policy: str
    name: str
    total_additions: int
    total_removals: int
    diffs: list[DiffOut]
    unresolved_users: list[str]
    ambiguous_users: dict[str, int]


class ApplyRequest(BaseModel):
    project: Optional[str] = None
    force: bool = False


class ApplyResponse(BaseModel):
    policy: str
    total_additions: int
    total_removals: int


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/projects", dependencies=[Depends(verify_api_key)])
async def list_projects(engine: PolicyEngine = Depends(get_policy_engine)) -> list[ProjectOut]:
    client = engine._client
    projects = client.list_projects()
    return [
        ProjectOut(key=p.key, name=p.name, project_type=p.project_type)
        for p in projects
    ]


@app.get("/policies", dependencies=[Depends(verify_api_key)])
async def list_policies(config: AppConfig = Depends(get_app_config)) -> list[PolicyOut]:
    return [
        PolicyOut(
            id=p.id,
            name=p.name,
            role_name=p.role_name,
            enforcement_mode=p.enforcement.mode,
        )
        for p in config.policies
    ]


@app.get("/policies/{policy_id}", dependencies=[Depends(verify_api_key)])
async def get_policy(policy_id: str, config: AppConfig = Depends(get_app_config)) -> PolicyOut:
    policy = next((p for p in config.policies if p.id == policy_id), None)
    if not policy:
        raise HTTPException(status_code=404, detail="Policy not found")
    return PolicyOut(
        id=policy.id,
        name=policy.name,
        role_name=policy.role_name,
        enforcement_mode=policy.enforcement.mode,
    )


@app.get("/policies/{policy_id}/plan", dependencies=[Depends(verify_api_key)])
async def plan_policy(
    policy_id: str,
    project: Optional[str] = None,
    engine: PolicyEngine = Depends(get_policy_engine),
) -> PolicyPlanOut:
    try:
        result = engine.evaluate_policy(policy_id, project_key=project)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    diffs = [
        DiffOut(
            project=d.project.key,
            role_id=d.role_id,
            to_add=sorted(d.to_add),
            to_remove=sorted(d.to_remove),
            warnings=d.warnings,
        )
        for d in result.diffs
    ]

    return PolicyPlanOut(
        policy=result.policy.id,
        name=result.policy.name,
        total_additions=result.total_additions,
        total_removals=result.total_removals,
        diffs=diffs,
        unresolved_users=result.unresolved_users,
        ambiguous_users=result.ambiguous_users,
    )


@app.post("/policies/{policy_id}/apply", dependencies=[Depends(verify_api_key)])
async def apply_policy(
    policy_id: str,
    payload: ApplyRequest,
    engine: PolicyEngine = Depends(get_policy_engine),
) -> ApplyResponse:
    try:
        result = engine.apply_policy(policy_id, project_key=payload.project, force=payload.force)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return ApplyResponse(
        policy=result.policy.id,
        total_additions=result.total_additions,
        total_removals=result.total_removals,
    )
