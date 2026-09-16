"""Work IQ MCP を自社エージェントのツールとして渡す。

職場のデータとは無関係な Microsoft Learn MCP も一緒に渡している。
Work IQ が「数あるツールの 1 つ」であり、どれを使うかをエージェントが
選ぶことを示すため。

Work IQ は「接続した本人のトークン」で呼ぶ。アプリの資格情報では呼ばないので、
参照できる範囲はその人が Microsoft 365 で見られる範囲と一致する。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.openai import OpenAIChatClient
from azure.identity import DefaultAzureCredential

# この 2 つは Work IQ の公開ディスカバリーが返す値。
#   curl https://workiq.svc.cloud.microsoft/.well-known/oauth-protected-resource/mcp
MCP_URL = "https://workiq.svc.cloud.microsoft/mcp"
SCOPE = "fdcc1f02-fc51-4226-8753-f668596af7f7/WorkIQAgent.Ask"

# 認証不要の 2 つ目の情報源。
LEARN_MCP_URL = "https://learn.microsoft.com/api/mcp"

# モデルに見せるツール。書き込み系 (create_entity / update_entity / delete_entity /
# do_action) は渡さない。fetch_blob も読み取りだが、返るのは base64 の生バイトで
# モデルが解釈できないため外す。
TOOLS = ["ask", "search_paths", "get_schema", "fetch", "call_function"]

# 指定しないと Work IQ は時刻を UTC で返す。
TIME_ZONE = os.environ.get("WORKIQ_TIME_ZONE", "Asia/Tokyo")

# ツールの使い方は書かない。ここに書くのはアプリ側の方針だけ。
INSTRUCTIONS = f"""あなたは利用者の仕事を助けるアシスタントです。
利用者の職場のデータ (workiq) を主に使います。
Microsoft 製品の仕様や手順など、公式情報が要るときは mslearn も使えます。
どちらを見るべきか決められないときは、両方あたってから答えてください。

推測で補わず、取得した内容だけを根拠にしてください。
自然文の質問を受け取るツールには、質問文の末尾にこの一文をそのまま足します。
「根拠になる箇所は原文のまま引用し、文書のどこにあるか (ページ、スライド番号、見出しなど) も示してください。」
根拠はこの形で並べます。

  引用:「原文をそのまま書き写す」
  出典: [ファイル名](URL) 12 ページ / 「見出し名」

場所が返らなかったときは、出典を名前とリンクだけにします。数字を推測で埋めないでください。
日時は {TIME_ZONE} で表示します。
"""

# 推論の強さ。空なら指定せずサービスの既定に任せる。
EFFORTS = ["none", "low", "medium", "high", "xhigh"]

TokenProvider = Callable[[], str]

_chat_client: OpenAIChatClient | None = None


def mcp_tool(get_token: TokenProvider) -> MCPStreamableHTTPTool:
    """Work IQ MCP サーバーを MAF のツールとして返す。

    allowed_tools はモデルに渡すツール定義そのものを絞る。トークンの権限は
    変わらないので、セキュリティ境界ではない。
    """
    return MCPStreamableHTTPTool(
        name="workiq",
        url=MCP_URL,
        # 呼ばれたツール名が workiq_ask のようになり、どの MCP のものか判別できる。
        tool_name_prefix="workiq",
        # リクエストのたびに呼ばれる。トークンのキャッシュと更新は MSAL 側が持つので、
        # 長く開いたままのセッションでも期限切れを気にしなくてよい。
        header_provider=lambda _kwargs: {"Authorization": f"Bearer {get_token()}"},
        allowed_tools=TOOLS,
        description="接続中の利用者の Microsoft 365 データにアクセスする",
        load_prompts=False,
    )


def _text_blocks(result: Any) -> list[str]:
    if isinstance(result, str):
        return [result]
    return [getattr(c, "text", "") or "" for c in result]


def _json_payloads(text: str) -> Iterator[dict[str, Any]]:
    """テキストに紛れた JSON オブジェクトを取り出す。

    MCP の content (回答文) と structuredContent (JSON) は、call_tool では別ブロックで
    返るが、エージェント経由では 1 本の文字列に連結される。素の json.loads では拾えない。
    """
    decoder = json.JSONDecoder()
    index = 0
    while (start := text.find("{", index)) >= 0:
        try:
            payload, index = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(payload, dict):
            yield payload


async def fetch(tool: MCPStreamableHTTPTool, entity_url: str) -> list[dict[str, Any]]:
    """fetch ツールを 1 件呼び、中身のエンティティを取り出す。

    コレクションならその中身、単体なら 1 件のリスト。
    """
    for block in _text_blocks(await tool.call_tool("fetch", entityUrls=[entity_url])):
        for payload in _json_payloads(block):
            results = payload.get("results")
            if not results:
                continue
            data = results[0].get("data") or {}
            return data.get("value") or ([data] if data.get("id") else [])
    return []


# Learn のツールリファレンスには記載が無いが、ask は引用元をこのキーで返す。
REFERENCE_KEY = "application/vnd.ms-workiq.reference"


def _display_name(url: str) -> str:
    """URL から人が読めるファイル名を作る。取れなければホスト名で代用する。"""
    parts = urlsplit(url)
    # SharePoint の Doc.aspx はパスではなくクエリにファイル名を持つ。
    files = parse_qs(parts.query).get("file")
    if files:
        return unquote(files[0])
    name = unquote(parts.path.rsplit("/", 1)[-1])
    if "." in name:
        return name
    return parts.netloc


def _references(payload: dict[str, Any]) -> list[dict[str, Any]]:
    refs = payload.get(REFERENCE_KEY)
    if not isinstance(refs, dict):
        return []

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs.values():
        url = ref.get("targetLink") if isinstance(ref, dict) else None
        if not url or url in seen:
            continue
        seen.add(url)
        label = ref.get("sensitivityLabel") or {}
        items.append({
            "url": url,
            "name": _display_name(url),
            # 検索でヒットしただけの候補も混ざるので、実際に引用されたものと区別する。
            "cited": bool(ref.get("isCitedInResponse")),
            "label": label.get("tooltip") or label.get("displayName"),
        })

    items.sort(key=lambda r: not r["cited"])
    return items


def _agent_references(response: Any) -> list[dict[str, Any]]:
    """エージェントが呼んだツールの生の結果から参照元を拾う。

    モデルが要約し直すと出典が落ちることがあるので、応答文ではなくツール結果を見る。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in response.messages or []:
        for content in message.contents or []:
            if getattr(content, "type", None) != "function_result":
                continue
            for block in _text_blocks(content.result):
                for payload in _json_payloads(block):
                    for ref in _references(payload):
                        if ref["url"] not in seen:
                            seen.add(ref["url"])
                            items.append(ref)
    return items


def _client() -> OpenAIChatClient:
    global _chat_client
    if _chat_client is None:
        _chat_client = OpenAIChatClient(
            model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            # ローカルは az login、App Service ではマネージド ID が使われる。
            credential=DefaultAzureCredential(),
        )
    return _chat_client


def _tool_calls(response: Any) -> list[dict[str, Any]]:
    """実際に呼ばれたツール名と引数を応答履歴から拾う。

    モデルは利用者の質問をそのまま渡さず書き換えるので、何を聞いたかを見えるようにする。
    """
    calls: list[dict[str, Any]] = []
    for message in response.messages or []:
        for content in message.contents or []:
            if getattr(content, "type", None) != "function_call":
                continue
            try:
                args = content.parse_arguments() or {}
            except Exception:
                args = {"raw": str(getattr(content, "arguments", ""))}
            calls.append({"name": content.name, "arguments": args})
    return calls


async def run_agent_stream(
    get_token: TokenProvider | None,
    question: str,
    *,
    use_workiq: bool = True,
    use_learn: bool = True,
    effort: str = "",
    middleware: list[Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """渡された MCP をツールとして持たせ、自社エージェントに答えさせる。

    どれを呼ぶか、そもそも呼ぶかはモデルが決める。両方外しても動く。
    進み具合を順に返し、最後に done で回答・呼ばれたツール・参照元をまとめて返す。
    """
    tools: list[MCPStreamableHTTPTool] = []
    if use_workiq and get_token:
        tools.append(mcp_tool(get_token))
    if use_learn:
        tools.append(
            MCPStreamableHTTPTool(
                name="mslearn",
                url=LEARN_MCP_URL,
                tool_name_prefix="mslearn",
                description="Microsoft の公式技術ドキュメントを検索・取得する",
                load_prompts=False,
            )
        )

    async with AsyncExitStack() as stack:
        # ツールの個数が変わるので async with を並べずに積む。
        for tool in tools:
            await stack.enter_async_context(tool)

        agent = Agent(
            client=_client(),
            instructions=INSTRUCTIONS,
            tools=tools,
            middleware=middleware,
        )
        stream = agent.run(
            question,
            stream=True,
            options={"reasoning": {"effort": effort}} if effort in EFFORTS else {},
        )

        seen: set[str] = set()
        async for update in stream:
            for content in update.contents or []:
                kind = getattr(content, "type", None)
                if kind == "text" and content.text:
                    yield {"type": "text", "delta": content.text}
                elif kind == "function_call":
                    # 引数が分割で届くので、同じ呼び出しは 1 回だけ知らせる。
                    if content.call_id not in seen:
                        seen.add(content.call_id)
                        yield {"type": "tool", "name": content.name}
                elif kind == "function_result":
                    yield {"type": "result"}

        response = await stream.get_final_response()

    yield {
        "type": "done",
        "answer": response.text,
        "tool_calls": _tool_calls(response),
        "references": _agent_references(response),
    }


async def run_agent(
    get_token: TokenProvider | None,
    question: str,
    *,
    use_workiq: bool = True,
    use_learn: bool = True,
    effort: str = "",
    middleware: list[Any] | None = None,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """run_agent_stream の結果だけが欲しいときに使う。"""
    async for event in run_agent_stream(
        get_token,
        question,
        use_workiq=use_workiq,
        use_learn=use_learn,
        effort=effort,
        middleware=middleware,
    ):
        if event["type"] == "done":
            return event["answer"], event["tool_calls"], event["references"]
    return "", [], []
