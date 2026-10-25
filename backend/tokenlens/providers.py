"""Small shared protocol contract for every streaming AI entry point."""
from __future__ import annotations

import copy
import urllib.parse


def stream_request(endpoint, payload, protocol):
    body = copy.deepcopy(payload)
    if protocol == "gemini":
        parts = urllib.parse.urlsplit(endpoint)
        path = parts.path.replace(":generateContent", ":streamGenerateContent")
        query = dict(urllib.parse.parse_qsl(parts.query))
        query["alt"] = "sse"
        endpoint = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path,
                                           urllib.parse.urlencode(query), ""))
        body.pop("stream", None)
        body.pop("stream_options", None)
    else:
        body["stream"] = True
        if protocol == "openai":
            body["stream_options"] = {"include_usage": True}
        elif protocol == "responses":
            body.pop("stream_options", None)
    return endpoint, body


def warmup_request(endpoint, model, protocol):
    if protocol == "gemini":
        body = {"contents": [{"parts": [{"text": "hi"}]}],
                "generationConfig": {"maxOutputTokens": 1}}
    elif protocol == "responses":
        body = {"model": model, "input": "hi", "store": False,
                "max_output_tokens": 16}
    else:
        body = {"model": model, "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}]}
    return stream_request(endpoint, body, protocol)
