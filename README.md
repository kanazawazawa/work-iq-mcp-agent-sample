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

- **`header_provider`** は辞書ではなく関数です。リクエストのたびに呼ばれるので、約 1 時間で切れるアクセス トークンの更新を MSAL に任せられます。
- **`allowed_tools`** はプロンプトでの「お願い」ではなく実際の制限です。読み取り専用の担保はここで行います。

Work IQ が公開するツールと使えるパスは [Work IQ MCP tool reference](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference) と [entity model](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/entity-model) を参照してください。

## 本番向けではありません

Work IQ MCP の呼び方を示すための最小構成です。そのまま本番で使うことは想定していません。

手つかずの箇所の例です。これで尽きているわけではありません。

- セッションの永続化と共有 (いまはプロセス内の辞書)
- エラー処理、レート制限、再試行
- 会話の継続 (`ask` の `conversationId`)
- ログと監査

## 参考

- [Work IQ overview](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/)
- [Work IQ MCP overview](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/overview)
- [Work IQ CLI](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/cli) — アプリ登録なしで疎通確認できます
- [Microsoft Agent Framework](https://learn.microsoft.com/agent-framework/)

## ライセンス

[MIT](LICENSE)

本リポジトリは個人が個人の見解で公開しているもので、所属組織の見解ではありません。
