from fastmcp import FastMCP
from notion_client import Client
import os

# ============================================
# CONFIG
# ============================================


NOTION_TOKEN = os.getenv("NOTION_TOKEN")
PARENT_PAGE_ID = os.getenv("PARENT_PAGE_ID")


notion = Client(auth=NOTION_TOKEN)

# Create MCP server
server = FastMCP("Notion MCP Server")

# ============================================
# TOOL: CREATE NOTE
# ============================================


@server.tool()
def create_note(title: str, content: str) -> str:
    response = notion.pages.create(
        parent={"type": "page_id", "page_id": PARENT_PAGE_ID},
        properties={
            "title": [
                {
                    "type": "text",
                    "text": {"content": title}
                }
            ]
        },
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": content}}
                    ]
                }
            }
        ]
    )

    return f"Note created with ID: {response['id']}"


# ============================================
# TOOL: SEARCH NOTES
# ============================================

@server.tool()
def search_notes(query: str) -> str:
    results = notion.search(query=query)

    return str(results.get("results", []))


# ============================================
# TOOL: UPDATE NOTE
# ============================================

@server.tool()
def update_note(page_id: str, content: str) -> str:
    notion.blocks.children.append(
        block_id=page_id,
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": content}}
                    ]
                }
            }
        ]
    )

    return "Note updated successfully"


# ============================================
# RUN SERVER
# ============================================

if __name__ == "__main__":
     port = int(os.environ.get("PORT", 10000))
     server.run(
         transport="sse",
        host="0.0.0.0",
        port=port
    )
