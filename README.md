# Work IQ MCP × Microsoft Agent Framework

自社の Web アプリに載せたエージェントから、**Work IQ MCP** を数あるツールの 1 つとして呼ぶ最小サンプルです。

比較用に、職場のデータとは無関係な **Microsoft Learn MCP** も一緒に渡しています。どちらを使うか、そもそも使うかはエージェントが判断します。

Work IQ は**サインインした本人の委任トークン**で呼びます。アプリの資格情報では呼ばないので、参照できる範囲はその人が Microsoft 365 で見られる範囲と一致します。

```
ブラウザ ──> FastAPI ──> Microsoft Agent Framework ──┬── Work IQ MCP      (本人のトークン)
                                                      └── Microsoft Learn MCP (認証なし)
```

| ファイル | 中身 |
| --- | --- |
| [workiq.py](workiq.py) | MCP ツールの組み立てとエージェント実行 |
| [main.py](main.py) | サインイン (MSAL)、ルーティング |
| [templates/index.html](templates/index.html) | 画面 |

## 前提

- **テナントで Work IQ を有効化**しておく（サービス プリンシパルの作成と従量課金プランの割り当て）
  → [Enable your tenant for Work IQ](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/enable-work-iq)
- Azure OpenAI のデプロイ (エージェント側のモデル)
- Python 3.12 以降

## セットアップ

### 1. アプリ登録

[Microsoft Entra 管理センター](https://entra.microsoft.com/) でアプリを登録します。

| 項目 | 値 |
| --- | --- |
| プラットフォーム | Web |
| リダイレクト URI | `http://localhost:8000/auth/callback` |
| クライアント シークレット | 発行して控える |
| API のアクセス許可 | Work IQ の **`WorkIQAgent.Ask`** (委任)。管理者の同意を付与 |

### 2. 設定

`.env.example` を `.env` にコピーして値を入れます。

### 3. 起動

Windows は `run.cmd` をダブルクリック。手動なら次のとおりです。

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # macOS/Linux は .venv/bin/pip
az login                                        # モデル呼び出しに使う
.venv/Scripts/python -m uvicorn main:app --reload
```

<http://localhost:8000> を開き、右上の「接続」から Microsoft 365 にサインインします。

## MCP をエージェントに渡す

これだけです ([workiq.py](workiq.py))。

```python
workiq_tool = MCPStreamableHTTPTool(
    name="workiq",
    url="https://workiq.svc.cloud.microsoft/mcp",
    tool_name_prefix="workiq",
    header_provider=lambda _kwargs: {"Authorization": f"Bearer {get_token()}"},
    allowed_tools=TOOLS,
    load_prompts=False,
)

async with workiq_tool, learn_tool:
    agent = Agent(client=..., instructions=INSTRUCTIONS, tools=[workiq_tool, learn_tool])
    response = await agent.run(question)
```

押さえておく点が 2 つあります。

- **`header_provider`** はリクエストのたびに呼ばれます。トークンの更新は MSAL 側が持つので、長く開いたままのセッションでも期限切れを気にせずに済みます。
- **`allowed_tools`** はプロンプトでの「お願い」ではなく実際の制限です。読み取り専用の担保はここで行います。書き込み系はテナントのポリシーでも既定で止まりますが、コード側でも渡していません。

Work IQ が公開するツールと使えるパスは [Work IQ MCP tool reference](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference) と [entity model](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/entity-model) を参照してください。

## このサンプルが省いていること

本番に持っていくときに足すことになる部分です。

- トークンをプロセス内の辞書に置いています。再起動でサインインが切れます
- エラー処理、レート制限、再試行
- 会話の継続 (`ask` の `conversationId`)
- ログと監査

## 参考

- [Work IQ overview](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/)
- [Work IQ MCP overview](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/overview)
- [Work IQ CLI](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/cli) — アプリ登録なしで疎通確認できます
- [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/)
