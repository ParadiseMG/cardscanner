"""Remote captcha-solving endpoint.

When the headless Playwright browser hits a captcha on the server,
it sends a push notification via ntfy and exposes the blocked page here.
Connor can open /captcha on his phone, see a live screenshot, and
click/type to solve it remotely.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response, JSONResponse

from app.services.browser_fetch import (
    get_captcha_state, get_captcha_screenshot,
    send_captcha_click, send_captcha_keys,
)

router = APIRouter(prefix="/captcha", tags=["captcha"])


@router.get("", response_class=HTMLResponse)
async def captcha_page():
    """Minimal page that shows the captcha screenshot and lets you interact."""
    return HTMLResponse("""<!DOCTYPE html>
<html><head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CardScanner — Captcha</title>
<style>
  body { font-family: system-ui; max-width: 600px; margin: 0 auto; padding: 16px;
         background: #1a1a2e; color: #e0e0e0; }
  h2 { color: #e94560; }
  #status { padding: 12px; border-radius: 8px; margin-bottom: 16px; }
  .waiting { background: #e94560; color: #fff; }
  .clear { background: #0f3460; }
  #captcha-img { width: 100%; cursor: crosshair; border: 2px solid #e94560;
                 border-radius: 4px; }
  button { background: #e94560; color: #fff; border: none; padding: 10px 20px;
           border-radius: 4px; font-size: 16px; cursor: pointer; margin: 4px; }
  button:active { opacity: 0.7; }
  #msg { margin-top: 8px; font-size: 14px; opacity: 0.7; }
</style>
</head><body>
<h2>Captcha Solver</h2>
<div id="status" class="clear">Checking...</div>
<img id="captcha-img" style="display:none" />
<div style="margin-top:12px">
  <button onclick="refresh()">Refresh Screenshot</button>
</div>
<div id="msg"></div>
<script>
const img = document.getElementById('captcha-img');
const status = document.getElementById('status');
const msg = document.getElementById('msg');

async function checkStatus() {
  const r = await fetch('/captcha/status');
  const d = await r.json();
  if (d.waiting) {
    status.textContent = 'Captcha waiting — click on the image to solve';
    status.className = 'waiting';
    refresh();
  } else {
    status.textContent = 'No captcha right now. This page auto-refreshes.';
    status.className = 'clear';
    img.style.display = 'none';
  }
}

async function refresh() {
  const r = await fetch('/captcha/screenshot?t=' + Date.now());
  if (r.ok) {
    const blob = await r.blob();
    img.src = URL.createObjectURL(blob);
    img.style.display = 'block';
  }
}

img.addEventListener('click', async (e) => {
  const rect = img.getBoundingClientRect();
  const scaleX = 1280 / rect.width;
  const scaleY = 1000 / rect.height;
  const x = Math.round((e.clientX - rect.left) * scaleX);
  const y = Math.round((e.clientY - rect.top) * scaleY);
  msg.textContent = 'Clicking (' + x + ', ' + y + ')...';
  await fetch('/captcha/click', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({x, y})
  });
  setTimeout(refresh, 1000);
});

checkStatus();
setInterval(checkStatus, 5000);
</script>
</body></html>""")


@router.get("/status")
async def captcha_status():
    return JSONResponse(get_captcha_state())


@router.get("/screenshot")
async def captcha_screenshot():
    data = await get_captcha_screenshot()
    if data is None:
        return JSONResponse({"error": "no captcha page active"}, status_code=404)
    return Response(content=data, media_type="image/png")


@router.post("/click")
async def captcha_click(body: dict):
    x = int(body.get("x", 0))
    y = int(body.get("y", 0))
    ok = await send_captcha_click(x, y)
    return JSONResponse({"ok": ok})


@router.post("/type")
async def captcha_type(body: dict):
    text = body.get("text", "")
    ok = await send_captcha_keys(text)
    return JSONResponse({"ok": ok})
