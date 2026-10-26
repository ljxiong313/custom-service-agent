"""演示用 MCP 服务器（stdio）：提供 query_order 工具，供 P4 端到端演示。
运行验证: py scripts/demo_mcp_server.py （等待 stdin，无输出为正常）
"""
import json

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo-order")

ORDERS = {
    "U1001": [{"order_id": "20261001A", "item": "清爽保湿面霜50g", "status": "已发货，顺丰SF12345", "amount": 129.0}],
    "U1002": [{"order_id": "20261015B", "item": "烟酰胺精华原液", "status": "待发货", "amount": 199.0}],
}

@mcp.tool()
def query_order(user_code: str) -> str:
    """按用户编码查询订单状态。演示编码: U1001 / U1002"""
    return json.dumps(ORDERS.get(user_code, []), ensure_ascii=False)

if __name__ == "__main__":
    mcp.run(transport="stdio")
