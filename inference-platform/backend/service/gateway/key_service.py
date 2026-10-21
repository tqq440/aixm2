"""中心统一认证；换 Key 不更换 application_id。"""

import hashlib
import hmac
import re
import secrets
from uuid import uuid4
from sqlalchemy import select
from backend.common.exceptions.app_error import AppError
from backend.data.database.database import database_now
from backend.data.gateway.key_models import GatewayKey
from backend.data.invocations.invocation_models import InferenceApplicationBudget
from backend.utils.time.time_utils import as_utc


class GatewayKeys:
    def __init__(self, core):self.core = core

    @staticmethod
    def view(row):
        return {"keyId": row.key_id, "applicationId": row.application_id, "label": row.label,
                "createdAt": as_utc(row.created_at), "revokedAt": as_utc(row.revoked_at) if row.revoked_at else None}

    def authenticate(self, header):
        token = header[7:] if isinstance(header,str) and header.startswith("Bearer ") else ""
        match = re.fullmatch(r"ipk_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})", token)
        with self.core._transaction() as session:
            row = session.get(GatewayKey, match[1]) if match else None
            valid = hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row.token_hash if row else "0"*64)
            app = session.get(InferenceApplicationBudget, row.application_id) if row and valid and row.revoked_at is None else None
            if not app or not app.enabled:
                raise AppError("API_KEY_UNAUTHORIZED", "应用凭据无效、已撤销或应用已停用。", 401)
            return app.application_id

    def issue(self, application_id, command, *, replace=None):
        with self.core._transaction(True) as session:
            app = session.get(InferenceApplicationBudget, application_id)
            if not app or application_id == "local-user":
                raise AppError("APPLICATION_NOT_FOUND", "请先登记独立应用，不能给旧本机身份签发 Key。", 404)
            if replace:
                old = session.get(GatewayKey, replace)
                if not old or old.application_id != application_id or old.revoked_at:
                    raise AppError("API_KEY_NOT_FOUND", "未找到有效的应用凭据。", 404)
                old.revoked_at = database_now(session)
            identifier = uuid4().hex
            token = "ipk_" + identifier + "." + secrets.token_urlsafe(32)
            row = GatewayKey(key_id=identifier, application_id=application_id, label=command.label,
                token_hash=hashlib.sha256(token.encode()).hexdigest(), created_at=database_now(session))
            session.add(row)
            return {**self.view(row), "apiKey": token, "notice": "仅此响应显示完整 Key，请安全保存。"}

    def revoke(self, application_id, key_id):
        with self.core._transaction(True) as session:
            row = session.get(GatewayKey, key_id)
            if not row or row.application_id != application_id:raise AppError("API_KEY_NOT_FOUND", "凭据不存在。",404)
            if not row.revoked_at:row.revoked_at = database_now(session)
            return self.view(row)

    def applications(self):
        with self.core._transaction() as session:
            return {"items": [{"applicationId": app.application_id, "version": app.version, "enabled": app.enabled,
                "allowedServices": app.allowed_services, "concurrencyLimit": app.concurrency_limit,
                "keys": [self.view(k) for k in session.scalars(select(GatewayKey).where(GatewayKey.application_id==app.application_id).order_by(GatewayKey.created_at))]}
                for app in session.scalars(select(InferenceApplicationBudget).order_by(InferenceApplicationBudget.application_id))]}
