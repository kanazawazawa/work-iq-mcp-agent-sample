"""Work IQ MCP を使う最小の Web アプリ。

Entra ID でサインインし、その人の委任トークンで Work IQ MCP を呼ぶ。
SPA も On-Behalf-Of も使わない。サーバー側の認可コードフロー 1 本、
アプリ登録 1 つで完結する。

    uvicorn main:app --reload
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import msal
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from starlette.middleware.sessions import SessionMiddleware

# workiq / scoped は取り込まれた時点で環境変数を読む。先に .env を反映させる。
load_dotenv()

import scoped  # noqa: E402
import workiq  # noqa: E402


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} が未設定です。.env.example を .env にコピーして設定してください。")
    return value


CLIENT_ID = _required("WORKIQ_CLIENT_ID")
CLIENT_SECRET = _required("WORKIQ_CLIENT_SECRET")
AUTHORITY = os.environ.get("WORKIQ_AUTHORITY", "https://login.microsoftonline.com/organizations")
REDIRECT_URI = os.environ.get("REDIRECT_URI", "http://localhost:8000/auth/callback")

# HTTPS なら認可コードを URL ではなく POST 本文で受け取る (RFC 9700 4.3.1)。
# これには cookie が SameSite=None である必要があり、それは Secure 必須なので
# http://localhost では成立しない。ローカルは query、デプロイ後は form_post。
USE_FORM_POST = REDIRECT_URI.startswith("https://")

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    same_site="none" if USE_FORM_POST else "lax",
    https_only=USE_FORM_POST,
)
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# 回答は LLM が書いた文字列なので HTML として描画する前に無害化する。
# html=False で生 HTML はエスケープされる。javascript:/data: リンクは
# markdown-it の既定のリンク検証が別途弾く。
_md = MarkdownIt("commonmark", {"html": False, "linkify": False})


def _external_link(self, tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer")
    return self.renderToken(tokens, idx, options, env)


_md.add_render_rule("link_open", _external_link)

# トークンはプロセス内に置く。cookie には識別子しか入れない。
# サンプルなので再起動すると全員サインインし直しになる。
_caches: dict[str, msal.SerializableTokenCache] = {}

# クリックで入力欄に入る質問例。想定する情報源が違うものを並べている。
EXAMPLES = [
    ("公式ドキュメント", "Azure AI Search と Foundry IQ の違いは何？"),
    ("職場のデータ", "担い手不足に関する資料を探して要約して"),
    ("社内規程", "社内規定上、課長の日当はいくらでしたっけ？あとグリーン車利用については？"),
]


def _msal(cache: msal.SerializableTokenCache | None = None) -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_SECRET, token_cache=cache
    )


def _line(event: dict) -> str:
    """1 行 1 JSON で流す。ブラウザー側は改行で区切って読む。"""
    return json.dumps(event, ensure_ascii=False) + "\n"


def _result_html(done: dict, folder: dict | None) -> str:
    """結果の表示はサーバー側で組む。JS に同じ描画を書かないで済む。"""
    return templates.get_template("result.html").render(
        answer_html=_md.render(done["answer"]),
        tool_calls=done["tool_calls"],
        references=done["references"],
        scope=folder,
    )


def _token_provider(sid: str) -> workiq.TokenProvider:
    """呼ばれるたびにキャッシュから有効なトークンを取り出す関数を返す。"""

    def get_token() -> str:
        cache = _caches.get(sid)
        accounts = _msal(cache).get_accounts() if cache else []
        if not accounts:
            raise RuntimeError("サインインが切れています。サインインし直してください。")

        result = _msal(cache).acquire_token_silent([workiq.SCOPE], account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError("トークンを更新できませんでした。サインインし直してください。")
        return result["access_token"]

    return get_token


def _render(request: Request, **extra):
    user = request.session.get("user")
    # 未接続なら M365 側は既定 OFF。一度実行したらそのときの状態を覚える。
    mcp = request.session.get("mcp") or {"workiq": bool(user), "learn": True}
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "examples": EXAMPLES,
            "use_workiq": mcp["workiq"],
            "use_learn": mcp["learn"],
            # WORKIQ_FOLDER_URL が未設定なら ② は出さない。
            "scoped": bool(scoped.FOLDER_URL),
            "folder_url": scoped.FOLDER_URL,
            "folder_name": scoped.FOLDER_NAME,
            **extra,
        },
    )


@app.get("/")
async def index(request: Request):
    return _render(request)


@app.get("/login")
async def login(request: Request):
    flow = _msal().initiate_auth_code_flow(
        [workiq.SCOPE],
        redirect_uri=REDIRECT_URI,
        response_mode="form_post" if USE_FORM_POST else "query",
    )
    request.session["flow"] = flow
    return RedirectResponse(flow["auth_uri"])


@app.api_route("/auth/callback", methods=["GET", "POST"])
async def auth_callback(request: Request):
    params = dict(await request.form()) if request.method == "POST" else dict(request.query_params)

    cache = msal.SerializableTokenCache()
    try:
        # state と PKCE の検証は MSAL がこの中で行う。
        # 古いコールバック URL を踏み直した場合などは state 不一致で ValueError。
        result = _msal(cache).acquire_token_by_auth_code_flow(request.session.pop("flow", {}), params)
    except ValueError:
        return _render(request, error="サインインの状態が確認できませんでした。もう一度サインインしてください。")

    if "access_token" not in result:
        return _render(request, error=f"{result.get('error')}: {result.get('error_description')}")

    sid = secrets.token_urlsafe(32)
    _caches[sid] = cache
    request.session["sid"] = sid
    request.session["user"] = result.get("id_token_claims", {}).get("name")
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    _caches.pop(request.session.get("sid", ""), None)
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.post("/ask")
async def ask(
    request: Request,
    question: str = Form(...),
    scope: str = Form("all"),
    # チェックボックスは OFF のとき送られてこないので、既定値がそのまま OFF になる。
    use_workiq: bool = Form(False),
    use_learn: bool = Form(False),
):
    # ② は Work IQ が前提なので、トグルの状態によらず入れる。
    workiq_on = use_workiq or scope == "folder"
    request.session["mcp"] = {"workiq": workiq_on, "learn": use_learn}
    sid = request.session.get("sid", "")

    async def events():
        if workiq_on and sid not in _caches:
            yield _line({"type": "login"})
            return

        get_token = _token_provider(sid) if sid in _caches else None
        stream = (
            scoped.run_agent_stream(get_token, question, use_learn=use_learn)
            if scope == "folder"
            else workiq.run_agent_stream(
                get_token, question, use_workiq=workiq_on, use_learn=use_learn
            )
        )

        folder = None
        try:
            async for event in stream:
                if event["type"] == "scope":
                    folder = event
                elif event["type"] == "done":
                    event = {"type": "done", "html": _result_html(event, folder)}
                yield _line(event)
        except Exception as exc:
            yield _line({"type": "error", "message": str(exc)})

    # 回答が出るまで 1 分以上かかるので、途中経過を流しながら返す。
    return StreamingResponse(events(), media_type="application/x-ndjson")
