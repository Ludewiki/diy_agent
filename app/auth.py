"""Opaque server-side browser sessions and password authentication."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import re
import secrets
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import Settings
from .database import Database
from .errors import ApiError
from .models import AuthSession, User, utc_now


_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PASSWORD_HASHER = PasswordHasher()
_DUMMY_PASSWORD_HASH = _PASSWORD_HASHER.hash("constant-time-login-placeholder")


def normalize_email(value: str) -> str:
    email = value.strip().casefold()
    if len(email) > 320 or not _EMAIL_PATTERN.fullmatch(email):
        raise ApiError(422, "INVALID_EMAIL", "请输入有效的邮箱地址。")
    return email


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def register(self, email: str, password: str) -> tuple[User, str]:
        user = User(
            email=normalize_email(email),
            password_hash=_PASSWORD_HASHER.hash(password),
        )
        try:
            with self.database.session_factory.begin() as session:
                session.add(user)
                session.flush()
        except IntegrityError as exc:
            raise ApiError(
                409,
                "EMAIL_ALREADY_REGISTERED",
                "该邮箱已经注册，请直接登录。",
            ) from exc
        return user, self._create_session(user.id)

    def login(self, email: str, password: str) -> tuple[User, str]:
        normalized = normalize_email(email)
        with self.database.session_factory() as session:
            user = session.scalar(select(User).where(User.email == normalized))
            password_hash = (
                user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
            )
            try:
                valid = _PASSWORD_HASHER.verify(password_hash, password)
            except (VerificationError, InvalidHashError):
                valid = False
            if user is None or not user.is_active or not valid:
                raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码不正确。")
            user_id = user.id
        return user, self._create_session(user_id)

    def _create_session(self, user_id: uuid.UUID) -> str:
        raw_token = secrets.token_urlsafe(32)
        now = utc_now()
        with self.database.session_factory.begin() as session:
            session.add(
                AuthSession(
                    user_id=user_id,
                    token_hash=hash_session_token(raw_token),
                    expires_at=now
                    + timedelta(days=self.settings.auth_session_lifetime_days),
                )
            )
        return raw_token

    def current_user(self, raw_token: str | None) -> User:
        if not raw_token:
            raise ApiError(401, "AUTHENTICATION_REQUIRED", "请先登录。")
        now = utc_now()
        with self.database.session_factory.begin() as session:
            auth_session = session.scalar(
                select(AuthSession).where(
                    AuthSession.token_hash == hash_session_token(raw_token),
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
            )
            if auth_session is None:
                raise ApiError(
                    401,
                    "AUTHENTICATION_REQUIRED",
                    "登录状态已失效，请重新登录。",
                )
            user = session.get(User, auth_session.user_id)
            if user is None or not user.is_active:
                raise ApiError(
                    401,
                    "AUTHENTICATION_REQUIRED",
                    "登录状态已失效，请重新登录。",
                )
            comparison_now = now
            if auth_session.last_seen_at.tzinfo is None:
                comparison_now = now.replace(tzinfo=None)
            if (comparison_now - auth_session.last_seen_at).total_seconds() >= 300:
                auth_session.last_seen_at = now
            return user

    def revoke(self, raw_token: str | None) -> None:
        if not raw_token:
            return
        with self.database.session_factory.begin() as session:
            auth_session = session.scalar(
                select(AuthSession).where(
                    AuthSession.token_hash == hash_session_token(raw_token),
                    AuthSession.revoked_at.is_(None),
                )
            )
            if auth_session is not None:
                auth_session.revoked_at = utc_now()


def tokens_match(cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)
