from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from apps.api.deps import PrincipalDep

router = APIRouter(tags=["auth"])


class MeOut(BaseModel):
    user_id: str
    workspace_id: str
    role: str


@router.get("/me", response_model=MeOut)
def me(principal: PrincipalDep) -> MeOut:
    return MeOut(
        user_id=str(principal.user_id),
        workspace_id=str(principal.workspace_id),
        role=principal.role.value,
    )
