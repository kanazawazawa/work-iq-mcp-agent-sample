"""決めておいたフォルダーの資料を先に見せる。

ask は既定で利用者が見られる資料すべてを探す。fileUrls を渡すとその資料を優先するので、
置き場所が決まっているならあたりがつきやすくなる。

★ 限定ではない。Learn の記述も "file URLs to use as context" で、渡した資料に答えが無ければ
サービス側の判断で外の資料も探す（実測で確認）。これで範囲を閉じることはできない。
また利用者が見られる範囲を狭めるものでもない。
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import unquote, urlsplit

from agent_framework import FunctionInvocationContext, function_middleware

import workiq

# SharePoint のフォルダー URL。ブラウザーでコピーしたものをそのまま貼る。
FOLDER_URL = os.environ.get("WORKIQ_FOLDER_URL", "").strip()
FOLDER_NAME = unquote(urlsplit(FOLDER_URL).path.rstrip("/").rsplit("/", 1)[-1])

_files: list[dict[str, str]] | None = None


def _share_id(url: str) -> str:
    """URL をそのまま ID として使える形にする。設定をコピー 1 行で済ませるため。"""
    return "u!" + base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")


async def files(get_token: workiq.TokenProvider) -> list[dict[str, str]]:
    """フォルダーの直下にあるファイルを集める。一覧は変わらない前提で 1 回だけ取る。"""
    global _files
    if _files is not None:
        return _files

    tool = workiq.mcp_tool(get_token)
    async with tool:
        found = await workiq.fetch(
            tool, f"/shares/{_share_id(FOLDER_URL)}/driveItem?$select=id,parentReference"
        )
        if not found:
            _files = []
            return _files

        folder = found[0]
        drive_id = folder.get("parentReference", {}).get("driveId")
        children = await workiq.fetch(
            tool,
            f"/drives/{drive_id}/items/{folder['id']}/children?$select=name,webUrl,folder&$top=100",
        )

    _files = [
        {"url": child["webUrl"], "name": child["name"]}
        for child in children
        if child.get("webUrl") and not child.get("folder")
    ]
    return _files


def pin_files(urls: list[str]):
    """ask に、渡した資料を先に見るよう伝えるミドルウェアを返す。

    モデルが決めた引数を手前で書き換えるので、モデルの判断で渡し忘れることはない。
    ただし渡した後にどこまで探すかは Work IQ 側が決める。
    """

    @function_middleware
    async def middleware(context: FunctionInvocationContext, next: Callable[[], Any]) -> None:
        if context.function.name.endswith("ask"):
            context.arguments["fileUrls"] = urls
        await next()

    return middleware


async def run_agent_stream(
    get_token: workiq.TokenProvider, question: str, *, use_learn: bool = True, effort: str = ""
) -> AsyncIterator[dict[str, Any]]:
    """決めたフォルダーの資料を先に見せて答えさせる。"""
    pinned = await files(get_token)
    yield {"type": "scope", "name": FOLDER_NAME, "url": FOLDER_URL, "count": len(pinned)}

    stream = workiq.run_agent_stream(
        get_token,
        question,
        use_learn=use_learn,
        effort=effort,
        middleware=[pin_files([file["url"] for file in pinned])],
    )
    async for event in stream:
        yield event
