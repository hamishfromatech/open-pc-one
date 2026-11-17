# AmaaraOS brand

**AmaaraOS** by **The A-Tech Corporation** — the operating system that ships on
Open-PC One. Two names on purpose: AmaaraOS is the software identity, Open-PC One
is the device. Nothing user-visible should say "Open-PC One" as if it were the OS.

## Palette

Sampled from the A-Tech club banner (cream paper, near-black ink, one crimson
accent, a red rule across the bottom):

| Token | Hex | Where |
|---|---|---|
| Cream | `#F9F4EE` | Desktop field, `/etc/issue` background, wallpaper ground |
| Cream shade | `#EFE4DB` | Pinstripe / subtle separators |
| Ink | `#0B0908` | Wordmark, primary text |
| Amaara Red | `#D72E35` | Accent rules, the crimson line, alert states |
| Deep Red | `#A8202A` | Pressed/hover, LED "needs attention" |

Accent colour is used at *rule* thickness (6 px at 1920 wide), never as a fill
behind body text — the banner uses it exactly that way.

## Typography

The banner pairs a high-contrast display serif with an italic serif in crimson.
Stock Debian has no equivalent, so the wallpaper is drawn with **DejaVu Serif
Bold** (wordmark) and **DejaVu Serif Italic** (the crimson line). Both ship with
the image. Swapping in the licensed brand face later means changing the font list
at the top of `os/brand/make-wallpaper.py` and re-running it.

## Where the brand appears

| Surface | File |
|---|---|
| `/etc/os-release` (`ID=amaaraos`, `PRETTY_NAME`, `VENDOR_NAME`, `AMAARAOS_DEVICE`) | `os/overlay/etc/os-release` |
| `/etc/issue` (serial console banner, how to get in) | `os/overlay/etc/issue` |
| `amaara-version` (OS, slot, model, kernel in one screen) | `os/overlay/usr/local/bin/amaara-version` |
| Desktop wallpaper 1920×1080 | `os/overlay/usr/share/backgrounds/amaaraos/amaaraos-1920x1080.png` |
| XFCE backdrop default + cream fallback colour | `os/overlay/etc/xdg/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml` |
| Unit descriptions (shown by `systemctl status`, printed at boot) | `os/overlay/etc/systemd/system/*.service` |
| Image name/version (`amaaraos-one`, `1.0.0`) | `os/image/mkosi.conf` |
| Package list for the branded session (`xfce4-panel`, `xfdesktop4`) | `os/image/mkosi.conf` |

Regenerate the wallpaper:

```bash
python3 os/brand/make-wallpaper.py --out os/overlay/usr/share/backgrounds/amaaraos/amaaraos-1920x1080.png
```

`os/tests/test_branding.py` fails if the surfaces disagree — a version bump in
`mkosi.conf` without one in `/etc/os-release`, a unit that still describes the
old product, or a desktop default pointing at a file that is not there.

## Deliberately not renamed

The service, binary and path namespace stays `openpc` (`openpc-engine`,
`/var/lib/openpc`, `openpc.target`, the WSS frame protocol). Renaming internals to
`amaara` would touch every unit, path, session directory and test for zero user
benefit, and would break compatibility with the `a-coder-cli` integration and the
paired clients. Brand the surface, keep the plumbing.

## The OS surface

The bar, the dock and the first-boot screens are one design system, not three
projects. Everything below is a rule, not a preference.

### Geometry

| Token | Value | Used for |
|---|---|---|
| Radius (control) | 11 px | inputs, buttons |
| Radius (card) | 14–16 px | panels, choice cards |
| Radius (dock) | 20 px | the dock shell |
| Hairline | `#E4D6CA` at 1 px | every divider — never a shadow-heavy border |
| Elevation | `0 12px 32px -18px rgba(11,9,8,.18)` | one card level, nothing deeper |
| Top bar | 30 px tall, full width | wordmark, focused app, status, clock |
| Dock | 64 px tall, bottom 10 px, centred | 4 apps, 44 px icons |

### Type

| Role | Face | Size / weight |
|---|---|---|
| Wordmark | DejaVu Serif Bold | 13 px (shell) · 38–62 px (setup) |
| Headline | DejaVu Serif Bold, red italic accent | 30–44 px / 1.08, tracking −2% |
| UI text | Inter | 15 px / 1.5 |
| Meta, keys, clocks | Inter 12 px or monospace | letterspaced 0.1–0.24 em when uppercase |

The serif/crimson-italic pairing is the brand's only decoration. Everything else
is quiet, which is what makes it read as an OS rather than a website.

### Motion

120–160 ms, ease-out, transform only (`translateY(-1px)` on a primary button,
`opacity` on a status pip). No parallax, no bounce, no autoplaying anything. A
desktop that moves when you did not move it is a toy.

### Surfaces that must exist for this to feel like an OS

| Surface | State |
|---|---|
| First boot: welcome → software → installing → ready | built (`services/setup`) |
| Top bar + dock (`amaara-shell`) | built |
| Wallpaper, GTK defaults, icon theme | built |
| Window chrome (xfwm4 titlebars) | Adwaita default — **needs a branded xfwm4 theme** |
| Lock screen (`amaara-shell`) | built — full-screen cover; no password by design, and it says so |
| Settings app (`amaara-settings`) | built — 7 panes reading the running system |
| Notification centre (`org.freedesktop.Notifications` server) + OSD | built — clock opens it; apps notify it |
| App launcher / search (`amaara-shell`) | built — click the wordmark; reads real `.desktop` entries |
| Window snapping + workspace gesture | xfwm4 defaults only |

## Verified against upstream, not assumed

Checked with Context7 against the xfwm4 and xfconf upstream documentation. Three
things changed as a result:

1. **`top-active.png` / `top-inactive.png` were missing.** `SIDE_TOP` is a frame
   part in its own right (titleless and undecorated windows use it), not just the
   titlebar's top edge. xfwm4 fails silently when a part is absent and falls back
   to the previous theme, which looks like "my theme did not apply".
2. **The brand properties are now locked.** xfconf only honours `locked` in
   *system* files, and the attribute takes a user list where `*` means everyone
   (`unlocked` defaults to `*`). `<property name="theme" locked="*"/>` in
   `/etc/xdg/...` is what makes the window chrome and the wallpaper the OS rather
   than a preference a customer can break.
3. **The window-controls mapping is H = hide, S = stick.** Upstream's options
   table prints `H` = shade, but the stock layout `O|HMC` shows a minimise button
   and no shade button, so `CHM|` is close / minimise / maximise as intended.

Confirmed as-is: `themerc` keys (`button_offset`, `button_spacing`,
`full_width_title`, `maximized_offset`, `title_alignment`, `title_shadow_*`,
`title_vertical_offset_*`, `shadow_*`), the `<part>-<state>.png` naming, the four
button states (`active`, `inactive`, `prelight`, `pressed`), and that
`$XDG_CONFIG_DIRS/xfce4/xfconf/xfce-perchannel-xml/<channel>.xml` is the correct
place for system defaults (system files merge first, the user file wins unless the
property is locked).

`os/tests/test_theme.py` now encodes all of it: every frame part in both focus
states, every button state, the themerc keys, the lock attributes, and valid XML.

### Known benign warning

`xfce4-session` logs `Unable to launch "xfce4-panel"` once per boot. It is not a
fault: we ship no panel (amaara-shell draws the bar and dock), and xfce4-session's
own failsafe path starts a panel whenever it has to start a window manager itself.
`/etc/xdg/xfce4/xfce4-session.rc` keeps our default session free of it, but the
failsafe path is not configurable. Removing the line means starting xfwm4 before
xfce4-session, which is a session-ordering change to verify on hardware rather
than assert in a test.

### Open: the window title does not render

The frame, the traffic-light controls and the hairline all draw correctly; the
**title text does not**. Four hypotheses have been eliminated, each by experiment
on a live session rather than by reading code:

| Hypothesis | Test | Result |
|---|---|---|
| Fractional font size (`Inter 10.5`) breaks Pango | `title_font=Inter 10`, then `Sans 10` | still nothing — not the font |
| Text colour must be a hex value | `active_text_color=#0B0908` | still nothing |
| The stock title images are required | copied `Default`'s `title-*.png` in | still nothing |
| A PNG-only theme has no symbol table | title strips now emitted as **XPM** with `s <symbol>` colour fields, `themerc` refers to `active_text_color_2` | symbols exist, text still absent |

What IS proven: the theme's symbols are loadable (the `themerc` text-colour keys
are symbol assignments, and a PNG-only theme with no assignment at all is
identical in behaviour), the frame parts are all present and correctly named, and
the `Default` theme in the same session *does* draw text — so the session, the
font stack and the WM are fine, and the difference is inside the theme.

Next attempt should not start from a generator: copy `Default`'s XPM set, replace
its colours with ours, and confirm text renders before re-deriving the design. If
that works, the missing piece is visible by diffing the two colour tables.
