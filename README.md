# 星环 Chat

面向团队的多部门 AI 对话工作台：**Chat / Agent 双模式、可导入 Agent Markdown 与参考脚本、RAG + Ontology、会话持久化、多用户与部门知识库隔离**。

![星环 Chat](webchat/screenshots/workspace.jpg)

## 快速开始

```bash
git clone https://github.com/rexxie0118/xinghuan-chat.git
cd xinghuan-chat

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python -m webchat --host 127.0.0.1 --port 8787
```

打开 <http://127.0.0.1:8787>，演示账号：

| 账号 | 密码 | 部门 |
| --- | --- | --- |
| `demo` | `demo1234` | 产品部 |
| `alice` | `alice1234` | 研发部 |
| `bob` | `bob1234` | 人事部 |

默认 `mock` provider，不需要 API Key，离线可跑通检索、图谱、Agent 脚本执行和引用链路。

## 功能

- 部门知识库切换，会话按「用户 + 知识库」隔离
- 输入框内 `Chat` / `Agent` 模式切换
- 左侧 Agent / Skill 子菜单，维护导入项
- 导入 Agent Markdown + `.py` / `.sh` / `.js` 参考脚本
- 从 GitHub 仓库/目录自动导入 `SKILL.md` / `AGENT.md`
- MCP stdio/HTTP 工具注册表（白名单、超时、调用 API）
- Apple Container 一次性 Agent 执行沙箱
- AgentKernel + 可信 plugin hook / manifest / tool 扩展
- LLM Tool Loop：MCP/plugin tools 自动调用
- 页面内 Docs：迭代记录、Todo、历史架构图
- Agent 模式执行脚本，展示 stdout / stderr / exit code / 工具轨迹
- RAG 混合检索 + 本体图谱 + 引用回查
- 多用户、HttpOnly Session、SQLite 持久化
- DeepSeek / ARK / OpenAI 兼容模型可配置
- 执行层抽象为 `SandboxRunner`，后续可替换 Docker / K8s

## 文档

- 应用详细说明：[webchat/README.md](webchat/README.md)
- Agent 执行扩展点：[webchat/docs/architecture.md](webchat/docs/architecture.md)
- 示例 Agent：[webchat/examples/sample_agent/AGENT.md](webchat/examples/sample_agent/AGENT.md)
- 测试：` .venv/bin/python -m unittest webchat.tests.test_api -v`

## 安全边界

当前 Agent 脚本由服务端本地受限子进程执行，**不是强沙箱**。只应导入可信脚本；生产环境请使用 `SandboxRunner` 接入 Docker / Kubernetes / gVisor，并增加 artifact 存储、配额与审计。

## License

未声明 License。使用前请先确认代码来源与授权。
