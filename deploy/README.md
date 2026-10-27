# Ubuntu 22.04 systemd daemon

这个服务守护 `web_app.py`。网页服务启动后会同时启动内置的每日定时任务调度器。

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

默认只监听 `127.0.0.1:8765`。如果前面有 Nginx 反向代理，保持这个配置即可。
如果确实需要直接暴露端口，可编辑 service，将 `--host 127.0.0.1` 改为 `--host 0.0.0.0`，然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart yunfornewversion-web.service
```

自定义 Python 或运行用户：

```bash
sudo PYTHON_BIN=/opt/yunForNewVersion/.venv/bin/python RUN_USER=yunrun RUN_GROUP=yunrun \
  bash deploy/install_systemd.sh /opt/yunForNewVersion
```

服务会自动重启，默认等待 5 秒；运行输出由 journald 管理，网页里的任务日志仍保存到项目的 `web_data/runs`。
