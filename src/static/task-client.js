/* Short polling releases Web request threads between progress updates. */
(() => {
  const key = () => 'SheetWeaveAgent.pending:' + location.pathname;
  const legacyKey = () => 'docAgent.pending:' + location.pathname;
  const read = () => {
    try {
      const saved = sessionStorage.getItem(key()) || sessionStorage.getItem(legacyKey());
      const task = JSON.parse(saved || 'null');
      if (task) {
        sessionStorage.setItem(key(), JSON.stringify(task));
        sessionStorage.removeItem(legacyKey());
      }
      return task;
    } catch (_error) { return null; }
  };
  const remember = task => { try { sessionStorage.setItem(key(), JSON.stringify(task)); } catch (_error) {} };
  const forget = () => { try { sessionStorage.removeItem(key()); sessionStorage.removeItem(legacyKey()); } catch (_error) {} };
  window.pendingReportTask = read;
  window.runQueuedReport = async ({form, url, signal, intent = 'preview', resume = false, onProgress, onComplete}) => {
    let task = resume ? read() : null;
    const abortError = () => new DOMException('已取消等待', 'AbortError');
    if (!task) {
      if (signal?.aborted) throw abortError();
      // Wait for the short enqueue response so cancellation has a concrete task ID.
      const response = await fetch(url, {method: 'POST', body: new FormData(form)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '无法提交任务，请重试。');
      task = {...result, intent}; remember(task);
    }
    if (!task) return;
    let cursor = 0;
    const cancel = async () => {
      try {
        const response = await fetch(task.cancel_url, {method:'POST'});
        if (response.ok) forget();
        else if (response.status === 409) throw new Error("任务已完成或正在保存，取消未生效。刷新页面可继续获取结果。");
      } catch (error) {
        if (error.message.includes("取消未生效")) throw error;
        throw new Error("无法确认取消，后台任务可能继续运行；刷新页面可重新连接。");
      }
      throw abortError();
    };
    let failures = 0;
    while (true) {
      if (signal?.aborted) return cancel();
      let response, status;
      try {
        response = await fetch(task.status_url + '?after=' + cursor, {cache:'no-store', signal});
        status = await response.json(); failures = 0;
      } catch (_error) {
        if (signal?.aborted) return cancel();
        failures++;
        onProgress('连接暂时中断，后台任务继续运行，正在重新获取进度。');
        if (failures >= 5) throw new Error('暂时无法获取任务进度，后台任务继续运行；刷新页面可重新连接。');
        await new Promise(resolve => setTimeout(resolve, 1000)); continue;
      }
      if (!response.ok) {
        if (response.status === 404) forget();
        throw new Error(status.error || '无法获取任务进度。');
      }
      if (status.state === 'queued') onProgress(`任务已排队，当前第 ${status.queue_position} 位。等待后台 Worker 处理。`);
      for (const event of status.events) {
        if (event.kind === 'progress') onProgress(event.payload.message);

      }
      cursor = status.cursor;
      if (status.state === 'failed' || status.state === 'cancelled') {
        forget(); throw new Error(status.error || (status.state === 'cancelled' ? '任务已取消。' : '后台任务失败，请重试。'));
      }
      if (status.state === 'succeeded' && !status.has_more) {
        await onComplete({...status.result, intent: task.intent}); forget(); return;
      }
      if (status.state === 'finalizing') onProgress('正在保存最终结果，此阶段无法取消。');
      if (!status.has_more) await new Promise(resolve => setTimeout(resolve, 750));
    }
  };
})();
