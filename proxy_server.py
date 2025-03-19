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
    # Special handling for static assets with specific paths
    if '/_app/immutable/' in path:
        # For these special paths, keep them exactly as they are
        # Just remove the notebook prefix if present
        if path.startswith(NB_PREFIX):
            return path[len(NB_PREFIX):]
        return path
        
    # Normal path handling
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
    
    # Authentication bypass - for testing
    env['WEBUI_AUTH'] = "False"  # Disable authentication completely
    env['ENABLE_SIGNUP'] = "True"
    env['ENABLE_LOGIN_FORM'] = "False" 
    env['DEFAULT_USER_ROLE'] = "admin"
    
    # Use the user-installed version of Open WebUI
    cmd = [
        os.path.expanduser("~/.local/bin/open-webui"), 
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
    path = request.path
    
    # Check if this is a request for a JavaScript module
    if '/_app/immutable/' in path:
        try:
            # Extract the file path part after /_app/immutable/
            file_path = path
            if file_path.startswith(NB_PREFIX):
                file_path = file_path[len(NB_PREFIX):]
            
            # Construct the local path to the file
            local_path = os.path.join(os.path.expanduser('~'), '.local/lib/python3.11/site-packages/open_webui/frontend/build', file_path)
            
            # Check if file exists
            if os.path.exists(local_path):
                with open(local_path, 'rb') as f:
                    content = f.read()
                
                # Determine content type
                content_type = None
                if path.endswith('.js'):
                    content_type = 'application/javascript'
                elif path.endswith('.css'):
                    content_type = 'text/css'
                elif path.endswith('.json'):
                    content_type = 'application/json'
                elif path.endswith('.svg'):
                    content_type = 'image/svg+xml'
                elif path.endswith('.png'):
                    content_type = 'image/png'
                elif path.endswith('.jpg') or path.endswith('.jpeg'):
                    content_type = 'image/jpeg'
                elif path.endswith('.ttf'):
                    content_type = 'font/ttf'
                else:
                    content_type = 'application/octet-stream'
                
                # Create the response
                response = web.Response(
                    body=content,
                    content_type=content_type
                )
                
                # Add CORS headers
                response.headers['Access-Control-Allow-Origin'] = '*'
                response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, DELETE'
                response.headers['Access-Control-Allow-Headers'] = 'Origin, X-Requested-With, Content-Type, Accept, Authorization'
                
                return response
            else:
                logger.warning(f"File not found: {local_path}")
        except Exception as e:
            logger.error(f"Error serving static file: {str(e)}")
    
    # For all other requests, use normal proxy
    # Normalize the path
    norm_path = normalize_path(path)
    target_url = f'http://127.0.0.1:{WEBUI_PORT}{norm_path}'
    
    # Add query parameters if present
    params = request.rel_url.query
    if params:
        target_url += '?' + '&'.join(f"{k}={v}" for k, v in params.items())
    
    logger.info(f"Proxying: {request.method} {request.path} -> {target_url}")  # Upgraded to INFO for debugging
    
    # Set proper content types for static assets
    content_type = None
    if path.endswith('.js') or '/_app/immutable/' in path and path.endswith('.js'):
        content_type = 'application/javascript'
    elif path.endswith('.css'):
        content_type = 'text/css'
    elif path.endswith('.json'):
        content_type = 'application/json'
    elif path.endswith('.ico'):
        content_type = 'image/x-icon'
    elif path.endswith('.png'):
        content_type = 'image/png'
    elif path.endswith('.jpg') or path.endswith('.jpeg'):
        content_type = 'image/jpeg'
    
    # Forward the request
    async with ClientSession() as session:
        # Copy headers but set the Host to match what Open WebUI expects
        headers = dict(request.headers)
        headers['Host'] = f'127.0.0.1:{WEBUI_PORT}'
        
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
                    body=body,
                    content_type=content_type or resp.headers.get('Content-Type')
                )
                
                # Add CORS headers
                response.headers['Access-Control-Allow-Origin'] = '*'
                response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS, PUT, DELETE'
                response.headers['Access-Control-Allow-Headers'] = 'Origin, X-Requested-With, Content-Type, Accept, Authorization'
                
                # Copy relevant headers from the response (except content-type which we handle separately)
                for key, value in resp.headers.items():
                    if key.lower() not in ('content-length', 'transfer-encoding', 'content-type'):
                        response.headers[key] = value
                
                # Fix location headers for redirects
                if 'Location' in response.headers:
                    location = response.headers['Location']
                    # If it's an absolute path but not a full URL, prepend the notebook prefix
                    if location.startswith('/') and not location.startswith('//'):
                        response.headers['Location'] = NB_PREFIX + location
                
                # If the request is for a JavaScript file but got HTML, it might be a routing issue
                if (path.endswith('.js') or '/_app/immutable/' in path) and b'<!DOCTYPE html>' in body[:100]:
                    # This suggests that server is returning the HTML index instead of the JS file
                    logger.warning(f"Received HTML for JavaScript request: {path}")
                    # Fix the path to include the notebook prefix for client-side imports
                    if path.startswith('/_app/'):
                        correct_path = f"{NB_PREFIX}{path}"
                        logger.info(f"Path should likely be: {correct_path}")
                
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
            
            async with session.ws_connect(
                ws_url, 
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
