from __future__ import annotations

import hmac
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jwt import InvalidTokenError

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)
ROLE_NAMES = {
    "ROLE-01": "知识管理员",
    "ROLE-02": "数据录入员",
    "ROLE-03": "审核员",
    "ROLE-04": "分析师",
}
DEFAULT_USERS = {
    "admin": {"password": "admin123", "role": "ROLE-01"},
    "editor": {"password": "editor123", "role": "ROLE-02"},
    "reviewer": {"password": "reviewer123", "role": "ROLE-03"},
    "analyst": {"password": "analyst123", "role": "ROLE-04"},
}


@dataclass(frozen=True)
class Principal:
    subject: str
    role: str


def issue_token(subject: str, role: str, secret: str, expire_minutes: int = 480) -> str:
    if role not in ROLE_NAMES:
        raise ValueError("未知角色")
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": subject, "role": role, "iat": now, "exp": now + timedelta(minutes=expire_minutes)},
        secret,
        algorithm="HS256",
    )


def authenticate(username: str, password: str) -> Principal | None:
    try:
        users = json.loads(os.getenv("KG_USERS", "")) or DEFAULT_USERS
    except json.JSONDecodeError:
        users = DEFAULT_USERS
    user = users.get(username)
    if not user or not hmac.compare_digest(str(user.get("password", "")), password):
        return None
    role = user.get("role")
    if role not in ROLE_NAMES:
        return None
    return Principal(username, role)


def principal_from_token(request: Request, token: str) -> Principal:
    try:
        payload = jwt.decode(token, request.app.state.settings.jwt_secret, algorithms=["HS256"])
        role = payload["role"]
        if role not in ROLE_NAMES:
            raise ValueError("invalid role")
        return Principal(subject=payload["sub"], role=role)
    except (InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token 无效或已过期", {"WWW-Authenticate": "Bearer"})


def current_principal(request: Request, token: str | None = Depends(oauth2_scheme)) -> Principal:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer Token", {"WWW-Authenticate": "Bearer"})
    return principal_from_token(request, token)


def require_roles(*roles: str):
    allowed = set(roles) | {"ROLE-01"}

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "当前角色无权执行此操作")
        return principal

    return dependency
