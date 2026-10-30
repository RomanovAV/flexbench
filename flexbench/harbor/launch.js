'use strict';
const $ = id => document.getElementById(id);
let catalogReady = false, optionsReady = false, busy = false, initializedModels = false;
let activeJob = null, activeKind = 'runs', pollTimer, requestId = null;
let experimentSpecs = [];
const isExperiment = () => $('mode').value === 'experiments';
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
  const valid = $('repeat').validity.valid && (isExperiment() || $('concurrency').validity.valid);
  $('count').textContent = `${models} модел. × ${cases} задач × ${repeat || 0} повтор. = ${models * cases * repeat || 0} попыток · одновременно до ${concurrency || 0}`;
  if (isExperiment()) $('count').textContent = `1 модель × 2 конфигурации × ${cases} задач × ${repeat || 0} повтор. = ${2 * cases * repeat || 0} попыток · последовательно`;
  $('start').disabled = busy || !catalogReady || !optionsReady || !models || !cases || !valid
    || (isExperiment() && (models !== 1 || !$('experiment-spec').value));
  if (!busy) $('start').textContent = isExperiment() ? 'Запустить эксперимент' : 'Запустить прогон';
}
function choice(container, item, checked, subtitle) {
  const label = document.createElement('label'); label.className = 'choice';
  const input = document.createElement('input'); input.type = container === 'models' && isExperiment() ? 'radio' : 'checkbox'; input.name = container; input.value = item.id; input.checked = checked;
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
    experimentSpecs = options.experiments || [];
    $('experiment-spec').replaceChildren();
    for (const spec of experimentSpecs) {
      const option = document.createElement('option'); option.value = spec.id;
      option.textContent = spec.name + (spec.error ? ` — недоступен: ${spec.error}` : ''); option.disabled = Boolean(spec.error);
      $('experiment-spec').append(option);
    }
    const first = experimentSpecs.find(spec => !spec.error);
    $('experiment-spec').value = first?.id || '';
    optionsReady = true; showExperiment(); updateMode();
  } catch (error) { message(error.message + '. Обновите страницу.', true); }
}

function showExperiment() {
  const spec = experimentSpecs.find(item => item.id === $('experiment-spec').value);
  $('experiment-hypothesis').textContent = spec?.hypothesis || 'Нет доступных экспериментов. Проверьте каталог спецификаций сервера.';
  $('experiment-configurations').replaceChildren();
  if (!spec?.configurations) return;
  for (const side of ['baseline', 'candidate']) {
    const config = spec.configurations[side], section = document.createElement('section');
    const title = document.createElement('h3'); title.textContent = `${side}: ${config.id}`;
    const settings = document.createElement('p');
    settings.textContent = `Лимит: ${config.timeout_seconds} с · approval: ${config.approval_mode} · tools: ${config.allowed_tools.join(', ') || 'настройки CLI'}`;
    const instructions = document.createElement('pre');
    instructions.textContent = (config.prompt_prefix ? `Вводный промпт:\n${config.prompt_prefix}\n\n` : '')
      + (config.instructions.length ? config.instructions.join('\n\n') : 'Без дополнительных инструкций');
    section.append(title, settings, instructions); $('experiment-configurations').append(section);
  }
}
function updateMode() {
  const experiment = isExperiment();
  $('experiment-settings').hidden = !experiment; $('concurrency-setting').hidden = experiment;
  $('concurrency').disabled = experiment; $('experiment-order').hidden = !experiment;
  $('models-title').textContent = experiment ? 'Модель' : 'Модели';
  $('repeat-label').textContent = experiment ? 'Повторов на каждую конфигурацию и задачу' : 'Повторов на пару «модель × задача»';
  $('mode-description').textContent = experiment ? 'Сравните две конфигурации харнесса на одной модели и одинаковых задачах.' : 'Выберите модели и задачи. Harbor выполнит попытки параллельно.';
  const previous = selected('models'), inputs = [...$('models').querySelectorAll('input')];
  // Converting a checked checkbox into a radio can uncheck an earlier radio.
  // Convert the entire group before restoring its selected value(s).
  for (const input of inputs) { input.checked = false; input.type = experiment ? 'radio' : 'checkbox'; }
  for (const input of inputs) input.checked = experiment ? input.value === previous[0] : previous.includes(input.value);
  requestId = null; updateCount();
}
async function loadHistory() {
  try {
    const items = await api('/api/flexbench/experiments'); $('experiment-history').replaceChildren();
    if (!items.length) $('experiment-history').textContent = 'Здесь появятся результаты запусков.';
    for (const item of items) {
      const link = document.createElement('a'); link.href = `/run?experiment=${encodeURIComponent(item.job_name)}`;
      link.textContent = `${item.name} · ${item.job_name}`; $('experiment-history').append(link);
    }
  } catch (error) { $('experiment-history').textContent = `История недоступна: ${error.message}`; }
}
function showSummary(summary) {
  if (activeKind === 'experiments') window.flexbenchExperimentSummary($('summary'), summary);
  else window.flexbenchSummary($('summary'), summary);
}
async function poll() {
  clearTimeout(pollTimer);
  if (!activeJob) return;
  const name = activeJob, kind = activeKind;
  try {
    const state = await api(`/api/flexbench/${kind}/${encodeURIComponent(name)}`);
    if (name !== activeJob || kind !== activeKind) return;
    const total = state.total || 0, completed = state.completed || 0;
    $('progress').max = Math.max(total, 1); $('progress').value = completed;
    $('log').textContent = state.log_tail;
    if (state.name) $('job-name').textContent = `${state.name} · ${name}`;
    $('results-link').href = state.url; $('results-link').hidden = !state.job_ready;
    $('stop').hidden = !state.can_stop;
    if (state.returncode !== null) {
      $('run-status').textContent = state.returncode === 0 ? `Завершён: ${completed}/${total} попыток, все успешны.`
        : state.returncode === 1 ? `Завершён: ${completed}/${total} попыток. Есть неуспешные попытки — смотрите результаты.`
        : state.returncode === 130 ? 'Прогон остановлен. Сохранённые результаты доступны в Harbor.'
        : 'Серия завершилась с ошибкой или непроверенными данными. Смотрите отчёт и журнал ниже.';
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
      $(artifact + '-link').href = `/api/flexbench/${kind}/${encodeURIComponent(name)}/${artifact}`;
    }
    $('config-link').hidden = kind !== 'experiments';
    $('config-link').href = `/api/flexbench/experiments/${encodeURIComponent(name)}/config`;
    if (state.returncode === null) pollTimer = setTimeout(poll, 2000);
  } catch (error) {
    $('run-status').textContent = `Не удалось обновить состояние: ${error.message}. Повторим через 3 секунды.`;
    pollTimer = setTimeout(poll, 3000);
  }
}
function follow(name, kind = 'runs') {
  activeJob = name; activeKind = kind; remember('flexbench.activeJob', name); remember('flexbench.activeKind', kind);
  $('run-title').textContent = kind === 'experiments' ? 'Сравнение конфигураций' : 'Прогон';
  window.dispatchEvent(new Event('flexbench:active-job'));
  $('run').hidden = false; $('job-name').textContent = name;
  $('run-status').textContent = 'Подключаемся к прогону…'; $('summary').replaceChildren();
  poll();
}

$('mode').addEventListener('change', updateMode);
$('experiment-spec').addEventListener('change', () => { showExperiment(); requestId = null; updateCount(); });
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
    const kind = isExperiment() ? 'experiments' : 'runs';
    const payload = {request_id: requestId, cases: selected('cases'), repeat: Number($('repeat').value),
      ...(kind === 'experiments' ? {model: selected('models')[0], spec_id: $('experiment-spec').value}
        : {models: selected('models'), concurrency: Number($('concurrency').value)})};
    const receipt = await api(`/api/flexbench/${kind}`, {method: 'POST', headers: writeHeaders, body: JSON.stringify(payload)});
    requestId = null; message('Запуск принят. Состояние появится ниже.'); follow(receipt.job_name, kind); loadHistory();
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
    await api(`/api/flexbench/${activeKind}/${encodeURIComponent(activeJob)}/stop`, {method: 'POST', headers: writeHeaders});
    $('run-status').textContent = 'Останавливаем прогон и сохраняем результаты…';
  } catch (error) { message(error.message, true); }
  finally { $('stop').disabled = false; }
});
loadOptions(); loadModels(); loadHistory();
const query = new URLSearchParams(location.search);
const previousValue = query.get('experiment') || query.get('job') || recalled('flexbench.activeJob');
const previousJob = previousValue?.match(/^(experiment-\d{8}T\d{6}-[0-9a-f]{8})-attempt-/)?.[1] || previousValue;
if (/^experiment-\d{8}T\d{6}-[0-9a-f]{8}$/.test(previousJob || '')) follow(previousJob, 'experiments');
else if (/^gigacode-\d{8}T\d{6}-[0-9a-f]{8}$/.test(previousJob || '')) follow(previousJob);
