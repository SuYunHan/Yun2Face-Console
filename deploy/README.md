# Ubuntu 22.04 systemd daemon

这个服务运行 `web_app.py`，启动 Flask JSON API 和内置定时任务调度器。保持单进程运行，定时任务由该进程统一调度。

假设项目部署在 `/opt/yunForNewVersion`，运行用户是 `ubuntu`：

```bash
cd /opt/yunForNewVersion
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
sudo bash deploy/install_systemd.sh /opt/yunForNewVersion
```

常用管理命令：

```bash
sudo systemctl status yunfornewversion-web.service
sudo systemctl restart yunfornewversion-web.service
sudo systemctl stop yunfornewversion-web.service
sudo journalctl -u yunfornewversion-web.service -f
```

默认 API 监听 `127.0.0.1:8765`。在现有 Nginx 站点里加入以下配置，让 Nginx 从 `web/` 提供页面与脚本，并把 `/api/` 转发给 Flask。将 `web/api-config.js` 中的 `window.YUN_API_BASE_URL` 设为空字符串，让浏览器使用同源 API。

```nginx
root /opt/yunForNewVersion/web;
index index.html;

location / {
    try_files $uri $uri/ =404;
}

location /api/ {
    client_max_body_size 0;
    proxy_pass http://127.0.0.1:8765;
}
```

旧版站点若把所有请求转发给后端，需要替换为以上静态文件与 `/api/` 配置，并重启后端。控制台可读取和修改本机数据，远程访问时请通过现有认证或私有网络提供访问。

自定义 Python 或运行用户：

```bash
sudo PYTHON_BIN=/opt/yunForNewVersion/.venv/bin/python RUN_USER=yunrun RUN_GROUP=yunrun \
  bash deploy/install_systemd.sh /opt/yunForNewVersion
```

服务会自动重启，默认等待 5 秒；运行输出由 journald 管理，网页里的任务日志按天写在项目的 `web_data/runs/<年-月>/<年-月-日>.json`。
