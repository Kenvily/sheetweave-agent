(() => {
  'use strict';
  const form = document.getElementById('profile-upload');
  if (!form) return;
  const sheet = document.getElementById('source-sheet'), field = document.getElementById('sheet-field');
  const status = document.getElementById('sheet-status'), button = document.getElementById('analyze-profile');
  let controller;
  form.elements.source.addEventListener('change', async () => {
    controller?.abort(); controller = new AbortController();
    const file = form.elements.source.files[0]; sheet.replaceChildren(); field.hidden = true;
    if (!file) { status.textContent = ''; button.disabled = false; return; }
    button.disabled = true; status.textContent = '正在读取工作表…';
    try {
      const body = new FormData(); body.set('source', file);
      const response = await fetch('/profiles/input-options', {method:'POST', body, signal:controller.signal});
      const result = await response.json(); if (!response.ok) throw Error(result.error);
      for (const name of result.sheets) { const option = new Option(name, name); sheet.append(option); }
      field.hidden = result.sheets.length === 0; sheet.value = result.selected_sheet || '';
      status.textContent = ''; button.disabled = false;
    } catch (error) { if (error.name !== 'AbortError') status.textContent = error.message; }
  });
  form.addEventListener('submit', () => { button.disabled = true; button.textContent = '正在提交…'; });
})();
