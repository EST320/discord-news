# discord-news

自动化新闻与事件追踪机器人集合，通过 GitHub Actions 定时抓取多个数据源（华尔街见闻快讯、Truth Social、财报日历、X/Twitter），并将格式化内容推送到指定的 Discord 频道。

## 功能概览

| 脚本 | 数据源 | 推送内容 | 触发频率 |
|---|---|---|---|
| `main.py` | 华尔街见闻美股频道 | 快讯标题 + 正文 | 每 2 分钟 |
| `main_a.py` | 华尔街见闻 A 股频道 | 快讯标题 + 正文 | 每 2 分钟 |
| `main_hk.py` | 华尔街见闻港股频道 | 快讯标题 + 正文 | 每 2 分钟 |
| `trump_tracker.py` | CNN Truth Social 归档 | 中文翻译 + 英文原帖卡片图 | 每 2 分钟 |
| `x_tracker.py` | X / Twitter 账号 | 推文中文翻译 + 原文卡片 | 定时轮询 |
| `earnings_calendar.py` | Finnhub 财报日历 API | 下周一至周五财报日历图表 | 每周五 |

## 项目结构

```text
discord-news/
├── .github/
│   └── workflows/                    # GitHub Actions 定时任务配置
├── main.py                           # 美股快讯推送
├── main_a.py                         # A股快讯推送
├── main_hk.py                        # 港股快讯推送
├── trump_tracker.py                  # Trump Truth Social 追踪器
├── x_tracker.py                      # X/Twitter 账号追踪器（调试中）
├── earnings_calendar.py              # 每周财报日历生成与推送
├── inspect_wallstreetcn_tags.py      # 调试用：检查华尔街见闻数据标签
├── seen.json                         # 美股去重状态
├── seen_a.json                       # A股去重状态
├── seen_hk.json                      # 港股去重状态
├── seen_trump.json                   # Trump 追踪去重状态
├── seen_x.json                       # X 追踪去重状态
├── requirements.txt                  # Python 依赖
└── README.md
```

## 运行原理

所有脚本遵循相同的核心模式：

1. 抓取：从各自数据源（REST API 或归档 JSON）拉取最新条目。
2. 去重：将条目 ID（部分脚本额外用正文哈希）与本地状态文件（seen*.json）比对，过滤已发送内容。
3. 时间窗口过滤：跳过发布时间过旧或时间异常的条目，避免历史内容被误发。
4. 格式化：翻译（DeepL）、生成图片卡片（Pillow / Plotly）或直接组装 Discord Embed。
5. 推送：通过 Discord Webhook 发送消息，仅在推送成功后才写回去重状态。
6. 状态持久化：GitHub Actions 在每次运行后将更新后的 seen*.json 提交回仓库，供下次运行读取。

## 环境依赖

pip install -r requirements.txt

主要依赖：

- requests — HTTP 请求
- deepl — 中文翻译（Trump / X 追踪器）
- Pillow — 生成原帖卡片图
- plotly + kaleido — 生成财报日历表格图

## 环境变量 / Secrets

在 GitHub 仓库的 Settings → Secrets and variables → Actions 中配置：

| Secret 名称 | 用途 |
|---|---|
| `DISCORD_WEBHOOK_URL` | 美股快讯频道 Webhook |
| `DISCORD_WEBHOOK_URL_A` | A股快讯频道 Webhook |
| `DISCORD_WEBHOOK_URL_HK` | 港股快讯频道 Webhook |
| `DISCORD_WEBHOOK_URL_TRUMP` | Trump 追踪频道 Webhook |
| `DISCORD_WEBHOOK_URL_X` | X 追踪频道 Webhook |
| `DISCORD_WEBHOOK_URL_EARNINGS` | 财报日历频道 Webhook |
| `DEEPL_API_KEY` | DeepL 翻译 API Key |
| `FINNHUB_API_KEY` | Finnhub 财报数据 API Key |

## 各脚本说明

### 华尔街见闻快讯（main.py / main_a.py / main_hk.py）

- 分别对接美股、A股、港股三个直播频道 API。
- 按 RETENTION_SECONDS 定期裁剪去重状态，避免状态文件无限增长。
- 首次运行会发送最近若干条作为初始化，后续仅推送新增内容。

### Trump Truth Social 追踪器（trump_tracker.py）

- 数据源为 CNN 维护的 Truth Social 公开归档（不直接访问 truthsocial.com，规避 Cloudflare 验证）。
- 使用 ID + 正文哈希双重去重，防止归档重复返回同一帖子。
- 用 Pillow 生成仿官方样式的英文原帖卡片图，中文翻译显示在 Discord Embed 描述中。
- Embed 标题为可点击链接，跳转回 Truth Social 原帖地址。
- 首次部署默认只建立去重基线，不补发历史内容（SEND_ON_FIRST_RUN = False）。

### X / Twitter 追踪器（x_tracker.py）— 调试中

- 通过 RSS 桥接方式获取指定账号最新推文，规避官方 API 高昂的读取成本。
- 复用与 Trump 追踪器相同的去重与卡片生成架构。

### 财报日历（earnings_calendar.py）

- 每周五运行，抓取下周一至周五的财报日历（而非当前周），确保用户有完整一周的提前准备时间。
- 通过 Finnhub 公司概况接口按市值过滤（默认 50 亿美元以上），避免推送过多小盘股信息。
- 使用 Plotly 生成表格图，按 BMO（开盘前）/ AMC（收盘后）分类排序。

## 本地测试

各脚本均支持在本地直接运行，需先设置好对应环境变量：

export DISCORD_WEBHOOK_URL_TRUMP="your_webhook_url"
export DEEPL_API_KEY="your_deepl_key"
python trump_tracker.py

多数脚本内置 TEST_MODE 开关，可发送模拟消息验证 Webhook、翻译、图片生成链路是否正常，而不影响真实去重状态：

TEST_MODE = True

## 触发方式：为什么不用 GitHub 原生 schedule

本项目的 workflow 文件里保留了 schedule 字段作为兜底，但实际生产环境的定时触发并不依赖 GitHub Actions 自带的 cron，而是使用第三方定时任务服务（如 cron-job.org）通过 HTTP 请求主动调用 workflow_dispatch API 来触发运行。

### 为什么不用 GitHub 自带的 schedule

- 延迟不可控：GitHub 官方文档明确说明，schedule 触发的 workflow 在负载高峰期可能延迟数分钟到数十分钟执行，甚至被跳过，这对于要求"每 2 分钟"这种高频轮询的快讯推送场景是不可接受的。
- 免费额度限制：schedule 事件在私有仓库上会计入 Actions 分钟数配额，高频轮询（每 2 分钟一次，一天 720 次）容易迅速耗尽额度。
- 无法灵活暂停/调整：GitHub 的 cron 表达式修改需要提交代码变更，而第三方定时服务可以在网页后台直接调整触发频率、暂停或恢复任务，不需要改动仓库文件。

### 实际触发链路

第三方定时服务（如 cron-job.org）
每隔 N 分钟发起一次 HTTP POST 请求
GitHub REST API
POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches
触发对应 workflow 的 workflow_dispatch 事件
Actions 运行器执行 Python 脚本 → 推送 Discord → 提交状态文件

### 配置方式

1. 在 GitHub 生成一个具备 repo 权限的 Personal Access Token (PAT)。
2. 在第三方定时服务（如 cron-job.org）创建一个定时任务，设置请求方式为 POST，目标地址为：

https://api.github.com/repos/{owner}/discord-news/actions/workflows/{workflow文件名}.yml/dispatches

3. 请求头部需包含：

Authorization: token YOUR_GITHUB_PAT
Accept: application/vnd.github+json

4. 请求体固定为：

{"ref": "main"}

5. 根据脚本的实际需求设置轮询间隔（快讯类建议 2 分钟一次，Trump/X 追踪器可视情况调整，财报日历仅需每周五一次）。

### workflow 文件中的 schedule 字段用途

.github/workflows/ 中保留的 schedule 配置仅作为备用兜底机制——即便第三方定时服务临时失效或未配置，workflow 仍能依赖 GitHub 自带调度继续运行（尽管存在延迟风险）。所有 workflow 同时保留 workflow_dispatch，这正是第三方服务发起触发所依赖的入口，也支持在 GitHub 网页上手动点击运行以便调试。

## 状态文件维护

seen*.json 采用"ID + 时间戳"结构，并按 RETENTION_SECONDS 自动裁剪过期记录：

{
  "seen": {
    "1234567890": 1784326800.12
  },
  "hashes": {
    "a1b2c3...": 1784326800.12
  }
}

如状态文件异常膨胀（如意外积累数万条记录），可安全地重置为空结构以重新初始化，不会导致历史内容被批量补发（前提是对应脚本的 SEND_ON_FIRST_RUN 设为 False）：

{
  "seen": {},
  "hashes": {}
}

## 许可

本项目仅供个人学习与自动化流程演示使用。所抓取的数据版权归原数据源所有方所有。
