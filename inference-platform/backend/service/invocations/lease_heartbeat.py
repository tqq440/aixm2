"""网络调用期间续期，与 token 到达频率无关；续期失败不作为远端停止证明。"""

from threading import Event, Thread


class LeaseHeartbeat:
    def __init__(self,core,lease):
        self.core,self.lease=core,lease;self.stop=Event()
        self.thread=Thread(target=self._run,name="node-lease-renew",daemon=False)

    def _run(self):
        while not self.stop.wait(max(0.1,min(5,self.core.settings.lease_seconds/3))):
            try:
                if not self.core.renew(self.lease):return
            except Exception:return

    def __enter__(self):self.thread.start();return self
    def __exit__(self,*_):self.stop.set();self.thread.join()
