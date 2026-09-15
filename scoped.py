"""探す範囲を、決めておいたフォルダーの中だけに固定する。

ask は既定で利用者が見られる範囲すべてを探す。fileUrls を渡すとその集合に固定されるので、
置き場所が決まっているなら無関係な資料を辿らずに済む。

これは検索範囲の指定であってアクセス制御ではない。利用者が見られる範囲は変わらない。
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from typing import Any

from agent_framework import FunctionInvocationContext, MCPStreamableHTTPTool, function_middleware

import workiq

# SharePoint のフォルダー URL。ブラウザーでコピーしたものをそのまま貼る。
FOLDER_URL = os.environ.get("WORKIQ_FOLDER_URL", "").strip()

_files: list[dict[str, str]] | None = None


async def _fetch(tool: MCPStreamableHTTPTool, entity_url: str) -> list[dict[str, Any]]:
    """fetch を 1 件呼び、中身のエンティティを取り出す。"""
    for block in workiq._text_blocks(await tool.call_tool("fetch", entityUrls=[entity_url])):
        for payload in workiq._json_payloads(block):
            results = payload.get("results")
            if not results:
                continue
            data = results[0].get("data") or {}
            return data.get("value") or ([data] if data.get("id") else [])
    return []


async def files(get_token: workiq.TokenProvider) -> list[dict[str, str]]:
    """フォルダーの URL から、その直下にあるファイルを集める。

    URL 自体を ID として使えるので、設定はコピーした 1 行で足りる。
    一覧は変わらない前提で 1 回だけ取る。
    """
    global _files
    if _files is not None:
        return _files

    share_id = "u!" + base64.urlsafe_b64encode(FOLDER_URL.encode()).decode().rstrip("=")
    tool = workiq._workiq_tool(get_token)
    async with tool:
        found = await _fetch(tool, f"/shares/{share_id}/driveItem?$select=id,parentReference")
        if not found:
            _files = []
            return _files
        drive_id = found[0].get("parentReference", {}).get("driveId")
        children = await _fetch(
            tool,
            f"/drives/{drive_id}/items/{found[0]['id']}/children?$select=name,webUrl,folder&$top=100",
        )

    _files = [
        {"url": c["webUrl"], "name": c.get("name") or workiq._display_name(c["webUrl"])}
        for c in children
        if c.get("webUrl") and not c.get("folder")
    ]
    return _files


def pin_files(urls: list[str]):
    """ask が探す範囲を、渡したファイルだけに差し替える。

    モデルが決めた引数を手前で書き換えるので、範囲はモデルの判断に左右されない。
    """

    @function_middleware
    async def middleware(context: FunctionInvocationContext, next: Callable[[], Any]) -> None:
        if context.function.name.endswith("ask"):
            context.arguments["fileUrls"] = urls
        await next()

    return middleware


async def run_agent(
    get_token: workiq.TokenProvider, question: str, use_learn: bool = True
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """決めたフォルダーの中だけを探して答えさせる。"""
    urls = [f["url"] for f in await files(get_token)]
    return await workiq.run_agent(
        get_token, question, use_learn=use_learn, middleware=[pin_files(urls)]
    )
