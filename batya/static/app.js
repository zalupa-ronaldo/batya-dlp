'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const activeStatuses = new Set(['queued', 'starting', 'downloading', 'processing']);
const statusLabels = {queued: 'В очереди', starting: 'Подготовка', downloading: 'Скачивается', processing: 'Собираем файл', completed: 'Готово', cancelled: 'Отменено', error: 'Не получилось', interrupted: 'Прервано'};
const qualityLabels = {'1080': 'До 1080p', '720': 'До 720p', best: 'Максимальное качество', audio: 'MP3'};
let state = null;
let filter = 'all';
let initialized = false;
let stopped = false;
let toastTimer;
let failures = 0;
const rows = new Map();

function toast(message, error = false) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('error', error);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, error ? 9000 : 4000);
}

async function api(path, data) {
  let response;
  try {
    response = await fetch(`/api/${path}`, {
      method: data === undefined ? 'GET' : 'POST',
      headers: data === undefined ? {} : {'Content-Type': 'application/json'},
      body: data === undefined ? undefined : JSON.stringify(data),
      cache: 'no-store',
    });
  } catch {
    throw new Error('Нет связи с приложением. Запустите приложение ещё раз.');
  }
  const result = await response.json();
  if (!response.ok) {
    throw new Error(typeof result.detail === 'string' ? result.detail : 'Проверьте ссылку и настройки.');
  }
  return result;
}

function bytes(value) {
  if (!value || !Number.isFinite(Number(value))) return '';
  const units = ['Б', 'КБ', 'МБ', 'ГБ'];
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {value /= 1024; unit++;}
  return `${value.toLocaleString('ru-RU', {maximumFractionDigits: unit > 1 ? 1 : 0})} ${units[unit]}`;
}

function remaining(eta) {
  if (eta === null || !Number.isFinite(Number(eta))) return '';
  const seconds = Math.max(0, Math.ceil(Number(eta)));
  return seconds >= 60 ? `Осталось ~${Math.ceil(seconds / 60)} мин` : `Осталось ~${seconds} сек`;
}

function button(label, icon, className, action) {
  const element = document.createElement('button');
  element.type = 'button';
  element.className = `button ${className}`;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', `#i-${icon}`);
  svg.append(use);
  element.append(svg, document.createTextNode(label));
  element.addEventListener('click', async () => {
    element.disabled = true;
    try {await action();} catch (error) {toast(error.message, true);} finally {element.disabled = false;}
  });
  return element;
}

function renderJobs() {
  const jobs = state.jobs.filter(job => filter === 'all' || job.status === 'completed');
  const ids = new Set(jobs.map(job => job.id));
  for (const [id, row] of rows) {
    if (!ids.has(id)) {row.remove(); rows.delete(id);}
  }
  $('#total-count').textContent = String(state.jobs.length);
  $('#empty').hidden = jobs.length > 0;
  $('#empty h3').textContent = filter === 'completed' ? 'Готовых видео пока нет' : 'Здесь будут ваши видео';
  jobs.forEach((job, index) => {
    let row = rows.get(job.id);
    if (!row) {
      row = $('#job-template').content.firstElementChild.cloneNode(true);
      rows.set(job.id, row);
    }
    if ($('#jobs').children[index] !== row) $('#jobs').insertBefore(row, $('#jobs').children[index] || null);
    const previous = row.dataset.status;
    row.dataset.status = job.status;
    $('.job-title', row).textContent = job.title;
    $('.job-title', row).title = job.title;
    let host = '';
    try {host = new URL(job.url).hostname.replace(/^www\./, '');} catch { /* Old history may contain an invalid URL. */ }
    $('.job-meta', row).textContent = `${qualityLabels[job.quality] || ''} · ${host}${job.playlist_title ? ' · ' + job.playlist_title + ' · ' + job.playlist_index + '/' + job.playlist_count : ''}`;
    $('.job-status', row).textContent = statusLabels[job.status] || job.status;
    const active = activeStatuses.has(job.status);
    $('.progress-region', row).hidden = !active;
    const percent = Math.min(100, Math.max(0, Number(job.percent) || 0));
    $('.progress-track', row).classList.toggle('indeterminate', job.status !== 'downloading' || !job.total);
    $('.progress-track', row).setAttribute('aria-valuenow', String(Math.round(percent)));
    $('.progress-fill', row).style.width = `${percent}%`;
    const details = [];
    if (job.status === 'queued') details.push('Начнём после предыдущего видео');
    else if (job.status === 'starting') details.push(job.note || 'Проверяем ссылку и доступные форматы…');
    else if (job.status === 'processing') details.push('Готовим видео для просмотра. Ещё немного…');
    else {
      if (job.downloaded) details.push(`${bytes(job.downloaded)}${job.total ? ' из ' + bytes(job.total) : ''}`);
      if (job.speed) details.push(`${bytes(job.speed)}/с`);
      if (job.eta !== null) details.push(remaining(job.eta));
    }
    $('.progress-detail', row).textContent = details.join(' · ');
    $('.progress-percent', row).textContent = job.status === 'downloading' && job.total ? `${Math.floor(percent)}%` : '';
    const messages = [job.cookie_warning, job.error].filter(Boolean);
    $('.job-error', row).textContent = messages.join('\n\n');
    $('.job-error', row).hidden = messages.length === 0;
    if (previous !== job.status) {
      const actions = $('.job-actions', row);
      actions.replaceChildren();
      if (job.status === 'completed') {
        actions.append(button(job.quality === 'audio' ? 'Слушать' : 'Смотреть', 'play', 'watch-button', async () => {
          await api(`jobs/${job.id}/open`, {});
          toast('Открываем в вашем проигрывателе');
        }));
        actions.append(button('Папка', 'folder', 'job-folder-button', () => api(`jobs/${job.id}/folder`, {})));
      } else if (active) {
        actions.append(button('Отменить', 'close', 'cancel-button', async () => {await api(`jobs/${job.id}/cancel`, {}); await refresh();}));
      } else {
        actions.append(button('Повторить', 'refresh', 'retry-button', async () => {
          await api('jobs', {url: job.url, quality: job.quality});
          filter = 'all'; updateFilters(); await refresh();
        }));
      }
    }
  });
}

function updateFilters() {
  document.querySelectorAll('.filter').forEach(element => {
    const selected = element.dataset.filter === filter;
    element.classList.toggle('active', selected);
    element.setAttribute('aria-pressed', String(selected));
  });
}

async function refresh() {
  state = await api('state');
  if (!initialized) {
    $('#folder').value = state.settings.folder;
    $('#folder').title = state.settings.folder;
    $('#quality').value = state.settings.quality;
    $('#browser').value = state.settings.browser || 'none';
    $('#cookies-file').value = state.settings.cookies_file || '';
    $('#cookie-file-fields').hidden = $('#browser').value !== 'file';
    initialized = true;
  }
  $('#app-version').textContent = state.app_version;
  $('#yt-version').textContent = state.yt_dlp_version;
  $('#update').disabled = state.update.busy || state.jobs.some(job => activeStatuses.has(job.status));
  $('#update').title = state.jobs.some(job => activeStatuses.has(job.status)) ? 'Обновление доступно после завершения загрузок' : '';
  $('#update span').textContent = state.update.busy ? 'Обновляем…' : 'Обновить yt-dlp';
  $('#update-message').textContent = state.update.message;
  $('#update-message').classList.toggle('error', state.update.error);
  $('#download-button').disabled = state.update.busy;
  renderJobs();
  failures = 0;
  $('#connection').classList.remove('offline');
  $('#connection-text').textContent = 'На вашем компьютере';
}

async function poll() {
  if (stopped) return;
  try {await refresh();} catch (error) {
    failures++;
    $('#connection').classList.add('offline');
    $('#connection-text').textContent = 'Приложение недоступно';
    if (failures === 1) toast(error.message, true);
  }
  if (!stopped) setTimeout(poll, document.hidden ? 3000 : 1000);
}

$('#download-form').addEventListener('submit', async event => {
  event.preventDefault();
  const element = $('#download-button');
  element.disabled = true;
  try {
    if (!state) throw new Error('Приложение ещё запускается. Подождите несколько секунд.');
    await api('jobs', {url: $('#url').value.trim(), quality: $('#quality').value});
    $('#url').value = '';
    filter = 'all'; updateFilters();
    await refresh();
    toast('Ссылка добавлена в очередь');
  } catch (error) {toast(error.message, true);} finally {element.disabled = false;}
});

$('#paste').addEventListener('click', async () => {
  try {$('#url').value = (await navigator.clipboard.readText()).trim(); $('#url').focus();}
  catch {toast('Нажмите на поле ссылки и вставьте её сочетанием Ctrl + V.'); $('#url').focus();}
});

async function saveFolder() {
  const result = await api('settings', {folder: $('#folder').value.trim(), quality: $('#quality').value});
  if (state) state.settings = result;
  $('#folder').value = result.folder;
  $('#folder').title = result.folder;
  toast('Папка сохранена');
}

function bind(id, action) {
  $(id).addEventListener('click', async () => {
    const element = $(id); element.disabled = true;
    try {await action();} catch (error) {toast(error.message, true);} finally {element.disabled = false;}
  });
}
bind('#save-folder', saveFolder);
$('#folder').addEventListener('keydown', async event => {
  if (event.key === 'Enter') {event.preventDefault(); try {await saveFolder();} catch (error) {toast(error.message, true);}}
});
bind('#pick-folder', async () => {
  const result = await api('folder/pick', {});
  if (result.folder) {$('#folder').value = result.folder; await saveFolder();}
});
bind('#open-folder', () => api('folder/open', {}));
$('#browser').addEventListener('change', () => {$('#cookie-file-fields').hidden = $('#browser').value !== 'file';});
bind('#save-auth', async () => {
  if (!state) return;
  state.settings = await api('settings', {folder: state.settings.folder, quality: $('#quality').value,
    browser: $('#browser').value, cookies_file: $('#cookies-file').value.trim()});
  toast('Доступ сохранён. Можно повторить загрузку.');
});
bind('#pick-cookies', async () => {
  const result = await api('cookies/pick', {});
  if (result.file) $('#cookies-file').value = result.file;
});
bind('#update', async () => {await api('update', {}); await refresh();});
bind('#quit', async () => {
  const busy = state && (state.jobs.some(job => activeStatuses.has(job.status)) || state.update.busy);
  if (busy && !window.confirm('Идёт загрузка или обновление. Закрыть приложение и прервать работу?')) return;
  await api('quit', {});
  stopped = true;
  $('#closed').hidden = false;
  document.title = 'Батя качает — до встречи!';
});
$('#quality').addEventListener('change', async () => {
  if (!state) return;
  try {state.settings = await api('settings', {folder: state.settings.folder, quality: $('#quality').value});}
  catch (error) {toast(error.message, true);}
});
document.querySelectorAll('.filter').forEach(element => element.addEventListener('click', () => {
  filter = element.dataset.filter; updateFilters(); if (state) renderJobs();
}));
poll();
