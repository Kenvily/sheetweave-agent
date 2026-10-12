/* Provisional text only: a completed server snapshot remains the export authority. */
(() => {
  const partialTexts = (raw) => {
    const texts = [];
    const pattern = /"text"\s*:\s*"((?:[^"\\]|\\[\s\S])*)/g;
    for (const match of raw.matchAll(pattern)) {
      const value = match[1].replace(/\\u[0-9a-fA-F]{0,3}$/, "");
      try { texts.push(JSON.parse('"' + value + '"')); } catch (_error) { /* wait for next delta */ }
    }
    return texts.join("\n\n");
  };

  window.streamWeeklyReport = async ({form, signal, onProgress, onBatch, onComplete}) => {
    const response = await fetch(form.dataset.streamUrl, {
      method: "POST", body: new FormData(form), signal,
    });
    if (!response.ok || !response.body || !response.headers.get("Content-Type")?.includes("text/event-stream")) {
      throw new Error("无法开始流式生成，请检查上传文件和服务状态。");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const batches = new Map();
    let pending = "";
    let completed = false;
    try {
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        pending += decoder.decode(value, {stream: true});
        let end;
        while ((end = pending.indexOf("\n\n")) >= 0) {
          const frame = pending.slice(0, end);
          pending = pending.slice(end + 2);
          const lines = frame.split("\n");
          const kind = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
          const data = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
          if (!kind || !data) continue;
          const payload = JSON.parse(data);
          if (kind === "error") throw new Error(payload.message);
          if (kind === "progress") onProgress(payload.message);
          if (kind === "delta") {
            const raw = (batches.get(payload.batch) || "") + payload.text;
            batches.set(payload.batch, raw);
            onBatch(payload.batch, partialTexts(raw));
          }
          if (kind === "complete") {
            if (completed) throw new Error("服务重复返回完成事件。");
            completed = true;
            await onComplete(payload);
          }
        }
      }
      if (!completed) throw new Error("生成连接提前结束，请重试；未完成内容不会用于导出。");
    } finally { await reader.cancel().catch(() => {}); }
  };
})();
