# discord-news

一组金融资讯采集与推送脚本：由 GitHub Actions 运行，从多个公开数据源增量拉取快讯、公告和市场指标，去重、过滤、格式化后推送到 Discord 频道。

2026 年 7 月上线，持续运行至今。去重状态随每次运行提交回仓库，提交历史即运行记录。

## 功能概览

| 脚本 | 数据源 | 推送内容 | 运行频率 |
|---|---|---|---|
| `main.py` | 华尔街见闻 · 美股快讯 | 标题 + 正文 | 每 2 分钟 |
| `main_a.py` | 华尔街见闻 · A 股快讯 | 标题 + 正文 | 每 2 分钟 |
| `main_hk.py` | 华尔街见闻 · 港股快讯 | 标题 + 正文 | 每 2 分钟 |
| `trump_tracker.py` | CNN 维护的 Truth Social 公开归档 | 中文翻译 + 英文原帖卡片图 | 每 2 分钟 |
| `feargreed_tracker.py` | CNN 恐惧贪婪指数、alternative.me 加密市场恐惧贪婪指数 | 指数仪表图 + 历史对比 + 较上次变化 | 定时 |
| `earnings_calendar.py` | Finnhub 财报日历与公司概况 API | 下周一至周五的财报日历表格图 | 每周五 |
| `backfill.py` | 华尔街见闻（三个频道） | 补发指定时间窗口内漏发的快讯 | 手动触发 |
| `x_tracker.py` | X / Twitter 指定账号 | 推文推送 | **调试中，未上线** |

## 项目结构

```text
discord-news/
├── .github/workflows/
│   ├── news.yml / news-a.yml / news-hk.yml   # 三个快讯频道
│   ├── news-trump.yml                        # Truth Social 追踪
│   ├── feargreed.yml                         # 恐惧贪婪指数
│   ├── news-earnings.yml                     # 每周财报日历
│   ├── backfill.yml                          # 手动补发
│   └── news-x.yml                            # X 追踪（调试中）
├── main.py / main_a.py / main_hk.py          # 华尔街见闻快讯
├── trump_tracker.py                          # Truth Social 追踪
├── feargreed_tracker.py                      # 恐惧贪婪指数
├── earnings_calendar.py                      # 财报日历
├── backfill.py                               # 补发工具
├── x_tracker.py                              # X 追踪（调试中）
├── inspect_wallstreetcn_tags.py              # 调试用：检查快讯数据字段
├── seen*.json                                # 各脚本的去重状态
├── assets/                                   # 卡片图素材
└── requirements.txt
```

## 处理流程

各脚本遵循同一套流程：

1. **增量拉取**：按游标分页请求数据源，遇到已处理过的条目或超出时间窗口的条目即停止翻页，不做全量扫描。
2. **去重**：用条目 ID 与状态文件比对；Truth Social 归档会重复返回同一帖子，因此额外用正文哈希做第二层去重。
3. **时间窗口过滤**：跳过发布时间过旧或时间戳缺失的条目（快讯 15 分钟，Truth Social 12 小时），防止历史内容被误推。
4. **格式化**：翻译（DeepL）、生成图片（Pillow / Plotly / Matplotlib），组装 Discord Embed。
5. **推送**：通过 Discord Webhook 发送。**每条推送成功后才把该条写入状态**，中途失败时已发出的不会重复，未发出的下轮补上。
6. **状态持久化**：运行结束后把状态文件提交回仓库，供下一次运行读取。

## 可靠性设计

| 问题 | 处理方式 |
|---|---|
| 数据源接口临时失败 | 最多重试 3 次，线性退避；仍失败则跳过本轮，由下一轮补上，不让 workflow 报错 |
| Discord 限流（HTTP 429） | 按返回的 `retry_after` 等待后重试，最多 5 次，避免持续限流时无限重试 |
| 中途异常 | 状态写入放在 `finally` 中，已发送的记录一定落盘 |
| 状态文件损坏 | 读取失败时按空状态处理，走首次运行逻辑，不中断 |
| 首次运行 | 快讯只补发最近 10 条，其余标记为已读；Truth Social 只建立去重基线，不补发历史 |
| 状态文件增长 | 按保留期自动裁剪（快讯 12 小时，Truth Social 30 天） |
| 多次运行并发写状态 | 快讯和 Truth Social 的 workflow 设置 `concurrency`，同一脚本串行执行；提交状态前 `git pull --rebase`，推送冲突时随机等待后重试 |
| 断档后漏发 | `backfill.py` 按时间窗口补发，跳过已记录的 ID，支持 `DRY_RUN` 试运行 |

## 设计取舍

### 触发方式：外部定时服务调用 `workflow_dispatch`

快讯要求约 2 分钟一次的稳定轮询。GitHub Actions 自带的 `schedule` 在负载高时会延迟甚至跳过，达不到这个频率，所以用外部定时服务（cron-job.org）调用 GitHub REST API 触发 workflow：

```text
cron-job.org 每 N 分钟发起 POST
  → POST /repos/{owner}/discord-news/actions/workflows/{workflow}.yml/dispatches   body: {"ref": "main"}
  → workflow 运行脚本 → 推送 Discord → 提交状态文件
```

另一个好处是调整频率、暂停或恢复任务不需要改代码。目前只有 `news-trump.yml` 额外保留了 `schedule` 作为兜底，其余 workflow 仅由 `workflow_dispatch` 触发，也可以在 GitHub 页面上手动运行调试。

配置要点：

1. 生成一个仅对本仓库有 Actions 写权限的 fine-grained Personal Access Token。
2. 在定时服务里创建 POST 任务，请求头：
   ```text
   Authorization: Bearer YOUR_GITHUB_TOKEN
   Accept: application/vnd.github+json
   ```
3. 请求体固定为 `{"ref": "main"}`。

### 状态存在仓库里

- **好处**：零成本、不需要额外的数据库或服务；状态变化有完整的历史记录，出问题时可以直接回看。
- **代价**：每次运行都产生一条提交，仓库提交历史被刷屏、体积持续增长；多个 workflow 同时写入需要处理冲突（见上表）。
- **后续**：规模再扩大时，会把状态迁到 SQLite 或键值存储，只把代码留在仓库里。

## 状态文件格式

记录每个 ID（以及正文哈希）首次处理的时间，超过保留期自动删除：

```json
{
  "seen":   { "1234567890": 1784326800.12 },
  "hashes": { "a1b2c3...": 1784326800.12 }
}
```

`hashes` 字段只有 `trump_tracker.py` 使用。状态文件可以安全地重置为 `{"seen": {}}`：脚本会走首次运行逻辑，不会批量补发历史内容。

## 各脚本说明

### 华尔街见闻快讯（`main.py` / `main_a.py` / `main_hk.py`）

- 对接美股、A 股、港股三个快讯频道，接口地址为 `api-one-wscn.awtmt.com`（华尔街见闻网页端当前使用的接口域名）。
- 每轮最多翻 10 页、推送 100 条。

### Truth Social 追踪（`trump_tracker.py`）

- 数据源为 CNN 维护的 Truth Social 公开归档 JSON。
- ID + 正文哈希双重去重。
- 英文原帖渲染为卡片图，DeepL 中文翻译放在 Embed 描述中，标题链接回原帖。

### 恐惧贪婪指数（`feargreed_tracker.py`）

- 同时拉取 CNN 股市恐惧贪婪指数和 alternative.me 加密市场恐惧贪婪指数。
- 用 Matplotlib 生成仪表图，附历史值对比和较上次的变化说明。

### 财报日历（`earnings_calendar.py`）

- 每周五运行，拉取**下周**一至周五的财报日历，留出一周准备时间。
- 通过 Finnhub 公司概况接口按市值过滤，只保留 100 亿美元以上的公司。
- 按盘前（BMO）、盘后（AMC）分组，组内按市值排序，用 Plotly 生成表格图。

### 补发工具（`backfill.py`）

- 独立脚本，不依赖、也不修改三个主脚本。
- 参数：`HOURS`（回溯小时数）、`CHANNELS`（`us,a,hk` 任选）、`DRY_RUN`（只列出待补发条目，不发送）。
- 可在本地运行，也可以通过 `backfill.yml` 在 GitHub 页面手动触发。

### X 追踪（`x_tracker.py`）— 调试中

尚未上线，目前没有稳定可用的数据获取方式。

## 本地运行

```bash
pip install -r requirements.txt

export DISCORD_WEBHOOK_URL="your_webhook_url"
python main.py

# 补发试运行：只列出最近 6 小时美股、港股的漏发条目，不发送
HOURS=6 CHANNELS=us,hk DRY_RUN=true python backfill.py
```

`trump_tracker.py` 和 `feargreed_tracker.py` 内置 `TEST_MODE` 开关，打开后发送模拟消息，用于验证 Webhook、翻译和出图链路，不影响真实状态。

## 环境变量（GitHub Secrets）

| 名称 | 用途 |
|---|---|
| `DISCORD_WEBHOOK_URL` | 美股快讯频道 |
| `DISCORD_WEBHOOK_URL_A` | A 股快讯频道 |
| `DISCORD_WEBHOOK_URL_HK` | 港股快讯频道 |
| `DISCORD_WEBHOOK_URL_TRUMP` | Truth Social 追踪频道 |
| `DISCORD_WEBHOOK_URL_FEARGREED` | 恐惧贪婪指数频道 |
| `DISCORD_WEBHOOK_URL_EARNINGS` | 财报日历频道 |
| `DEEPL_API_KEY` | DeepL 翻译 |
| `FINNHUB_API_KEY` | Finnhub 财报与公司数据 |

## 已知局限与后续计划

- `main.py`、`main_a.py`、`main_hk.py` 只有频道配置不同，计划合并为一个按配置运行的脚本（`backfill.py` 已采用这种写法）。
- 目前没有单元测试，计划先为去重、时间窗口过滤和状态裁剪补测试。
- 状态存储迁出仓库（见「设计取舍」）。

## 说明

本项目用于个人学习和自动化流程实践。所有数据的版权归原数据源所有。
