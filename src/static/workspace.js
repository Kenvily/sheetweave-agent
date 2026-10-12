/* Panel-scoped tracing and reader preferences; report content remains unchanged. */
(() => {
  const sourceRows = [...document.querySelectorAll('[data-source-id]')];
  const blocks = [...document.querySelectorAll('[data-block-id]')];
  const previews = [...document.querySelectorAll('[data-preview-block]')];
  let activeBlock = null;
  const behavior = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  const find = (attribute, value) => document.querySelector(`[${attribute}="${CSS.escape(value)}"]`);
  const reveal = (target) => {
    if (!target || !target.getClientRects().length) return;
    const panel = target.closest('.review-panel');
    if (panel && panel.scrollHeight > panel.clientHeight + 1) {
      const bounds = panel.getBoundingClientRect();
      const heading = panel.querySelector('.panel-heading').getBoundingClientRect();
      const top = target.getBoundingClientRect().top;
      panel.scrollTo({top: panel.scrollTop + top - bounds.top - heading.height - 16, behavior: behavior()});
    } else {
      target.scrollIntoView({behavior: behavior(), block: 'nearest', inline: 'nearest'});
    }
  };
  const clear = () => [...sourceRows, ...blocks, ...previews].forEach(item => item.classList.remove('trace-active'));
  const highlight = (attribute, id) => find(attribute, id)?.classList.add('trace-active');
  const revealPreview = () => {
    if (activeBlock) reveal(find('data-preview-block', activeBlock));
  };
  const activateSource = (row) => {
    clear(); row.classList.add('trace-active');
    const ids = row.dataset.blockIds.split(' ').filter(Boolean);
    ids.forEach(id => { highlight('data-block-id', id); highlight('data-preview-block', id); });
    activeBlock = ids[0] || null;
    if (activeBlock) reveal(find('data-block-id', activeBlock));
    revealPreview();
  };
  const activateBlock = (block, fromPreview = false) => {
    clear(); block.classList.add('trace-active');
    activeBlock = block.dataset.blockId;
    highlight('data-preview-block', activeBlock);
    const ids = block.dataset.sourceIds.split(' ').filter(Boolean);
    ids.forEach(id => highlight('data-source-id', id));
    if (ids.length) reveal(find('data-source-id', ids[0]));
    if (fromPreview) reveal(block); else revealPreview();
  };
  const bindActivation = (element, activate, ignoreControls = false) => {
    element.addEventListener('click', event => {
      if (ignoreControls && event.target.closest('form, details, button, input, textarea, select, a')) return;
      activate();
    });
    element.addEventListener('keydown', event => {
      if (event.target === element && ['Enter', ' '].includes(event.key)) {
        event.preventDefault(); activate();
      }
    });
  };
  sourceRows.forEach(row => bindActivation(row, () => activateSource(row)));
  blocks.forEach(block => bindActivation(block, () => activateBlock(block), true));
  previews.forEach(preview => bindActivation(preview, () => {
    const block = find('data-block-id', preview.dataset.previewBlock);
    if (block) activateBlock(block, true);
  }));

  const storageKey = 'docAgent.review.reader';
  let preferences = {};
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || '{}');
    if (saved && typeof saved === 'object' && !Array.isArray(saved)) preferences = saved;
  } catch (_error) { /* Reading preferences is optional. */ }
  const save = () => { try { localStorage.setItem(storageKey, JSON.stringify(preferences)); } catch (_error) {} };
  const contents = {source: '.source-list', content: '.review-blocks', document: '.paper-preview'};
  document.querySelectorAll('[data-panel-zoom]').forEach(select => {
    const panel = select.dataset.panelZoom;
    const target = document.querySelector(contents[panel]);
    const apply = () => { target.style.zoom = Number(select.value) / 100; };
    const saved = String(preferences[panel]);
    if ([...select.options].some(option => option.value === saved)) select.value = saved;
    apply();
    select.addEventListener('change', () => { apply(); preferences[panel] = Number(select.value); save(); });
  });
  const updatePreviewControls = () => {
    const html = document.querySelector('[data-preview-panel="html"]');
    const select = document.querySelector('[data-panel-zoom="document"]');
    select.disabled = html.hidden;
    select.title = html.hidden ? 'PDF 请使用阅读器自带的缩放工具' : '只调整 HTML 显示，不影响 DOCX';
  };
  updatePreviewControls();
  document.addEventListener('workspace:preview-mode', event => {
    updatePreviewControls();
    if (event.detail === 'html') requestAnimationFrame(revealPreview);
  });
  const focusButton = document.getElementById('focus-review');
  const applyFocus = () => {
    const enabled = preferences.focus === true;
    document.body.classList.toggle('workspace-focus', enabled);
    focusButton.setAttribute('aria-pressed', String(enabled));
    focusButton.textContent = enabled ? '退出专注' : '专注审核';
  };
  applyFocus();
  focusButton.addEventListener('click', () => { preferences.focus = preferences.focus !== true; applyFocus(); save(); });
})();
