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
STARTUP_TIMEOUT = 120  # Max seconds to wait for Open WebUI to start (increased for initial DB setup)
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
    env['WEBUI_URL'] = f"http://localhost:{PROXY_PORT}{NB_PREFIX}"
    env['DATA_DIR'] = "/home/jovyan/.open-webui"
    
    # Use uv to run Open WebUI
    cmd = [
        "/root/.cargo/bin/uv", 
        "--python", 
        "3.11", 
        "run", 
        "open-webui@latest", 
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
    except:
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
        # Copy headers but set the Host to match what Open WebUI expects
        headers = dict(request.headers)
        headers['Host'] = f'127.0.0.1:{WEBUI_PORT}'
        
        # Add X-Forwarded headers to help Open WebUI understand it's behind a proxy
        headers['X-Forwarded-Proto'] = request.headers.get('X-Forwarded-Proto', 'http')
        headers['X-Forwarded-For'] = request.headers.get('X-Forwarded-For', request.remote)
        headers['X-Forwarded-Host'] = request.headers.get('X-Forwarded-Host', request.host)
        headers['X-Forwarded-Prefix'] = NB_PREFIX
        
        # Remove headers that might cause issues
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
                
                # Fix location headers for redirects
                if 'Location' in response.headers:
                    location = response.headers['Location']
                    # If it's an absolute path but not a full URL, prepend the notebook prefix
                    if location.startswith('/') and not location.startswith('//'):
                        response.headers['Location'] = NB_PREFIX + location
                
                return response
        except Exception as e:
            error_msg = f"Proxy error: {str(e)}"
            logger.error(error_msg)
            
            # Return appropriate error response based on Accept header
            if 'application/json' in request.headers.get('Accept', ''):
                return web.json_response({"error": error_msg}, status=500)
            else:
                return web.Response(status=500, text=error_msg)

# WebSocket proxy handler
async def websocket_proxy(request):
    # Normalize the path
    ws_path = normalize_path(request.path)
    ws_url = f"ws://127.0.0.1:{WEBUI_PORT}{ws_path}"
    
    logger.debug(f"WebSocket request: {request.path} -> {ws_url}")
    
    # Prepare client WebSocket
    ws_client = web.WebSocketResponse(heartbeat=HEARTBEAT_INTERVAL)
    await ws_client.prepare(request)
    
    # Two tasks for bidirectional communication
    client_to_server_task = None
    server_to_client_task = None
    
    try:
        async with ClientSession() as session:
            logger.debug(f"Connecting to WebSocket: {ws_url}")
            
            # Add custom headers for the WebSocket connection
            headers = dict(request.headers)
            headers['X-Forwarded-Prefix'] = NB_PREFIX
            
            async with session.ws_connect(
                ws_url,
                headers=headers,
                timeout=60, 
                heartbeat=HEARTBEAT_INTERVAL
            ) as ws_server:
                logger.debug("WebSocket connected")
                
                # Server to client message forwarding
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
                
                # Client to server message forwarding
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
                
                # Start both forwarding tasks
                server_to_client_task = asyncio.create_task(forward_server_to_client())
                client_to_server_task = asyncio.create_task(forward_client_to_server())
                
                # Wait until either task completes (meaning a connection closed)
                done, pending = await asyncio.wait(
                    [server_to_client_task, client_to_server_task],
                    return_when=asyncio.FIRST_COMPLETED
                )
                
                # Cancel the pending task
                for task in pending:
                    task.cancel()
                
    except Exception as e:
        logger.error(f"WebSocket proxy error: {e}")
    finally:
        # Clean up tasks if they exist and weren't already cancelled
        for task in [client_to_server_task, server_to_client_task]:
            if task and not task.done():
                task.cancel()
        
        # Make sure the client WebSocket is closed
        if not ws_client.closed:
            await ws_client.close()
    
    return ws_client

# Monitor Open WebUI health and restart if needed
async def health_monitor():
    global webui_process
    
    while True:
        # Check if process is still running
        if webui_process.poll() is not None:
            logger.warning("Open WebUI process died, restarting...")
            webui_process = start_openwebui()
        
        # Sleep for 30 seconds between checks
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
    
    # Create and start health monitor
    health_task = asyncio.create_task(health_monitor())
    
    # Create app with routes
    app = web.Application(client_max_size=MAX_REQUEST_SIZE)
    
    # Set up WebSocket routes
    app.router.add_routes([
        web.get(NB_PREFIX + '/ws', websocket_proxy),
        web.get('/ws', websocket_proxy),
    ])

    # Set up a catch-all route for all other requests
    app.router.add_route('*', '/{path:.*}', proxy_handler)
    
    # Start the proxy server
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PROXY_PORT)
    await site.start()
    
    logger.info(f"Proxy server running at http://0.0.0.0:{PROXY_PORT}{NB_PREFIX}/")
    
    # Keep running until interrupted
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down...")
    finally:
        # Clean up
        health_task.cancel()
        
        if webui_process:
            webui_process.terminate()
            try:
                # Wait for process to terminate gracefully
                webui_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't terminate in time
                webui_process.kill()
            
            logger.info("Open WebUI process terminated")
        
        # Clean up the web server
        await runner.cleanup()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user, shutting down")
