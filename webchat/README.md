# 星环 Chat · Web 版 AI 对话工作台（Demo）

一个可以直接跑起来的网页版 AI 对话应用：

- **多部门知识库**：每个部门有自己的知识库，登录后可切换；会话按「用户 + 知识库」隔离。
- **Chat / Agent 双模式**：在输入框内切换；Agent 模式可选择默认 Agent、导入 Agent 或导入 Skill。
- **Agent / Skill 管理**：左侧菜单维护导入项，支持本地 Markdown + 脚本，也支持从 GitHub 仓库/目录自动导入。
- **导入 Agent**：上传 Markdown 定义 + 参考脚本（Python / Bash / Node），在对话中触发执行，并展示 stdout / stderr / exit code / 轨迹。
- **接入独立 Agent 服务**：也可以把另一个 repo 的 Agent 暴露成 HTTP 契约；失败自动回退本地 RAG。
- **多用户**：注册 / 登录、HttpOnly Session、部门权限、会话与消息严格按 `user_id` 隔离。
- **对话记录持久化**：SQLite 保存会话和消息，刷新、换设备或重新登录后仍在。
- **RAG + Ontology**：复用仓库根目录的 `kb` 引擎，回答带引用，右侧实时展示实体关系图和原文证据。
- **离线可跑**：默认 `mock` provider + 规则抽取，不需要任何 API Key。

![星环 Chat 工作台](screenshots/workspace.jpg)

演示账号：

| 用户名 | 密码 | 部门 | 可访问知识库 |
| --- | --- | --- | --- |
| `demo` | `demo1234` | 产品部 | 产品知识库、公司公共知识库 |
| `alice` | `alice1234` | 研发部 | 研发知识库、公司公共知识库 |
| `bob` | `bob1234` | 人事部 | 人事知识库、公司公共知识库 |

也可以直接注册新账号，注册时选择部门，系统会自动授予「部门知识库 + 公司公共知识库」。

---

## 1. 启动

```bash
cd xinghuan-chat

# 依赖已装在仓库根目录的 .venv 中；如果是新环境：
# python3 -m venv .venv
# .venv/bin/pip install -r webchat/requirements.txt

.venv/bin/python -m webchat --host 127.0.0.1 --port 8787
# 或：./webchat/run.sh --port 8787
```

打开 <http://127.0.0.1:8787>，用 `demo / demo1234` 登录。

首次启动会自动：

1. 在 `webchat/data/webchat.sqlite3` 建用户 / 会话 / 消息表并写入演示账号；
2. 按 `webchat/kbs.json` 创建 4 个部门知识库，分别从 `webchat/knowledge/<kb>/` 摄取到 `webchat/data/kb_<kb>.sqlite3`；
3. 启动后即可提问。新增 Markdown 放到对应部门目录，重启应用即可增量摄取（内容没变会自动跳过）。

> 生产环境请把 `WEBCHAT_SEED_DEMO_USERS` 设为 `false`，并使用 HTTPS + `WEBCHAT_SECURE_COOKIE=true`。

---

## 2. 界面

| 部门知识库切换 | Agent / Skill 管理 |
| --- | --- |
| ![知识库切换](screenshots/kb-menu.jpg) | ![Agent / Skill 菜单](screenshots/agent-skill-menu.jpg) |

GitHub 导入见 [screenshots/github-import.jpg](screenshots/github-import.jpg)，Agent 脚本执行见 [screenshots/chat.jpg](screenshots/chat.jpg)。登录页见 [screenshots/login.jpg](screenshots/login.jpg)，移动端见 [screenshots/mobile.jpg](screenshots/mobile.jpg)。

- **左侧**：**知识库切换器**、新建对话、会话搜索、当前知识库的历史记录、重命名 / 删除。
- **中间**：流式回答、Markdown、引用角标、脚本执行结果、可展开的执行轨迹、追问建议。
- **输入框**：`Chat` / `Agent` 切换；Agent 模式下右侧出现 Agent 选择器。
- **右侧**：当前知识库的本体图谱（可拖拽 / 缩放 / 点节点看证据）、引用列表、知识资料与 provider 状态。
- **顶部**：`Chat` / `Agent` 引擎切换（和输入框同步），当前 Agent 来源（本地 / 外部 / 导入）。

两种模式：

| 模式 | 行为 |
| --- | --- |
| `Chat` | 混合检索 → 直接生成带引用回答（内部 mode=`rag`） |
| `Agent` | 检索 → 展开 2 跳图谱 → 组合证据 → 默认 Agent 或导入 Agent（可执行脚本） |

离线模式下回答会明确标注「离线抽取」，不会假装调用过大模型。

---

## 3. 多部门知识库

配置在 [`kbs.json`](kbs.json)：

```json
{
  "departments": ["产品部", "研发部", "人事部"],
  "knowledge_bases": [
    {
      "id": "product",
      "name": "产品知识库",
      "department": "产品部",
      "dir": "knowledge/product",
      "sample_questions": ["北极星项目的负责人是谁？"]
    },
    {
      "id": "common",
      "name": "公司公共知识库",
      "department": "all",
      "dir": "knowledge/common"
    }
  ]
}
```

访问规则：

- `department` 等于用户部门的知识库：可读写自己的会话。
- `department: "all"` 的知识库：所有部门可见，用于公司制度、术语和公共资料。
- 其他部门的知识库：接口返回 403，切换器里也不会出现。
- 对话创建时写入 `kb_id`；切换知识库后只看到该知识库下的会话。

知识库目录：

| 知识库 | 部门 | 语料 | 示例问题 |
| --- | --- | --- | --- |
| 产品知识库 | 产品部 | 6 篇 | 北极星负责人、星环能力、Agent 接入 |
| 研发知识库 | 研发部 | 4 篇 | 依赖关系、发布回滚、P1 值班 |
| 人事知识库 | 人事部 | 4 篇 | 年假、试用期、报销、远程 |
| 公司公共知识库 | 全公司 | 3 篇 | 公司业务、信息安全、术语 |

新增部门只要：加一个语料目录、在 `kbs.json` 增加条目、在 `departments` 数组增加部门名，然后重启。新注册用户会自动获得对应部门的知识库。

---

## 4. 导入 Agent 并执行脚本

> 执行层已经抽象成 `SandboxRunner`；后续换 Docker/K8s 不需要改 Agent 编排或前端，见 [架构扩展点](docs/architecture.md)。

左侧菜单有 **Agent / Skill** 子菜单：

- 点击任一条目：切到 Agent 模式并选用它；
- 「管理」：查看、删除、上传；
- 「GitHub 导入」：粘贴 GitHub 仓库/目录地址自动导入。

![Agent / Skill 菜单](screenshots/agent-skill-menu.jpg)

输入框右侧的 Agent 选择器也提供同样的列表。

### 从 GitHub 自动导入

![GitHub 导入](screenshots/github-import.jpg)

支持以下地址：

```text
https://github.com/owner/repo
https://github.com/owner/repo/tree/main/skills/pdf
https://github.com/owner/repo/blob/main/skills/pdf/SKILL.md
https://raw.githubusercontent.com/owner/repo/main/skills/pdf/SKILL.md
```

导入规则：

- 目录下优先找 `SKILL.md`，其次 `AGENT.md` / `agent.md`，最后 `README.md`；
- `SKILL.md` → `kind=skill`，`AGENT.md` → `kind=agent`；
- 最多 250 个文本文件、单文件 512KB、总大小 5MB，目录最多 4 层；
- 只要文本类脚本/引用（`.py` / `.sh` / `.js` / `.mjs` / `.html` / `.css` / `.md` / `.json` / `.yaml` 等），跳过图片和二进制；
- 私有仓库需要给 Web 服务配置 `GITHUB_TOKEN`；
- 只接受 `github.com` / `raw.githubusercontent.com`，通过 GitHub API 拉取，不做任意 URL 代理。

GitHub Skill 通常没有单一入口脚本，所以导入后：

- 有 `entrypoint`：Agent 模式执行脚本；
- 没有 `entrypoint`：在有真实 LLM 的环境下，把 `SKILL.md` 作为 system instruction + 知识库证据作答；mock 环境回退 RAG。

### 本地上传

在「Agent / Skill 管理」里也可以上传：

- 一个 **Agent Markdown**：描述目标、步骤和约束；
- 一组 **参考脚本**：`.py` / `.sh` / `.js` / `.mjs`（也可带 `.txt` / `.json` 数据文件）；
- 类型可选 **Agent** 或 **Skill**；可选填入口脚本。

Markdown 可以用 frontmatter 声明入口：

```markdown
---
name: 发布值守助手
description: 发布失败后的止损与回滚建议
entrypoint: run.py
runtime: python
timeout: 8
---

# 发布值守助手

先止损，再判断是否回滚，最后同步值班群。
```

脚本约定：

- `argv[1]` 是用户问题；
- stdin 是一段 JSON：`{question, agent, context}`，其中 `context.hits` 是本次 RAG 召回；
- stdout 直接作为回答（支持 Markdown）；写 `[1]`、`[2]` 会映射为引用；
- stderr 和退出码会展示在「脚本执行」面板里。

界面触发链路：

```
切到 Agent → 选择导入的 Agent → 发送消息
  → 检索当前知识库
  → 加载 Agent Markdown + 脚本
  → 写临时目录 → 执行 entrypoint（timeout）
  → stdout 作为回答 / 失败回退知识库
  → 展示脚本执行 + 工具轨迹
```

参考示例：[examples/sample_agent/AGENT.md](examples/sample_agent/AGENT.md) 和 [run.py](examples/sample_agent/run.py)。

### 执行边界

这不是强沙箱，只是 Demo 级隔离：

- 临时工作目录，脚本文件只在该目录内材料化；
- 环境变量清空，只保留 PATH / TMPDIR 等最小集合；
- 单次 timeout 最大 15 秒，输出截断；
- 最多 250 个文件、单文件 512KB、总大小 5MB；
- 子进程独立进程组，超时会 kill 整个进程组；
- 脚本仍可能读取宿主文件或访问网络。**只导入你信任的脚本**，生产请放到容器 / 独立 job runner / gVisor 等强隔离环境。

服务端可以通过 `WEBCHAT_ALLOW_SCRIPT_EXEC=0` 完全关闭脚本执行；Agent 管理接口：

| 接口 | 作用 |
| --- | --- |
| `GET /api/agents` | 当前用户导入的 Agent / Skill |
| `POST /api/agents` | 导入 Markdown + files |
| `POST /api/agents/import/github` | 从 GitHub 仓库/目录自动导入 |
| `GET /api/agents/{id}` | 完整定义与脚本内容 |
| `DELETE /api/agents/{id}` | 删除自己的 Agent / Skill |

选中导入 Agent 后，发送消息会在请求体带 `agent_id`；服务端校验归属后执行。导入 Agent 优先于外部 Agent 服务。

---

## 5. 接入另一个 repo 的 Agent

星环 Chat **不 import** Agent 代码，只依赖 HTTP。配置：

```bash
export WEBCHAT_AGENT_URL=http://127.0.0.1:8790
export WEBCHAT_AGENT_TOKEN=optional-bearer-token
export WEBCHAT_AGENT_TIMEOUT=25
.venv/bin/python -m webchat
```

请求：

```http
POST {AGENT_BASE_URL}/chat
Authorization: Bearer <可选>
Content-Type: application/json
```

```json
{
  "user": {"id": "usr_...", "name": "演示用户"},
  "conversation_id": "conv_...",
  "knowledge_base": {"id": "product", "name": "产品知识库", "department": "产品部"},
  "mode": "agent",
  "messages": [{"role": "user", "content": "北极星项目的负责人是谁？"}],
  "context": {
    "question": "北极星项目的负责人是谁？",
    "hits": [
      {
        "index": 1,
        "chunk_id": "chk_...",
        "doc_title": "团队与职责",
        "heading_path": "团队与职责 › 分工",
        "text": "……",
        "score": 0.04
      }
    ],
    "graph": {"nodes": [], "edges": []},
    "memories": []
  }
}
```

响应：

```json
{
  "answer": "最终回答，支持 Markdown",
  "citations": [1, 2],
  "confidence": 0.72,
  "trace": [
    {"title": "调用工具", "detail": "search_knowledge(...)", "status": "done"}
  ],
  "suggestions": ["继续追问 1", "继续追问 2"]
}
```

- `citations` 可以是编号数组（映射到请求里的 `hits`），也可以是完整引用对象。
- 外部 Agent 超时、非 200 或 JSON 非法时，服务端记录错误并**自动回退到本地 RAG**，用户消息和会话不受影响。
- 可运行的参考实现：[`examples/external_agent_server.py`](examples/external_agent_server.py)。把它复制到 Agent repo，替换 `answer()` 里的策略即可。

本地验证外部 Agent：

```bash
# 终端 A
cd webchat/examples && ../../.venv/bin/python external_agent_server.py

# 终端 B
cd xinghuan-chat
WEBCHAT_AGENT_URL=http://127.0.0.1:8790 .venv/bin/python -m webchat --port 8788
```

---

## 6. RAG + Ontology

知识库引擎来自仓库根目录的 `kb/`，Web 层只做适配：

```
用户问题
  ├─ 向量召回 (特征哈希 / 真 embedding)
  ├─ FTS 中文子串召回
  ├─ 图谱多跳召回（实体别名 → 2 跳关系）
  └─ RRF 融合 → 片段 + 实体 + 关系 + 证据句
        ├─ 回答（LLM 或离线抽取式）
        ├─ 引用回填 [n]
        └─ 右侧图谱 / 引用 / 资料面板
```

相关接口（都要求登录）：

| 接口 | 作用 |
| --- | --- |
| `GET /api/kbs` | 当前用户可访问的知识库与文档量 |
| `GET /api/kb/overview?kb=` | 文档、实体、关系、类型、provider 状态 |
| `GET /api/kb/graph?kb=&query=` | 全局图，或问题相关子图 |
| `GET /api/kb/search?kb=&q=` | 只检索不生成，含三路排名与耗时 |
| `GET /api/kb/entities/{id}?kb=` | 实体详情、关系、原文证据 |

每个知识库是独立的 SQLite 文件（`kb_product.sqlite3`、`kb_engineering.sqlite3`…），互不污染。部门权限在 Web 层校验，检索器本身不需要知道用户是谁。

### 接真实模型

#### 方式 A：DeepSeek（最快）

```bash
export WEBCHAT_KB_PROVIDER=deepseek
export DEEPSEEK_API_KEY=sk-你的key
# 可选
export DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
export DEEPSEEK_MODEL=deepseek-chat

.venv/bin/python -m webchat --port 8787
```

也可以写进 `webchat/.env`（已 gitignore，应用启动时自动读取，不会覆盖已存在的环境变量）：

```dotenv
WEBCHAT_KB_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-你的key
```

DeepSeek 只提供 OpenAI 兼容的 **chat** 接口，没有 embeddings 接口。因此这个模式：

- 回答生成走 DeepSeek；
- 检索继续使用本地 hash embedding + FTS + 图谱，**不需要重建知识库**；
- 新文档的实体抽取使用规则抽取，避免启动时消耗 DeepSeek token（回答仍然走 DeepSeek）。

验证是否生效：登录后右下角「资料」面板里，`生成` 应为 `deepseek-chat`；回答徽标会从「离线抽取」变成「LLM 生成」。

#### 方式 B：ARK / OpenAI 兼容（chat + embedding）

```bash
export WEBCHAT_KB_PROVIDER=ark
export ARK_API_KEY=...
.venv/bin/python -m webchat
```

`kb` 也支持 `openai` / 任意 OpenAI 兼容网关。注意：如果原来用 mock 建过索引，换 embedding 模型后需要重建知识库（删除 `webchat/data/kb_*.sqlite3` 后重启，或使用 `kb reindex` 指向该库），否则向量维度不一致会降级为全文检索。

---

## 7. 多用户与数据模型

```sql
users(id, username UNIQUE, display_name, password_hash, department, created_at)
sessions(token_hash PK, user_id, expires_at)
conversations(id, user_id, title, mode, kb_id, created_at, updated_at)
messages(id, conversation_id, user_id, role, content,
         citations_json, meta_json, created_at)
agents(id, user_id, name, description, markdown, entrypoint,
       runtime, timeout, files_json, created_at)
```

- 密码：`scrypt` 加盐哈希；Session Token 只把 SHA-256 摘要写库。
- Cookie：`HttpOnly` + `SameSite=Lax`，默认 7 天，可设 `Secure`。
- 所有会话 / 消息查询都同时带 `conversation_id` 和 `user_id`；猜到别人的 ID 也只会返回 404。
- 会话额外带 `kb_id`；加载会话和发送消息时都会重新校验用户对该知识库的访问权限。
- 旧库启动时自动迁移：补 `users.department` 和 `conversations.kb_id` 字段，默认归到产品知识库。
- 每次回答的引用、轨迹、置信度、图谱快照都存在 `messages.meta_json`，刷新后可完整还原。
- CSRF：写请求校验 `Origin` 与 `Host` 一致；CSP 禁止内联脚本。

自动生成标题：首条用户消息生成会话标题（超过 22 字截断），之后可手动重命名。

---

## 8. 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `WEBCHAT_HOST` / `WEBCHAT_PORT` | `127.0.0.1` / `8787` | 监听地址 |
| `WEBCHAT_DATA_DIR` | `webchat/data` | SQLite 与各知识库数据目录 |
| `WEBCHAT_DB` | `<data>/webchat.sqlite3` | 用户 / 会话库 |
| `WEBCHAT_KBS_CONFIG` | `webchat/kbs.json` | 部门、知识库与示例问题配置 |
| `WEBCHAT_KB_PROVIDER` | `mock` | 所有知识库共用：`mock` / `deepseek` / `ark` / `openai` / `openai-compatible` |
| `WEBCHAT_ALLOW_SCRIPT_EXEC` | `true` | 是否允许执行导入 Agent 的参考脚本 |
| `WEBCHAT_SANDBOX` | `local` | 执行后端；当前为 `local`，可注册 `docker` / `k8s` 实现 |
| `GITHUB_TOKEN` | 空 | 导入私有 GitHub 仓库 / 提高 API 速率限制 |
| `DEEPSEEK_API_KEY` | 空 | DeepSeek Key；配合 `WEBCHAT_KB_PROVIDER=deepseek` |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com/v1` | DeepSeek 兼容网关地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 可换 `deepseek-reasoner` |
| `WEBCHAT_AGENT_URL` | 空 | 外部 Agent 服务地址；为空用本地 Agent |
| `WEBCHAT_AGENT_TOKEN` | 空 | 外部 Agent Bearer Token |
| `WEBCHAT_AGENT_TIMEOUT` | `25` | 外部 Agent 超时（秒） |
| `WEBCHAT_SESSION_DAYS` | `7` | Session 有效期 |
| `WEBCHAT_SECURE_COOKIE` | `false` | HTTPS 下应设 `true` |
| `WEBCHAT_SEED_DEMO_USERS` | `true` | 是否写入 demo / alice / bob |

完整示例见 [`.env.example`](.env.example)。

---

## 9. 测试

```bash
cd xinghuan-chat
.venv/bin/python -m unittest webchat.tests.test_api -v
```

覆盖：登录 / 错误密码 / 登出、注册校验、部门知识库访问控制、知识库维度的会话隔离、SSE 流式回答与持久化、自动标题、双账号会话隔离、知识图谱与实体详情、Agent/Skill 导入、嵌套脚本路径、GitHub URL 解析、脚本执行 / 用户隔离。

> 当前共 11 项测试，全部通过。

---

## 10. 目录

```
webchat/
├── main.py                 FastAPI 路由、SSE、鉴权依赖、静态资源
├── config.py               Settings（全部环境变量）
├── db.py / repository.py   SQLite schema 与数据访问（按 user_id 隔离）
├── security.py             scrypt、Session Token
├── knowledge.py            kb 引擎适配、多知识库注册表、检索/图谱/实体
├── agent.py                本地 / 外部 / 导入 Agent 编排
├── agent_defs.py           Agent / Skill 导入、解析、存储
├── github_importer.py      GitHub 仓库/目录导入（API + URL 校验）
├── sandbox/                SandboxRunner 协议 + local 实现（可扩展 Docker/K8s）
├── kbs.json                部门、知识库、示例问题配置
├── knowledge/              17 篇演示语料
│   ├── product/            产品部 · 6 篇
│   ├── engineering/        研发部 · 4 篇
│   ├── hr/                 人事部 · 4 篇
│   └── common/             全公司 · 3 篇
├── static/                 index.html / styles.css / app.js / favicon
├── examples/               外部 Agent 参考服务 + sample_agent/
├── tests/                  TestClient 端到端测试
└── data/                   运行时生成（已 gitignore）
```

---

## 11. 边界与下一步

这是可运行的 Demo，不是生产系统：

- 默认离线抽取式回答只会“组织原文”，不具备真正的语言推理；接真实模型后质量取决于模型与语料。
- 知识库按「部门」授权，一个用户当前只属于一个部门；跨部门项目组、文档级 ACL、管理员后台还没有做。
- 注册时自选部门只适合 Demo；生产环境应由管理员或 SSO 分配部门，不能让用户自己提升权限。
- 用户不能上传文档。生产需要管理员摄取、审核、配额和审计日志。
- 外部 Agent 契约是同步 JSON，Web 端做本地打字流；如需真正的 token 级流式，可把 `/chat` 改成 SSE 并把 `delta` 透传。
- 导入 Agent 的脚本执行是非强沙箱；生产必须换成容器 / 独立 job runner 并加资源配额、网络策略和审计。
- 没有限流、审计日志、邮箱验证、密码找回；上线前应补齐。
- 图谱布局是前端轻量力导向，适合几十个节点；大规模图谱应换成 Cytoscape / G6 + 后端采样。

### 已经为后续留好的扩展点

1. **Agent**：实现 `/chat` 契约即可，不需要改 Web 代码；`trace` 可直接渲染成时间线。
2. **知识库**：`knowledge.py` 是唯一适配层；新增部门只需加语料目录和 `kbs.json` 条目。
3. **多模型**：`kb` 的 provider 已抽象 embedding / chat，接 ARK、OpenAI、本地 vLLM 都走同一配置。
4. **消息级扩展**：`meta_json` 可继续塞 token 用量、工具参数、审批状态、评分反馈。
