"""Account connections (OAuth) and uploaders for YouTube Shorts, Instagram Reels and Facebook Reels.

Tokens live in tokens/ (git-ignored, chmod 600). Passwords are never handled: each platform's own
login page issues a token that can only do what the requested scopes allow.
"""
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlencode

import requests

from .config import ROOT

TOKENS = ROOT / "tokens"
GRAPH = "https://graph.facebook.com/v23.0"
BASE_URL = "http://localhost:5050"
META_REDIRECT = f"{BASE_URL}/auth/meta/callback"
YT_REDIRECT = f"{BASE_URL}/auth/youtube/callback"
YT_SECRET = ROOT / "youtube_client_secret.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
META_SCOPES = ["pages_show_list", "pages_read_engagement", "pages_manage_posts",
               "instagram_basic", "instagram_content_publish", "business_management"]
_pending_state: Dict[str, object] = {}


def _env(k: str) -> str:
    from dotenv import dotenv_values

    return (dotenv_values(ROOT / ".env").get(k) or os.getenv(k) or "").strip()


def _save(name: str, data: dict) -> None:
    TOKENS.mkdir(exist_ok=True)
    p = TOKENS / name
    p.write_text(json.dumps(data, indent=2))
    p.chmod(0o600)


def _load(name: str) -> Optional[dict]:
    p = TOKENS / name
    return json.loads(p.read_text()) if p.exists() else None


def _graph_err(r: requests.Response) -> str:
    try:
        e = r.json().get("error", {})
        return f"{e.get('message', r.text[:300])} (code {e.get('code')})"
    except ValueError:
        return r.text[:300]


# ------------------------------------------------------------------ connection status
def connections() -> dict:
    meta, yt = _load("meta.json"), _load("youtube.json")
    return {
        "meta": {"ready": bool(_env("META_APP_ID") and _env("META_APP_SECRET")), "connected": bool(meta),
                 "page": meta and meta.get("page_name"), "instagram": meta and meta.get("ig_username"),
                 "r2": bool(_env("R2_PUBLIC_URL"))},
        "youtube": {"ready": YT_SECRET.exists(), "connected": bool(yt), "channel": yt and yt.get("channel")},
    }


# ------------------------------------------------------------------ Meta (Facebook + Instagram)
def meta_login_url() -> str:
    state = secrets.token_urlsafe(16)
    _pending_state[state] = "meta"
    return "https://www.facebook.com/v23.0/dialog/oauth?" + urlencode({
        "client_id": _env("META_APP_ID"), "redirect_uri": META_REDIRECT, "state": state,
        "scope": ",".join(META_SCOPES), "response_type": "code"})


def meta_callback(code: str, state: str) -> dict:
    if _pending_state.pop(state, None) != "meta":
        raise ValueError("Login expired or state mismatch. Click Connect again")
    app_id, secret = _env("META_APP_ID"), _env("META_APP_SECRET")
    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "client_id": app_id, "client_secret": secret, "redirect_uri": META_REDIRECT, "code": code}, timeout=30)
    if not r.ok:
        raise RuntimeError("Meta token exchange failed: " + _graph_err(r))
    short = r.json()["access_token"]
    r = requests.get(f"{GRAPH}/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id, "client_secret": secret,
        "fb_exchange_token": short}, timeout=30)
    user_token = r.json()["access_token"] if r.ok else short
    # Page tokens obtained from a long-lived user token don't expire.
    r = requests.get(f"{GRAPH}/me/accounts", params={
        "access_token": user_token, "fields": "id,name,access_token,instagram_business_account{id,username}"},
        timeout=30)
    if not r.ok:
        raise RuntimeError("Couldn't list your Facebook Pages: " + _graph_err(r))
    pages = r.json().get("data", [])
    if not pages:
        raise RuntimeError("No Facebook Page found. Create one and select it on the Meta login screen")
    page = next((p for p in pages if p.get("instagram_business_account")), pages[0])
    ig = page.get("instagram_business_account") or {}
    data = {"page_id": page["id"], "page_name": page["name"], "page_token": page["access_token"],
            "ig_user_id": ig.get("id"), "ig_username": ig.get("username"), "connected_at": int(time.time()),
            "other_pages": [p["name"] for p in pages if p["id"] != page["id"]]}
    _save("meta.json", data)
    return data


def post_facebook(video: Path, caption: str) -> str:
    m = _load("meta.json") or {}
    if not m:
        raise RuntimeError("Not connected yet. Click 'Connect Instagram & Facebook' at the top, then Retry")
    page, tok = m["page_id"], m["page_token"]
    r = requests.post(f"{GRAPH}/{page}/video_reels", data={"upload_phase": "start", "access_token": tok}, timeout=30)
    if not r.ok:
        raise RuntimeError("Facebook start failed: " + _graph_err(r))
    vid = r.json()["video_id"]
    size = video.stat().st_size
    with video.open("rb") as f:
        r = requests.post(f"https://rupload.facebook.com/video-upload/v23.0/{vid}", data=f, timeout=600,
                          headers={"Authorization": f"OAuth {tok}", "offset": "0", "file_size": str(size)})
    if not r.ok:
        raise RuntimeError("Facebook upload failed: " + _graph_err(r))
    r = requests.post(f"{GRAPH}/{page}/video_reels", timeout=60, data={
        "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED", "description": caption,
        "access_token": tok})
    if not r.ok or not r.json().get("success", True):
        raise RuntimeError("Facebook publish failed: " + _graph_err(r))
    for _ in range(60):  # wait for processing so errors surface here, not silently later
        s = requests.get(f"{GRAPH}/{vid}", params={"fields": "status", "access_token": tok}, timeout=30).json()
        st = s.get("status", {})
        if st.get("video_status") == "error" or st.get("processing_phase", {}).get("status") == "error":
            raise RuntimeError(f"Facebook processing error: {json.dumps(st)[:300]}")
        if st.get("publishing_phase", {}).get("status") == "complete":
            break
        time.sleep(5)
    return f"https://www.facebook.com/reel/{vid}"


def _r2():
    import boto3

    return boto3.client("s3", endpoint_url=f"https://{_env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
                        aws_access_key_id=_env("R2_ACCESS_KEY_ID"),
                        aws_secret_access_key=_env("R2_SECRET_ACCESS_KEY"), region_name="auto")


def post_instagram(video: Path, caption: str) -> str:
    m = _load("meta.json") or {}
    if not m:
        raise RuntimeError("Not connected yet. Click 'Connect Instagram & Facebook' at the top, then Retry")
    if not m.get("ig_user_id"):
        raise RuntimeError(f"Page '{m.get('page_name')}' has no Instagram linked. Link it in the Page settings, "
                           "then Disconnect + Connect again")
    if not _env("R2_PUBLIC_URL"):
        raise RuntimeError("R2 not set up (Instagram needs a public video URL)")
    ig, tok = m["ig_user_id"], m["page_token"]
    key = f"clips/{uuid.uuid4().hex}.mp4"
    s3, bucket = _r2(), _env("R2_BUCKET")
    s3.upload_file(str(video), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    try:
        url = f"{_env('R2_PUBLIC_URL').rstrip('/')}/{key}"
        r = requests.post(f"{GRAPH}/{ig}/media", timeout=60, data={
            "media_type": "REELS", "video_url": url, "caption": caption[:2200], "share_to_feed": "true",
            "access_token": tok})
        if not r.ok:
            raise RuntimeError("Instagram container failed: " + _graph_err(r))
        cid = r.json()["id"]
        for _ in range(90):
            s = requests.get(f"{GRAPH}/{cid}", params={"fields": "status_code,status", "access_token": tok},
                             timeout=30).json()
            if s.get("status_code") == "FINISHED":
                break
            if s.get("status_code") in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Instagram processing failed: {s.get('status')}")
            time.sleep(5)
        else:
            raise RuntimeError("Instagram processing timed out")
        r = requests.post(f"{GRAPH}/{ig}/media_publish", data={"creation_id": cid, "access_token": tok}, timeout=60)
        if not r.ok:
            raise RuntimeError("Instagram publish failed: " + _graph_err(r))
        mid = r.json()["id"]
        link = requests.get(f"{GRAPH}/{mid}", params={"fields": "permalink", "access_token": tok}, timeout=30).json()
        return link.get("permalink") or f"https://www.instagram.com/{m.get('ig_username', '')}"
    finally:
        s3.delete_object(Bucket=bucket, Key=key)  # Instagram keeps its own copy


# ------------------------------------------------------------------ YouTube
def _yt_flow():
    from google_auth_oauthlib.flow import Flow

    return Flow.from_client_secrets_file(str(YT_SECRET), scopes=YT_SCOPES, redirect_uri=YT_REDIRECT)


def youtube_login_url() -> str:
    flow = _yt_flow()
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _pending_state[state] = getattr(flow, "code_verifier", None) or "yt"
    return url


def youtube_callback(code: str, state: str) -> dict:
    if state not in _pending_state:
        raise ValueError("Login expired or state mismatch. Click Connect again")
    verifier = _pending_state.pop(state)
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # localhost redirect is plain http
    flow = _yt_flow()
    if isinstance(verifier, str) and verifier != "yt":
        flow.code_verifier = verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    from googleapiclient.discovery import build

    ch = build("youtube", "v3", credentials=creds).channels().list(part="snippet", mine=True).execute()
    name = ch["items"][0]["snippet"]["title"] if ch.get("items") else "(no channel on this account)"
    data = json.loads(creds.to_json())
    data["channel"] = name
    _save("youtube.json", data)
    return data


def post_youtube(video: Path, title: str, description: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    d = _load("youtube.json")
    if not d:
        raise RuntimeError("YouTube not connected. Add youtube_client_secret.json, click 'Connect YouTube', then Retry")
    creds = Credentials.from_authorized_user_info(d, YT_SCOPES)
    if not creds.valid:
        creds.refresh(Request())
        _save("youtube.json", {**json.loads(creds.to_json()), "channel": d.get("channel")})
    privacy = _env("YOUTUBE_PRIVACY") or "private"  # API uploads are private anyway until Google's audit
    body = {"snippet": {"title": title[:100], "description": description[:5000], "categoryId": "20",
                        "tags": ["gta6", "gta 6", "gta vi", "gaming", "shorts"]},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False}}
    req = build("youtube", "v3", credentials=creds).videos().insert(
        part="snippet,status", body=body, media_body=MediaFileUpload(str(video), mimetype="video/mp4", resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return f"https://youtube.com/shorts/{resp['id']}"
