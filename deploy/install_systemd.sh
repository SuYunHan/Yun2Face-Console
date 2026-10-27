#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${1:-/opt/yunForNewVersion}"
SERVICE_NAME="yunfornewversion-web.service"
SERVICE_SRC="$PROJECT_ROOT/deploy/$SERVICE_NAME"
SERVICE_DST="/etc/systemd/system/$SERVICE_NAME"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="/usr/bin/python3"
fi
RUN_USER="${RUN_USER:-${SUDO_USER:-ubuntu}}"
RUN_GROUP="${RUN_GROUP:-$RUN_USER}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请使用 sudo 运行：sudo bash deploy/install_systemd.sh /opt/yunForNewVersion" >&2
  exit 1
fi

if [[ ! -f "$PROJECT_ROOT/web_app.py" ]]; then
  echo "找不到 $PROJECT_ROOT/web_app.py" >&2
  exit 1
fi

if [[ ! -f "$SERVICE_SRC" ]]; then
  echo "找不到服务模板：$SERVICE_SRC" >&2
  exit 1
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
  echo "用户不存在：$RUN_USER（可用 RUN_USER=... 指定）" >&2
  exit 1
fi

install -d -o "$RUN_USER" -g "$RUN_GROUP" "$PROJECT_ROOT"
install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
sed -i "s#^ExecStart=.*#ExecStart=$PYTHON_BIN $PROJECT_ROOT/web_app.py --host 0.0.0.0 --port 8765 --no-browser#" "$SERVICE_DST"
sed -i "s#^WorkingDirectory=.*#WorkingDirectory=$PROJECT_ROOT#" "$SERVICE_DST"
sed -i "s#^User=.*#User=$RUN_USER#" "$SERVICE_DST"
sed -i "s#^Group=.*#Group=$RUN_GROUP#" "$SERVICE_DST"

if id "$RUN_USER" >/dev/null 2>&1; then
  chown -R "$RUN_USER:$RUN_GROUP" "$PROJECT_ROOT/students_cofig" \
    "$PROJECT_ROOT/sutdents_face" "$PROJECT_ROOT/students_face_json" \
    "$PROJECT_ROOT/web_data" 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME"

echo
echo "服务已安装：$SERVICE_NAME"
echo "查看实时日志：sudo journalctl -u $SERVICE_NAME -f"
