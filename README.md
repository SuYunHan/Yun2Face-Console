# 云运动自动跑步脚本（Yun2Face Console）

云运动（校园跑步）任务的**本地网页控制台 + 命令行工具**：人脸素材预检、跑步任务表管理、离线演练 / 真实运行、按学号定时任务、学期统计与历史导入、路线预览与轨迹合成。

> 本仓库由 [Zirconium233/yunForNewVersion](https://github.com/Zirconium233/yunForNewVersion) 修改而来：协议实现与规则沿用上游，本地面板 / 定时任务 / 任务表工具为本仓库新增；配置与任务表已按**安徽邮电职业技术学院（安邮）**整理，更适合安邮宝宝体质。

控制台只在本机运行（默认 `127.0.0.1:8765`），浏览器里完成照片标注与预检、任务表校验与派生、一键启动 `main.py` 并查看实时日志；不想开网页时也可以直接用命令行。

> ⚠️ 仅用于学习与自查接口行为，请遵守所在学校的规定。任何字段对齐都不构成"绕过"承诺——服务端还有行为/数据层面的分析（配速分布、请求节奏、TLS/HTTP 指纹等），也不保证任何账号能通过校验。

---

## 功能一览

网页控制台（`web_app.py` + `web/`，原生 JS 无构建步骤）共 8 个页签：

| 页签 | 功能 |
|---|---|
| 人脸素材 | 按学号上传照片、YuNet 自动检测生成标注、手工拖动人脸框 / 五点微调、批量预检、删除；标注与照片内容哈希（`source_sha256`）绑定 |
| 跑步任务 | 浏览任务表（速度 / 里程 / 步数曲线）、单表校验、全部校验、导入、复制 / 改名 / 删除、按配速缩放或漂移**派生**新表、送入项目目录 |
| 运行控制 | 选择学号账号、任务目录、任务表、人脸源与标注，启动**离线演练**或**真实运行**并实时看日志 / 退出码；预览实际命令行 |
| 定时运行 | 每天固定时间自动跑；学号取左侧学号栏选中项，列表只显示该学号的定时任务；启用/停用/立即运行/修改/删除 |
| 学期统计 | 读取服务端本学期记录（总次数、早晚跑、合格/不合格） |
| 历史导入 | 拉取服务端历史跑步记录 → 轨迹预览 → 另存为任务表（自动取下一个 `tasklist_N.json` 写入任务表库） |
| 路线预览 | 打表回放与 Deform 轨迹合成，离线画布对照参考路线与当前路线，输出指标与实际命令行 |
| 项目配置 | 编辑当前学号 `config.ini`；敏感字段留空保留原值，支持续期后写回 token |

命令行侧：

- `main.py`：登录 / 建记录 / 分批上传轨迹点 / 人脸窗口 / 结束链；支持离线演练与几何生成。
- `face_check.py`：人脸输入"上传前预检"独立工具（规则与运行链路完全一致）。
- `history.py`：拉取历史记录并保存成任务表。
- `live_probe.py`：只做登录 + 准入查询的 L1 探测，不创建跑步记录。
- `yun_route.py` / `tools/generate_route.py`：轨迹合成与几何模板生成。

---

## 环境要求

- **Python 3.9+**（开发验证于 3.11，线上部署验证于 3.12）
- Windows / Linux 均可；Windows 下用 `.venv` 或系统 Python 都可以，Linux 建议 venv + systemd
- 依赖见 `requirements.txt`：`requests`、`gmssl==3.2.2`（**不要替换版本**）、`tqdm`、`schedule`、`mitmproxy`（仅抓包脚本用）、`pycryptodome`、`pillow`、`numpy`、`opencv-python-headless`
- `opencv`/`numpy` 用于网页端 **YuNet 自动检测** 与视频抽帧；不装也能跑基础预检，只是不能自动生成标注

## 安装

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
# 测试依赖（只跑测试才需要）
pip install -r requirements-dev.txt
```

> `gmssl` 必须锁 `3.2.2`：3.2.1 缺少 `mode/asn1` 参数，加密布局不同。

---

## 快速开始（网页控制台）

```bash
python web_app.py                 # 默认 127.0.0.1:8765，自动打开浏览器
python web_app.py --port 9000 --no-browser
```

首次启动会在项目根目录自动创建/使用这些目录：

```
students_cofig/<学号>/config.ini     # 每个学号的账号配置
sutdents_face/<学号>/<照片>          # 人脸照片（目录名沿用历史拼写）
students_face_json/<学号>/<stem>.json  # 人脸标注
web_data/runs/                       # 运行日志、runs.json、schedules.json
out/                                 # 任务表锁定副本与缩略图缓存
```

推荐流程：

1. **左侧学号栏**点「+ 新建」建学号（或选中已有学号）；
2. 「人脸素材」上传正面自拍 → 「批量生成标注」（自动检测）→ 手工核对框/五点 → 「批量预检」；
3. 「跑步任务」导入或选择任务表 → 校验（里程/配速/步频/点数/围栏）；
4. 「运行控制」：先用 **离线演练** 确认命令行与轨迹，再切 **真实运行**（需要配置里的 token 或登录）；
5. 「定时运行」：选中左侧学号 → 选任务表/时间（需要人脸的任务要先准备人脸源）→ 保存；
6. 需要历史数据时用「学期统计」「历史导入」。

> 真实运行会**创建服务端记录**。第一次请先用离线演练把配置和任务表走通。

---

## 配置

每个学号一份 `config.ini`（网页「项目配置」页可直接编辑，也可手工改 `students_cofig/<学号>/config.ini`）：

### `[Yun]` 主机与协议参数

| 键 | 说明 |
|---|---|
| `yun_host` | 云运动主接口地址 |
| `school_host` | 学校跑步接口地址（形如 `https://sports.example.com:8000`；带 `/m-api/` 或私网 IP 时要保留完整 URL） |
| `school_id` / `school_name` / `school_login_url` | 学校目录参数（`school_login_url` 需与学校对应，如 `appLogin` / `appLoginHGD`） |
| `app_edition` | 请求头 `version`，按当前 App 版本填（本仓库 `config.ini` 为 `3.6.7`；协议分析基线 3.6.6） |
| `PublicKey` / `PrivateKey` | SM2 密钥对；生产加密只需要公钥，仓库示例私钥不配对属正常 |
| `cipherkey` / `cipherkeyencrypted` | 登录/学校目录旧路线的固定 SM4 key 与其 SM2 密文 |
| `md5key` | 签名用 appsecret |
| `platform` | 固定 `android` |

仓库自带的 `config.ini` 已指向 **安徽邮电职业技术学院（schoolId=195，`sports.aiyyd.com:8000`）**，换学校请改这里。

### `[User]` 身份

`token`（留空时网页「登录」或 `main.py` 会尝试用 `[Login]` 账密登录）、`device_id`、`device_name`（格式 `厂商(型号)`）、`sys_edition` / `sys_version`、`legacy_uuid`（旧协议兼容，默认关闭，每请求随机大写 UUID）。

### `[Run]` 跑步参数

`split_count`（每组轨迹点数，默认 10）、`strides`（步幅/米）、`min_distance`、`allow_overflow_distance`、`single_mileage_min_offset` / `single_mileage_max_offset`、`cadence_min_offset` / `cadence_max_offset`、`min_consume` / `max_consume`、`exclude_points` 等，均有示例值可改。

### `[Login]`

`username` / `password`，供登录与 **401 自动续期** 使用（`--reauth-save-token` 或网页勾选后会写回新 token）。

### 学校地址查询

```bash
python tools/getUrl_Id.py --list
python tools/getUrl_Id.py --school "学校全称" --write --config config.ini
```

已抓好的对照表见 [`docs/SCHOOL_DIRECTORY_20260913.md`](docs/SCHOOL_DIRECTORY_20260913.md)。连接失败先核对学校地址，不要猜端口。

---

## 命令行

```bash
# 交互式（选配置 → 可选登录 → 选任务表 → 执行）
python main.py

# 全自动打表（指定配置与任务表目录）
python main.py -a -f students_cofig/JW250220/config.ini -t tasks_fch

# 加轨迹漂移
python main.py -a -f config.ini -t tasks_fch -d

# 离线演练（零网络、不写配置、不 sleep）
python main.py --dry-run --dry-home dry_run_home.json

# 几何模板生成（V4，替代打表；不能与 -t/-d 混用）
python main.py --route-config examples/routes/deform_fch.json -f config.ini

# 人脸任务：照片 + 标注 JSON；或视频（逐帧标注，需 opencv）
python main.py -a -f config.ini -t tasks_fch \
  --face-photo sutdents_face/JW250220/JW250220_D1.jpg \
  --face-detection students_face_json/JW250220/JW250220_D1.json

# 登录重试 / 会话续期
python main.py -a -f config.ini -t tasks_fch --login-retries 2 --login-retry-delay 3 --reauth-save-token
# 关闭 401 自动重登：--no-auto-reauth
```

路径契约：显式传入的相对路径按**调用时工作目录**解析；未传入的缺省资源按项目根解析。

### 其他工具

```bash
python face_check.py rules                  # 打印全部阈值
python face_check.py check 图.jpg [标注.json]  # 单张预检（无标注则自动检测）
python face_check.py gen 文件夹 --out out      # 批量预检并产出标注
python face_check.py video 视频.mp4 [标注.json] # 视频源预检
python face_check.py show 图.jpg [标注.json]    # 画框+五点+门限线

python history.py                           # 交互式拉取历史跑步记录
python live_probe.py <raRunArea>            # 准入探测（0=Y 2=缺主机 3=登录失败 4=业务失败 5=非Y/未知）
```

---

## 定时运行

- 定时计划保存在 `web_data/runs/schedules.json`，每天固定 `HH:MM` 执行；
- 学号取**左侧学号栏**选中项：列表只显示该学号的定时任务，切换学号即切换；
- 时间固定按 **北京时间（UTC+8）** 解释，不跟随服务器时区；需要别的时区用环境变量 `YUN_SCHEDULE_TZ_OFFSET_MIN`（分钟，东八区=480）覆盖；
- 错过的运行**不补跑**：晚于计划时间超过 15 分钟直接顺延到下一次（日志会显示"已跳过 N 次"）；
- 选了具体任务表时，调度器会把它复制到 `out/task_pick/<run_id>/` 并以单表目录作为 `-t`，避免脚本随机换表；
- 每个运行实例的日志保留在 `web_data/runs`，网页"运行控制"里可回看；
- 多学号并发建议**错开时间、控制在 2–3 个以内**：同一出口 IP 同时高频发请求时，学校侧可能重置 TLS 连接，而脚本对"结果未知"的写请求不会自动重放（见常见问题）。

## 人脸核验

任务 `runFaceStatus=Y` 时脚本会按服务端下发的 `randomList` 打开人脸窗口：

- 网页端流程：上传照片 → 自动检测生成标注（含 `source_sha256` 绑定与 `bind_apply` 坐标系声明）→ 批量预检（取景质量门 + APK 压缩链）→ 运行时自动带上 `--face-photo` / `--face-detection`；
- 照片按 EXIF 摆正、宽度 > 720 才缩放、超过 150KB 走质量阶梯（80→20），与 App 行为一致；
- 窗口按单调时钟计时，`faceTime + 4s` 为总预算；比对失败立即重试 ≤3 次，等待期复发同一张图；
- 窗口未弹或比对未确认 → **拒绝发送 finish**，脚本停止并保留现场（不静默收尾、不自动补发）。
- 不支持人脸注册/预登记链路；**断点续跑（窗口持久化）未实现**，进程中断后不会恢复未完成窗口。
- 细节与策略偏差见 [`docs/USAGE.md`](docs/USAGE.md) §4。

## 路线预览 / 轨迹合成

网页「路线预览」支持两种方案：

- **打表回放**：直接使用任务表原始轨迹；
- **Deform 轨迹合成**：以底图 JSON 为参考，生成新的轨迹点（可设随机种子、采样间隔），输出可校验的任务表。

命令行等价入口是 `--route-config <json>`（示例见 `examples/routes/`），生成逻辑与网页共用 `yun_route.py`。原理见 [`docs/ROUTE_GENERATION.md`](docs/ROUTE_GENERATION.md)。

---

## 目录结构

```
Yun2Face-Console/
├─ web_app.py              # 网页控制台后端（HTTP API + 运行管理 + 定时调度）
├─ web/                    # 网页前端（index.html / app.js，无构建）
├─ web.py                  # 旧版独立面板（信息配置 / 路线预览 / 执行日志，默认 8080）
├─ main.py                 # 命令行主流程
├─ yun_http.py             # 请求构造 / SM2/SM4 / 响应解码 / 续期钩子
├─ yun_session.py          # token 401 自动重登（只重发一次）与会话统计
├─ yun_face.py             # 人脸窗口状态机、图像链、比对重试
├─ core/                   # 离线预检规则（规则原文副本）与最小导入替身
├─ face_check.py           # 上传前预检独立工具（rules/check/gen/video/show）
├─ task_tools.py           # 任务表校验、派生、里程重算
├─ video_preflight.py      # 视频源逐帧预检
├─ yun_route.py            # 轨迹合成 / 几何模板生成
├─ history.py              # 拉取历史跑步记录
├─ live_probe.py           # 准入探测
├─ tools/                  # 学校目录、登录、漂移、配速、抓包等辅助脚本
├─ models/yunet.onnx       # 网页端人脸自动检测模型
├─ deploy/                 # Ubuntu systemd 服务与安装脚本
├─ docs/                   # USAGE / ARCHITECTURE / ROUTE_GENERATION / 学校目录
├─ tests/                  # 离线 pytest 测试
├─ examples/routes/        # 路线生成示例配置
├─ config.ini              # 示例配置（安徽邮电职业技术学院，schoolId=195）
├─ students_cofig/         # 运行时生成：每学号 config.ini（含 token，不入库）
├─ sutdents_face/          # 运行时生成：人脸照片（不入库）
├─ students_face_json/     # 运行时生成：人脸标注（不入库）
├─ web_data/               # 运行时生成：运行日志 / 定时计划（不入库）
└─ out/                    # 运行时生成：任务表锁定副本 / 缓存（不入库）
```

## 数据与隐私

以下目录属于**个人数据/运行产物，已加入 `.gitignore`，不要提交到公开仓库**：

| 目录 | 内容 |
|---|---|
| `students_cofig/` | 每个学号的 `config.ini`（含 token、可能含账密） |
| `sutdents_face/`、`students_face_json/` | 人脸照片与人脸标注（生物特征数据） |
| `web_data/` | 跑步记录、定时计划、运行日志 |
| `out/` | 任务表副本、缩略图缓存 |

控制台默认只监听 `127.0.0.1`，且所有写操作都限制在照片/标注/配置目录内并做文件名白名单校验。**不要在公网直接暴露端口**；需要远程使用时请通过 SSH 隧道或放在带鉴权的反向代理后面。

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

测试全部是**离线**用例（假传输 / fixture），不需要账号、不访问网络。命名中的"离线兼容"只证明本地实现与反编译证据一致，不代表服务端接受或能通过人脸。

## 部署（Ubuntu 22.04 + systemd）

```bash
cd /opt/yunForNewVersion
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
sudo bash deploy/install_systemd.sh /opt/yunForNewVersion
```

```bash
sudo systemctl status yunfornewversion-web.service
sudo journalctl -u yunfornewversion-web.service -f
```

服务默认监听 `127.0.0.1:8765`（前面可以放 Nginx 反向代理），并同时启动内置的每日定时调度器；网页里的运行日志仍写在 `web_data/runs`。详见 [`deploy/README.md`](deploy/README.md)。

## 常见问题

**1. `传输失败(结果未知，勿自动重发)` / `Max retries exceeded … SSLEOFError` / `RemoteDisconnected`**

写请求在传输层被对端中断或超时。服务端可能已经入账，脚本按设计**不会自动重放**，只会停止并报告 `recordId` 与最后确认位置。先用 `python history.py` 或官方 App 查记录状态，再决定下一步，不要盲目重发。

**2. token 过期 / `code=401`**

默认开启"401 → 自动重登 → 重发一次"（仅安全边界内），网页运行时勾选"续期后把新 token 写回配置"即可持久化。用 `--no-auto-reauth` 可关闭。

**3. 人脸窗口未通过 / 漏窗**

窗口比对未确认、或实际经过范围内存在未完成窗口时，脚本拒绝发送 finish 并停止。请在官方 App 完成核验或重新安排一次任务；不支持自动补拍。

**4. `isStandard` 非 Y 或 `isCheat=Y`**

服务端明确判定不达标/作弊，脚本停止，不再发尾批与 finish。按提示检查配速、步频、里程与踩点后再决定。

**5. 服务端留下"未完成"记录**

被策略停止时服务端可能已创建记录，但不会 finish。可在官方 App 的跑步记录里删除该条。

**6. 定时任务没跑**

- 检查服务器/浏览器时区提示：定时按北京时间解释，`YUN_SCHEDULE_TZ_OFFSET_MIN` 可覆盖；
- 晚于计划时间超过 15 分钟会被跳过（页面会显示"已跳过 N 次过期运行"）；
- 任务处于"停用"状态，或左侧选中的学号与该任务不一致（列表只显示当前学号的任务）。

**7. 高峰时段多个学号同时跑容易失败**

学校服务器在 07:00–07:30 等高峰对并发连接更敏感，可能出现连接重置/读超时；建议错峰（间隔 5 分钟以上）并控制在 2–3 个并发，失败后按"结果未知"处理。

## 更多文档

- [`docs/USAGE.md`](docs/USAGE.md)：配置、人脸、失败分支、异常处置的详细说明
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)：模块边界与协议取证基线
- [`docs/ROUTE_GENERATION.md`](docs/ROUTE_GENERATION.md)：轨迹合成算法
- [`docs/SCHOOL_DIRECTORY_20260913.md`](docs/SCHOOL_DIRECTORY_20260913.md)：学校地址对照表
- [`deploy/README.md`](deploy/README.md)：systemd 部署
- [`proxy.md`](proxy.md)：手机抓包辅助流程（仅用于取证/兜底获取 token）

## 更新日志

- **2026-10-28**
  - `config.ini` 的 `app_edition` 由 `3.6.6` 更新为 `3.6.7`；
  - 「定时运行」改为跟随左侧学号栏：列表只显示当前学号的定时任务，切换学号即切换，新建/修改也直接使用当前学号；
  - 重写 README（本文件），按当前控制台的实际功能整理，替代上游原版说明。

## 来源与致谢

本项目的请求协议、加密封装、人脸取景/质量门等规则源自 [yunForNewVersion](https://github.com/Zirconium233/yunForNewVersion) 项目；本仓库在其基础上增加了本地网页控制台、按学号定时任务、任务表工具与工程化整理，`core/rules.py` 保留上游规则原文副本以保证行为一致。

## License

[MIT](LICENSE) © 2026 YumYu
