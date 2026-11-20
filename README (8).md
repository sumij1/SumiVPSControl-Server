# 服务器故障反馈中心

这是一个可自行部署的开源版本，包含服务端、探针脚本和完整网页客户端。

仓库不预设任何正式服务器、域名、联系人或品牌名。运行后会优先使用网页所在主机作为 API 地址，首次启动时自动生成本机密钥和 SQLite 数据库。

主要功能：

- 多服务器资源、端口、磁盘、GPU、流量与 SSH 登录监控
- 服务器世界地图、在线状态和故障定位
- 1 到 5 级故障分级、活动工单和人工处理备注
- 浏览器 SSH、远程重启/关机入口、VPS 供应商管理链接
- Minecraft Java / GMod 查询和 RCON 控制台
- QQ、Telegram、Discord、微信、WhatsApp 反馈汇总
- 可选 OpenAI 兼容接口，用于故障分级和日志路径推断

## 一键启动

Linux 服务器需要 Python 3.11 或更新版本：

```bash
bash ./start.sh
```

首次运行会自动完成：

1. 创建 `.venv`
2. 安装 Python 依赖
3. 创建 `.env`
4. 生成 `ADMIN_KEY`、`AGENT_KEY` 和 `CONFIG_MASTER_KEY`
5. 初始化 `data/ops_hub.sqlite3`
6. 启动后端并同时托管网页客户端

打开：

```text
http://127.0.0.1:8080/
```

如果从其他电脑访问，请使用服务器实际 IP 或你配置的域名。登录密钥位于 `.env`：

```bash
grep '^ADMIN_KEY=' .env
```

重复启动不需要重新安装，再次运行 `bash ./start.sh` 即可。

## 网页客户端

网页源码和构建结果都在本地：

- 可编辑模板：`frontend/index.template.html`
- 构建后的客户端：`frontend/index.html`
- 独立客户端配置：`frontend/config.js`
- 服务端同源副本：`index.html`、`config.js`

默认由服务端访问 `/` 时直接提供客户端，不需要另外启动静态服务器。

当前工作区还生成了可直接拷走的交付包：

- `release/client.zip`：完整的网页客户端，包含本地 xterm 终端资源
- `release/client/`：未压缩的客户端目录
- `release/server.zip`：完整服务端、探针、脚本和网页源码
- `release/SHA256SUMS.txt`：交付包校验值

如果把网页单独部署到其他静态托管平台，编辑 `frontend/config.js`：

```js
window.APP_CONFIG = Object.freeze({
  apiBase: "https://你的后端地址",
  appName: "你的名称",
  ownerName: "",
  contactText: "",
  contactUrl: "",
});
```

同时在服务端 `.env` 填写允许的网页来源：

```dotenv
CORS_ORIGINS=https://你的网页地址
PUBLIC_BASE_URL=https://你的后端地址
```

`apiBase` 留空表示自动调用同源 API，适合把网页直接交给本项目后端托管。

## 名称和联系方式留空

以下字段默认全部为空：

```dotenv
APP_NAME=
APP_OWNER=
APP_CONTACT_TEXT=
APP_CONTACT_URL=
```

页面在 `APP_NAME` 为空时显示通用标题“故障处理中心”。填写后会替换登录页、顶栏和浏览器标题。项目不包含原部署方的品牌图片、域名、邮箱或联系人。

## 添加被监控服务器

1. 登录网页。
2. 点击“添加服务器”。
3. 填写服务器名称、SSH 地址、端口、检查端口和日志路径。
4. 保存后复制安装命令，在目标服务器上以 root 执行。

生成的安装命令会把当前服务端地址、`AGENT_KEY` 和服务器 ID 一起传给探针。请不要把 `.env`、数据库或安装命令提交到公开仓库。

## 关键配置

首次启动生成的 `.env` 已包含全部配置项。常用字段：

| 字段 | 用途 |
| --- | --- |
| `BIND_HOST` | 监听地址，快速部署默认为 `0.0.0.0` |
| `BIND_PORT` | 监听端口，默认 `8080` |
| `PUBLIC_BASE_URL` | 自己选定的后端公网地址，留空时根据当前请求生成安装命令 |
| `CORS_ORIGINS` | 独立网页来源，多个值用英文逗号分隔 |
| `DATABASE_PATH` | SQLite 数据库路径 |
| `SERVICE_NAME` | 可选 systemd 单元名；不经 systemd 运行时保持为空 |
| `AI_API_KEY` | 可选，OpenAI 兼容服务的密钥 |
| `AI_API_BASE` | 可选，例如 `https://你的AI服务/v1` |
| `AI_MODEL` | 可选，服务端支持的模型名 |

不填写 AI 配置时，故障分拣和工单仍可使用规则模式运行。

## 公网部署建议

- 使用反向代理提供 HTTPS，只开放实际需要的端口。
- `.env` 权限保持 `600`，并定期轮换三个密钥。
- `ADMIN_KEY` 只用于管理端，`AGENT_KEY` 只发给探针。
- 不要把数据库、`.env`、探针安装命令和含真实地址的日志放入开源仓库。
- 远程 SSH、电源控制和 RCON 都是高风险功能，建议只在可信网络中启用。

## 开发与构建

构建网页客户端：

```bash
node scripts/build_frontend.js
```

该命令会生成 `frontend/index.html`、`index.html`、`config.js` 和 `worker.js`。

## 许可

MIT，详见 `LICENSE`。
