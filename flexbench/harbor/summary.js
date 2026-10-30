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

window.flexbenchExperimentSummary = (target, summary) => {
  target.replaceChildren();
  if (!summary) return;
  const format = value => value === null || value === undefined ? 'Неизвестно' : value.toLocaleString('ru-RU', {maximumFractionDigits: 2});
  const note = document.createElement('p'); note.className = 'fb-note';
  note.textContent = summary.comparison_valid
    ? `Модель: ${summary.model}. Candidate − baseline: ${format(summary.delta_percentage_points)} п. п. по доле полностью успешных попыток.`
    : `Модель: ${summary.model}. Итоговое сравнение пока недоступно: ${summary.complete ? 'есть непроверенные данные или ошибки запуска' : 'серия не завершена'}.`;
  target.append(note);
  function table(headers, rows) {
    const wrap = document.createElement('div'); wrap.className = 'fb-table-wrap';
    const element = document.createElement('table'), head = document.createElement('thead'), titles = document.createElement('tr');
    for (const title of headers) { const cell = document.createElement('th'); cell.textContent = title; titles.append(cell); }
    head.append(titles); element.append(head);
    const body = document.createElement('tbody');
    for (const values of rows) {
      const row = document.createElement('tr');
      for (const value of values) {
        const cell = document.createElement('td');
        if (value instanceof Node) cell.append(value); else cell.textContent = value;
        row.append(cell);
      }
      body.append(row);
    }
    element.append(body); wrap.append(element); target.append(wrap);
  }
  table(['Конфигурация', 'Полный успех', 'Приёмка пройдена / измерена', 'Время всех попыток, с', 'Всего токенов · покрытие'],
    Object.entries(summary.variants).map(([name, stats]) => [name, `${stats.successes}/${stats.attempts}`,
      `${stats.functional_passes}/${stats.functional_measured}`, format(stats.executor_seconds.total),
      `${format(stats.tokens.total_tokens.total)} · ${stats.tokens.total_tokens.measured_runs}/${stats.attempts}`]));
  table(['Задача', 'Baseline', 'Candidate', 'Изменение, п. п.'], summary.cases.map(item => [item.case,
    `${item.variants.baseline.successes}/${item.variants.baseline.attempts}`,
    `${item.variants.candidate.successes}/${item.variants.candidate.attempts}`, format(item.delta_percentage_points)]));
  const heading = document.createElement('h3'); heading.textContent = 'Попытки и причины ошибок'; target.append(heading);
  table(['Задача / повтор', 'Вариант', 'Завершение', 'Функциональная приёмка', 'Проваленные проверки', 'Подробности'],
    summary.attempts.map(attempt => {
      let link = '—';
      if (attempt.trial) {
        link = document.createElement('a'); link.textContent = 'Открыть в Harbor →';
        link.href = `/jobs/${encodeURIComponent(attempt.job_name)}/trials/${encodeURIComponent(attempt.trial)}`;
      }
      return [`${attempt.case} / ${attempt.repeat}`, attempt.variant, attempt.status,
        attempt.functional_pass === null ? 'Неизвестно' : attempt.functional_pass === 1 ? 'Пройдена' : 'Не пройдена',
        attempt.grading_issue || attempt.error || attempt.checks.filter(check => !check.passed).map(check => check.id).join(', ') || '—', link];
    }));
  for (const issue of summary.evidence_issues || []) {
    const warning = document.createElement('p'); warning.className = 'fb-note';
    warning.textContent = `${issue.job_name}: ${issue.issues.join('; ')}`; target.append(warning);
  }
  const limits = document.createElement('p'); limits.className = 'fb-note';
  limits.textContent = 'Неизвестные измерения не считаются нулевыми. Это открытый dev-набор; повторные попытки не заменяют новые задачи. Подробные расходы и ограничения сохранены в JSON.';
  target.append(limits);
};
