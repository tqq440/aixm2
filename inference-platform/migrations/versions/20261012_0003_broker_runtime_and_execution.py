"""建立持久化通知、执行租约结果字段和实际运行快照。

Revision ID: 20261012_0003
Revises: 20261012_0002
Create Date: 2026-10-12 19:21:51.972339
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '20261012_0003'
down_revision: Union[str, Sequence[str], None] = '20261012_0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 添加列均允许旧任务保留原始值；不会修改旧任务状态或生成通知。
    op.create_table('runtime_components',
    sa.Column('component_id', sa.String(length=32), nullable=False),
    sa.Column('session_id', sa.String(length=32), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=False),
    sa.Column('child_pid', sa.Integer(), nullable=True),
    sa.Column('state', sa.String(length=32), nullable=False),
    sa.Column('model_ready', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('active_task_id', sa.String(length=32), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(), nullable=False),
    sa.Column('last_error', sa.String(length=256), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("component_id IN ('worker', 'dispatcher')", name=op.f('ck_runtime_components_component_id_allowed')),
    sa.CheckConstraint('pid > 0', name=op.f('ck_runtime_components_pid_positive')),
    sa.PrimaryKeyConstraint('component_id', name=op.f('pk_runtime_components'))
    )
    op.create_table('notification_records',
    sa.Column('notification_id', sa.String(length=32), nullable=False),
    sa.Column('event_id', sa.String(length=32), nullable=False),
    sa.Column('task_id', sa.String(length=32), nullable=False),
    sa.Column('queue_key', sa.String(length=128), nullable=False),
    sa.Column('dispatch_version', sa.Integer(), nullable=False),
    sa.Column('execution_plan_version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=32), server_default='ready', nullable=False),
    sa.Column('delivery_token', sa.String(length=64), nullable=True),
    sa.Column('delivery_owner', sa.String(length=64), nullable=True),
    sa.Column('visible_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.Column('acked_at', sa.DateTime(), nullable=True),
    sa.Column('deliveries', sa.Integer(), server_default='0', nullable=False),
    sa.CheckConstraint("status IN ('ready', 'inflight', 'acked')", name=op.f('ck_notification_records_valid_status')),
    sa.CheckConstraint('deliveries >= 0', name=op.f('ck_notification_records_deliveries_nonnegative')),
    sa.CheckConstraint('dispatch_version >= 1', name=op.f('ck_notification_records_dispatch_version_positive')),
    sa.CheckConstraint('execution_plan_version >= 1', name=op.f('ck_notification_records_execution_plan_version_positive')),
    sa.CheckConstraint('length(notification_id) = 32', name=op.f('ck_notification_records_notification_id_length')),
    sa.ForeignKeyConstraint(['event_id'], ['dispatch_events.event_id'], name=op.f('fk_notification_records_event_id_dispatch_events')),
    sa.ForeignKeyConstraint(['task_id'], ['tasks.task_id'], name=op.f('fk_notification_records_task_id_tasks')),
    sa.PrimaryKeyConstraint('notification_id', name=op.f('pk_notification_records')),
    sa.UniqueConstraint('event_id', name=op.f('uq_notification_records_event_id'))
    )
    with op.batch_alter_table('notification_records', schema=None) as batch_op:
        batch_op.create_index('ix_notification_records_queue_visible', ['queue_key', 'status', 'visible_at'], unique=False)

    with op.batch_alter_table('dispatch_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('published_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('notification_id', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('publish_attempts', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('last_error', sa.String(length=256), nullable=True))

    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lease_token', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('lease_worker_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('started_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('finished_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('result_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('error_json', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('result_fingerprint', sa.String(length=64), nullable=True))



def downgrade() -> None:
    # 降级会丢弃第四部分通知、运行快照和执行结果，应先停止相关进程并备份。
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('result_fingerprint')
        batch_op.drop_column('error_json')
        batch_op.drop_column('result_json')
        batch_op.drop_column('finished_at')
        batch_op.drop_column('started_at')
        batch_op.drop_column('lease_expires_at')
        batch_op.drop_column('lease_worker_id')
        batch_op.drop_column('lease_token')

    with op.batch_alter_table('dispatch_events', schema=None) as batch_op:
        batch_op.drop_column('last_error')
        batch_op.drop_column('publish_attempts')
        batch_op.drop_column('notification_id')
        batch_op.drop_column('published_at')

    with op.batch_alter_table('notification_records', schema=None) as batch_op:
        batch_op.drop_index('ix_notification_records_queue_visible')

    op.drop_table('notification_records')
    op.drop_table('runtime_components')
