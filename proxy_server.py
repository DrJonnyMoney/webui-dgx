import os
import sys
import asyncio
import subprocess
import time
import logging
from aiohttp import web, ClientSession, WSMsgType

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("openwebui-proxy")

# Configuration
NB_PREFIX = os.environ.get('NB_PREFIX', '')
WEBUI_PORT = 8080  # Open WebUI's default port
PROXY_PORT = 8888  # Kubeflow accessible port
MAX_REQUEST_SIZE = 1024 * 1024 * 1024  # 1GB
HTTP_TIMEOUT = 600  # 10 minutes
STARTUP_TIMEOUT = 120  # Max seconds to wait for Open WebUI to start
HEARTBEAT_INTERVAL = 30  # WebSocket heartbeat interval in seconds
webui_process = None

# Common path normalization function
def normalize_path(path):
    """Normalize path by removing prefix and ensuring it starts with a slash"""
    if path.startswith(NB_PREFIX):
        path = path[len(NB_PREFIX):]
    if not path.startswith('/'):
        path = '/' + path
    return path

# Start Open WebUI as a subprocess
def start_openwebui():
    # Environment variables for Open WebUI
    env = os.environ.copy()
    env['PORT'] = str(WEBUI_PORT)
    env['DATA_DIR'] = "/home/jovyan/.open-webui"
    
    cmd = [
        "open-webui", 
        "serve"
    ]
    logger.info(f"Starting Open WebUI with command: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr, env=env)

# Check if Open WebUI is responding
async def is_openwebui_ready():
    """Check if Open WebUI is up and responding to requests"""
    try:
        async with ClientSession() as session:
            async with session.get(f'http://127.0.0.1:{WEBUI_PORT}/', timeout=2) as resp:
                return resp.status < 500
    except Exception:
        return False

# Proxy handler for HTTP requests
async def proxy_handler(request):
    # Normalize the path
    path = normalize_path(request.path)
    target_url = f'http://127.0.0.1:{WEBUI_PORT}{path}'
    
    # Add query parameters if present
    params = request.rel_url.query
    if params:
        target_url += '?' + '&'.join(f"{k}={v}" for k, v in params.items())
    
    logger.debug(f"Proxying: {request.method} {request.path} -> {target_url}")
    
    # Forward the request
    async with ClientSession() as session:
        # Copy headers from the original request
        headers = dict(request.headers)
        # -- Additional headers added to help bypass auth redirection --
        headers['X-Forwarded-For'] = request.remote
        headers['X-Forwarded-Proto'] = request.scheme
        headers['X-Forwarded-Host'] = request.host
        # Force the Origin to the internal host
        headers['Origin'] = f'http://127.0.0.1:{WEBUI_PORT}'
        # Set Host header for the internal request
        headers['Host'] = f'127.0.0.1:{WEBUI_PORT}'
        # Remove Content-Length to let the client session compute it properly
        headers.pop('Content-Length', None)
        
        # Read request data if not a GET request
        data = await request.read() if request.method != 'GET' else None
        
        try:
            async with session.request(
                request.method, 
                target_url, 
                headers=headers, 
                data=data, 
                allow_redirects=False,
                cookies=request.cookies,
                timeout=HTTP_TIMEOUT
            ) as resp:
                # Read the response body
                body = await resp.read()
                
                # Create response with the same status code and body
                response = web.Response(
                    status=resp.status,
                    body=body
                )
                
                # Copy relevant headers from the response
                for key, value in resp.headers.items():
                    if key.lower() not in ('content-length', 'transfer-encoding'):
                        response.headers[key] = value
                
                # Fix location headers for redirects if needed
                if 'Location' in response.headers:
                    location = response.headers['Location']
                    if location.startswith('/') and not location.startswith('//'):
                        response.headers['Location'] = NB_PREFIX + location
                
                return response
        except Exception as e:
            error_msg = f"Proxy error: {str(e)}"
            logger.error(error_msg)
            
            if 'application/json' in request.headers.get('Accept', ''):
                return web.json_response({"error": error_msg}, status=500)
            else:
                return web.Response(status=500, text=error_msg)

# WebSocket proxy handler (modified similarly if needed)
async def websocket_proxy(request):
    ws_path = normalize_path(request.path)
    ws_url = f"ws://127.0.0.1:{WEBUI_PORT}{ws_path}"
    
    logger.debug(f"WebSocket request: {request.path} -> {ws_url}")
    
    ws_client = web.WebSocketResponse(heartbeat=HEARTBEAT_INTERVAL)
    await ws_client.prepare(request)
    
    client_to_server_task = None
    server_to_client_task = None
    
    try:
        async with ClientSession() as session:
            logger.debug(f"Connecting to WebSocket: {ws_url}")
            async with session.ws_connect(
                ws_url, 
                timeout=60, 
                heartbeat=HEARTBEAT_INTERVAL
            ) as ws_server:
                logger.debug("WebSocket connected")
                
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

# Monitor Open WebUI health and restart if needed
async def health_monitor():
    global webui_process
    while True:
        if webui_process.poll() is not None:
            logger.warning("Open WebUI process died, restarting...")
            webui_process = start_openwebui()
        await asyncio.sleep(30)

async def main():
    global webui_process
    
    # Start Open WebUI
    webui_process = start_openwebui()
    
    # Wait for Open WebUI to start by polling
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
    
    # WebSocket routes and catch-all HTTP route
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
