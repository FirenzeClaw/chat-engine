# Chat Engine — 独立 QQ 智能机器人

> 零 CLI 依赖，纯 HTTP 调用大模型 API  
> 集 QQ 协议 + LLM 引擎 + 多脑协调 + 记忆系统于一体

[![Python](https://img.shields.io/badge/Python-3.12-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Phase](https://img.shields.io/badge/Phase%201-Implemented-success.svg)](specs/001-cognitive-architecture/)
[![SQLite](https://img.shields.io/badge/SQLite-FTS5-orange.svg)](https://www.sqlite.org/fts5.html)

## 目录

- [架构](#架构)
- [特性](#特性)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [配置参考](#配置参考)
- [API](#api)
- [记忆系统](#记忆系统)
- [项目结构](#项目结构)
- [与 qq-bot 的关系](#与-qq-bot-的关系)

## 架构

```
QQ 用户发消息
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  main.py                    单进程统一入口            │
│                                                      │
│  ┌──────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ qq_protocol  │  orchestrator │  HTTP Server    │  │
│  │ QQ 长连接    │  消息→AI 协调  │  前端 Web UI    │  │
│  └──────┬───────┘  └──────┬───────┘  └──────┬────────┘  │
│         │                 │                  │          │
│         │    ┌────────────▼──────────┐      │          │
│         │    │  engine.py  LLM 引擎   │      │          │
│         │    │  brain.py   多脑评估   │      │          │
│         │    │  session.py 会话管理   │      │          │
│         │    └────────────┬──────────┘      │          │
│         │                 │                  │          │
│         │    ┌────────────▼──────────┐      │          │
│         │    │  memory_store SQLite   │      │          │
│         │    │  social.py  社交采集    │      │          │
│         │    │  botuser.py 用户数据    │      │          │
│         │    └───────────────────────┘      │          │
└─────────────────────────────────────────────────────┘
         │                          ▲
         ▼                          │
    QQ API                    DeepSeek / Ollama / ...
```

## 特性

- **零 CLI 依赖** — 直调 OpenAI 兼容 API，不经过任何外部子进程
- **全 LLM 兼容** — DeepSeek / OpenAI / Ollama / vLLM / 硅基流动，改 `.env` 即切换
- **System Prompt 完全可控** — 无预设 coding prompt，性格自定义
- **多脑协调** — 辅脑快速回复 + 双主脑并行评估 + 融合决策 + 追答生成
- **语义记忆检索** — 关键词提取 + FTS5 粗筛 + LLM 精排 top-5，按话题注入相关记忆（FR-1）
- **双层记忆模型** — gist 模糊层（慢衰减）+ detail 精确层（艾宾浩斯衰减 + 自动模糊化）
- **记忆纠错链** — corrected/superseded_by 版本链，旧记忆不删除，纠错 boost salience+3（FR-3）
- **记忆关联图** — 规则同日建边 + LLM 每日语义关联 + 检索扩散激活（FR-4）
- **实体分类检索** — entity_type/topic_tags/about_person 标记 + 多跳图谱遍历（FR-5）
- **深刻记忆集群** — 高频访问触发 → LLM 验证 → 共享极慢衰减曲线
- **跨场景记忆** — 私聊/群聊独立标记，场景权重排序（FR-9）
- **SQLite 记忆** — 三层命名空间，FTS5 全文搜索，惰性索引注入，艾宾浩斯衰减
- **QQ 社交采集** — 自动获取昵称/群名，24h/1h 缓存
- **会话持久化** — JSON 文件自动保存，重启不丢失
- **Web UI** — 实时消息监控 + 手动回复

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| LLM SDK | openai >= 1.0 (AsyncOpenAI) |
| 数据库 | SQLite + FTS5 (aiosqlite) |
| HTTP | aiohttp |
| QQ 协议 | WebSocket + REST API |
| 中文分词 | jieba (可选增强，回退规则分词) |
| 部署 | 单进程 localhost:18090 |

## 快速开始

### 1. 获取凭证

- **QQ Bot**: 登录 [QQ 开放平台](https://q.qq.com) → 创建机器人 → 获取 AppID + Token
- **LLM API**: [DeepSeek](https://platform.deepseek.com) 或其他兼容 API 的 Key

### 2. 配置

```bash
cd chat-engine
cp .env.example .env
# 编辑 .env 填入以下必填项：
#   QQ_BOT_APPID=你的AppID
#   QQ_BOT_SECRET=你的AppSecret
#   LLM_API_KEY=sk-你的Key
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 启动

```bash
# 启动
./manage.sh start

# 查看状态
./manage.sh status

# 查看日志
./manage.sh logs

# 停止
./manage.sh stop
```

## 配置参考

```bash
# === QQ Bot（必填）===
QQ_BOT_APPID=your_app_id
QQ_BOT_SECRET=your_app_secret

# === LLM API（必填）===
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.deepseek.com
LLM_FAST_MODEL=deepseek-chat      # 辅脑：快速便宜
LLM_STRONG_MODEL=deepseek-chat    # 主脑：评估+追答

# === 可选 ===
HTTP_PORT=18090                    # Web UI 端口
FOLLOW_UP_ENABLED=true            # 追答开关
FOLLOW_UP_MAX_PER_HOUR=5          # 追答频率限制
SESSION_TTL=3600                  # 会话过期时间（秒）
DEFAULT_SYSTEM_PROMPT=...         # 默认性格 prompt

# === Phase 1: 记忆引擎 ===
DECAY_GIST_DAYS=90                # 模糊层过期天数
DECAY_DETAIL_DAYS=30              # 精确层半衰天数
AUTO_MIGRATE_DAYS=60              # 自动模糊化天数
MAX_RETRIEVAL_CANDIDATES=20       # 检索候选上限
MAX_RETRIEVAL_RESULTS=5           # 检索最终返回数量
CLUSTER_TRIGGER_DAYS=14           # 集群触发窗口（天）
CLUSTER_TRIGGER_MIN_ACCESS=3      # 集群最少访问次数
```

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/v1/chat` | POST | 快速回复（辅脑） |
| `/v1/chat/full` | POST | 一站式：回复 + 异步评估 |
| `/v1/evaluate` | POST | 独立双脑评估 |
| `/v1/chat` | GET | WebSocket 实时交互 |
| `/v1/sessions/{id}` | GET | 会话信息 |
| `/v1/sessions/{id}/evaluation` | GET | 轮询评估结果 |
| `/v1/sessions/{id}/health` | GET | 会话健康报告（token 饱和度/空闲） |
| `/v1/monitor` | GET | 全局监测摘要 |
| `/v1/health` | GET | 健康检查 |
| `/v1/status` | GET | 引擎状态 |

### 示例

```bash
# 快速回复
curl -X POST http://127.0.0.1:18090/v1/chat \
  -d '{"session_id":"u1","message":"你好"}'

# 响应
{"reply":"你好！有什么可以帮你的吗？😊","latency_ms":1452,"session_id":"u1"}
```

## 记忆系统

```
用户发消息
    │
    ├─ social.py → QQ API 获取昵称/群名 → 缓存
    ├─ engine._extract_keywords() → 规则/LLM 提取关键词（jieba 可选增强）
    ├─ engine._assemble_system_prompt() → retrieve_relevant() 语义检索
    │   ├─ FTS5 粗筛 top-20 → LIKE 逐词降级（中文兼容）
    │   ├─ 候选>5 → LLM 精排 top-5（1s timeout）
    │   ├─ 扩散激活 → 沿 memory_links 关联扩散 ≤2 条
    │   ├─ 集群 boost → 集群成员加分
    │   └─ 场景权重 → 同场景 > 私聊 > 其他群
    │   系统 prompt = persona + "相关记忆: ..."
    │
    ├─ engine.chat() → LLM 回复（上下文=persona+精选记忆+纯净历史+原始消息）
    ├─ orchestrator 保存摘要 → 含 source/group_id 场景标记
    └─ brain.evaluate() → 双脑评估 → salience_score → 追答/记忆更新
```

### 每日批处理

```
main.py 启动后按偏移调度:
  +60s  → apply_decay()    艾宾浩斯衰减 + 自动模糊化 + 高频boost
  +1h   → _daily_link_scan()   LLM 语义关联建边
  +2h   → _daily_batch_tag()   LLM 批量实体标注 + 社交关系边
  +3h   → _check_cluster_trigger()  访问频率触发 → LLM 确认 → 集群建立
```

**命名空间**: `user/{uid}/profile` | `user/{uid}/facts` | `user/{uid}/conversations` | `group/{gid}/info` | `global/persona`

**数据表**: `entries`（15 新列）| `memory_links` | `memory_clusters` | `cluster_members` | `access_log`

## 项目结构

```
chat-engine/
├── main.py              # 单进程统一入口 (HTTP + QQ + LLM)
├── engine.py            # LLM 引擎 + 上下文组装 + 关键词提取
├── brain.py             # 多脑评估 (理性脑 + 感性脑 + 融合决策)
├── orchestrator.py      # QQ 消息路由 + 场景标记 + 追答调度
├── memory_store.py      # SQLite/FTS5 记忆系统 (检索/纠错/衰减/集群)
├── qq_protocol.py       # QQ WebSocket 长连接
├── session.py           # 会话管理 + 持久化
├── config.py            # 环境变量集中管理
├── social.py            # QQ 社交信息采集 (昵称/群名)
├── botuser.py           # 用户数据存储
├── server.py            # HTTP/WS 服务器
├── manage.sh            # 启停管理脚本
├── index.html           # Web UI 前端
├── specs/               # 规格文档 (spec/plan/tasks/contracts)
└── docs/                # 设计文档
```

## 与 qq-bot 的关系

chat-engine 已内置 QQ 协议层，**不再依赖 qq-bot**。qq-bot 作为历史项目保留，可通过 `AI_BACKEND=chat-engine` 接入本引擎。

## License

MIT — 详见 [LICENSE](LICENSE)
