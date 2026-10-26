"""只管理测试自己创建的Linux Redis子进程，永不向外部实例发送关闭命令。"""

import os
import json
from datetime import datetime,timezone
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import time

from backend.utils.files.deployment_files import new_directory,private_text


def redis_config(folder,port,password):
    path=str(folder)
    if any(char in path for char in ('"','\\','\n','\r')):raise ValueError("Unsupported Redis test path")
    if not 1024<=port<=65535 or not password.isalnum() or len(password)<32:raise ValueError("Invalid test configuration")
    return (f'bind 127.0.0.1\nport {port}\nprotected-mode yes\ndaemonize no\n'
        f'dir "{path}"\nlogfile "{path}/redis.log"\n'
        f'requirepass {password}\nappendonly yes\nappendfsync always\nsave ""\n'
        'maxmemory-policy noeviction\n')


class OwnedRedis:
    def __init__(self,binary,folder,client_factory):
        if platform.system()!="Linux":raise ValueError("Real Linux Redis process test required")
        executable=Path(binary)
        if not executable.is_absolute() or not executable.is_file() or not os.access(executable,os.X_OK):
            raise ValueError("Explicit installed redis-server executable required")
        if executable.name!="redis-server":raise ValueError("Only redis-server is supported")
        self.binary=executable;self.folder=new_directory(folder);self.process=None;self.client=None
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1",0));self.port=port_socket.getsockname()[1]
        self.password=secrets.token_hex(32)
        self.url=f"redis://:{self.password}@127.0.0.1:{self.port}/0"
        self.config=self.folder/"redis.conf"
        private_text(self.config,redis_config(self.folder,self.port,self.password))
        private_text(self.folder/"OWNED-TEST-INSTANCE","Only this test's direct Popen process may be stopped.\n")
        self.client=client_factory(self.url)

    def start(self):
        if self.process is not None and self.process.poll() is None:raise RuntimeError("Test Redis is already running")
        self.process=subprocess.Popen([str(self.binary),str(self.config)],cwd=self.folder,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            shell=False,start_new_session=True)
        deadline=time.monotonic()+10
        try:
            while time.monotonic()<deadline:
                if self.process.poll() is not None:raise RuntimeError("Owned Redis exited before ready; inspect its private log")
                try:info=self.client.info("server")
                except Exception:
                    time.sleep(.05);continue
                if int(info.get("process_id",-1))!=self.process.pid:raise RuntimeError("Redis PID does not match owned child")
                actual=self.client.config_get("dir")
                if Path(actual["dir"]).resolve()!=self.folder.resolve():raise RuntimeError("Redis data directory differs from owned test workspace")
                self.version=str(info["redis_version"])
                persistence={key:self.client.config_get(key).get(key) for key in ("appendonly","appendfsync")}
                if persistence!={"appendonly":"yes","appendfsync":"always"}:raise RuntimeError("Test Redis persistence configuration differs")
                private_text(self.folder/("started-"+secrets.token_hex(8)+".json"),json.dumps({
                    "recordedAt":datetime.now(timezone.utc).isoformat(),"pid":self.process.pid,"redisVersion":self.version,
                    "platform":platform.system(),"persistence":persistence,"scope":"owned-loopback-test-only"})+'\n')
                return
            raise RuntimeError("Owned Redis readiness timed out")
        except BaseException:
            self.stop();raise

    def stop(self,*,crash=False):
        process=self.process
        if process is None or process.poll() is not None:return
        if crash:process.kill()
        else:process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=5)

    def close(self):
        try:self.stop()
        finally:
            if self.client:self.client.close()
