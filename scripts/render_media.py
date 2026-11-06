"""Render the README images from the recorded run in examples/local-run/ (real Claude
answers, real discord.py, fake Discord). Nothing here is typed in by hand: every message
and every webhook result comes from the JSON files the run wrote.

    python scripts/render_media.py   (needs: pip install playwright && playwright install chromium)
"""
import html
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "examples" / "local-run"
MEDIA = ROOT / "media"
FONT = "'gg sans', 'Noto Sans', -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
COLORS = {"maya": "#f47fff", "priya": "#3ba55c", "omar": "#faa61a", "sam_mod": "#5865f2", "jonah": "#00b0f4"}
STYLE = f"""<style>
 body{{margin:0;background:#313338;font-family:{FONT};color:#dbdee1}}
 .wrap{{display:inline-block;width:1000px;padding:22px 26px}}
 h1{{font-size:20px;margin:0 0 4px;color:#f2f3f5}} .sub{{color:#949ba4;font-size:13.5px;margin-bottom:16px;line-height:1.45}}
 .msg{{display:flex;gap:14px;margin:0 0 16px}}
 .av{{width:40px;height:40px;border-radius:50%;flex:none;display:flex;align-items:center;justify-content:center;
   font-weight:700;color:#fff;font-size:16px}}
 .name{{font-weight:600;font-size:15px}} .bot{{background:#5865f2;color:#fff;font-size:10px;border-radius:3px;
   padding:1px 4px;margin-left:5px;vertical-align:2px}} .time{{color:#949ba4;font-size:12px;margin-left:6px}}
 .cmd{{color:#949ba4;font-size:13px;margin-bottom:3px}} .cmd b{{color:#c9cdfb;font-weight:500}}
 .text{{font-size:15px;line-height:1.42;white-space:pre-wrap}} .small{{color:#949ba4;font-size:12.5px;display:block;margin-top:5px}}
 .eph{{color:#949ba4;font-size:12px;margin-top:4px}}
 .row{{display:flex;gap:10px;align-items:baseline;background:#2b2d31;border-radius:6px;padding:7px 11px;margin:5px 0;font-size:13.5px}}
 .code{{font-family:Menlo,Consolas,monospace;font-size:12px;color:#b5bac1}}
 .ok{{color:#23a55a;font-weight:700;min-width:44px}} .bad{{color:#f23f43;font-weight:700;min-width:44px}}
 .tag{{margin-left:auto;color:#949ba4;font-size:12px;white-space:nowrap}}
</style>"""


def avatar(name):
    return f"<div class='av' style='background:{COLORS.get(name, '#80848e')}'>{name[0].upper()}</div>"


def bubble(t):
    opts = " ".join(f"<b>{k}:</b> {html.escape(str(v))}" for k, v in t["options"].items())
    reply = html.escape(t["reply"])
    if "\n\n-# Sources:" in reply:
        body, src = reply.split("\n\n-# Sources:", 1)
        reply = f"{body}<span class='small'>Sources:{src}</span>"
    eph = "<div class='eph'>Only you can see this</div>" if t["ephemeral"] else ""
    return (f"<div class='msg'>{avatar('P')}<div><div class='cmd'>{avatar_small(t['user'])} used "
            f"<b>/{t['command']}</b> {opts}</div><span class='name' style='color:#c9cdfb'>Pixelforge Helper</span>"
            f"<span class='bot'>APP</span><span class='time'>answered in {t['seconds']}s</span>"
            f"<div class='text'>{reply}</div>{eph}</div></div>")


def avatar_small(name):
    return f"<span style='color:{COLORS.get(name, '#dbdee1')};font-weight:600'>@{name}</span>"


def page(body):
    return f"<html><head><meta charset='utf-8'>{STYLE}</head><body><div class='wrap'>{body}</div></body></html>"


def chat(transcript, summary):
    asks = [t for t in transcript if t["command"] == "ask"]
    return page("<h1>#general: members ask, Claude answers from the server guide</h1>"
                f"<div class='sub'>Recorded {summary['date'][:10]}: the real discord.py bot answering slash commands, "
                f"replies written live by {summary['model']} with the sources it used. Discord itself is a local fake "
                "until the live test (no bot token yet).</div>" + "".join(bubble(t) for t in asks))


def flows(transcript, webhooks, summary, hooks):
    rows = []
    for w in webhooks:
        ok = w["http"] == 200
        resp = w["response"]
        change = resp.get("role_change")
        what = resp.get("error") or (f"{resp.get('status')}, role {change}" if change else resp.get("status"))
        rows.append(f"<div class='row'><span class='{'ok' if ok else 'bad'}'>{w['http']}</span>"
                    f"<span>{html.escape(w['label'])}</span><span class='tag code'>{html.escape(what)}</span></div>")
    cmds = [t for t in transcript if t["command"] != "ask"]
    crow = "".join(f"<div class='row'><span class='ok'>/{t['command']}</span><span>{avatar_small(t['user'])}: "
                   f"{html.escape(t['reply'].splitlines()[0][:95])}</span></div>" for t in cmds)
    hook_types = ", ".join(sorted({h["type"] for h in hooks}))
    roles = "; ".join(f"{k}: {', '.join(v) or 'no roles'}" for k, v in summary["roles_at_end"].items())
    return page("<h1>Paid roles, tickets and CRM hooks: what really happened</h1>"
                "<div class='sub'>The webhook server, running on a real port, was sent events signed exactly as Stripe, "
                "Lemon Squeezy and Calendly sign them. Forged and replayed events are refused before anything is read.</div>"
                + "".join(rows) + "<h1 style='margin-top:18px;font-size:17px'>Slash commands</h1>" + crow
                + f"<div class='sub' style='margin-top:14px'>Roles at the end: {html.escape(roles)}.<br>"
                f"{len(hooks)} hooks delivered to an n8n-style receiver, every signature checked "
                f"({'all valid' if summary['hook_signatures_ok'] else 'SOME INVALID'}): {html.escape(hook_types)}.</div>")


def main():
    transcript = json.loads((RUN / "transcript.json").read_text())
    webhooks = json.loads((RUN / "webhooks.json").read_text())
    summary = json.loads((RUN / "summary.json").read_text())
    hooks = json.loads((RUN / "hooks-received.json").read_text())
    MEDIA.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(device_scale_factor=2, viewport={"width": 1060, "height": 400})
        for name, doc in (("ask-answers.png", chat(transcript, summary)),
                          ("roles-and-webhooks.png", flows(transcript, webhooks, summary, hooks))):
            pg.set_content(doc)
            pg.locator(".wrap").screenshot(path=str(MEDIA / name))
            print("wrote", MEDIA / name)
        browser.close()


if __name__ == "__main__":
    main()
