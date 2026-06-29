---
name: browse
description: Use when the user wants to drive a real browser (a usr/bin/chromium instance) to navigate, read, or act on live web pages -- click, fill forms, search, screenshot, scrape rendered/JS content, or work through a logged-in session. Agentic browser: you observe (shot/snap) then act (click/type), turn by turn. Reaches JS-rendered and session-gated pages that scrape/forge can't.
---

Script `./browse`. Drives a real `/usr/bin/chromium` over CDP (Playwright). 
One long-lived chromium; each CLI call reconnects, acts, and exits -- so an agent loops observe -> decide -> act across invocations. 

## Loop

`start` once, then per turn: read state (`shot` / `snap`), decide, act (`click` / `type` / ...), repeat. `stop` when done.

- `./browse start [--port N] [--headless]` launch chromium (headful by default). Own profile in `.browse/profile` (persists logins between runs).
- `./browse open <url>` navigate active page (waits for domcontentloaded).
- `./browse snap` numbered map of in-view interactive elements -> `.browse/refs.json`; prints `@N  role  label  @x,y`.
- `./browse shot [-o f.png]` screenshot active page (default `.browse/shot.png`). Read the PNG to see it.
- `./browse click <target>` / `type <target> <text> [--enter]` / `key <keys>` / `scroll <dy>` / `back|forward|reload`.
- `./browse wait '<css>' [--timeout ms]` block until a selector appears (for JS pages).
- `./browse eval '<js>'` run JS in the page, print JSON result.
- `./browse info` active url+title. `./browse stop` kill chromium.

## Hybrid targeting (the point)

Every action `<target>` accepts three forms, one map:
- `@N` -- DOM ref from the last `snap`. Tries the captured CSS selector first, falls back to the captured center pixel if the DOM shifted.
- `'css'` -- a raw CSS selector.
- `x,y` -- a raw viewport pixel (vision-style click).

So DOM and vision back each other up: `snap` gives you both a selector and a coordinate per element. Quote selectors so the shell doesn't glob them.

## Reading pages

`shot` is for *seeing* (layout, images, visual state); `eval` is for *quoting exactly* (DOM text, structured data). Typical lead-paragraph / infobox pull:
`./browse eval "[...document.querySelectorAll('#mw-content-text p')].map(p=>p.innerText.trim()).find(t=>t.length>80)"`

## Notes

- "Active page" = last tab of the first context (the tab you last opened). Multi-tab targeting isn't modelled.
- Reads (`snap`/`shot`/`eval`) don't auto-wait; after a click that triggers a JS render, `wait '<css>'` before observing instead of guessing with sleep.
- `@N` refs are only valid until the page changes -- re-`snap` after navigation.
- stdout stays clean (page output / paths); status goes to stderr. Chromium stdout/stderr -> `.browse/chromium.log`.
