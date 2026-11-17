"""AmaaraOS setup UI: the design layer, kept away from the state machine.

Design intent: an operating system's first screen, not a form on a web page. A
brand column that never moves, a step rail that always says where you are, one
headline, one decision per screen. Cream paper, ink, and one crimson action — the
same tokens as the wallpaper, so the handover to the desktop is continuous.

Everything is server-rendered and the CSS is inline: the box may have no network
at all, and the first screen must not wait for a font CDN that will never answer.
"""

from __future__ import annotations

import html

CREAM = "#F9F4EE"
CREAM_2 = "#F2E9E0"
INK = "#0B0908"
RED = "#D72E35"
RED_DEEP = "#A8202A"
MUTED = "#6E6058"
HAIR = "#E4D6CA"
WHITE = "#FFFDF9"

CSS = f"""
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%}}
body{{
  background:{CREAM};color:{INK};
  font:15px/1.5 "Inter","Segoe UI",-apple-system,system-ui,"DejaVu Sans",sans-serif;
  -webkit-font-smoothing:antialiased;
}}
.shell{{display:grid;grid-template-columns:minmax(360px,38%) 1fr;min-height:100vh}}

/* ---- brand column ---------------------------------------------------- */
.brand{{
  position:relative;overflow:hidden;padding:64px 56px;
  background:{WHITE};
  border-right:1px solid {HAIR};
  display:flex;flex-direction:column;justify-content:space-between;
}}
.brand::after{{
  content:"";position:absolute;inset:0;pointer-events:none;
  background-image:repeating-linear-gradient(64deg,rgba(228,214,202,.55) 0 1px,transparent 1px 30px);
}}
.masthead{{display:flex;align-items:center;gap:14px;position:relative;z-index:1}}
.rule{{width:46px;height:6px;background:{RED}}}
.kicker{{font-size:11px;letter-spacing:.24em;text-transform:uppercase;font-weight:600}}
.wordmark{{
  position:relative;z-index:1;font-family:"DejaVu Serif",Georgia,serif;font-weight:700;
  font-size:clamp(38px,4.6vw,62px);line-height:1;letter-spacing:-.02em;
}}
.wordmark em{{
  display:block;margin-top:14px;color:{RED};font-size:clamp(17px,1.6vw,23px);
  font-style:italic;font-weight:400;letter-spacing:0;
}}
.brandfoot{{position:relative;z-index:1}}
.specs{{font:12px/1.9 ui-monospace,"DejaVu Sans Mono",Menlo,monospace;color:{MUTED}}}
.specs b{{color:{INK};font-weight:600}}
.stamp{{
  margin-top:26px;display:inline-flex;align-items:center;gap:8px;
  font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:{MUTED};
}}
.stamp .dot{{width:7px;height:7px;border-radius:50%;background:{RED}}}

/* ---- content column -------------------------------------------------- */
.content{{padding:64px 60px 96px;display:flex;flex-direction:column;justify-content:center;
  max-width:900px}}
/* The dock floats over the bottom of the screen; the setup column must not
   put its footer under it. */
.steps{{display:flex;gap:26px;margin-bottom:52px}}
.step{{display:flex;align-items:center;gap:9px;font-size:12px;color:{MUTED}}}
.step .num{{
  width:22px;height:22px;border-radius:50%;display:grid;place-items:center;
  border:1px solid {HAIR};font-size:11px;font-weight:600;background:{WHITE};
}}
.step.on{{color:{INK};font-weight:600}}
.step.on .num{{background:{RED};border-color:{RED};color:#fff}}
.step.done .num{{background:{INK};border-color:{INK};color:{CREAM}}}
h1{{
  font-family:"DejaVu Serif",Georgia,serif;font-weight:700;
  font-size:clamp(30px,3.1vw,44px);line-height:1.08;letter-spacing:-.02em;margin-bottom:12px;
}}
h1 em{{color:{RED};font-style:italic;font-weight:400}}
.sub{{color:{MUTED};font-size:16px;max-width:52ch;margin-bottom:34px}}
.panel{{
  background:{WHITE};border:1px solid {HAIR};border-radius:16px;padding:26px 28px;
  box-shadow:0 1px 2px rgba(11,9,8,.04),0 12px 32px -18px rgba(11,9,8,.18);
}}
.panel + .panel{{margin-top:14px}}
.label{{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:{MUTED};margin-bottom:10px}}
input[type=text]{{
  width:100%;padding:17px 18px;border-radius:11px;border:1px solid {HAIR};
  background:{CREAM};color:{INK};
  font:600 19px/1 ui-monospace,"DejaVu Sans Mono",Menlo,monospace;
  letter-spacing:.12em;text-transform:uppercase;
}}
input[type=text]:focus{{outline:none;border-color:{RED};box-shadow:0 0 0 3px rgba(215,46,53,.14)}}
.hint{{margin-top:12px;font-size:12.5px;color:{MUTED}}}
.hint code{{font-family:ui-monospace,Menlo,monospace;color:{INK};font-size:12px}}

/* ---- choices --------------------------------------------------------- */
.choice{{
  display:grid;grid-template-columns:auto 1fr auto;gap:16px;align-items:start;
  padding:20px 22px;border:1px solid {HAIR};border-radius:14px;background:{WHITE};
  cursor:pointer;transition:border-color .16s ease,box-shadow .16s ease,transform .16s ease;
}}
.choice + .choice{{margin-top:10px}}
.choice:hover{{border-color:#d3bfae;box-shadow:0 8px 26px -18px rgba(11,9,8,.35)}}
.choice.sel{{border-color:{RED};box-shadow:0 0 0 3px rgba(215,46,53,.10)}}
.choice.off{{opacity:.5;cursor:not-allowed}}
.choice input{{appearance:none;width:22px;height:22px;border-radius:7px;border:1.5px solid #d3bfae;
  background:{CREAM};margin-top:2px;position:relative;cursor:pointer;transition:all .14s ease}}
.choice input:checked{{background:{RED};border-color:{RED}}}
.choice input:checked::after{{
  content:"";position:absolute;left:6px;top:2.5px;width:6px;height:11px;
  border:solid #fff;border-width:0 2.5px 2.5px 0;transform:rotate(45deg);
}}
.choice input:disabled{{opacity:.45;cursor:not-allowed}}
.t{{font-weight:600;font-size:15.5px;margin-bottom:3px}}
.d{{color:{MUTED};font-size:13.5px;line-height:1.5}}
.tag{{
  font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:{MUTED};
  border:1px solid {HAIR};border-radius:999px;padding:5px 10px;white-space:nowrap;height:fit-content;
}}
.tag.now{{color:{RED_DEEP};border-color:#f0c3c1;background:#fdf1f0}}
.tag.inc{{color:{MUTED};background:{CREAM}}}

/* ---- actions --------------------------------------------------------- */
.actions{{display:flex;align-items:center;gap:14px;margin-top:32px}}
button{{font:600 15px/1 inherit;border:0;border-radius:11px;padding:15px 26px;cursor:pointer;
  transition:transform .12s ease,background .14s ease,box-shadow .14s ease}}
button.primary{{background:{RED};color:#fff;box-shadow:0 8px 22px -14px rgba(215,46,53,.9)}}
button.primary:hover{{background:{RED_DEEP};transform:translateY(-1px)}}
button.ghost{{background:transparent;color:{INK};border:1px solid {HAIR}}}
button.ghost:hover{{background:{CREAM_2}}}
button.link{{background:none;color:{MUTED};padding:8px 0;font-weight:500;text-decoration:underline;
  text-underline-offset:3px}}
button.link:hover{{color:{INK}}}
.spacer{{flex:1}}

/* ---- progress -------------------------------------------------------- */
.row{{display:grid;grid-template-columns:16px 1fr auto;gap:12px;align-items:start;
  padding:14px 0;border-bottom:1px solid #f0e7de;font-size:13.5px}}
.row:last-child{{border-bottom:0}}
.pip{{width:10px;height:10px;border-radius:50%;margin-top:5px}}
.pip.ok{{background:#2f7d4f}}
.pip.bad{{background:{RED}}}
.pip.wait{{background:#d8c8bb;animation:pulse 1.6s ease-in-out infinite}}
@keyframes pulse{{0%,100%{{opacity:.35}}50%{{opacity:1}}}}
.row .meta{{font:11.5px/1.6 ui-monospace,Menlo,monospace;color:{MUTED};margin-top:2px}}
.row .at{{font:11.5px/1.6 ui-monospace,Menlo,monospace;color:{MUTED}}}
.err{{
  background:#fdf1f0;border:1px solid #f0c3c1;color:#8a1c22;border-radius:12px;
  padding:13px 16px;font-size:13.5px;margin-bottom:20px;
}}
.err b{{display:block;margin-bottom:2px}}
.bar{{height:5px;border-radius:999px;background:{CREAM_2};overflow:hidden;margin:6px 0 24px}}
.bar i{{display:block;height:100%;background:{RED};transition:width .4s ease}}
.footbar{{margin-top:44px;padding-top:20px;display:flex;align-items:center;gap:10px;
  border-top:6px solid {RED};font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:{MUTED}}}
.badge{{display:inline-flex;align-items:center;gap:8px;background:{CREAM_2};border-radius:10px;
  padding:9px 13px;font:12.5px/1 ui-monospace,Menlo,monospace;color:{INK}}}
@media (max-width:900px){{
  .shell{{grid-template-columns:1fr}}
  .brand{{border-right:0;border-bottom:1px solid {HAIR};padding:36px 28px}}
  .content{{padding:36px 28px}}
  .steps{{gap:16px;margin-bottom:30px}}
}}
"""

STEPS = ("License", "Software", "Ready")


def document(title: str, content: str, *, refresh: int = 0) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">{meta}
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} — AmaaraOS</title><style>{CSS}</style></head><body>
{content}
</body></html>""".encode()


def brand_column(device: str, batch: str, checks: int, phase: str) -> str:
    return f"""
  <aside class="brand">
    <div class="masthead"><div class="rule"></div><div class="kicker">The A-Tech Corporation</div></div>
    <div class="wordmark">AmaaraOS<em>the operating system for your own AI</em></div>
    <div class="brandfoot">
      <div class="specs">
        device <b>{html.escape(device)}</b><br>
        batch <b>{html.escape(batch or "—")}</b><br>
        factory checks <b>{checks}</b>
      </div>
      <div class="stamp"><span class="dot"></span> AmaaraOS 1.0 · {html.escape(phase)}</div>
    </div>
  </aside>"""


def stepper(current: str) -> str:
    items = []
    for index, name in enumerate(STEPS, start=1):
        state = ""
        if name == current:
            state = " on"
        elif STEPS.index(name) < STEPS.index(current):
            state = " done"
        items.append(f'<div class="step{state}"><span class="num">{index}</span>{name}</div>')
    return f'<div class="steps">{"".join(items)}</div>'


def shell(content: str, *, title: str, device: str, batch: str, checks: int, phase: str, step: str,
          refresh: int = 0) -> bytes:
    body = f"""<div class="shell">
  {brand_column(device, batch, checks, phase)}
  <main class="content">
    {stepper(step)}
    {content}
    <div class="footbar">AmaaraOS 1.0.0 · Open-PC One · Built by The A-Tech Corporation</div>
  </main>
</div>"""
    return document(title, body, refresh=refresh)
