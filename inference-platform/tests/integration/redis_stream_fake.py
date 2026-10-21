"""隔离 redis-py 接口替身：PEL、重新领取及掉电丢数据；不是真实 Redis。"""

from threading import RLock


class RedisStreamFake:
    def __init__(self):
        self.lock = RLock()
        self.entries, self.pending, self.delivered = {}, {}, set()
        self.groups, self.clock, self.serial = set(), 0, 0
        self.down = False
        self.after_add = None
        self.before_ack = None
        self.fail_ack = False
        self.commands = []
        self.version = "7.2.0"
        self.closed = False

    def _check(self, command):
        self.commands.append(command)
        if self.down: raise ConnectionError("PRIVATE_REDIS_PASSWORD")

    def ping(self):
        self._check("PING"); return True

    def info(self, section):
        self._check("INFO"); return {"redis_version": self.version}

    def xgroup_create(self, stream, group, id, mkstream):
        with self.lock:
            self._check("XGROUP")
            if (stream, group) in self.groups: raise RuntimeError("BUSYGROUP Consumer Group name already exists")
            self.groups.add((stream, group)); return True

    def xadd(self, stream, values):
        with self.lock:
            self._check("XADD")
            self.serial += 1; message_id = str(self.serial) + "-0"
            self.entries[(stream, message_id)] = dict(values)
            if self.after_add: self.after_add(message_id)
            return message_id

    def xreadgroup(self, group, consumer, streams, count):
        with self.lock:
            self._check("XREADGROUP")
            stream = next(iter(streams))
            if (stream, group) not in self.groups: raise RuntimeError("NOGROUP")
            messages = []
            for (key, message_id), values in self.entries.items():
                if key == stream and (stream, group, message_id) not in self.delivered:
                    identity = (stream, group, message_id)
                    self.pending[identity] = (consumer, self.clock)
                    self.delivered.add(identity); messages.append((message_id, dict(values)))
                    if len(messages) >= count: break
            return [(stream, messages)] if messages else []

    def xautoclaim(self, stream, group, consumer, min_idle_time, start_id, count):
        with self.lock:
            self._check("XAUTOCLAIM")
            if (stream, group) not in self.groups: raise RuntimeError("NOGROUP")
            messages = []
            for identity, (_, since) in list(self.pending.items()):
                key, saved_group, message_id = identity
                if key == stream and saved_group == group and self.clock - since >= min_idle_time:
                    self.pending[identity] = (consumer, self.clock)
                    if (key, message_id) in self.entries: messages.append((message_id, dict(self.entries[(key, message_id)])))
                    if len(messages) >= count: break
            return ["0-0", messages, []]

    def xack(self, stream, group, message_id):
        with self.lock:
            self._check("XACK")
            if self.before_ack: self.before_ack(message_id)
            if self.fail_ack: raise ConnectionError("PRIVATE_ACK_FAILURE")
            return int(self.pending.pop((stream, group, message_id), None) is not None)

    def xdel(self, stream, message_id):
        with self.lock:
            self._check("XDEL")
            return int(self.entries.pop((stream, message_id), None) is not None)

    def eval(self, script, number_of_keys, stream, group, message_id, *fields):
        with self.lock:
            self._check("EVAL")
            expected = dict(zip(fields[::2], fields[1::2]))
            actual = self.entries.get((stream, message_id))
            if actual is None:
                self.xack(stream, group, message_id)
                return 0
            if actual != expected: return -1
            self.xack(stream, group, message_id)
            return self.xdel(stream, message_id)

    def lose_data(self):
        with self.lock:
            self.entries.clear(); self.pending.clear(); self.delivered.clear(); self.groups.clear()

    def close(self):
        self.closed = True
