'use strict';
const $ = id => document.getElementById(id);
let catalogReady = false, optionsReady = false, busy = false, initializedModels = false;
let activeJob = null, pollTimer, requestId = null;
const selected = id => [...$(id).querySelectorAll('input:checked')].map(input => input.value);
const writeHeaders = {'Content-Type': 'application/json', 'X-FlexBench-Request': '1'};

async function api(path, options) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok) {
    const detail = value.detail;
    throw new Error(typeof detail === 'string' ? detail : `Ошибка запроса (${response.status})`);
  }
  return value;
}
function message(text, error = false) {
  $('message').textContent = text;
  $('message').className = error ? 'notice error' : 'notice';
  $('message').hidden = !text;
}
function remember(key, value) {
  try { localStorage.setItem(key, value); } catch (_) { /* Storage may be disabled. */ }
}
function recalled(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}
function updateCount() {
  const models = selected('models').length, cases = selected('cases').length;
  const repeat = Number($('repeat').value), concurrency = Number($('concurrency').value);
  const valid = $('repeat').validity.valid && $('concurrency').validity.valid;
  $('count').textContent = `${models} модел. × ${cases} задач × ${repeat || 0} повтор. = ${models * cases * repeat || 0} попыток · одновременно до ${concurrency || 0}`;
  $('start').disabled = busy || !catalogReady || !optionsReady || !models || !cases || !valid;
}
function choice(container, item, checked, subtitle) {
  const label = document.createElement('label'); label.className = 'choice';
  const input = document.createElement('input'); input.type = 'checkbox'; input.value = item.id; input.checked = checked;
  const text = document.createElement('span'); text.textContent = item.name || item.id;
  if (subtitle) { const note = document.createElement('small'); note.textContent = subtitle; text.append(note); }
  label.append(input, text); $(container).append(label);
}
async function loadModels() {
  const previous = new Set(selected('models'));
  catalogReady = false; $('refresh-models').disabled = true;
  $('catalog-status').textContent = 'Загружаем каталог GigaCode…'; updateCount();
  try {
    const catalog = await api('/api/flexbench/models');
    $('models').replaceChildren();
    for (const model of catalog.models) {
      const current = model.id === catalog.current_model_id;
      choice('models', model, initializedModels ? previous.has(model.id) : current,
        current ? 'Текущая модель GigaCode' : model.name !== model.id ? model.id : '');
    }
    initializedModels = true; catalogReady = true;
    $('catalog-status').textContent = `GigaCode ${catalog.executor_version} · доступно моделей: ${catalog.models.length}`;
  } catch (error) {
    $('catalog-status').textContent = error.message;
  } finally {
    $('refresh-models').disabled = false; updateCount();
  }
}
async function loadOptions() {
  try {
    const options = await api('/api/flexbench/options');
    $('cases').replaceChildren();
    for (const task of options.cases) choice('cases', task, task.id === 'js-retry-state', task.category);
    optionsReady = true; updateCount();
  } catch (error) { message(error.message + '. Обновите страницу.', true); }
}

function showSummary(summary) {
  $('summary').replaceChildren();
  if (!summary) return;
  const table = document.createElement('table');
  const head = document.createElement('thead'), titles = document.createElement('tr');
  for (const title of ['Модель', 'Попытки', 'Полный успех', 'Проверки пройдены', 'Токены']) {
    const cell = document.createElement('th'); cell.textContent = title; titles.append(cell);
  }
  head.append(titles); table.append(head);
  const body = document.createElement('tbody');
  for (const model of summary.models) {
    const tokens = model.tokens.total_tokens;
    const row = document.createElement('tr');
    for (const value of [model.model, model.attempts, model.successes, model.functional_passes,
      tokens.total === null ? `неизвестно (${tokens.measured_runs}/${tokens.attempts})` : tokens.total.toLocaleString('ru-RU')]) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    body.append(row);
  }
  table.append(body); $('summary').append(table);
  const explanation = document.createElement('p'); explanation.className = 'muted';
  explanation.textContent = 'Полный успех: проверки пройдены и GigaCode завершился штатно. Подробности ошибок и покрытие измерений — в сводке JSON и логах Harbor.';
  $('summary').append(explanation);
}
async function poll() {
  clearTimeout(pollTimer);
  if (!activeJob) return;
  const name = activeJob;
  try {
    const state = await api(`/api/flexbench/runs/${encodeURIComponent(name)}`);
    if (name !== activeJob) return;
    const total = state.total || 0, completed = state.completed || 0;
    $('progress').max = Math.max(total, 1); $('progress').value = completed;
    $('log').textContent = state.log_tail;
    $('results-link').href = state.url; $('results-link').hidden = !state.job_ready;
    $('stop').hidden = !state.can_stop;
    if (state.returncode !== null) {
      $('run-status').textContent = state.returncode === 0 ? `Завершён: ${completed}/${total} попыток, все успешны.`
        : state.returncode === 1 ? `Завершён: ${completed}/${total} попыток. Есть неуспешные попытки — смотрите результаты.`
        : state.returncode === 130 ? 'Прогон остановлен. Сохранённые результаты доступны в Harbor.'
        : 'Ошибка запуска. Подробности в журнале ниже.';
      if (state.returncode !== 0 && state.returncode !== 1) $('log-details').open = true;
    } else if (state.state === 'unobserved') {
      $('run-status').textContent = 'Сервер перезапущен. Выполнение могло продолжиться; состояние проверяйте в Harbor. Повторно запускать задание не требуется.';
    } else if (state.state === 'unknown') {
      $('run-status').textContent = 'Не удалось подтвердить запуск процесса. Проверьте журнал запуска и историю Harbor перед новым прогоном.';
    } else {
      $('run-status').textContent = `Выполняется · завершено ${completed}/${total} попыток`;
    }
    showSummary(state.summary);
    for (const artifact of ['report', 'summary']) {
      $(artifact + '-link').hidden = !state.summary;
      $(artifact + '-link').href = `/api/flexbench/runs/${encodeURIComponent(name)}/${artifact}`;
    }
    if (state.returncode === null) pollTimer = setTimeout(poll, 2000);
  } catch (error) {
    $('run-status').textContent = `Не удалось обновить состояние: ${error.message}. Повторим через 3 секунды.`;
    pollTimer = setTimeout(poll, 3000);
  }
}
function follow(name) {
  activeJob = name; remember('flexbench.activeJob', name);
  $('run').hidden = false; $('job-name').textContent = name;
  $('run-status').textContent = 'Подключаемся к прогону…'; $('summary').replaceChildren();
  poll();
}

$('refresh-models').addEventListener('click', loadModels);
$('all-cases').addEventListener('click', () => {
  const boxes = [...$('cases').querySelectorAll('input')], all = boxes.every(input => input.checked);
  boxes.forEach(input => { input.checked = !all; }); requestId = null; updateCount();
});
$('launch-form').addEventListener('input', () => { requestId = null; updateCount(); });
$('launch-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy || $('start').disabled) return;
  busy = true; updateCount(); $('start').textContent = 'Проверяем модели и запускаем…';
  message('Проверяем актуальный каталог GigaCode и окружение выбранных задач…');
  requestId ||= crypto.randomUUID();
  try {
    const receipt = await api('/api/flexbench/runs', {method: 'POST', headers: writeHeaders, body: JSON.stringify({
      request_id: requestId, models: selected('models'), cases: selected('cases'),
      repeat: Number($('repeat').value), concurrency: Number($('concurrency').value)
    })});
    requestId = null; message('Прогон запущен. Состояние появится ниже.'); follow(receipt.job_name);
    $('run').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    message(`${error.message}\nЕсли ответ сервера потерялся, повторите запуск с теми же настройками: запрос не создаст второй прогон.`, true);
  } finally {
    busy = false; $('start').textContent = 'Запустить прогон'; updateCount();
  }
});
$('stop').addEventListener('click', async () => {
  $('stop').disabled = true;
  try {
    await api(`/api/flexbench/runs/${encodeURIComponent(activeJob)}/stop`, {method: 'POST', headers: writeHeaders});
    $('run-status').textContent = 'Останавливаем прогон и сохраняем результаты…';
  } catch (error) { message(error.message, true); }
  finally { $('stop').disabled = false; }
});
loadOptions(); loadModels();
const previousJob = recalled('flexbench.activeJob');
if (previousJob && /^gigacode-\d{8}T\d{6}-[0-9a-f]{8}$/.test(previousJob)) follow(previousJob);
