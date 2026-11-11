# Mid-turn control: steering, and Stop as a hold window

What happens when the user types while an answer is still streaming, and what happens when they
press Stop. Both look like one gesture in the UI; underneath they are three different code paths in
Open WebUI, and only some of them send anything this proxy can see.

Source references are Open WebUI **v0.11.3** (`src/lib/components/chat/Chat.svelte`,
`MessageInput.svelte`). Line numbers will drift; the behaviours below are what matter.

## What the client actually sends

### 1. Type while generating, press Enter

`MessageInput` dispatches `submit` → `Chat.svelte.submitPrompt` (≈3197), which first asks whether a
response is still being produced:

```js
const lastMessage = history.currentId ? history.messages[history.currentId] : null;
const isGenerating = lastMessage && lastMessage.role === 'assistant' && !lastMessage.done;

if (isGenerating) {
    if ($settings?.enableMessageQueue ?? true) {
        chatRequestQueues.update(...);   // local queue in the browser
        messageInput?.setText('');
        return;                          // ← no HTTP request at all
    } else {
        await stopResponse();            // ← aborts the running stream
    }
}
```

`enableMessageQueue` defaults to **true** (Settings → Interface → *Enable Message Queue*). So by
default this path never reaches the proxy: the message sits in the browser until the current turn
finishes. With the setting off, the client aborts first and then sends.

### 2. "Send now" on a queued message

`Chat.svelte.sendQueuedMessageNow` (≈2496), bound to `onQueueSendNow` on every `QueuedMessageItem`:

```js
chatRequestQueues.update(...);        // remove from the local queue
await stopResponse(false);            // ← abort() the running stream, mark messages done
await tick();
await submitPrompt(item.prompt, item.files);   // ← send the next request immediately
```

Abort and send are milliseconds apart, with no delay in between.

### 3. Stop

The Stop button only exists while the composer is **empty** (`isActive && prompt === '' &&
files.length === 0`); the moment the user types, it becomes the send button again. `Escape` also
calls `stopResponse()`.

`Chat.svelte.stopResponse` (≈3729) does three things: calls Open WebUI's own
`/api/tasks/chat/{id}/stop`, marks every pending response message `done = true`, then
`generationController?.abort()`.

## What this proxy does with each

| Client path | What reaches the proxy | Result |
|---|---|---|
| Type + Enter, message queue **on** (default) | nothing | nothing to do — the message is queued in the browser |
| Type + Enter, message queue **off** | abort, then a new request | enters the hold window, then behaves like send-now |
| **Send now** | abort + new request milliseconds later | steered, and the follow-up keeps streaming the held answer |
| **Stop**, then type (phone) | abort; the follow-up arrives seconds later | same, as long as it lands inside `stop_grace_seconds` |
| Stop, then take longer than the window | abort; the follow-up arrives late | the held run was already hard-stopped; the follow-up is an ordinary new turn |

The phone path is why the window exists at all: pressing Stop and then typing on a phone is slow,
and a 20 s window expired in real use. It is 60 s now.

## The hold window

When a streaming request is cancelled, the handler does **not** close the upstream socket. It hands
it to `run_control.grace_stop` and marks the conversation record `holding = true`. From then on:

- the upstream keeps delivering, and the watcher keeps draining it (so the gateway's SSE writes
  never block), buffering chunks into a bounded queue (`stream_queue`, 400 chunks);
- the conversation stays addressable: a follow-up that arrives while `holding` is set is converted
  into `POST /v1/runs/{id}/steer` and takes the stream over — a prelude the client can attach to,
  then the upstream's own frames verbatim, terminal event included;
- steering clears `stop_pending`, so a user who took over is never hard-stopped;
- if nobody takes over before the window closes, the watcher sends `POST /v1/runs/{id}/stop` — and
  only if the recorded run id is still the active one;
- when the watcher exits it clears the record (id-checked) and closes the socket.

Whatever the answer had already emitted belongs to the connection the user closed; the follow-up
shows what comes after the handover point. The next message simply attaches again.

Outside a hold there is nothing to hand over: while the original request still owns the stream, a
follow-up can only be acknowledged.

## Invariants that broke in production

Each of these was a real failure, not a hypothetical:

1. **Conversation key order is header → sid marker → derived.** Real Open WebUI never sends
   `X-Hermes-Session-Id`; the sid rides inside the input as a `<!--hermes-sid:…-->` marker on the
   first assistant message. A derived key (`api-<sha>`) never matches the sid the live run was
   registered under, so every follow-up became a second queued turn. Verifying with a
   self-supplied header hides exactly this failure — test with the real shape.
2. **`holding` is set synchronously in the cancel path.** Send-now is abort + submit with no gap; a
   follow-up can arrive before the watcher task is scheduled. Setting it only inside `grace_stop`
   loses that race.
3. **`/stop` carries its own run id.** The expired window must not address "whatever is active for
   this conversation now": if a follow-up had already become a fresh run, that killed the new
   answer about 19 s in and the UI simply showed it stopping.
4. **A hard stop must land between tool calls too.** The interrupt flag used to be checked only
   inside an in-flight API call, so a stop that arrived while tools were running was never read —
   the run was marked cancelled and the agent issued 11 more API calls over 56 s. Requires
   `api_server_run_control.patch` (see `patches/README.md`).
5. **The record is cleared when the hold ends**, otherwise the next message counts as mid-turn and
   burns a `409` round-trip before falling back.

## Measurements (2026-11-11, RTX 3090 box, qwen-27b-default)

| Scenario | Result |
|---|---|
| Send-now (zero gap between abort and follow-up) | steered; follow-up streamed 15.4 s / 24,183 bytes to `response.completed`, ack line + full report |
| Stop, 8 s, then send (phone) | `60s window for a follow-up message`, steered, 17,738 bytes, ended on the agent's own reply |
| Stop, 26 s, then send (past a 20 s window) | ordinary new turn, normal answer, no hang |
| Stop during an in-flight API call | turn ended in 0.1 s, `interrupted_during_api_call`, 0 further API calls |
| Stop while a tool was running | turn ended in 0.5 s, `interrupted_by_user`, tool returned `[Command interrupted]`, 0 further API calls |
| Before the run-control fix | stop marked the run cancelled but the agent ran on 56 s / 11 API calls, follow-up queued behind it |

## Costs and knobs

- `stop_grace_seconds` (config.yaml, default 60). Longer = the user has more time to type after
  pressing Stop, but a Stop that was meant as "give up" keeps the turn alive that long.
- The queue is capped; a reader that never attaches simply loses buffered chunks. Only the
  handover point onwards is guaranteed.
- Keepalive comments are emitted every 10 s of silence so a tool-heavy turn does not trip the
  client's idle timer.
