"""Evidence-based pilot reports. Suggested causes are hypotheses, never grades."""

from collections import Counter
import json
from pathlib import Path

from .metrics import efficiency


DIMENSIONS = {
    "boundaries": (
        "Граничные случаи и точность требований",
        "Возможно, реализация покрыла основной сценарий, но пропустила часть условий.",
        "На dev сравнить исходный профиль с одним правилом: перед реализацией выписать границы требований и проверить их тестами.",
    ),
    "async_state": (
        "Переходы состояния и устаревшие ответы",
        "Возможно, не учтены повторные события или связь ответа с текущим запросом.",
        "На dev проверить одно дополнение: описывать допустимые переходы и проверять перестановки событий; затем оценить на новой задаче этого класса.",
    ),
    "cross_file": (
        "Согласованность изменений между файлами",
        "Возможно, исполнитель исправил один файл без проверки наследования и остальных модулей.",
        "На dev проверить правило о поиске всех мест использования изменяемой версии и сверке итоговой конфигурации модулей.",
    ),
    "preservation": (
        "Сохранение существующего контракта",
        "Возможно, исправление нарушило соседнее поведение или изменило входные данные.",
        "На dev проверить одно требование к процессу: перечислить сохраняемые инварианты и проверить их после изменения.",
    ),
    "build": (
        "Компиляция и загрузка результата",
        "Возможны синтаксическая ошибка, несовместимость API либо проблема среды; одного теста недостаточно для выбора причины.",
        "Сначала прочитать grader.log и журнал исполнителя; проверить, доступна ли ему та же команда сборки, что оценщику.",
    ),
}

CHECK_DIMENSIONS = {
    "java-nearest-activity": {
        "compiles":"build", "owner_status_future":"boundaries", "nearest":"boundaries",
        "inclusive_now":"boundaries", "stable_tie":"boundaries", "null_entries":"boundaries",
        "null_list":"boundaries", "empty_no_matches":"boundaries", "no_mutation":"preservation",
    },
    "js-retry-state": {
        "loads":"build", "retry_only_failed":"async_state", "start":"async_state",
        "start_idle":"async_state", "start_invalid":"boundaries", "duplicate_start":"async_state",
        "stale_result":"async_state", "result_requires_pending":"async_state", "success":"async_state",
        "failure":"async_state", "failure_fallback":"boundaries", "unknown_identity":"preservation",
    },
    "maven-version-alignment": {
        "xml":"build", "root_coordinates":"preservation", "module_parent":"preservation",
        "single_version_source":"cross_file", "core_managed":"cross_file", "http_aligned":"cross_file",
        "utility_preserved":"preservation", "no_extra_dependencies":"preservation",
        "all_managed_versions_aligned":"cross_file", "no_plugins_repositories":"preservation",
    },
}


def diagnose(result):
    facts, directions = [], []
    status = result["status"]
    if status.startswith("executor_") or status == "grader_error":
        facts.append("Зафиксирована техническая остановка: " + status)
        directions.append({"area":"execution", "title":"Исполнение и инфраструктура",
                           "hypothesis":"Причина пока не установлена: нужны журналы запуска и проверки.",
                           "next_experiment":"Разобрать ошибку/тайм-аут и воспроизвести один запуск; до этого не менять содержательные rules.",
                           "evidence":[result["run_id"] + ":status"]})
    elif result["functional_pass"] is False:
        grouped = {}
        for check in result["evaluation"]["checks"]:
            if not check["passed"]:
                dimension = CHECK_DIMENSIONS.get(result["case"]["id"],{}).get(check["id"],"boundaries")
                grouped.setdefault(dimension,[]).append(check["id"])
        for dimension, checks in grouped.items():
            facts.append("Провалены проверки: " + ", ".join(checks))
            title, hypothesis, experiment = DIMENSIONS[dimension]
            directions.append({"area":dimension,"title":title,"hypothesis":hypothesis,
                               "next_experiment":experiment,
                               "evidence":[result["run_id"] + ":check:" + c for c in checks]})
    else:
        facts.append("Все обязательные проверки этой fixture пройдены.")
    observations = result.get("observations",[])
    reported_interventions = sum(e.get("kind")=="human_intervention" for e in observations)
    completeness = next((e["text"] for e in reversed(observations) if e.get("kind")=="intervention_summary"), None)
    gaps = []
    if not result.get("log_evidence"):
        gaps.append("Нет сохранённого журнала исполнителя; последовательность действий неизвестна.")
    if completeness is None:
        gaps.append("Нет итогового указания о вмешательствах человека; отсутствие событий не означает ноль вмешательств.")
    reported_models = (result.get("adapter_info") or {}).get("models", [])
    if (not result["profile"].get("model") and not reported_models) or not result["profile"].get("executor_version"):
        gaps.append("Не зафиксированы модель и/или версия исполнителя.")
    gaps.extend(result.get("measurement_issues", []))
    return {"run_id":result["run_id"],"case":result["case"]["id"],"profile":result["profile"]["id"],
            "status":status,"facts":facts,"directions":directions,"evidence_gaps":gaps,
            "reported_intervention_events":reported_interventions,
            "intervention_summary_reported":completeness is not None,
            "observation_counts":dict(Counter(e.get("kind","note") for e in observations)),
            "log_evidence":result.get("log_evidence",[])}


def display(value):
    if value is None:
        return "неизвестно"
    if isinstance(value, float):
        return "{:.8g}".format(value)
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def efficiency_lines(stats, title):
    n = stats["attempts"]
    lines = ["", "## Эффективность: " + display(title), "",
             "Успех: {}/{} ({:.1%}).".format(stats["successes"], n, stats["pass_rate"]), "",
             "| Метрика | Полная сумма | Известная часть | Полных попыток |",
             "|---|---:|---:|---:|"]
    rows = [("Время исполнителя, с", stats["executor_seconds"])]
    rows += [(name, stats["tokens"][key]) for key, name in (
        ("input_tokens", "Входные токены (включая кеш)"), ("output_tokens", "Выходные токены"),
        ("cached_input_tokens", "Кешированные входные токены"), ("total_tokens", "Все токены"))]
    rows += [("Стоимость, " + code, values) for code, values in stats["cost_by_currency"].items()]
    for name, values in rows:
        lines.append("| {} | {} | {} | {}/{} |".format(name, display(values["total"]),
                     display(values["observed_total"]), values["measured_runs"], n))
    lines += ["", "Медиана известного времени исполнителя: {} с.".format(display(stats["executor_seconds"]["median_observed"])),
              "Стоимость измерена полностью для {}/{} попыток.".format(stats["cost_measured_runs"], n)]
    if not stats["cost_by_currency"]:
        lines.append("Стоимость и стоимость успешной попытки: неизвестно.")
    for code, values in stats["cost_by_currency"].items():
        lines += ["Стоимость успешной попытки: {} {}. Источники: {}.".format(
            display(values["per_successful_attempt"]), code,
            ", ".join("{}: {}".format(k, v) for k, v in values["methods"].items()))]
    return lines


def write_report(comparison, groups, output):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("Report output already exists; choose a new directory")
    single = comparison is None
    if len(groups) != (1 if single else 2) or not all(groups):
        raise ValueError("Report requires nonempty result groups")
    for group in groups:
        if len({r["profile"]["digest"] for r in group}) != 1:
            raise ValueError("Each group must contain exactly one profile configuration")
        if len({r["run_id"] for r in group}) != len(group):
            raise ValueError("Duplicate run IDs in report")
        for case_id in {r["case"]["id"] for r in group}:
            versions = {(r["case"]["digest"], json.dumps(r["runtime"], sort_keys=True))
                        for r in group if r["case"]["id"] == case_id}
            if len(versions) != 1:
                raise ValueError("Incompatible case versions or runtimes in report")
    sides = ("current",) if single else ("baseline", "candidate")
    analyses = []
    for side, group in zip(sides, groups):
        analyses.extend({**diagnose(result),"side":side} for result in group)
    counts = Counter((a["side"],d["area"]) for a in analyses for d in a["directions"])
    # Do not export command arguments, raw prompts, source code or free-text operator notes.
    metadata = [{"run_id":r["run_id"],"case":r["case"]["id"],"case_digest":r["case"]["digest"],
                 "profile_digest":r["profile"]["digest"],"model":r["profile"].get("model"),
                 "executor_version":r["profile"].get("executor_version"),"runtime":r["runtime"],
                 "adapter":r["profile"].get("adapter"),"adapter_info":r.get("adapter_info"),
                 "candidate_digest":r.get("candidate_digest"),"usage":r.get("usage"),"cost":r.get("cost"),
                 "executor_seconds":(r.get("executor") or {}).get("seconds"),
                 "checks":[{"id": c["id"], "passed": c["passed"]}
                           for c in (r.get("evaluation") or {}).get("checks", [])],
                 "measurement_evidence":r.get("measurement_evidence"),
                 "measurement_issues":r.get("measurement_issues", [])}
                for group in groups for r in group]
    stats = {side: efficiency(group) for side, group in zip(sides, groups)}
    case_stats = {side: [{"case": case_id, **efficiency([r for r in group if r["case"]["id"] == case_id])}
                         for case_id in sorted({r["case"]["id"] for r in group})]
                  for side, group in zip(sides, groups)}
    summary = {"schema_version":2, "kind":"single" if single else "comparison",
               "cases":case_stats["current"] if single else comparison["cases"],
               "efficiency":stats, "efficiency_by_case":case_stats,
               "status_counts":[s["status_counts"] for s in stats.values()],
               "macro_delta_percentage_points":None if single else comparison["macro_delta_percentage_points"],
               "changed_profile_fields":[] if single else list(comparison["profile_changes"]),
               "warnings":["Exploratory synthetic pilot, not evidence of production effectiveness"] if single else comparison["warnings"],
               "runs":analyses,"metadata":metadata,
               "method":"Check failures establish observed symptoms; directions are untested hypotheses."}
    lines = ["# FlexBench: результаты и направления проверки", "",
             "Направления ниже — гипотезы по проваленным требованиям, а не доказанные причины ошибок.",
             "Три приближённые dev-задачи не доказывают преимущество на рабочих проектах.", ""]
    if single:
        profile = groups[0][0]["profile"]
        configured_model = profile.get("model") or ("настройки CLI" if profile.get("adapter") == "gigacode" else None)
        lines += ["Профиль: {}. Заданная модель: {}. Версия исполнителя: {}.".format(
            display(profile["id"]), display(configured_model), display(profile.get("executor_version"))), "",
            "| Задача | Попытки | Успехи | Доля успеха |", "|---|---:|---:|---:|"]
        for row in summary["cases"]:
            lines.append("| {} | {} | {} | {:.1%} |".format(row["case"], row["attempts"], row["successes"], row["pass_rate"]))
    else:
        lines += ["| Задача | Попыток на сторону | Baseline | Candidate | Изменение, п.п. |",
                  "|---|---:|---:|---:|---:|"]
        for row in comparison["cases"]:
            lines.append("| {} | {} | {:.0%} | {:.0%} | {:+.1f} |".format(row["case"],row["runs_per_side"],
                         row["baseline_pass_rate"],row["candidate_pass_rate"],row["delta_percentage_points"]))
    lines += ["", "Полные суммы доступны только при полном покрытии всех попыток. Известная часть может быть неполной.",
              "Стоимость успешной попытки = расходы на все попытки / число успешных попыток, включая расходы на ошибки.",
              "При отсутствии успехов, неполной стоимости или разных валютах эта величина неизвестна.",
              "reported — сообщённая стоимость; estimated — оценка по сохранённому тарифу. Валюты не конвертируются.",
              "Время — только исполнение, без подготовки и grader; ручное время неизвестно."]
    for side, group in zip(sides, groups):
        models = sorted({model for r in group for model in (r.get("adapter_info") or {}).get("models", [])})
        if models:
            lines += ["", "Модели, сообщённые GigaCode ({}): {}.".format(side, ", ".join(display(m) for m in models))]
        lines += efficiency_lines(stats[side], side)
    lines += ["", "## Метрики каждой попытки", "",
              "| Сторона / run_id | Статус | Время, с | Вход / выход / кеш | Стоимость | Источник / полнота |",
              "|---|---|---:|---|---|---|"]
    for side, group in zip(sides, groups):
        for result in group:
            usage, cost = result.get("usage") or {}, result.get("cost") or {}
            sources = []
            for metric in (usage, cost):
                if metric:
                    sources.append("{} / {}{}".format(display(metric["source"]),
                                   "полная" if metric["complete"] else "частичная",
                                   " / " + metric["method"] if "method" in metric else ""))
            lines.append("| {} / {} | {} | {} | {} | {} | {} |".format(
                side, display(result["run_id"]), display(result["status"]),
                display((result.get("executor") or {}).get("seconds")),
                " / ".join(display(usage.get(k)) for k in ("input_tokens", "output_tokens", "cached_input_tokens")),
                "{} {}".format(display(cost.get("amount")), cost["currency"]) if cost else "неизвестно",
                "; ".join(sources) or "неизвестно"))
    lines += ["", "## Повторяющиеся направления", "",
              "Числа — количество попыток с таким симптомом. Повторы одной задачи не независимые задачи.", ""]
    for (side, area), count in sorted(counts.items(),key=lambda item:(-item[1],item[0])):
        title = DIMENSIONS[area][0] if area in DIMENSIONS else "Исполнение и инфраструктура"
        lines.append("- {}: {} — {} попыток.".format(side,title,count))
    if not counts:
        lines.append("Все оценённые результаты прошли проверки; следующий шаг — расширение задач, а не вывод об отсутствии ошибок вообще.")
    for analysis in analyses:
        lines += ["", "## {} / {} / {}".format(analysis["side"],analysis["case"],analysis["run_id"]), ""]
        lines += ["- Наблюдение: " + fact for fact in analysis["facts"]]
        for direction in analysis["directions"]:
            lines += ["- Гипотеза: " + direction["hypothesis"],
                      "- Следующая проверка: " + direction["next_experiment"],
                      "- Основание: " + ", ".join(direction["evidence"])]
        lines += ["- Неизвестно: " + gap for gap in analysis["evidence_gaps"]]
        lines.append("- Зафиксировано событий вмешательства: {} (со слов оператора).".format(analysis["reported_intervention_events"]))
    lines += ["", "## Как выбрать улучшение", "",
              "Сначала устранить сбои исполнения и проверить фактически применённую модель/конфигурацию.",
              "Затем выбрать повторяющийся симптом, прочитать связанный diff и журнал, изменить один параметр профиля.",
              "Повторить baseline/candidate на dev, проверить также регрессии; подтвердить результат на новых задачах.",
              "Не делать вывод «модель не тестировала» только по проваленному критерию: это проверяется журналом.", "",
              "В summary.json включены проверки, версии и ссылки по run_id; сырые логи, код, команды и тексты заметок не включены.",
              "Для разбора причины отдельно используйте соответствующий run: changes.patch, candidate/, executor.log или session.log, observations.jsonl."]
    output.mkdir(parents=True)
    (output / "summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (output / "report.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    return output
