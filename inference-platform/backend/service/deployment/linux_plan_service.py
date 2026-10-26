"""离线生成验证部署资料；不安装、不连接服务器、不调用 systemctl。"""

import json
from pathlib import Path
from backend.schema.deployment.linux_schema import LinuxDeploymentPlan
from backend.utils.files.deployment_files import new_directory,private_text


def render_linux_plan(plan,output):
    plan=LinuxDeploymentPlan.model_validate(plan.model_dump())
    output=new_directory(Path(output))
    root=plan.state_root;prefix="inference-"+plan.name
    common=("[Unit]\nDescription=Inference isolated validation {role}\nStartLimitIntervalSec=60\nStartLimitBurst=3\n\n"
            "[Service]\nType=simple\nWorkingDirectory={project}\nUMask=0077\n"
            "Environment=PYTHONUNBUFFERED=1\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
            "ExecStart={python} -m backend.modules.deployment.linux_cli --env-file {root}/config/{env}.env {command}\n"
            "Restart=on-failure\nRestartSec=5\nRestartPreventExitStatus=2\n"
            "KillSignal=SIGTERM\nKillMode=mixed\nTimeoutStopSec=960\n"
            "StandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=default.target\n")
    files={}
    for role,env,command in (("center","center","serve center"),("executor","center","executor"),("node","node","serve node")):
        files[f"{prefix}-{role}.service"]=common.format(role=role,project=plan.project_dir,python=plan.python_executable,root=root,env=env,command=command+f" --threads {plan.threads}" if role!="executor" else command)
    files["plan.json"]=json.dumps(plan.model_dump(by_alias=True),ensure_ascii=False,indent=2)+'\n'
    # 仅回环验证入口；公网/校园网 TLS、域名、访问控制留真实部署阶段。
    files["nginx-validation.conf"]=(f"server {{\n    listen 127.0.0.1:{plan.ui_port};\n    server_name localhost;\n"
        f"    root {plan.project_dir}/frontend/dist;\n    access_log off;\n    client_max_body_size 21m;\n"
        f"    location /api/ {{ proxy_pass http://127.0.0.1:{plan.center_port}; proxy_set_header Host $host; proxy_buffering off; proxy_read_timeout 930s; }}\n"
        "    location / { try_files $uri $uri/ /index.html; }\n}\n"
        f"server {{\n    listen 127.0.0.1:{plan.public_proxy_port};\n    server_name localhost;\n    access_log off;\n    client_max_body_size 21m;\n"
        f"    location /api/v1/ {{\n        proxy_pass http://127.0.0.1:{plan.node_port};\n        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n        proxy_buffering off;\n        proxy_request_buffering off;\n"
        "        proxy_read_timeout 930s;\n        proxy_send_timeout 930s;\n    }\n    location / { return 404; }\n}\n")
    files["review.sh"]=("#!/bin/sh\nset -eu\n"
        f"cd '{plan.project_dir}'\n"
        f"'{plan.python_executable}' -m backend.modules.deployment.linux_cli --env-file '{root}/config/center.env' inspect --require-linux\n"
        f"'{plan.python_executable}' -m backend.modules.deployment.linux_cli --env-file '{root}/config/node.env' inspect --require-linux\n"
        "# Only checks. Does not install units, migrate databases, start services or control containers.\n")
    files["OPERATIONS.md"]=(f"# {plan.name} 独立 Linux 验证资料\n\n"
        "本目录不含密钥，不会自动安装或启停服务。先审阅 plan.json 与 service 文件。\n\n"
        f"1. 在 Linux 项目目录，用 {plan.python_executable} 安装已审查的 requirements/linux.txt；Redis客户端按6B再安装。\n"
        f"2. 创建父目录`install -d -m 700 {plan.project_dir}/runtime/deployment-validation`；运行`{plan.python_executable} -m backend.modules.deployment.linux_cli init --root {root} --name {plan.name} --center-port {plan.center_port} --node-port {plan.node_port}`，只允许全新目录；不复制日常.env。\n"
        "3. `sh review.sh`。同时核对 requirements 解析、各端口、文件权限、磁盘和两个数据库版本。任何错误先处理，不能靠启动服务修复。\n"
        "4. 仅实际部署获准后，把本批三个.service复制到本人 ~/.config/systemd/user/，执行systemctl --user daemon-reload，再分别start。Node与中心没有停止依赖。\n"
        f"5. 查看 `systemctl --user status {prefix}-node.service`、相应center/executor及journalctl --user -u单元。验证完单独stop本批单元；不要停止其他部署。\n"
        "6. 开机自启需enable本人单元，并由管理员确认user lingering。生成文件不等于已配置开机自启；systemd/Gunicorn/nginx真实启动均需Linux验收。\n\n"
        "nginx-validation.conf为可选回环配置，需已有frontend/dist；先nginx -t，不能直接覆盖现有配置。管理入口只能本机使用。校园网开放与TLS留后续。\n"
        "队列初始为SQLite独立验证模式，不代表Redis已验收。Docker及业务执行默认关闭，没有登记或启动模型。\n"
        "不要使用Gunicorn HUP/USR2热重载或TTIN增进程；单库只允许一个API/Node所有者，升级采用显式停止核对后重启。线程数不是业务并发额度。\n"
        "日志为每进程JSON滚动文件，journal另由主机管理；不要把含密钥的env、请求正文或上传内容放进日志/工单。\n"
        "退出等待最长960秒；不能据此把未知调用当作取消或释放额度。停止后继续保留状态库、上传、结果、调用恢复记录。\n")
    for name,content in files.items():private_text(output/name,content)
    return {"output":str(output),"files":sorted(files),"installed":False,"servicesStarted":False,"secretsIncluded":False}
