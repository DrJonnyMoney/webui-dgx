import os
import sys
import asyncio
import subprocess
import time
import logging
import re
from aiohttp import web, ClientSession, WSMsgType

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("openwebui-proxy")

# Configuration
NB_PREFIX = os.environ.get('NB_PREFIX', '')  # e.g. "/notebook/jonny/openui"
WEBUI_PORT = 8080      # Open WebUI's default port
PROXY_PORT = 8888      # External port
MAX_REQUEST_SIZE = 1024 * 1024 * 1024  # 1GB
HTTP_TIMEOUT = 600     # 10 minutes
STARTUP_TIMEOUT = 120  # Seconds to wait for Open WebUI to start
HEARTBEAT_INTERVAL = 30  # WebSocket heartbeat interval
webui_process = None

def normalize_path(path):
    """Strip NB_PREFIX from incoming path and ensure it starts with a slash."""
    original = path
    if path.startswith(NB_PREFIX):
        path = path[len(NB_PREFIX):]
    if not path.startswith('/'):
        path = '/' + path
    logger.debug(f"normalize_path: original={original} normalized={path}")
    return path

def start_openwebui():
    """Start Open WebUI as a subprocess."""
    env = os.environ.copy()
    env['PORT'] = str(WEBUI_PORT)
    env['DATA_DIR'] = "/home/jovyan/.open-webui"
    cmd = ["open-webui", "serve"]
    logger.info(f"Starting Open WebUI with command: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr, env=env)

async def is_openwebui_ready():
    """Poll the internal endpoint to see if Open WebUI is up."""
    try:
        async with ClientSession() as session:
            async with session.get(f'http://127.0.0.1:{WEBUI_PORT}/', timeout=2) as resp:
                logger.debug(f"is_openwebui_ready: status={resp.status}")
                return resp.status < 500
    except Exception as e:
        logger.debug(f"is_openwebui_ready exception: {e}")
        return False

def rewrite_html(body_text):
    """
    Rewrite the HTML to:
      1. Inject a <base> tag into the <head> so relative URLs resolve correctly.
      2. Rewrite asset URLs (href/src attributes) that start with "/" to include NB_PREFIX.
    """
    # 1. Inject a <base> tag.
    base_tag = f'<base href="{NB_PREFIX.rstrip("/")}/">'
    body_text = re.sub(r'(<head[^>]*>)', r'\1' + base_tag, body_text, count=1, flags=re.IGNORECASE)
    logger.debug(f"Injected base tag: {base_tag}")

    # 2. Rewrite asset URLs.
    def repl(match):
        attr = match.group(1)
        url = match.group(2)
        # Only rewrite if the URL doesn't already start with NB_PREFIX.
        if not url.startswith(NB_PREFIX):
            new_url = NB_PREFIX + url
            logger.debug(f"Rewriting {attr} URL: {url} -> {new_url}")
            return f'{attr}="{new_url}"'
        return match.group(0)

    body_text = re.sub(r'(href|src)="(/[^"]+)"', repl, body_text)
    return body_text

async def proxy_handler(request):
    """
    Forward requests to Open WebUI after stripping NB_PREFIX from the path.
    For HTML responses, rewrite the HTML so that asset URLs include NB_PREFIX.
    For static asset requests (manifest.json or any path starting with /_app),
    remove cookies—and for manifest.json also remove/override the Origin header—
    to avoid triggering auth redirects.
    """
    logger.info(f"Incoming request: {request.method} {request.path}")
    logger.debug(f"Incoming headers: {dict(request.headers)}")
    logger.debug(f"Incoming cookies: {request.cookies}")

    path = normalize_path(request.path)
    target_url = f'http://127.0.0.1:{WEBUI_PORT}{path}'
    params = request.rel_url.query
    if params:
        target_url += '?' + '&'.join(f"{k}={v}" for k, v in params.items())
    logger.info(f"Forwarding to target URL: {target_url}")

    # Determine if this is a static asset request that should bypass auth.
    static_asset = path == "/manifest.json" or path.startswith("/_app")
    if static_asset:
        forwarded_cookies = {}
        logger.debug("Static asset request detected; not forwarding cookies.")
    else:
        forwarded_cookies = request.cookies

    async with ClientSession() as session:
        headers = dict(request.headers)
        headers.pop('Content-Length', None)
        # For manifest.json, remove the Origin header to avoid CORS issues.
        if static_asset and path == "/manifest.json":
            headers.pop('Origin', None)
            logger.debug("Removed Origin header for manifest.json request.")
        data = await request.read() if request.method != 'GET' else None
        logger.debug(f"Forwarding headers: {headers}")
        logger.debug(f"Forwarding cookies: {forwarded_cookies}")

        try:
            async with session.request(
                request.method,
                target_url,
                headers=headers,
                data=data,
                allow_redirects=False,
                cookies=forwarded_cookies,
                timeout=HTTP_TIMEOUT
            ) as resp:
                logger.info(f"Received response: status={resp.status}")
                resp_headers = dict(resp.headers)
                logger.debug(f"Response headers: {resp_headers}")
                body = await resp.read()
                content_type = resp_headers.get('Content-Type', '')
                logger.info(f"Response content type: {content_type}")

                # If the response is HTML, rewrite it to inject base tag and adjust asset URLs.
                if 'text/html' in content_type:
                    try:
                        body_text = body.decode('utf-8')
                        body_text = rewrite_html(body_text)
                        body = body_text.encode('utf-8')
                    except Exception as e:
                        logger.error(f"Error rewriting HTML: {e}")

                response = web.Response(status=resp.status, body=body)
                for key, value in resp_headers.items():
                    if key.lower() not in ('content-length', 'transfer-encoding'):
                        response.headers[key] = value
                if 'Location' in response.headers:
                    location = response.headers['Location']
                    if location.startswith('/') and not location.startswith('//'):
                        new_location = NB_PREFIX + location
                        logger.debug(f"Rewriting Location header: {location} -> {new_location}")
                        response.headers['Location'] = new_location
                return response
        except Exception as e:
            error_msg = f"Proxy error: {str(e)}"
            logger.error(error_msg)
            if 'application/json' in request.headers.get('Accept', ''):
                return web.json_response({"error": error_msg}, status=500)
            else:
                return web.Response(status=500, text=error_msg)

async def websocket_proxy(request):
    """
    Proxy handler for WebSocket connections.
    """
    logger.info(f"Incoming WebSocket request: {request.path}")
    ws_path = normalize_path(request.path)
    ws_url = f"ws://127.0.0.1:{WEBUI_PORT}{ws_path}"
    logger.info(f"Forwarding WebSocket to: {ws_url}")
    ws_client = web.WebSocketResponse(heartbeat=HEARTBEAT_INTERVAL)
    await ws_client.prepare(request)
    client_to_server_task = None
    server_to_client_task = None

    try:
        async with ClientSession() as session:
            async with session.ws_connect(ws_url, timeout=60, heartbeat=HEARTBEAT_INTERVAL) as ws_server:
                async def forward_server_to_client():
                    try:
                        async for msg in ws_server:
                            if msg.type == WSMsgType.TEXT:
                                await ws_client.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await ws_client.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break
                    except Exception as e:
                        logger.error(f"Error forwarding server to client: {e}")
                async def forward_client_to_server():
                    try:
                        async for msg in ws_client:
                            if msg.type == WSMsgType.TEXT:
                                await ws_server.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await ws_server.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break
                    except Exception as e:
                        logger.error(f"Error forwarding client to server: {e}")
                server_to_client_task = asyncio.create_task(forward_server_to_client())
                client_to_server_task = asyncio.create_task(forward_client_to_server())
                done, pending = await asyncio.wait(
                    [server_to_client_task, client_to_server_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
    finally:
        for task in [client_to_server_task, server_to_client_task]:
            if task and not task.done():
                task.cancel()
        if not ws_client.closed:
            await ws_client.close()
    return ws_client

async def health_monitor():
    global webui_process
    while True:
        if webui_process.poll() is not None:
            logger.warning("Open WebUI process died, restarting...")
            webui_process = start_openwebui()
        await asyncio.sleep(30)

async def main():
    global webui_process
    webui_process = start_openwebui()
    logger.info("Waiting for Open WebUI to start...")
    start_time = time.time()
    while not await is_openwebui_ready():
        if time.time() - start_time > STARTUP_TIMEOUT:
            logger.error(f"Open WebUI failed to start within {STARTUP_TIMEOUT} seconds")
            webui_process.terminate()
            sys.exit(1)
        await asyncio.sleep(1)
    logger.info("Open WebUI started successfully")
    health_task = asyncio.create_task(health_monitor())
    app = web.Application(client_max_size=MAX_REQUEST_SIZE)
    app.router.add_routes([
        web.get(NB_PREFIX + '/ws', websocket_proxy),
        web.get('/ws', websocket_proxy),
        web.route('*', '/{path:.*}', proxy_handler),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PROXY_PORT)
    await site.start()
    logger.info(f"Proxy server running at http://0.0.0.0:{PROXY_PORT}{NB_PREFIX}/")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        health_task.cancel()
        if webui_process:
            webui_process.terminate()
            try:
                webui_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                webui_process.kill()
            logger.info("Open WebUI process terminated")
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user, shutting down")
