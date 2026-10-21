"""公共通知 outbox 与安装身份；不改旧队列、结果或容量占位。"""

from uuid import uuid4

from alembic import context, op
import sqlalchemy as sa


revision = "20261021_0017"
down_revision = "20261021_0016"
branch_labels = None
depends_on = None


def upgrade():
    if context.is_offline_mode():
        raise RuntimeError("0017 须在线核对原公共调用，不支持离线 SQL 迁移")
    op.create_table("inference_queue_states",
        sa.Column("state_id", sa.String(16), primary_key=True),
        sa.Column("broker_id", sa.String(32), nullable=False, unique=True),
        sa.Column("availability", sa.String(16), nullable=False), sa.Column("checked_at", sa.DateTime()),
        sa.Column("last_error_code", sa.String(64)),
        sa.CheckConstraint("availability IN ('unknown','available','unavailable')", name="availability_allowed"))
    op.create_table("inference_queue_outboxes",
        sa.Column("event_id", sa.String(32), primary_key=True),
        sa.Column("invocation_id", sa.String(32), sa.ForeignKey("inference_invocations.invocation_id"), nullable=False, unique=True),
        sa.Column("redis_required", sa.Boolean(), nullable=False), sa.Column("state", sa.String(16), nullable=False),
        sa.Column("message_id", sa.String(64)), sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("publisher_token", sa.String(32)), sa.Column("publisher_expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime()), sa.Column("received_at", sa.DateTime()),
        sa.Column("settled_at", sa.DateTime()), sa.Column("last_error_code", sa.String(64)),
        sa.CheckConstraint("state IN ('pending','published','received','settled','online')", name="state_allowed"),
        sa.CheckConstraint("publish_attempts >= 0", name="attempts_nonnegative"))
    op.create_index("ix_inference_queue_outboxes_due", "inference_queue_outboxes", ["state", "next_attempt_at"])
    op.execute(sa.text("INSERT INTO inference_queue_states(state_id,broker_id,availability) VALUES ('redis',:broker,'unknown')").bindparams(broker=uuid4().hex))
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT invocation_id,kind,state FROM inference_invocations")).mappings():
        # 只补内部通知资料，不发布、不造事件、不变更执行或原任务期限。
        state = "settled" if row["state"] == "finished" else "online" if row["kind"] == "llm" else "pending"
        connection.execute(sa.text("INSERT INTO inference_queue_outboxes(event_id,invocation_id,redis_required,state,"
            "publish_attempts,created_at,next_attempt_at) VALUES (:event,:invocation,0,:state,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
            {"event": uuid4().hex, "invocation": row["invocation_id"], "state": state})


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("公共队列降级须在线核查")
    if op.get_bind().scalar(sa.text("SELECT COUNT(*) FROM inference_queue_outboxes")):
        raise RuntimeError("公共队列已有绑定，拒绝删除通知及 Redis 路由身份")
    op.drop_table("inference_queue_outboxes")
    op.drop_table("inference_queue_states")
