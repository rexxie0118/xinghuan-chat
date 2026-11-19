# 外部 Agent 接入规范

星环 Chat 不直接导入另一个仓库的 Agent 代码，而是通过一个稳定的 HTTP 契约调用 Agent 服务。

## 一句话回答

外部 Agent 的接入方式是：星环 Chat 调用 Agent 服务的 `/chat` HTTP 接口，把 user、conversation_id、messages、mode 和 context 一起传过去。

## 请求

应用向 `${AGENT_BASE_URL}/chat` 发送 POST 请求，请求体包含 user、conversation_id、knowledge_base、messages、mode 和 context 六个字段。knowledge_base 标识当前选中的部门知识库。context 里已经放好 RAG 召回片段和图谱节点关系，因此 Agent 可以选择直接使用，也可以忽略它并调用自己的工具。

请求头使用 Bearer Token 鉴权，密钥来自环境变量 AGENT_API_KEY。

## 响应

Agent 返回 answer、citations、trace、suggestions 四个字段。answer 是最终回答；citations 是引用编号数组；trace 是工具调用轨迹，每一项包含 title、detail 和 status；suggestions 是三条可点击的追问建议。

## 失败处理

如果 Agent 服务超时、返回非 200 或 JSON 格式错误，星环 Chat 会自动回退到本地 RAG 回答，并在轨迹里标注「外部 Agent 不可用」。回退不会中断会话，也不会丢失用户消息。

## 设计取舍

Agent 仓库独立部署，升级和扩缩容不影响 Web 应用。Web 应用只依赖契约，不依赖 Agent 的语言和框架。Agent 服务应当无状态，会话状态由星环 Chat 持久化后随请求传入。
