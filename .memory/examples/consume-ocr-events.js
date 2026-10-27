/** Parse the OCR API's UTF-8 SSE response, including fragmented network chunks. */
export async function consumeOcrEvents(response, onEvent) {
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
  }
  if (!response.headers.get("content-type")?.includes("text/event-stream")) {
    throw new Error("Expected an OCR event stream.");
  }
  if (!response.body) throw new Error("Response has no readable body.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer = (buffer + decoder.decode(value, { stream: !done })).replace(/\r\n/g, "\n");
      let end;
      while ((end = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        let event = "message";
        let id;
        const data = [];
        for (const line of frame.split("\n")) {
          if (!line || line.startsWith(":")) continue;
          const colon = line.indexOf(":");
          const field = colon === -1 ? line : line.slice(0, colon);
          let text = colon === -1 ? "" : line.slice(colon + 1);
          if (text.startsWith(" ")) text = text.slice(1);
          if (field === "event") event = text;
          if (field === "id") id = Number(text);
          if (field === "data") data.push(text);
        }
        // Heartbeat comments have no data and must not be JSON-parsed.
        if (data.length) await onEvent({ event, id, data: JSON.parse(data.join("\n")) });
      }
      if (done) break; // SSE dispatches only complete, blank-line-terminated frames.
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
