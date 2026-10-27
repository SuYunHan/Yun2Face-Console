# 云运动自动跑步脚本（develop 使用手册）
# 本仓库由[Zirconium233/yunForNewVersion](https://github.com/Zirconium233/yunForNewVersion)修改而来，更适合安邮宝宝体质
**文档导航**：本文是主使用文档；协议构造细节见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，
逐条 CLI 与失败分支表见 [docs/USAGE.md](docs/USAGE.md)，合成配置完整键表见
[docs/ROUTE_GENERATION.md](docs/ROUTE_GENERATION.md)。技术细节与历史档案在本文第 4 部分。

## 1.速览

### 统一网页控制台

项目根目录新增 `web_app.py`，把两套工具的常用功能合并到一个本地控制台：

- `人脸素材`：照片上传、标注编辑、自动检测、预检和批处理；
- `跑步任务`：任务表浏览、曲线查看、校验、复制/改名/派生和导入；
- `运行控制`：选择账号项目、任务目录、人脸源，启动离线演练或真实运行并查看实时日志；
- `路线预览`：打表回放与 Deform 轨迹合成，离线画布对照参考路线和当前路线；
- `项目配置`：安全掩码读取和保存当前 `config.ini`。
- 云端账号资料使用项目根目录下的 `students_cofig/<学号>/config.ini`、`sutdents_face/<学号>/照片` 和 `students_face_json/<学号>/照片.json`；代码与任务表统一使用本项目根目录。
- `定时运行`：学号直接取**左侧学号栏的选中项**（点左侧学号即切换），列表只显示该学号的定时任务；按该学号的任务表、离线/真实模式以及人脸照片和 JSON 按天定时运行，计划与运行日志保存在 `web_data/runs`。
  - 选中的学号若没有账号项目（缺 `config.ini`）会禁用保存并提示原因；其它学号的定时任务不在列表里，但会在列表下方提示条数（切到对应学号即可查看/修改）。
  - 定时时间固定按**北京时间（UTC+8）**解释，不跟随服务器时区（容器/云服务器默认 UTC 会把 07:10 变成 15:10）；需要别的时区用环境变量 `YUN_SCHEDULE_TZ_OFFSET_MIN`（分钟）覆盖。
  - 服务停机或任务停用期间错过的运行**不会补跑**：晚于计划时间超过 15 分钟直接顺延到下一次。
- Ubuntu daemon：`deploy/yunfornewversion-web.service` 与 `deploy/install_systemd.sh`，可用 systemd 开机启动并自动重启网页服务。

启动（默认仅监听本机 `127.0.0.1:8765`）：

```bash
python3 web_app.py --no-browser
```

首次使用会创建 `students_cofig`、`sutdents_face`、`students_face_json` 和 `web_data/runs`，不会改动已有任务表。真实运行前建议先用“离线演练”确认命令和轨迹。

develop：云运动（3.6.6）自动跑步脚本，解决 master 两个老大难问题：

1. **人脸验证**——master 停在 3.6.4/3.6.6 核验上无法开工；develop 重构请求链并实现人脸窗口状态机，已基本解决（少量实测失败待复核）。
2. **轨迹重复**——master 的 `-d` 漂移只是整条轨迹约 0.1 毫米刚性平移（几何不变，易被近重复检测命中）；develop 用 `--route-config` 把已有轨迹形变成新轨迹（deform 引擎），几何形状与节奏都变，远好于老漂移。

其余改动：每请求新鲜签名、splitPoint 载荷保真、结束前状态检查、dry-run 离线演练。

```
├── main.py                 入口 + 会话编排 + 交互式菜单
├── yun_http.py             协议层（SM2/SM4 信封、签名、解码）
├── yun_face.py             人脸子系统（窗口调度、压缩、状态机）
├── yun_route.py            轨迹合成引擎（deform 形变 + 任务表构造）
├── web.py                  本地网页面板（配置 / 预览 / 执行日志，纯标准库）
├── live_probe.py           跑前准入探测（只读，不建记录）
├── history.py              历史记录查看器
├── tools/                  登录 / 学校地址查询 / 老漂移(弃用) / 抓包 / 路由生成 CLI
├── examples/routes/        合成配置样例（deform_*.json + 围栏 + dry-run 夹具）
├── tasks_fch|txl|xc/       三个校区的打表任务表
├── tests/                  离线测试（219 项，禁网可跑）
├── docs/                   USAGE / ARCHITECTURE / ROUTE_GENERATION / 学校目录
├── config.ini              唯一配置文件
└── dry_run_home.json       dry-run 离线夹具
```
## 2.更新日志
2026/10/28 将config.ini版本号修改为3.6.7,同时将定时运行中的任务查看改为点击学号查看对应任务，要不然太多任务看的眼花缭乱
