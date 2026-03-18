# 行业每日新闻推送系统 v6

这是一个可直接运行的 Python 项目，用于：

1. 从公开 RSS、网页列表页和新闻 API 抓取 各行业最新相关新闻 (示例为NGS / genomics / sequencing，支持自定义更改） ；
2. 调用大模型接口生成中文摘要；
3. 将每日摘要整理成 HTML 邮件并发送给指定收件人；
4. 监听反馈邮箱，解析用户自然语言反馈，更新多用户画像、抓取源、关键词与摘要风格；
5. 基于历史反馈对新闻进行个性化排序，并实现跨天不重复推送；
6. 提供一个面向非技术用户的 Web 可视化仪表盘，用于配置、运行和监控系统。

当前默认模型配置为：
- 服务端点：火山方舟 OpenAI 兼容接口
- 默认 Base URL：`https://ark.cn-beijing.volces.com/api/v3`
- 默认模型名：`deepseek-v3-2-251201`

---

## 1. 项目结构

```text
news-summary-system/
├── .env.example
├── requirements.txt
├── run.py
├── run_dashboard.py
├── start.sh
├── README.md
├── src/
│   ├── __init__.py
│   ├── app.py                  # Flask Dashboard
│   ├── config.py
│   ├── env_manager.py          # .env 读写
│   ├── feedback.py             # 反馈邮箱监听、规则+LLM 解析、自动回执
│   ├── fetcher.py              # RSS / 网页 / API 抓取、去重、个性化排序
│   ├── mailer.py               # HTML 邮件发送、反馈解释回执
│   ├── pipeline_service.py     # 主流程与局部运行控制
│   ├── profile_manager.py      # 多用户画像数据库
│   ├── runtime_state.py        # 仪表盘运行状态存储
│   ├── summarizer.py           # 大模型摘要与摘要风格定制
│   ├── templates/
│   │   ├── base.html
│   │   └── dashboard.html
│   └── static/
│       └── style.css
└── tests/
    └── test_config.py
```

---

## 2. 核心能力

### 2.1 新闻抓取
- RSS 抓取
- 网页列表页抓取 + 详情页展开
- 外部新闻 API 抓取
- 支持代理、随机请求头、随机停顿、指数退避、按域名限速、可选 `robots.txt` 检查

### 2.2 跨天去重
- 已推送新闻写入 `news_state.db`
- 同一用户不会收到重复条目
- 支持历史记录自动清理

### 2.3 多用户个性化
- 每个收件人拥有独立画像
- 支持偏好关键词、负向关键词、偏好来源、自定义抓取源、摘要风格、摘要长度、关注焦点
- 同一轮任务中不同用户会收到不同版本的日报

### 2.4 反馈闭环
- IMAP 拉取反馈邮箱
- 规则解析器 + LLM 解析器双模式
- 新增来源、关键词调整、摘要风格调整、满意度反馈
- 支持对具体新闻条目显式点赞 / 点踩
- 支持新闻标题模糊匹配点赞 / 点踩
- 支持反馈解释邮件自动回执

### 2.5 可视化 Dashboard
Dashboard 中提供：
- 概览卡片
- 配置管理
- 抓取源管理
- 用户画像查看 / 编辑
- 反馈日志查看
- 今日新闻预览
- 手动运行控制

---

## 3. 安装步骤

### 3.1 创建虚拟环境

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3.2 安装依赖

```bash
pip install -r requirements.txt
```

### 3.3 复制配置模板

```bash
cp .env.example .env
```

Windows：

```powershell
copy .env.example .env
```

---

## 4. 最低可运行配置

至少需要填写：

```env
LLM_API_KEY=你的模型密钥
SMTP_HOST=你的SMTP服务器
SMTP_PORT=465
SMTP_USE_SSL=1
SMTP_USERNAME=发件邮箱账号
SMTP_PASSWORD=发件邮箱密码或授权码
SMTP_FROM_EMAIL=发件邮箱
SMTP_TO=收件人1@example.com,收件人2@example.com
```

若需要启用反馈监听，再补充：

```env
FEEDBACK_ENABLED=1
FEEDBACK_IMAP_HOST=imap.example.com
FEEDBACK_IMAP_PORT=993
FEEDBACK_IMAP_USE_SSL=1
FEEDBACK_EMAIL=feedback@example.com
FEEDBACK_PASSWORD=反馈邮箱密码或授权码
```

---

## 5. 运行方式

### 5.1 运行主流程

```bash
python run.py
```

或：

```bash
python -m src.main
```

### 5.2 启动 Dashboard

```bash
python run_dashboard.py
```

默认访问：

```text
http://127.0.0.1:8501
```

---

## 6. Dashboard 使用说明

### 6.1 概览卡片
显示：
- 今日新闻数量
- 待处理反馈数
- 上次运行时间
- 系统状态

### 6.2 配置管理
按分组展示：
- LLM 设置
- 邮件发送设置
- 反馈邮箱设置
- 抓取源设置
- 个性化权重

点击保存后会写回 `.env`。
密码与 API Key 字段采用掩码占位显示，留空则保持原值。

### 6.3 抓取源管理
支持：
- 新增 RSS / 网页 / API 源
- 删除源
- 测试抓取（当前为模拟测试输出）

### 6.4 用户画像查看 / 编辑
支持查看和编辑：
- 偏好关键词
- 负向关键词
- 偏好来源
- 自定义源
- 摘要风格
- 摘要长度
- 关注焦点

键值输入格式可写成：

```text
single-cell sequencing:2.0
spatial transcriptomics:1.5
```

### 6.5 反馈日志
展示最近反馈记录，包括：
- 时间
- 用户
- 原始反馈内容
- 解析出的结构化动作

支持按日期筛选。

### 6.6 今日新闻预览
显示：
- 标题
- 来源
- 发布时间
- 摘要
- 相关性得分
- 去重 / 个性化排序等标签

支持手动标记：
- 相关
- 不相关

这些操作会写入多用户画像数据库，并影响后续排序。

### 6.7 手动运行控制
支持：
- 运行完整流程
- 仅抓取
- 仅发送
- 仅检查反馈

---

## 7. 反馈与个性化说明

### 7.1 支持的反馈类型

#### 新增来源

```text
请增加网站 https://www.nature.com/subjects/sequencing 的抓取
请增加来源：https://www.genomeweb.com/rss.xml
```

#### 关注领域 / 关键词

```text
更关注单细胞测序、空间转录组和临床测序
关键词：single-cell sequencing, spatial transcriptomics
```

#### 摘要长度与风格

```text
摘要太长，请精简到 80 字以内
摘要太短，请详细一点
```

#### 针对具体新闻条目的反馈

```text
点赞 新闻ID: abc123def456
点踩 https://example.com/article
点赞标题：A major breakthrough in single-cell sequencing chemistry
```

### 7.2 标题模糊匹配点赞 / 点踩
当用户未提供新闻 ID 或 URL，而是直接给标题时，系统会：
1. 在最近推送给该用户的新闻中检索候选条目；
2. 用标题相似度进行模糊匹配；
3. 若相似度高于阈值，则记录为该条新闻的显式点赞 / 点踩。

阈值配置：

```env
FUZZY_TITLE_MATCH_THRESHOLD=0.62
```

### 7.3 反馈解释邮件自动回执
反馈被解析并应用后，系统会向反馈用户发送一封简短回执邮件，说明：
- 已记录的动作
- 使用的解析方式（rule / llm+rule）
- 后续将如何影响推送

开关：

```env
FEEDBACK_AUTO_REPLY=1
```

---

## 8. 多用户画像数据库

数据库位置：

```text
./data/profiles.db
```

主要表：
- `user_profiles`
- `feedback_events`
- `article_feedback`
- `processed_feedback_messages`

每个用户独立维护：
- `preferred_keywords`
- `negative_keywords`
- `preferred_sources`
- `custom_rss_feeds`
- `custom_web_pages`
- `custom_api_endpoints`
- `summary_style`
- `summary_max_chars`
- `summary_focus`
- `explicit_article_feedback`

---

## 9. 定时任务部署

### 9.1 Linux / macOS cron

每天 08:00 运行：

```cron
0 8 * * * cd /path/to/news-summary-system && /path/to/python run.py >> logs/cron.log 2>&1
```

### 9.2 Windows 任务计划程序
- 操作：启动程序
- 程序：`python`
- 参数：`run.py`
- 起始于：项目目录

### 9.3 Dashboard 常驻
本地常驻查看可用：

```bash
python run_dashboard.py
```

---

## 10. 已覆盖的特色功能

当前项目已覆盖：
- 跨天去重
- 多用户画像数据库
- 规则 / LLM 双反馈解析
- 显式点赞 / 点踩
- 标题模糊匹配点赞 / 点踩
- 反馈解释邮件自动回执
- 个性化排序
- 摘要风格定制
- Dashboard 可视化监控

---

## 11. 注意事项

1. Dashboard 中“测试抓取”是模拟输出，用于非技术用户快速确认配置流程。
2. 主流程运行是真实执行，会调用抓取、摘要和邮件发送。
3. 若在公司网络或特殊网络环境下使用，请配置 `HTTP_PROXY` / `HTTPS_PROXY`。
4. 若某些站点风控较严，请适当调大 `MIN_REQUEST_INTERVAL_SECONDS` 和 `MAX_REQUEST_INTERVAL_SECONDS`。

