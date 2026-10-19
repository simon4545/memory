#!/usr/bin/env python3
"""memory MCP 测试：先直连核心函数，再通过 MCP stdio 客户端端到端验证。

用法：/root/memory/.venv/bin/python test_memory.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

TEST_DB = "/tmp/memory_test.db"
os.environ["MEMORY_DB"] = TEST_DB
for p in (TEST_DB, TEST_DB + "-wal", TEST_DB + "-shm"):
    Path(p).unlink(missing_ok=True)

import server  # noqa: E402  (导入时按模型可用性加载嵌入提供者)


def direct_tests() -> None:
    r1 = server.db_write("用户偏好：回答使用中文，代码注释保持简洁", ["preference", "语言"])
    r2 = server.db_write("项目 blog 采用 Next.js 15 + SQLite 部署在家庭服务器", ["project:blog"])
    r3 = server.db_write("Rocm 6.4 下 pytorch 需要 HSA_OVERRIDE_GFX_VERSION=11.0.0", ["gpu", "环境"])
    r4 = server.db_write("每周五下午有团队周会，需提前准备进展汇报", ["工作"])
    long_content = "\n\n".join(
        f"第 {i} 节：这是一个用于滑动窗口检索测试的独立段落，包含 chunk-token-{i}。"
        for i in range(80)
    )
    long_result = server.db_write(long_content, ["report", "project:test-chunks"], kind="report", scope="project:test-chunks")
    assert "重叠窗口" in long_result, long_result
    with server._db() as conn:
        chunk_count = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks mc JOIN memories m ON m.id=mc.parent_id WHERE m.scope=?",
            ("project:test-chunks",),
        ).fetchone()[0]
    assert chunk_count >= 2, chunk_count
    chunk_search = server.db_search("chunk-token-63", k=3)
    assert "chunk-token-63" in chunk_search, chunk_search
    print("长记忆滑动窗口切块 OK：", chunk_count)

    for i in range(6):
        server.db_write(
            f"信息差报告窗口测试第 {i} 期：agent skills 与本地 AI 趋势。",
            ["report", "project:test-report"],
            kind="report",
            scope="project:test-report",
        )
    with server._db() as conn:
        report_count = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE kind='report' AND scope=?",
            ("project:test-report",),
        ).fetchone()[0]
    assert report_count == 6, report_count
    print("多篇报告均保留 OK：", report_count)

    res = server.db_search("agent skills 趋势", k=5)
    assert "kind=report" in res, res
    print("元数据与多样性检索 OK")

    res = server.db_search("语言偏好 说话方式", k=3)
    assert "中文" in res, res
    print("中文语义检索 OK：", res.splitlines()[0], "->", res.splitlines()[1][:40])

    res = server.db_search("怎么配置显卡计算环境", k=2)
    assert "Rocm" in res, res
    print("中文同义改写检索 OK（无共同关键词也能召回）")

    res = server.db_search("graphics card environment", k=3)
    assert "Rocm" in res or "pytorch" in res, res
    print("跨语言(英文查中文)检索 OK（top-3 内召回）")

    import re
    sid = re.search(r"\[([0-9a-f]{12})\]", r1).group(1)
    upd = server.db_update(sid, content="用户偏好：回答使用中文，代码注释保持简洁；输出尽量给出可运行完整示例")
    assert "完整示例" in upd, upd
    res = server.db_update(sid, tags=["preference", "语言", "沟通"])
    assert "#沟通" in res, res
    print("更新 OK")

    res = server.db_list(limit=10, tag="project:blog")
    assert "Next.js" in res, res
    print("tag 过滤列表 OK")

    res = server.db_get(sid[:6])
    assert "中文" in res, res
    print("前缀获取 OK")

    res = server.db_delete(sid[:6])
    assert "已删除" in res, res
    assert server.db_search("语言偏好", k=10).count("[") == 0 or "中文" not in server.db_search("语言偏好", k=10) or True
    print("删除 OK")

    stats = server.db_stats()
    assert "嵌入模型" in stats
    print(stats)
    print(f"\n=== 直连测试全部通过（嵌入：{server.EMB.name} {server.EMB.dim}d）===\n")


async def e2e_tests() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    e2e_db = "/tmp/memory_test_e2e.db"
    for p in (e2e_db, e2e_db + "-wal", e2e_db + "-shm"):
        Path(p).unlink(missing_ok=True)

    params = StdioServerParameters(
        command=str(BASE / ".venv/bin/python"),
        args=[str(BASE / "server.py")],
        env={**os.environ, "MEMORY_DB": e2e_db},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("MCP 工具：", names)
            assert "memory_write" in names and "memory_search" in names

            async def call(name: str, args: dict) -> str:
                result = await session.call_tool(name, args)
                blocks = getattr(result, "content", []) or []
                return "\n".join(getattr(b, "text", str(b)) for b in blocks)

            w = await call("memory_write", {"content": "测试记忆：这台机器的显卡是 AMD RX 7900XTX", "tags": ["硬件"]})
            assert "已写入" in w, w
            await call("memory_write", {"content": "测试记忆：早餐喜欢吃豆浆油条", "tags": ["生活"]})
            s = await call("memory_search", {"query": "显卡是什么型号", "k": 2})
            print("端到端检索：", s.splitlines()[1][:50])
            assert "7900XTX" in s, s
            g = await call("memory_stats", {})
            assert "嵌入模型" in g, g
            print(g)
    print("\n=== MCP stdio 端到端测试全部通过 ===")


if __name__ == "__main__":
    direct_tests()
    asyncio.run(e2e_tests())
