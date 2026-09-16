# Work IQ MCP × Microsoft Agent Framework

自社の Web アプリに載せたエージェントから、**Work IQ MCP** を数あるツールの 1 つとして呼ぶ最小サンプルです。

比較用に、職場のデータとは無関係の **Microsoft Learn MCP** も一緒に渡しています。画面のトグルでそれぞれ付け外しできます。どちらを使うか、そもそも使うかはエージェントが判断します。

Work IQ は**サインインした本人の委任トークン**で呼びます。アプリの資格情報では呼ばないので、参照できる範囲がその人の Microsoft 365 の権限を超えることはありません。

```
ブラウザ ──> FastAPI ──> Microsoft Agent Framework ──┬── Work IQ MCP      (本人のトークン)
                                                      └── Microsoft Learn MCP (認証なし)
```

| ファイル | 中身 |
| --- | --- |
| [workiq.py](workiq.py) | MCP ツールの組み立てとエージェント実行 |
| [scoped.py](scoped.py) | 決めたフォルダーの資料を先に見せる (任意) |
| [main.py](main.py) | サインイン (MSAL)、ルーティング |
| [templates/](templates) | 画面 |

## 前提

- **テナントで Work IQ を有効化**しておく（サービス プリンシパルの作成と従量課金プランの割り当て）
  → [Enable your tenant for Work IQ](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/enable-work-iq)
- Azure OpenAI のデプロイ (エージェント側のモデル)
- Python 3.12 以降（動作確認は 3.14）

### ライセンスと課金

- [Work IQ の概要 — アクセスと価格](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/#access-and-pricing)
- [Work IQ API — ライセンスの要件](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/api-overview#licensing-requirements)

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

<http://localhost:8000> を開き、**M365 連携** を ON にして Microsoft 365 に接続します。

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

- **`header_provider`** は辞書ではなく関数です。リクエストのたびに呼ばれるので、アクセス トークンの期限切れを MSAL に任せられます。
- **`allowed_tools`** はモデルに渡すツール定義そのものを絞ります。プロンプトで禁じるより確実で、モデルが名前を作って呼ぶこともできません。ただしクライアント側の制限で、トークンの権限は変わりません。

トグルで MCP の数が変わるので、実際のコードは `AsyncExitStack` に積んでいます。

何を許可するかはテナント側のポリシーで決まります。Microsoft 365 管理センターの **エージェント > ツール > Work IQ MCP > ポリシー** で、アクセスできる種別、ページングの可否、作成・更新・削除の可否が操作ごとに切り替わります。テナントによって違うので、拒否された応答は再試行せずそのまま見せてください。
→ [Policy governance for Work IQ MCP](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/policy-governance-mcp)

Work IQ が公開するツールと使えるパスは [Work IQ MCP tool reference](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference) と [entity model](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/entity-model) を参照してください。

## ② 決めた資料を優先する

`ask` は既定で利用者が見られる資料すべてを探します。参照する資料の置き場所が決まっているなら、`.env` にそのフォルダーの URL を書くと、画面に「② 決めた資料を優先」が出ます。

```
WORKIQ_FOLDER_URL=https://contoso.sharepoint.com/sites/Sales/Shared%20Documents/提案書
```

同じ質問を ① と ② で投げると、参照先の顔ぶれが変わります。

やっているのは `ask` の `fileUrls` を埋めることだけです ([scoped.py](scoped.py))。モデルが引数を決めた後に差し替えるので、モデルの判断で渡し忘れることはありません。
→ [ask ツールの `fileUrls`](https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/mcp/tool-reference#ask)

注意点が 3 つあります。

- **限定ではありません。** Learn の記述も *"file URLs to use as context"* です。渡した資料に答えが無ければ、Work IQ の判断で外の資料も探します（実測で確認）。
- **絞れるのはファイルだけです。** メール・予定・チャットは ② でもこれまでどおり対象になります。種別ごとに止めるならテナント ポリシーの「パス アクセス」側です。
- **アクセス制御ではありません。** 利用者が見られる範囲は変わりません。

## 本番向けではありません

Work IQ MCP の呼び方を示すための最小構成です。そのまま本番で使うことは想定していません。

たとえば次の点は実装していません。

- セッションの永続化と共有
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

本リポジトリの内容は個人の見解であり、所属する組織の見解ではありません。
