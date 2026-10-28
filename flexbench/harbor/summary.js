// One coverage-aware presentation for the launcher and native Harbor pages.
window.flexbenchSummary = (target, summary) => {
  target.replaceChildren();
  if (!summary) return;
  const format = value => value.toLocaleString('ru-RU');
  const tokens = summary.efficiency.tokens;
  const grid = document.createElement('div'); grid.className = 'fb-metric-grid';
  for (const [key, label] of [['total_tokens', 'Всего токенов'], ['input_tokens', 'Входные, включая кэш'],
    ['cached_input_tokens', 'Из них кэш'], ['output_tokens', 'Выходные']]) {
    const metric = tokens[key], block = document.createElement('div');
    const caption = document.createElement('span'); caption.textContent = label;
    const value = document.createElement('strong'); value.textContent = metric.total === null ? 'Неизвестно' : format(metric.total);
    const coverage = document.createElement('small');
    coverage.textContent = `Полные измерения: ${metric.measured_runs}/${metric.attempts}`;
    if (metric.total === null && metric.observed_total !== null) coverage.textContent += ` · известная часть: ${format(metric.observed_total)}`;
    block.append(caption, value, coverage); grid.append(block);
  }
  target.append(grid);
  const wrapper = document.createElement('div'); wrapper.className = 'fb-table-wrap';
  const table = document.createElement('table'), head = document.createElement('thead'), titles = document.createElement('tr');
  for (const title of ['Модель', 'Попытки', 'Полный успех', 'Проверки пройдены', 'Всего токенов']) {
    const cell = document.createElement('th'); cell.textContent = title; titles.append(cell);
  }
  head.append(titles); table.append(head);
  const body = document.createElement('tbody');
  for (const model of summary.models) {
    const metric = model.tokens.total_tokens, row = document.createElement('tr');
    for (const value of [model.model || 'Настройки GigaCode', model.attempts, model.successes, model.functional_passes,
      metric.total === null ? `Неизвестно (${metric.measured_runs}/${metric.attempts})` : format(metric.total)]) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    body.append(row);
  }
  table.append(body); wrapper.append(table); target.append(wrapper);
  const explanation = document.createElement('p'); explanation.className = 'fb-note';
  const costs = Object.entries(summary.efficiency.cost_by_currency);
  const money = value => value.toLocaleString('ru-RU', {maximumSignificantDigits: 8});
  const costText = costs.length ? costs.map(([currency, metric]) => metric.total === null
    ? `${currency}: неизвестно (${metric.measured_runs}/${metric.attempts})` : `${money(metric.total)} ${currency}`).join('; ')
    : 'неизвестна — тариф не задан или CLI не передал стоимость';
  explanation.textContent = `Кэш входит во входные токены и не прибавляется к ним повторно. Стоимость: ${costText}.`;
  target.append(explanation);
};
