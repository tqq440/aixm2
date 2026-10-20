"""增加固定期限、有限重试和执行历史，保留已有任务及结果。

Revision ID: 20261012_0004
Revises: 20261012_0003
"""

from alembic import op
from datetime import timedelta
import sqlalchemy as sa


revision = "20261012_0004"
down_revision = "20261012_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("max_attempts", sa.Integer(), server_default="2", nullable=False))
        for name in ("queue_deadline_at", "total_deadline_at", "attempt_deadline_at", "next_retry_at"):
            batch.add_column(sa.Column(name, sa.DateTime(), nullable=True))
        batch.create_check_constraint("max_attempts_positive", "max_attempts >= 1")
        batch.create_index("ix_tasks_status_lease", ["status", "lease_expires_at"])
        batch.create_index("ix_tasks_status_deadlines", ["status", "queue_deadline_at", "total_deadline_at"])
    # 旧数据使用本阶段已确认的初值，从原始受理/执行时间计算，绝不从升级时间重新计时。
    # SQLite 日期计算会截断微秒，因此按编号游标分批保留 Python datetime 的原精度。
    tasks = sa.table("tasks", sa.column("task_id", sa.String(32)),
                     sa.column("created_at", sa.DateTime()), sa.column("started_at", sa.DateTime()),
                     sa.column("queue_deadline_at", sa.DateTime()),
                     sa.column("total_deadline_at", sa.DateTime()),
                     sa.column("attempt_deadline_at", sa.DateTime()))
    connection = op.get_bind()
    cursor = ""
    while True:
        rows = connection.execute(sa.select(tasks.c.task_id, tasks.c.created_at, tasks.c.started_at)
                                  .where(tasks.c.task_id > cursor).order_by(tasks.c.task_id).limit(100)).mappings().all()
        if not rows:
            break
        connection.execute(sa.update(tasks).where(tasks.c.task_id == sa.bindparam("task_key")).values(
            queue_deadline_at=sa.bindparam("queue_at"), total_deadline_at=sa.bindparam("total_at"),
            attempt_deadline_at=sa.bindparam("attempt_at")), [{
                "task_key": row["task_id"], "queue_at": row["created_at"] + timedelta(seconds=600),
                "total_at": row["created_at"] + timedelta(seconds=1800),
                "attempt_at": row["started_at"] + timedelta(seconds=900) if row["started_at"] else None,
            } for row in rows])
        cursor = rows[-1]["task_id"]
    op.create_table(
        "execution_attempts",
        sa.Column("lease_token", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("tasks.task_id"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("dispatch_version", sa.Integer(), nullable=False),
        sa.Column("execution_plan_version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("error_json", sa.JSON(), nullable=True),
        sa.Column("failure_fingerprint", sa.String(64), nullable=True),
        sa.UniqueConstraint("task_id", "attempt", name="uq_execution_attempts_task_attempt"),
        sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
    )
    op.execute("INSERT INTO execution_attempts "
               "(lease_token, task_id, attempt, worker_id, dispatch_version, execution_plan_version, "
               "started_at, finished_at, outcome, error_json, failure_fingerprint) "
               "SELECT lease_token, task_id, attempt, lease_worker_id, dispatch_version, execution_plan_version, "
               "started_at, finished_at, status, error_json, "
               "CASE WHEN status = 'failed' THEN result_fingerprint ELSE NULL END "
               "FROM tasks WHERE attempt >= 1 AND lease_token IS NOT NULL "
               "AND lease_worker_id IS NOT NULL AND started_at IS NOT NULL")


def downgrade() -> None:
    # 降级丢弃期限/历史；新状态无法在旧版本继续，应先停止服务、备份并处理未完成任务。
    incompatible = op.get_bind().scalar(sa.text(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('retry_wait', 'cancel_requested', 'cancelled')"))
    if incompatible:
        raise RuntimeError("存在旧版本无法读取的任务状态，不能降级至 0003；请先停止服务并备份、处理这些任务。")
    op.drop_table("execution_attempts")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_status_deadlines")
        batch.drop_index("ix_tasks_status_lease")
        batch.drop_constraint("max_attempts_positive", type_="check")
        for name in ("next_retry_at", "attempt_deadline_at", "total_deadline_at", "queue_deadline_at", "max_attempts"):
            batch.drop_column(name)
