# Async (asyncio) client

`AsyncMacpClient` is a [`grpc.aio`](https://grpc.github.io/grpc/python/grpc_asyncio.html)
client that sits alongside the sync `MacpClient`. Use it when your agent already
runs on an event loop: an async web framework, an async agent framework, or many
concurrent sessions in one process.

It mirrors the sync API method for method:

| Sync | Async |
|------|-------|
| `MacpClient` | `AsyncMacpClient` |
| `MacpStream` | `AsyncMacpStream` |
| `BaseSession` | `AsyncBaseSession` |
| `DecisionSession` | `AsyncDecisionSession` |
| `ProposalSession` | `AsyncProposalSession` |
| `TaskSession` | `AsyncTaskSession` |
| `HandoffSession` | `AsyncHandoffSession` |
| `QuorumSession` | `AsyncQuorumSession` |

The method names, arguments, defaults, validation, auth model and error types
are the same. The only differences:

- **Unary RPCs and session actions are coroutines.** That covers `initialize`,
  `send`, `get_session`, `list_sessions`, `start`, `propose`, `commit`, `cancel`
  and the rest, so you `await` them.
- **`watch_*` RPCs are async iterators.** You consume them with `async for`.
- **`open_stream()` returns an `AsyncMacpStream`.** Its `send`, `send_subscribe`,
  `read` and `close` are coroutines, and `responses()` is an async iterator.
- **Use `async with` for teardown.** Close the client with
  `async with AsyncMacpClient(...)` or `await client.close()`.

The sync and async helpers share one builder layer, so for the same arguments
they produce the same envelope. The unit suite checks this step by step for a
full flow in every mode.

## Quick start

This is the async version of the canonical
[`first_session.py`](https://github.com/multiagentcoordinationprotocol/multiagentcoordinationprotocol/blob/main/docs/quickstart.md)
quickstart. It also ships as `examples/async_first_session.py`. Start a local
runtime first:

```bash
docker run -p 50051:50051 -e MACP_ALLOW_INSECURE=1 -e MACP_MEMORY_ONLY=1 \
  ghcr.io/multiagentcoordinationprotocol/macp-runtime:0.9.0
```

```python
import asyncio

from macp.v1.envelope_pb2 import SessionState

from macp_sdk import AsyncDecisionSession, AsyncMacpClient, AuthConfig


async def main() -> None:
    async with AsyncMacpClient(
        target="127.0.0.1:50051",
        allow_insecure=True,  # local dev only
        auth=AuthConfig.for_dev_agent("coordinator"),
    ) as client:
        session = AsyncDecisionSession(client)
        await session.start(
            intent="pick a deployment plan",
            participants=["coordinator", "alice", "bob"],
            ttl_ms=60_000,
        )
        await session.propose("p1", "deploy v2.1", rationale="tests passed")
        await session.evaluate(
            "p1", "APPROVE", confidence=0.94, reason="low risk",
            sender="alice", auth=AuthConfig.for_dev_agent("alice"),
        )
        await session.vote(
            "p1", "APPROVE", reason="ship it",
            sender="bob", auth=AuthConfig.for_dev_agent("bob"),
        )

        winner = session.decision_projection.majority_winner()
        await session.commit(
            action="deployment.approved",
            authority_scope="release-management",
            reason=f"winner={winner}",
        )

        meta = (await session.metadata()).metadata
        print("winner:", winner)
        print("state:", SessionState.Name(meta.state), "mode:", meta.mode)


asyncio.run(main())
```

Output:

```
winner: p1
state: SESSION_STATE_RESOLVED mode: macp.mode.decision.v1
```

The client also logs a one-line insecure-channel warning to stderr, as the sync
client does.

## Create the client inside the loop

`grpc.aio` binds a channel to the event loop that is running when the channel
is created. Construct `AsyncMacpClient` inside your coroutine, as above, and not
at module import time. Use one client per loop.

`open_stream()` is a plain method rather than a coroutine. It starts a
background reader task, so it must also be called from a running loop.

## Streaming

`AsyncMacpStream` keeps the contract of `MacpStream` (see
[Streaming](streaming.md)):

```python
async with client.open_stream() as stream:
    await stream.send_subscribe(session_id)          # replay, then live
    async for envelope in stream.responses(timeout=5.0):
        print(envelope.message_type)
```

- **`read(timeout=...)`.** It raises `MacpTimeoutError` when nothing arrives in
  time. The stream stays open, because a background task owns the underlying
  `read()` and a timed-out wait doesn't cancel the RPC. It returns `None` once
  the stream has ended, and keeps returning `None` after that.
- **`cancel()`.** It aborts the call immediately. The next `read()` raises
  `MacpTransportError(code="CANCELLED")`, then reads return `None`.
- **`close()`.** It half-closes the request side. Leaving the `async with` block
  cancels the call and waits for the reader task.
- **`on_inline_error(callback)`.** It receives inline `MACPError` frames without
  closing the stream.

The watch RPCs are plain async iterators. Breaking out of the loop, or closing
the generator, cancels the server stream:

```python
async for resp in client.watch_sessions():
    event = resp.event
    print(event.event_type, event.session.session_id)
```

A `grpc.RpcError` raised mid-stream surfaces as `MacpTransportError` with the
gRPC status on `.code`, for example `RESOURCE_EXHAUSTED` for consumer lag.

## Concurrency

The point of the async client is running sessions concurrently on one channel:

```python
async with AsyncMacpClient(target=..., auth=...) as client:
    sessions = [AsyncQuorumSession(client) for _ in range(10)]
    await asyncio.gather(*(
        s.start(intent="approve", participants=["coordinator", "alice"], ttl_ms=30_000)
        for s in sessions
    ))
```

Each session keeps its own projection. Don't share one session object across
concurrent tasks that send to it at the same time: a projection is updated
after each accepted send, in completion order.

## Errors

The error mapping is shared with the sync client:

- A NACK raises `MacpAckError`, or is returned when you pass `raise_on_nack=False`.
- A runtime-0.9 `macp-error-code` trailer surfaces on `.macp_code`.
- A read-only registry raises `MacpAckError(code="FAILED_PRECONDITION")`.
- Transport failures raise `MacpTransportError`.

See [Error Handling & Retry](error-handling.md).

`retry_send` is sync-only today. In async code, wrap `await client.send(...)` in
your own backoff loop.

## What is sync-only

- **The deprecated Task aliases** `request`, `update`, `complete` and `fail`.
  Use the `*_task` names.
- **`macp_sdk.watchers`** (the thread-based watcher helpers). In async code,
  iterate the `watch_*` methods directly.
- **The `macp_sdk.agent` participant framework.** It runs on the sync client.
