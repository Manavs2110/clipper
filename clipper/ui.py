"""Local review UI: .venv/bin/python -m clipper.ui  ->  http://127.0.0.1:5050"""
import json
import os
import threading
import traceback
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

from . import config, publish
from .__main__ import run
from .safety import PLATFORMS, evaluate, platform_meta

app = Flask(__name__)
STATE = {"running": False, "log": []}
STATUSES = {"pending_review", "approved", "rejected"}
AUTO_POST = {"youtube", "instagram", "facebook"}  # twitch is manual (no upload API)
LOCK = threading.RLock()
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")  # OAuth redirect is http://localhost
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def cfg():
    return config.load(require_twitch=False)


def meta_path(name: str) -> Path:
    d = cfg().review_dir.resolve()
    p = (d / f"{name}.json").resolve()
    if p.parent != d or not p.exists():
        abort(404)
    return p


@app.get("/")
def index():
    return PAGE


@app.get("/api/clips")
def clips():
    c = cfg()
    out = []
    for j in sorted(c.review_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = json.loads(j.read_text())
        if "platforms" not in meta:
            continue
        missing = [p for p in PLATFORMS if p not in meta["platforms"]]
        if missing and "safety" in meta:  # platform added after this clip was made: evaluate it now
            fresh = platform_meta(meta["title"], meta["credit"], {p: evaluate(meta["safety"], p) for p in PLATFORMS})
            for p in missing:
                meta["platforms"][p] = fresh[p]
            j.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        if all(p in meta["platforms"] for p in PLATFORMS):
            out.append({"name": j.stem, **meta})
    return jsonify(clips=out, twitch_ready=bool(c.twitch_client_id and c.twitch_client_secret))


def _edit(name: str, fn):
    """Locked read-modify-write of one clip's JSON."""
    with LOCK:
        p = meta_path(name)
        meta = json.loads(p.read_text())
        result = fn(meta)
        p.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
        return meta, result


def _publish_job(name: str, plat: str) -> None:
    meta = json.loads(meta_path(name).read_text())
    e = meta["platforms"][plat]
    video = meta_path(name).with_suffix(".mp4")
    try:
        if plat == "youtube":
            url = publish.post_youtube(video, e["title"], e["caption"])
        elif plat == "instagram":
            url = publish.post_instagram(video, e["caption"])
        else:
            url = publish.post_facebook(video, e["caption"])
        res = {"state": "published", "url": url, "at": int(__import__("time").time())}
        _edit(name, lambda m: m["platforms"][plat].__setitem__("publish", res))
        _archive(name)
        return
    except Exception as ex:
        traceback.print_exc()
        res = {"state": "failed", "error": str(ex)[:500]}
    _edit(name, lambda m: m["platforms"][plat].__setitem__("publish", res))


def _archive(name: str) -> None:
    """Copy a posted clip to the Google Drive folder once."""
    if not publish.connections()["gdrive"]["connected"]:
        return
    meta = json.loads(meta_path(name).read_text())
    if meta.get("drive", {}).get("url"):
        return
    try:
        url = publish.save_to_drive(meta_path(name).with_suffix(".mp4"), publish.drive_description(meta))
        res = {"url": url}
    except Exception as ex:
        traceback.print_exc()
        res = {"error": str(ex)[:300]}
    _edit(name, lambda m: m.__setitem__("drive", res))


def _start_publish(name: str, plat: str) -> bool:
    def mark(m):
        pub = m["platforms"][plat].get("publish", {})
        if pub.get("state") in ("publishing", "published"):
            return False
        m["platforms"][plat]["publish"] = {"state": "publishing"}
        return True

    _, started = _edit(name, mark)
    if started:
        threading.Thread(target=_publish_job, args=(name, plat), daemon=True).start()
    return started


@app.post("/api/clips/<name>")
def update(name):
    body = request.get_json(force=True)
    plat = body.get("platform")
    if plat not in PLATFORMS:
        abort(400)

    def apply(meta):
        entry = meta["platforms"][plat]
        for k in ("title", "caption"):
            if isinstance(body.get(k), str):
                entry[k] = body[k]
        if body.get("status") in STATUSES:
            if entry["verdict"] == "block":
                return "blocked by safety filter"
            entry["status"] = body["status"]
        return None

    meta, err = _edit(name, apply)
    if err:
        return jsonify(ok=False, error=err), 409
    entry = meta["platforms"][plat]
    if plat in AUTO_POST and entry["status"] == "approved" and (body.get("status") == "approved" or body.get("action") == "post"):
        _start_publish(name, plat)
    return jsonify(ok=True)


# ---------------------------------------------------------------- account connections
@app.get("/api/connections")
def connections():
    return jsonify(publish.connections())


def _done(msg: str):
    return f"""<!doctype html><meta charset="utf-8"><body style="font:16px system-ui;background:#0e0f13;color:#e8e9ee;padding:40px">
<p>{msg}</p><p><a style="color:#ff8a5b" href="/">Back to Clipper</a></p><script>setTimeout(()=>location="/",2500)</script>"""


@app.get("/auth/meta")
def auth_meta():
    if not publish.connections()["meta"]["ready"]:
        return _done("Add META_APP_ID and META_APP_SECRET to .env first."), 400
    return redirect(publish.meta_login_url())


@app.get("/auth/meta/callback")
def auth_meta_cb():
    if request.args.get("error"):
        return _done(f"Meta login cancelled: {request.args.get('error_description', request.args['error'])}")
    try:
        d = publish.meta_callback(request.args["code"], request.args.get("state", ""))
    except Exception as e:
        traceback.print_exc()
        return _done(f"Couldn't connect Meta: {e}"), 400
    ig = f"Instagram @{d['ig_username']}" if d.get("ig_username") else "no Instagram linked to this Page"
    return _done(f"Connected ✓ Facebook Page <b>{d['page_name']}</b> · {ig}")


@app.get("/auth/youtube")
def auth_youtube():
    if not publish.YT_SECRET.exists():
        return _done("Put youtube_client_secret.json in the Clipper folder first (see SETUP.md step 2)."), 400
    return redirect(publish.youtube_login_url())


@app.get("/auth/youtube/callback")
def auth_youtube_cb():
    if request.args.get("error"):
        return _done(f"YouTube login cancelled: {request.args['error']}")
    try:
        d = publish.youtube_callback(request.args["code"], request.args.get("state", ""))
    except Exception as e:
        traceback.print_exc()
        return _done(f"Couldn't connect YouTube: {e}"), 400
    return _done(f"Connected ✓ YouTube channel <b>{d['channel']}</b>")


@app.get("/auth/gdrive")
def auth_gdrive():
    if not publish.connections()["gdrive"]["ready"]:
        return _done("Add GDRIVE_FOLDER_ID to .env and youtube_client_secret.json first."), 400
    return redirect(publish.gdrive_login_url())


@app.get("/auth/gdrive/callback")
def auth_gdrive_cb():
    if request.args.get("error"):
        return _done(f"Google Drive login cancelled: {request.args['error']}")
    try:
        d = publish.gdrive_callback(request.args["code"], request.args.get("state", ""))
    except Exception as e:
        traceback.print_exc()
        return _done(f"Couldn't connect Google Drive: {e}"), 400
    threading.Thread(target=_backfill_drive, daemon=True).start()
    return _done(f"Connected ✓ Google Drive <b>{d['account']}</b> → folder <b>{d['folder']}</b>. "
                 "Already-posted clips are being copied there now.")


def _backfill_drive() -> None:
    for j in cfg().review_dir.glob("*.json"):
        m = json.loads(j.read_text())
        posted = any(v.get("publish", {}).get("state") == "published" and v.get("verdict") != "block"
                     for v in m.get("platforms", {}).values())
        if posted:
            _archive(j.stem)


@app.post("/api/disconnect/<svc>")
def disconnect(svc):
    f = {"meta": "meta.json", "youtube": "youtube.json", "gdrive": "gdrive.json"}.get(svc)
    if not f:
        abort(400)
    (publish.TOKENS / f).unlink(missing_ok=True)
    return jsonify(ok=True)


@app.get("/video/<name>")
def video(name):
    meta_path(name)
    return send_from_directory(cfg().review_dir, f"{name}.mp4", as_attachment=bool(request.args.get("dl")))


@app.post("/api/fetch")
def fetch():
    if STATE["running"]:
        return jsonify(ok=False, error="already running"), 409
    b = request.get_json(force=True) or {}
    STATE.update(running=True, log=[])

    def job():
        try:
            run(int(b.get("hours", 24)), int(b.get("limit", 5)), int(b.get("min_views", 50)),
                str(b.get("lang", "en")), log=STATE["log"].append)
            STATE["log"].append("Done.")
        except BaseException as e:  # config.load raises SystemExit on missing keys
            STATE["log"].append(f"Error: {e}")
            traceback.print_exc()
        finally:
            STATE["running"] = False

    threading.Thread(target=job, daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    return jsonify(running=STATE["running"], log=STATE["log"][-200:])


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Clipper</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0e0f13;--card:#181a21;--line:#2a2d37;--text:#e8e9ee;--dim:#8b8fa0;--accent:#ff8a5b;--ok:#3ecf8e;--warn:#f5c04a;--bad:#ff5b6e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,system-ui,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:14px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:2;flex-wrap:wrap}
h1{font-size:18px;margin:0}h1 span{color:var(--accent)}
.controls{display:flex;gap:8px;align-items:center;margin-left:auto;flex-wrap:wrap}
label{color:var(--dim);font-size:12px}input,select,textarea{background:var(--card);color:var(--text);border:1px solid var(--line);border-radius:6px;padding:6px 8px;font:inherit}
input[type=number]{width:64px}button,.btn{border:0;border-radius:6px;padding:7px 14px;font-weight:600;cursor:pointer;background:var(--line);color:var(--text);text-decoration:none;text-align:center;font-size:14px}
button.primary{background:var(--accent);color:#111}button.ok{background:var(--ok);color:#111}button.bad{background:var(--bad);color:#111}button:disabled{opacity:.5;cursor:default}
nav.plat{display:flex;gap:8px;padding:14px 24px 0}nav.plat button{padding:10px 18px;font-size:15px;background:var(--card);color:var(--dim);border:1px solid var(--line)}
nav.plat button.on{color:var(--text);border-color:var(--accent)}nav.plat .n{margin-left:6px;font-size:12px;color:var(--accent)}
.tabs{display:flex;gap:4px;padding:12px 24px 0}.tabs button{background:none;color:var(--dim)}.tabs button.on{background:var(--card);color:var(--text)}
.note{margin:12px 24px 0;padding:10px 14px;border:1px solid var(--line);border-radius:8px;color:var(--dim);font-size:13px}.note b{color:var(--text)}
#banner{border-color:var(--accent);display:none}
#log{margin:12px 24px 0;background:#000;color:#9fe;font:12px ui-monospace,monospace;padding:10px;border-radius:8px;max-height:160px;overflow:auto;display:none;white-space:pre-wrap}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,340px));gap:16px;padding:16px 24px 40px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
video{width:100%;max-height:70vh;aspect-ratio:9/16;object-fit:contain;background:#000;display:block}
.body{padding:12px;display:flex;flex-direction:column;gap:8px}.meta{color:var(--dim);font-size:12px}.meta a{color:var(--accent)}
textarea{width:100%;resize:vertical}input.t{width:100%}.row{display:flex;gap:8px}.row>*{flex:1}
.verdict{border-radius:6px;padding:8px 10px;font-size:12px}.verdict b{display:block;margin-bottom:2px;font-size:13px}
.verdict.pass{background:rgba(62,207,142,.12);color:var(--ok)}.verdict.warn{background:rgba(245,192,74,.12);color:var(--warn)}.verdict.block{background:rgba(255,91,110,.12);color:var(--bad)}
.verdict ul{margin:4px 0 0;padding-left:16px;color:var(--text)}
.empty{color:var(--dim);padding:40px 24px}
#conn{display:flex;gap:8px;flex-wrap:wrap;padding:12px 24px 0}#conn .c{display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font-size:13px}
#conn .dot{width:8px;height:8px;border-radius:50%;background:var(--bad)}#conn .dot.on{background:var(--ok)}#conn a,#conn button{font-size:12px;padding:4px 10px}
.pub{font-size:12px;border-radius:6px;padding:6px 8px;background:var(--line)}.pub.published{background:rgba(62,207,142,.15);color:var(--ok)}.pub.failed{background:rgba(255,91,110,.15);color:var(--bad)}.pub a{color:inherit;font-weight:600}
</style></head><body>
<header><h1>Clip<span>per</span></h1>
<div class="controls">
<label>hours <input id="hours" type="number" value="24"></label>
<label>max clips <input id="limit" type="number" value="5"></label>
<label>min views <input id="minv" type="number" value="50"></label>
<label>lang <select id="lang"><option value="en">en</option><option value="">any</option></select></label>
<button class="primary" id="fetch">Fetch new clips</button></div></header>
<div id="banner" class="note"><b>Twitch keys missing.</b> Put <code>TWITCH_CLIENT_ID</code> and <code>TWITCH_CLIENT_SECRET</code> in <code>.env</code>.</div>
<div id="conn"></div>
<pre id="log"></pre>
<nav class="plat"></nav>
<div class="note" id="pnote"></div>
<div class="tabs"></div>
<div id="grid"></div>
<script>
const P={youtube:{label:"YouTube Shorts",note:"<b>Filters:</b> nudity & slurs block · suggestive, swearing in first 7s, music warn · over 3 min blocks. Shorts can't be age-restricted, so suggestive clips risk a strike."},
 instagram:{label:"Instagram Reels",note:"<b>Filters:</b> nudity & slurs block · suggestive and music warn · over 3 min blocks. Swearing is OK."},
 facebook:{label:"Facebook Reels",note:"<b>Filters:</b> same as Instagram (Meta rules) · must be 3–90 seconds. Posts to your Facebook <b>Page</b>, not a personal profile."},
 twitch:{label:"Twitch",note:"<b>Manual upload:</b> Twitch has no upload API. Download approved clips and upload them at dashboard.twitch.tv → Content → Video Producer → Upload. <b>Filters:</b> nudity & slurs block · suggestive and music warn (DMCA)."}};
const S=[["pending_review","To review"],["approved","Approved"],["rejected","Rejected"],["blocked","Blocked by filter"]];
let plat=localStorage.getItem("plat")||"youtube", tab="pending_review", data=[];
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let conn=null, pollT=null;
async function load(){const [r,c]=await Promise.all([fetch("/api/clips").then(r=>r.json()),fetch("/api/connections").then(r=>r.json())]);
 data=r.clips;conn=c;$("#banner").style.display=r.twitch_ready?"none":"block";drawConn();draw();
 const busy=data.some(x=>Object.values(x.platforms).some(p=>p.publish?.state==="publishing"));
 clearTimeout(pollT);if(busy)pollT=setTimeout(load,4000)}
function drawConn(){const m=conn.meta,y=conn.youtube;
 const yt=y.connected?`YouTube: <b>${esc(y.channel)}</b> <button data-dc="youtube">Disconnect</button>`
  :y.ready?`YouTube <a class="btn primary" href="/auth/youtube">Connect YouTube</a>`:`YouTube: add <code>youtube_client_secret.json</code> (SETUP.md step 2)`;
 const mt=m.connected?`Facebook: <b>${esc(m.page)}</b> · Instagram: <b>${m.instagram?"@"+esc(m.instagram):"not linked"}</b>${m.r2?"":" · R2 missing"} <button data-dc="meta">Disconnect</button>`
  :m.ready?`Instagram + Facebook <a class="btn primary" href="/auth/meta">Connect Instagram &amp; Facebook</a>`:`Meta: add META_APP_ID/SECRET to .env`;
 const g=conn.gdrive, gd=g.connected?`Drive: <b>${esc(g.folder)}</b> (${esc(g.account)}) <button data-dc="gdrive">Disconnect</button>`
  :g.ready?`Google Drive <a class="btn primary" href="/auth/gdrive">Connect Google Drive</a>`:`Drive: add GDRIVE_FOLDER_ID to .env`;
 $("#conn").innerHTML=`<div class="c"><span class="dot ${y.connected?"on":""}"></span>${yt}</div><div class="c"><span class="dot ${m.connected?"on":""}"></span>${mt}</div><div class="c"><span class="dot ${g.connected?"on":""}"></span>${gd}</div>`}
$("#conn").addEventListener("click",async e=>{const b=e.target.closest("button[data-dc]");if(!b)return;await fetch("/api/disconnect/"+b.dataset.dc,{method:"POST"});load()});
function draw(){
 $("nav.plat").innerHTML=Object.entries(P).map(([k,v])=>{const n=data.filter(c=>c.platforms[k].status==="pending_review").length;
  return `<button data-p="${k}" class="${k===plat?"on":""}">${v.label}${n?`<span class="n">${n}</span>`:""}</button>`}).join("");
 $("#pnote").innerHTML=P[plat].note;
 $(".tabs").innerHTML=S.map(([k,l])=>`<button data-s="${k}" class="${k===tab?"on":""}">${l} (${data.filter(c=>c.platforms[plat].status===k).length})</button>`).join("");
 const items=data.filter(c=>c.platforms[plat].status===tab);
 $("#grid").innerHTML=items.length?items.map(card).join(""):`<div class="empty">Nothing here.</div>`}
function pubHtml(p){const u=p.publish;if(!u||plat==="twitch")return"";
 if(u.state==="publishing")return`<div class="pub">⏳ Posting… (can take a few minutes)</div>`;
 if(u.state==="published")return`<div class="pub published">✓ Posted · <a href="${esc(u.url)}" target="_blank" rel="noopener">open</a>${plat==="youtube"?" (private until Google's audit)":""}</div>`;
 return`<div class="pub failed">✗ Post failed: ${esc(u.error)}<br><button data-a="post" style="margin-top:6px">Retry post</button></div>`}
function card(c){const p=c.platforms[plat],blocked=p.verdict==="block",v=encodeURIComponent(c.name);
 const head={pass:"✓ Passed all filters",warn:"⚠ Passed with warnings",block:"⛔ Blocked: can't be posted here"}[p.verdict];
 return `<div class="card" data-n="${esc(c.name)}"><video src="/video/${v}" controls preload="metadata" playsinline></video>
 <div class="body"><div class="verdict ${p.verdict}"><b>${head}</b>${p.reasons.length?`<ul>${p.reasons.map(r=>`<li>${esc(r)}</li>`).join("")}</ul>`:""}</div>
 <div class="meta">${esc(c.views_on_twitch)} Twitch views · <a href="${esc(c.source_url)}" target="_blank" rel="noopener">source</a> · ${esc(c.credit)}</div>
 ${blocked?"":`<label>Title</label><input class="t" value="${esc(p.title)}" maxlength="${plat==="twitch"?140:100}">
 ${plat==="twitch"?"":`<label>Caption</label><textarea class="c" rows="4">${esc(p.caption)}</textarea>`}
 ${pubHtml(p)}
 <div class="row">${p.status!=="approved"?`<button class="ok" data-a="approved">${plat==="twitch"?"Approve":"Approve &amp; post"}</button>`:""}
 ${p.status!=="rejected"?'<button class="bad" data-a="rejected">Reject</button>':""}
 ${p.status!=="pending_review"?'<button data-a="pending_review">Back to review</button>':""}</div>
 ${plat==="twitch"&&p.status==="approved"?`<a class="btn primary" href="/video/${v}?dl=1">Download for Twitch</a>`:""}`}
 </div></div>`}
$("nav.plat").addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;plat=b.dataset.p;try{localStorage.setItem("plat",plat)}catch{};draw()});
$(".tabs").addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;tab=b.dataset.s;draw()});
$("#grid").addEventListener("click",async e=>{const b=e.target.closest("button[data-a]");if(!b)return;const card=b.closest(".card");
 const body=b.dataset.a==="post"?{platform:plat,action:"post"}:{platform:plat,status:b.dataset.a,title:card.querySelector(".t")?.value};const c=card.querySelector(".c");if(c)body.caption=c.value;
 await fetch("/api/clips/"+encodeURIComponent(card.dataset.n),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});load()});
$("#fetch").onclick=async()=>{const r=await fetch("/api/fetch",{method:"POST",headers:{"Content-Type":"application/json"},
 body:JSON.stringify({hours:$("#hours").value,limit:$("#limit").value,min_views:$("#minv").value,lang:$("#lang").value})});if(r.ok)poll()};
async function poll(){$("#fetch").disabled=true;$("#log").style.display="block";
 const s=await fetch("/api/status").then(r=>r.json());$("#log").textContent=s.log.join("\n")||"Starting…";$("#log").scrollTop=1e9;
 load();if(s.running)setTimeout(poll,2000);else $("#fetch").disabled=false}
fetch("/api/status").then(r=>r.json()).then(s=>s.running&&poll());load();
</script></body></html>"""


def main():
    print("Clipper UI → http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)


if __name__ == "__main__":
    main()
