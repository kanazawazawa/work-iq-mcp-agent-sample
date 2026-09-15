"""Work IQ MCP を使う最小の Web アプリ。

Entra ID でサインインし、その人の委任トークンで Work IQ MCP を呼ぶ。
SPA も On-Behalf-Of も使わない。サーバー側の認可コードフロー 1 本、
アプリ登録 1 つで完結する。

    uvicorn main:app --reload
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

import msal
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
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

# 直前の実行結果。POST の応答ではなくリダイレクト先の GET で見せるために一旦置く。
# cookie には入れない（回答は 4KB を超えうる）。
_results: dict[str, dict] = {}

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
            **extra,
        },
    )


@app.get("/")
async def index(request: Request):
    # 直前の結果は一度だけ見せて捨てる。再読込しても同じ質問が再実行されない。
    return _render(request, **_results.pop(request.session.get("sid", ""), {}))


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
    sid = request.session.get("sid", "")
    _caches.pop(sid, None)
    _results.pop(sid, None)
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
    if workiq_on and sid not in _caches:
        return RedirectResponse("/login", status_code=303)

    get_token = _token_provider(sid) if sid in _caches else None
    try:
        if scope == "folder":
            answer, tool_calls, references = await scoped.run_agent(
                get_token, question, use_learn=use_learn
            )
            scope_files = await scoped.files(get_token)
        else:
            answer, tool_calls, references = await workiq.run_agent(
                get_token, question, use_workiq=workiq_on, use_learn=use_learn
            )
            scope_files = []
    except Exception as exc:
        _results[sid] = {"question": question, "error": str(exc)}
        return RedirectResponse("/", status_code=303)

    _results[sid] = {
        "question": question,
        "answer_html": _md.render(answer),
        "references": references,
        "tool_calls": tool_calls,
        "scope_files": scope_files,
    }
    # 結果を直接返さず GET に逃がす。ブラウザの「フォームを再送信しますか」が出なくなる。
    return RedirectResponse("/", status_code=303)


@app.post("/reset")
async def reset(request: Request):
    _results.pop(request.session.get("sid", ""), None)
    return RedirectResponse("/", status_code=303)
