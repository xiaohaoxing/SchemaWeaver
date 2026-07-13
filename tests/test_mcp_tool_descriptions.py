import inspect

from schemaweaver import mcp_server


def test_mcp_tool_docstrings_include_schemaweaver_for_tool_search():
    source = inspect.getsource(mcp_server)

    for tool_name in (
        "schema_search",
        "table_detail",
        "enum_resolve",
        "sql_grounding_check",
    ):
        # The tool functions are nested in build_server; inspect source text to keep
        # this test independent from FastMCP internals.
        marker = f"def {tool_name}"
        start = source.index(marker)
        next_tool = source.find("    @mcp.tool()", start + len(marker))
        block = source[start: next_tool if next_tool != -1 else len(source)]
        assert "SchemaWeaver" in block
