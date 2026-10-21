"""无依赖公共调用示例；Key 从环境读取，只访问入口 Node。"""

import argparse
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from uuid import uuid4


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--kind',choices=('asr','vision','llm'),required=True)
    parser.add_argument('--service',required=True)
    parser.add_argument('--file',type=Path)
    parser.add_argument('--text')
    parser.add_argument('--idempotency-key',default=uuid4().hex)
    args=parser.parse_args()
    origin=os.environ['INFERENCE_NODE_URL'].rstrip('/')
    key=os.environ['INFERENCE_API_KEY']
    opener=build_opener(ProxyHandler({}),NoRedirect())
    def call(method,path,body=None,content_type=None):
        headers={'Authorization':'Bearer '+key,'Idempotency-Key':args.idempotency_key,'Accept-Encoding':'identity'}
        if content_type:headers['Content-Type']=content_type
        return opener.open(Request(origin+'/api/v1'+path,data=body,headers=headers,method=method),timeout=900)
    print('Idempotency-Key:',args.idempotency_key)
    try:
        if args.kind=='llm':
            if not args.text:parser.error('llm 需要 --text')
            body=json.dumps({'serviceId':args.service,'messages':[{'role':'user','content':args.text}]}).encode()
            with call('POST','/llm/requests',body,'application/json') as response:
                if not response.headers.get('Content-Type','').startswith('text/event-stream'):
                    print(json.loads(response.read(3*1024*1024)));return
                # 示例逐行展示；只有 event=done 才完成，EOF 不能代替终态。
                event='';finished=False
                for raw in response:
                    line=raw.decode('utf-8').rstrip('\r\n')
                    if line.startswith('event:'):event=line[6:].strip()
                    elif line.startswith('data:'):
                        value=json.loads(line[5:])
                        if event=='delta':print(value['text'],end='',flush=True)
                        elif event in ('accepted','done','error'):print('\n',event,value)
                        if event=='done':finished=True
                if not finished:print('\n未确认成功；按 accepted 中的 id 查询原请求，不更换幂等键重发。')
            return
        if not args.file:parser.error('离线任务需要 --file')
        maximum=(20 if args.kind=='asr' else 10)*1024*1024
        with args.file.open('rb') as source:content=source.read(maximum+1)
        if len(content)>maximum:parser.error('文件超出大小限制')
        boundary='gateway-'+uuid4().hex
        body=(f'--{boundary}\r\nContent-Disposition: form-data; name="serviceId"\r\n\r\n{args.service}\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="input.upload"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()+content+f'\r\n--{boundary}--\r\n'.encode()
        with call('POST','/tasks/'+args.kind,body,'multipart/form-data; boundary='+boundary) as response:
            task=json.loads(response.read(3*1024*1024))['data'];identifier=task['id'];print('taskId:',identifier)
        end=time.monotonic()+900
        while time.monotonic()<end:
            with call('GET','/tasks/'+identifier) as response:task=json.loads(response.read(3*1024*1024))['data']
            if task['status'] in ('succeeded','failed','cancelled','unknown'):
                print(task)
                if task['resultAvailable']:
                    with call('GET','/tasks/'+identifier+'/result') as response:print(json.loads(response.read(3*1024*1024))['data'])
                return
            time.sleep(2)
        print('本地等待到期；任务未取消，请继续查询原 taskId。')
    except HTTPError as error:
        print('HTTP',error.code, error.read(4096).decode('utf-8',errors='replace'))
        raise SystemExit(1) from None


if __name__=='__main__':main()
