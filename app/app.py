import aiohttp
from loguru import logger
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI(debug=True)


def build_forward_headers(request: Request) -> dict:
    """Forward client headers, filtering out hop-by-hop and host headers."""
    filtered = {"host", "connection", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}
    headers = {}
    for key, value in request.headers.items():
        if key.lower() not in filtered:
            headers[key] = value
    return headers


def build_response_headers(response: aiohttp.ClientResponse) -> dict:
    """转发上游响应头，过滤由代理自身重新计算或不应透传的头。"""
    filtered = {"transfer-encoding", "content-length", "connection", "keep-alive",
                "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade"}
    return {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in filtered
    }


@app.api_route("/mcp/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def proxy_mcp(request: Request, path: str):
    """Proxy requests to https://mcp.tavily.com/mcp/"""
    query_string = str(request.query_params)
    target_url = f"https://mcp.tavily.com/mcp/{path}"
    if query_string:
        target_url += f"?{query_string}"

    forward_headers = build_forward_headers(request)
    if "content-type" not in {k.lower() for k in forward_headers}:
        forward_headers["Content-Type"] = "application/json"

    body = await request.body() if request.method != "GET" else None
    await log_request(request, target_url, body)

    session = aiohttp.ClientSession()
    response = None
    keep_stream_open = False
    try:
        response = await session.request(
            method=request.method,
            url=target_url,
            headers=forward_headers,
            data=body,
        )
        content_type = response.headers.get("content-type", "")
        is_sse = "text/event-stream" in content_type

        if is_sse:
            keep_stream_open = True

            async def stream_response():
                try:
                    async for chunk in response.content.iter_any():
                        yield chunk
                finally:
                    response.close()
                    await session.close()

            return StreamingResponse(
                stream_response(),
                status_code=response.status,
                headers=build_response_headers(response),
            )

        response_content = await response.read()
        log(response_content)
        return Response(
            content=response_content,
            status_code=response.status,
            headers=build_response_headers(response),
        )
    except Exception as e:
        logger.error(f"Error during forwarding request: {e.__str__()}")
        return Response("Internal server error", status_code=500)
    finally:
        if not keep_stream_open:
            if response is not None and not response.closed:
                response.close()
            if not session.closed:
                await session.close()


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def proxy(request: Request, path):
    headers = dict(request.headers)
    auth_key = headers.get("authorization")
    if not auth_key:
        raise HTTPException(status_code=401, detail="Authorization key is required")
    target_url = f"https://api.tavily.com/{path}"
    forward_headers = build_forward_headers(request)
    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(
                    method=request.method,
                    url=target_url,
                    headers=forward_headers,
                    data=await request.body() if request.method != "GET" else None,
            ) as response:
                response_content = await response.read()
                log(response_content)
                return Response(
                    content=response_content,
                    status_code=response.status,
                )
        except Exception as e:
            logger.error(f"Error during forwarding request: {e.__str__()}")
            return Response("Internal server error", status_code=500)


def log(bytes_string: bytes) -> None:
    try:
        json_string = bytes_string.decode("utf-8")
        logger.info(json_string)
    except Exception:
        logger.info(bytes_string)


async def log_request(request: Request, target_url: str, body: bytes | None) -> None:
    """Log request details for debugging."""
    logger.info(f"[REQUEST] {request.method} {target_url}")
    for key, value in request.headers.items():
        logger.info(f"[REQUEST HEADER] {key}: {value}")
    if body:
        try:
            logger.info(f"[REQUEST BODY] {body.decode('utf-8')}")
        except Exception:
            logger.info(f"[REQUEST BODY] {body}")
