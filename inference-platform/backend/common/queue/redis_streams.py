"""官方 redis-py 的窄接口；无自动重试，不裁剪未完成消息。"""

from backend.common.exceptions.app_error import AppError


class RedisStreams:
    group = "executors-v1"
    # 检查消息完整身份与 ACK/删除原子进行，防止切换/恢复后同消息 ID 指向其他任务。
    ack_script = """
local rows = redis.call('XRANGE', KEYS[1], ARGV[2], ARGV[2], 'COUNT', 1)
if #rows == 0 then
  redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
  return 0
end
local fields = rows[1][2]
if #fields ~= #ARGV - 2 then return -1 end
local expected = {}
for i = 3, #ARGV, 2 do expected[ARGV[i]] = ARGV[i+1] end
local seen = {}
for i = 1, #fields, 2 do
  if seen[fields[i]] then return -1 end
  seen[fields[i]] = true
  if expected[fields[i]] ~= fields[i+1] then return -1 end
end
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
return redis.call('XDEL', KEYS[1], ARGV[2])
"""

    def __init__(self, client, stream, consumer, visibility_seconds):
        self.client, self.stream, self.consumer = client, stream, consumer
        self.visibility_ms = int(visibility_seconds * 1000)
        self.cursor = "0-0"

    @staticmethod
    def _unavailable():
        return AppError("REDIS_UNAVAILABLE", "Redis 通知通道暂不可用，暂停新的上游调用；已受理任务和在途结果仍保存在 SQLite。", 503)

    def _call(self, operation):
        try:
            return operation()
        except AppError:
            raise
        except Exception:
            raise self._unavailable() from None

    def ensure(self):
        def operation():
            if self.client.ping() is not True:
                raise self._unavailable()
            version = str(self.client.info("server").get("redis_version", "0.0")).split(".")
            if tuple(int(part) for part in version[:2]) < (6, 2):
                raise AppError("REDIS_VERSION_UNSUPPORTED", "Redis Streams 恢复需要 Redis 6.2 或更高版本。", 503)
            try:
                self.client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)
            except Exception as error:
                # 只有明确 BUSYGROUP 才代表已存在；不把 ACL/超时误当成功。
                if not str(error).startswith("BUSYGROUP "):
                    raise self._unavailable() from None
        return self._call(operation)

    def publish(self, notice):
        return self._call(lambda: self.client.xadd(self.stream, notice.model_dump(by_alias=True)))

    def receive(self, limit=20):
        def operation():
            claimed = self.client.xautoclaim(self.stream, self.group, self.consumer, self.visibility_ms,
                                            start_id=self.cursor, count=limit)
            self.cursor = claimed[0]
            messages = list(claimed[1])
            if len(messages) < limit:
                fresh = self.client.xreadgroup(self.group, self.consumer, {self.stream: ">"}, count=limit - len(messages))
                for _, rows in fresh or []:
                    messages.extend(rows)
            return messages
        return self._call(operation)

    def acknowledge(self, message_id, values):
        def operation():
            fields = [part for pair in sorted(values.items()) for part in pair]
            return self.client.eval(self.ack_script, 1, self.stream, self.group, message_id, *fields) == 1
        return self._call(operation)

    def close(self):
        self.client.close()
