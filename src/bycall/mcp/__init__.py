"""MCP 工具网关（M4）：把外部 MCP server 的工具纳入治理脊柱。

红线 #5：agent 永不直连 MCP server。本包是唯一与 MCP server 通信的层——

- ``client.MCPClient``：一个 MCP server 的持久连接 + 工具发现 + 同步调用桥。
- ``manager.MCPManager``：单例，管理全部 MCPClient，启动时发现工具并注册进
  ``tools/registry._REGISTRY``，对治理管道透明（RBAC 靠既有 allowed_tools 白名单）。

工具命名 ``mcp__{server}__{tool}``，与内置工具不冲突。
"""
