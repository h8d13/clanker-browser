#!/usr/bin/env python3
# Agentic browser control: drive a real /usr/bin/chromium over CDP (Playwright).
# Persistent across CLI invocations (one long-lived chromium, reconnect each
# call), so an agent observes (shot/snap) then acts (click/type/scroll) turn by
# turn. Hybrid action model: target an element by DOM ref (@N from `snap`), by
# CSS selector, or by raw "x,y" pixel coordinate.
# Usage:
#   ./browse start              launch chromium (headful) + CDP endpoint
#   ./browse open <url>
#   ./browse snap               numbered interactive-element map (-> .browse/refs.json)
#   ./browse shot [-o f.png]    screenshot active page
#   ./browse click @3 | 'css' | 412,260
#   ./browse type @5 "hello"
#   ./browse key Enter
#   ./browse wait '.results'        block until JS renders, then observe
#   ./browse stop
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

CHROMIUM = "/usr/bin/chromium"
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".browse")
STATE = os.path.join(STATE_DIR, "state.json")
REFS = os.path.join(STATE_DIR, "refs.json")
PROFILE = os.path.join(STATE_DIR, "profile")


def _log(msg):
	# Status to stderr so stdout stays clean for piping/captured output.
	print("[browse] {}".format(msg), file=sys.stderr)


def _load_state():
	try:
		with open(STATE) as f:
			return json.load(f)
	except FileNotFoundError:
		return None


def _cdp_ready(port):
	# /json/version is the CDP discovery endpoint; presence == chromium is up.
	try:
		with urllib.request.urlopen(
			"http://127.0.0.1:{}/json/version".format(port), timeout=1
		) as r:
			return json.load(r).get("webSocketDebuggerUrl")
	except Exception:
		return None


# --- session: launch + reconnect -------------------------------------------

def start(port=9222, headful=True):
	if _load_state() and _cdp_ready(port):
		_log("already running on :{}".format(port))
		return
	os.makedirs(PROFILE, exist_ok=True)
	args = [
		CHROMIUM,
		"--remote-debugging-port={}".format(port),
		"--user-data-dir={}".format(PROFILE),
		"--no-first-run", "--no-default-browser-check",
		"--remote-allow-origins=*",  # let CDP client attach
		# Force XWayland: native Wayland picks the Vulkan GPU path, which is
		# incompatible -> no compositor frame -> screenshot capture hangs.
		"--ozone-platform=x11",
	]
	if not headful:
		args.append("--headless=new")
	# Detached: survives this CLI process. stdout/stderr to a log file so a
	# crash leaves a trace instead of vanishing (no /dev/null silencing).
	logf = open(os.path.join(STATE_DIR, "chromium.log"), "ab")
	p = subprocess.Popen(args, stdout=logf, stderr=logf,
						  start_new_session=True)
	for _ in range(50):  # up to ~10s for CDP to bind
		if _cdp_ready(port):
			break
		time.sleep(0.2)
	else:
		_log("chromium did not expose CDP on :{} (see chromium.log)".format(port))
		sys.exit(1)
	with open(STATE, "w") as f:
		json.dump({"port": port, "pid": p.pid, "headful": headful}, f)
	_log("started pid={} cdp=:{}".format(p.pid, port))


def stop():
	st = _load_state()
	if not st:
		_log("not running")
		return
	try:
		os.killpg(os.getpgid(st["pid"]), 15)
	except ProcessLookupError:
		pass
	try:
		os.remove(STATE)
	except FileNotFoundError:
		pass
	_log("stopped pid={}".format(st["pid"]))


class _Session:
	# Reconnect to the live chromium for one command, then drop the Playwright
	# connection (the browser process keeps running). The "active" page is the
	# last page of the first context — matches the tab the user/agent last
	# opened. Enter/exit so callers get cleanup in one ordered place.
	def __enter__(self):
		st = _load_state()
		if not st or not _cdp_ready(st["port"]):
			_log("no live session; run `./browse start` first")
			sys.exit(1)
		self._pw = sync_playwright().start()
		self._browser = self._pw.chromium.connect_over_cdp(
			"http://127.0.0.1:{}".format(st["port"]))
		ctx = self._browser.contexts[0]
		self.page = ctx.pages[-1] if ctx.pages else ctx.new_page()
		return self

	def __exit__(self, *exc):
		# Close the CDP client only, never the browser.
		self._browser.close()
		self._pw.stop()


# --- observe ----------------------------------------------------------------

# Enumerate interactive elements in viewport. Returns one row per element with
# its center pixel (for coordinate clicks) and a best-effort unique selector
# (for DOM clicks) — the two action paths share one map.
_ENUMERATE_JS = r"""
() => {
  const SEL = 'a,button,input,textarea,select,summary,[role=button],' +
              '[role=link],[role=tab],[role=checkbox],[onclick],[contenteditable=""],' +
              '[contenteditable=true]';
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return null;
    if (r.bottom < 0 || r.right < 0 ||
        r.top > innerHeight || r.left > innerWidth) return null;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none' || s.opacity === '0')
      return null;
    return r;
  };
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && parts.length < 5) {
      let sel = el.tagName.toLowerCase();
      const p = el.parentElement;
      if (p) {
        const sibs = [...p.children].filter(c => c.tagName === el.tagName);
        if (sibs.length > 1) sel += ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
      }
      parts.unshift(sel);
      if (el.id) { parts[0] = '#' + CSS.escape(el.id); break; }
      el = p;
    }
    return parts.join(' > ');
  };
  const out = [];
  for (const el of document.querySelectorAll(SEL)) {
    const r = vis(el);
    if (!r) continue;
    const name = (el.getAttribute('aria-label') || el.value ||
      el.innerText || el.getAttribute('placeholder') ||
      el.getAttribute('title') || el.getAttribute('alt') || ''
    ).trim().replace(/\s+/g, ' ').slice(0, 80);
    out.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      name: name,
      x: Math.round(r.left + r.width / 2),
      y: Math.round(r.top + r.height / 2),
      selector: cssPath(el),
    });
  }
  return out;
}
"""


def snap():
	with _Session() as s:
		els = s.page.evaluate(_ENUMERATE_JS)
		refs = {}
		lines = []
		for i, e in enumerate(els):
			refs[str(i)] = {"selector": e["selector"], "x": e["x"], "y": e["y"]}
			label = e["name"] or "(no text)"
			tag = e["role"] or e["tag"]
			lines.append("@{:<3} {:<10} {:<30} @{},{}".format(
				i, tag[:10], label[:30], e["x"], e["y"]))
		with open(REFS, "w") as f:
			json.dump(refs, f)
		print("{}  {}".format(s.page.url, s.page.title()))
		print("\n".join(lines) if lines else "(no interactive elements in view)")


def shot(out=None):
	out = out or os.path.join(STATE_DIR, "shot.png")
	with _Session() as s:
		s.page.screenshot(path=out)
	print(out)


def info():
	with _Session() as s:
		print(json.dumps({"url": s.page.url, "title": s.page.title()}))


# --- act --------------------------------------------------------------------

def _resolve(target):
	# Map a target token to ("coord", (x,y)) or ("selector", css). @N reads the
	# ref map from the last `snap`; "x,y" is a raw pixel; anything else is CSS.
	if target.startswith("@"):
		try:
			with open(REFS) as f:
				ref = json.load(f)[target[1:]]
		except (FileNotFoundError, KeyError):
			_log("unknown ref {} (run `./browse snap` first)".format(target))
			sys.exit(1)
		# Prefer the selector (survives small layout shifts); fall back to coord.
		return ("selector", ref["selector"], (ref["x"], ref["y"]))
	if "," in target and all(p.strip().isdigit() for p in target.split(",", 1)):
		x, y = (int(p) for p in target.split(",", 1))
		return ("coord", None, (x, y))
	return ("selector", target, None)


def open_url(url):
	if "://" not in url:
		url = "https://" + url
	with _Session() as s:
		s.page.goto(url, wait_until="domcontentloaded")
		print("{}  {}".format(s.page.url, s.page.title()))


def click(target):
	kind, selector, coord = _resolve(target)
	with _Session() as s:
		if kind == "coord":
			s.page.mouse.click(*coord)
		else:
			try:
				s.page.click(selector, timeout=4000)
			except Exception:
				if coord:  # ref selector went stale; use its captured pixel
					_log("selector miss, clicking coord {}".format(coord))
					s.page.mouse.click(*coord)
				else:
					raise
	_log("clicked {}".format(target))


def type_text(target, text, submit=False):
	kind, selector, coord = _resolve(target)
	with _Session() as s:
		if kind == "coord":
			s.page.mouse.click(*coord)
			s.page.keyboard.type(text)
		else:
			s.page.fill(selector, text)
		if submit:
			s.page.keyboard.press("Enter")
	_log("typed into {}".format(target))


def key(keys):
	with _Session() as s:
		s.page.keyboard.press(keys)
	_log("key {}".format(keys))


def scroll(dy):
	with _Session() as s:
		s.page.mouse.wheel(0, dy)
	_log("scroll {}".format(dy))


def wait(selector, timeout=10000):
	# Block until a selector appears, for JS pages that render after load.
	# The read commands (snap/shot/eval) have nothing to auto-wait on, so this
	# replaces blind `sleep` between an action and the next observation.
	with _Session() as s:
		try:
			# "attached" (in DOM), not the default "visible": wait gates the
			# read commands, which read the DOM. A present-but-hidden node
			# (e.g. an empty layout <p>) should satisfy it, not time out.
			s.page.wait_for_selector(selector, state="attached", timeout=timeout)
		except PWTimeout:
			_log("timeout: {} not present after {}ms".format(selector, timeout))
			sys.exit(1)
	_log("present {}".format(selector))


def nav(action):
	with _Session() as s:
		getattr(s.page, "go_back" if action == "back" else
				"go_forward" if action == "forward" else "reload")()
		print("{}  {}".format(s.page.url, s.page.title()))


def run_js(expr):
	with _Session() as s:
		print(json.dumps(s.page.evaluate(expr)))


# --- CLI --------------------------------------------------------------------

def main():
	os.makedirs(STATE_DIR, exist_ok=True)
	ap = argparse.ArgumentParser(prog="browse", description=__doc__)
	sub = ap.add_subparsers(dest="cmd", required=True)

	p = sub.add_parser("start", help="launch chromium + CDP")
	p.add_argument("--port", type=int, default=9222)
	p.add_argument("--headless", action="store_true")
	sub.add_parser("stop", help="kill chromium")
	sub.add_parser("snap", help="numbered interactive-element map")
	sub.add_parser("info", help="active page url+title")
	sub.add_parser("back")
	sub.add_parser("forward")
	sub.add_parser("reload")

	p = sub.add_parser("shot", help="screenshot active page")
	p.add_argument("-o", "--out")

	p = sub.add_parser("open", help="navigate active page to url")
	p.add_argument("url")

	p = sub.add_parser("click", help="@ref | css | x,y")
	p.add_argument("target")

	p = sub.add_parser("type", help="@ref|css|x,y  text")
	p.add_argument("target")
	p.add_argument("text")
	p.add_argument("--enter", action="store_true", help="press Enter after")

	p = sub.add_parser("key", help="press a key, e.g. Enter, Escape, Control+L")
	p.add_argument("keys")

	p = sub.add_parser("scroll", help="wheel delta (px, +down/-up)")
	p.add_argument("dy", type=int)

	p = sub.add_parser("wait", help="block until a css selector is present")
	p.add_argument("selector")
	p.add_argument("--timeout", type=int, default=10000, help="ms (default 10000)")

	p = sub.add_parser("eval", help="evaluate JS in active page")
	p.add_argument("expr")

	a = ap.parse_args()
	if a.cmd == "start":
		start(port=a.port, headful=not a.headless)
	elif a.cmd == "stop":
		stop()
	elif a.cmd == "snap":
		snap()
	elif a.cmd == "shot":
		shot(out=a.out)
	elif a.cmd == "info":
		info()
	elif a.cmd == "open":
		open_url(a.url)
	elif a.cmd == "click":
		click(a.target)
	elif a.cmd == "type":
		type_text(a.target, a.text, submit=a.enter)
	elif a.cmd == "key":
		key(a.keys)
	elif a.cmd == "scroll":
		scroll(a.dy)
	elif a.cmd == "wait":
		wait(a.selector, timeout=a.timeout)
	elif a.cmd in ("back", "forward", "reload"):
		nav(a.cmd)
	elif a.cmd == "eval":
		run_js(a.expr)


if __name__ == "__main__":
	main()
