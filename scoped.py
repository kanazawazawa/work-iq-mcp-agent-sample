"""探す資料を、決めておいたフォルダーの中だけに固定する。

ask は既定で利用者が見られる資料すべてを探す。fileUrls を渡すとその集合に固定されるので、
置き場所が決まっているなら無関係な資料を辿らずに済む。

固定されるのはファイルだけで、メール・予定・チャットは引き続き対象になる (実測)。
また検索範囲の指定であってアクセス制御ではない。利用者が見られる範囲は変わらない。
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
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
    """ask が探す範囲を、渡したファイルだけに差し替えるミドルウェアを返す。

    モデルが決めた引数を手前で書き換えるので、範囲はモデルの判断に左右されない。
    """

    @function_middleware
    async def middleware(context: FunctionInvocationContext, next: Callable[[], Any]) -> None:
        if context.function.name.endswith("ask"):
            context.arguments["fileUrls"] = urls
        await next()

    return middleware


async def run_agent(
    get_token: workiq.TokenProvider, question: str, *, use_learn: bool = True
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """決めたフォルダーの中だけを探して答えさせる。"""
    urls = [file["url"] for file in await files(get_token)]
    return await workiq.run_agent(
        get_token, question, use_learn=use_learn, middleware=[pin_files(urls)]
    )
