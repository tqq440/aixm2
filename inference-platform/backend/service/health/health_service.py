from sqlalchemy.exc import SQLAlchemyError

from backend.common.exceptions.app_error import AppError
from backend.data.database.database import Database
from backend.schema.health.health_schema import HealthStatus


class HealthService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def check(self) -> HealthStatus:
        try:
            self.database.check_connection()
        except SQLAlchemyError as exc:
            raise AppError("DATABASE_UNAVAILABLE", "数据库暂不可用。", 503) from exc
        return HealthStatus()
