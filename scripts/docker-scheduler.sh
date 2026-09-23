#!/bin/sh
set -u

log() {
  printf '%s %s\n' "$(date -Iseconds)" "$*"
}

run_step() {
  name="$1"
  shift
  log "START ${name}"
  if "$@"; then
    log "OK ${name}"
    return 0
  fi
  code="$?"
  log "FAIL ${name} exit=${code}"
  return "$code"
}

run_required_step() {
  name="$1"
  shift
  run_step "$name" "$@" || exit 1
}

CYCLE_INTERVAL_SECONDS="${CYCLE_INTERVAL_SECONDS:-21600}"
RUN_DISCOVER_ON_START="${RUN_DISCOVER_ON_START:-1}"
DISCOVER_EVERY_CYCLES="${DISCOVER_EVERY_CYCLES:-4}"
RUN_MAINTENANCE_ON_START="${RUN_MAINTENANCE_ON_START:-1}"
MAINTENANCE_EVERY_CYCLES="${MAINTENANCE_EVERY_CYCLES:-24}"
DISCOVER_TIMEOUT="${DISCOVER_TIMEOUT:-4}"
DISCOVER_WORKERS="${DISCOVER_WORKERS:-10}"
PARSE_WORKERS="${PARSE_WORKERS:-10}"
FULL_TEXT_LIMIT="${FULL_TEXT_LIMIT:-200}"
FULL_TEXT_MIN_CHARS="${FULL_TEXT_MIN_CHARS:-800}"
AI_PROCESS_LIMIT="${AI_PROCESS_LIMIT:-100}"
AI_OFFLINE="${AI_OFFLINE:-0}"
SKIP_BOOTSTRAP="${SKIP_BOOTSTRAP:-0}"
# STREAMING_PIPELINE=1 заменяет parse+fetch-full-text+process на единый parse-process.
# На сервере с 1.9 ГБ RAM рекомендуется PARSE_WORKERS<=5 при стриминге.
STREAMING_PIPELINE="${STREAMING_PIPELINE:-0}"
STREAM_POLL_INTERVAL="${STREAM_POLL_INTERVAL:-10}"
STREAM_PROCESS_BATCH="${STREAM_PROCESS_BATCH:-20}"
# FULLTEXT_RETRY_TOO_SHORT=1 — повторять попытку для статей со статусом too_short
# (полезно после добавления trafilatura — запустить один раз вручную).
FULLTEXT_RETRY_TOO_SHORT="${FULLTEXT_RETRY_TOO_SHORT:-0}"
# FETCH_EXTERNAL_ENABLED=1 — источники network_region='external' (западные WAF/таймаут
# с РФ-сервера) фетчатся через зарубежный воркер. Шаг enqueue-external-scrape ставит
# их в external-fetch/external-playwright; команда сама no-op при выключенном контуре.
FETCH_EXTERNAL_ENABLED="${FETCH_EXTERNAL_ENABLED:-0}"
EXTERNAL_REFETCH_LIMIT="${EXTERNAL_REFETCH_LIMIT:-100}"
# Перепечатки (№21): одна новость, разошедшаяся по изданиям. Правило сужает корпус
# до десятков пар, решает модель, копия помечается (не удаляется) и уходит из ленты.
# Окно намеренно шире периода запуска: уже помеченные пары правило не выдаёт
# повторно, поэтому перекрытие почти ничего не стоит, а пропуск дубля стоит того,
# что заказчик снова видит четыре карточки одной новости.
# Раз в REPRINTS_INTERVAL_HOURS часов по времени прошлого прогона в базе (0 — выключено).
REPRINTS_INTERVAL_HOURS="${REPRINTS_INTERVAL_HOURS:-12}"
REPRINTS_DAYS="${REPRINTS_DAYS:-7}"
REPRINTS_LIMIT="${REPRINTS_LIMIT:-200}"
SOURCE_DISCOVERY_ENABLED="${SOURCE_DISCOVERY_ENABLED:-0}"
SOURCE_DISCOVERY_EVERY_CYCLES="${SOURCE_DISCOVERY_EVERY_CYCLES:-24}"
SOURCE_DISCOVERY_TOPIC_LIMIT="${SOURCE_DISCOVERY_TOPIC_LIMIT:-3}"
SOURCE_DISCOVERY_LIMIT="${SOURCE_DISCOVERY_LIMIT:-10}"
SOURCE_DISCOVERY_ARTICLE_LIMIT="${SOURCE_DISCOVERY_ARTICLE_LIMIT:-5}"
SOURCE_DISCOVERY_OFFLINE="${SOURCE_DISCOVERY_OFFLINE:-1}"
SOURCE_DISCOVERY_EVALUATE="${SOURCE_DISCOVERY_EVALUATE:-1}"
SOURCE_DISCOVERY_TOPICS="${SOURCE_DISCOVERY_TOPICS:-}"
SOURCE_DISCOVERY_PLANNER_ENABLED="${SOURCE_DISCOVERY_PLANNER_ENABLED:-1}"
SOURCE_DISCOVERY_MODE="${SOURCE_DISCOVERY_MODE:-plan}"
SOURCE_DISCOVERY_TARGET_PER_TOPIC="${SOURCE_DISCOVERY_TARGET_PER_TOPIC:-10}"
SOURCE_DISCOVERY_MAX_ACTIONS="${SOURCE_DISCOVERY_MAX_ACTIONS:-5}"
SOURCE_DISCOVERY_MAX_ITERATIONS="${SOURCE_DISCOVERY_MAX_ITERATIONS:-3}"
SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS="${SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS:-4}"
SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES="${SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES:-100}"
SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS="${SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS:-100}"
SIGNAL_DISCOVERY_ENABLED="${SIGNAL_DISCOVERY_ENABLED:-0}"
SIGNAL_DISCOVERY_EVERY_CYCLES="${SIGNAL_DISCOVERY_EVERY_CYCLES:-4}"
SIGNAL_DISCOVERY_DAYS="${SIGNAL_DISCOVERY_DAYS:-14}"
SIGNAL_DISCOVERY_LIMIT="${SIGNAL_DISCOVERY_LIMIT:-80}"
SIGNAL_DISCOVERY_MIN_SCORE="${SIGNAL_DISCOVERY_MIN_SCORE:-40}"
SIGNAL_DISCOVERY_MAX_SIGNALS="${SIGNAL_DISCOVERY_MAX_SIGNALS:-10}"
SIGNAL_DISCOVERY_OFFLINE="${SIGNAL_DISCOVERY_OFFLINE:-1}"
SIGNAL_DISCOVERY_WEB="${SIGNAL_DISCOVERY_WEB:-0}"
SIGNAL_DISCOVERY_WEB_ONLY="${SIGNAL_DISCOVERY_WEB_ONLY:-0}"
SIGNAL_DISCOVERY_WEB_QUERY_LIMIT="${SIGNAL_DISCOVERY_WEB_QUERY_LIMIT:-8}"
SIGNAL_DISCOVERY_RESEARCH_ROUNDS="${SIGNAL_DISCOVERY_RESEARCH_ROUNDS:-2}"
SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT="${SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT:-20}"
SIGNAL_DISCOVERY_DAILY_ENABLED="${SIGNAL_DISCOVERY_DAILY_ENABLED:-1}"

if [ "$SKIP_BOOTSTRAP" != "1" ]; then
  log "Bootstrapping database and seed data"
  run_required_step "init-db" python -m oiltech_digest.cli init-db
  run_required_step "seed-sources" python -m oiltech_digest.cli seed-sources
  run_required_step "seed-tags" python -m oiltech_digest.cli seed-tags
  run_required_step "seed-scoring" python -m oiltech_digest.cli seed-scoring
  run_step "seed-signal-topics" python -m oiltech_digest.cli seed-signal-topics
  run_step "apply-source-overrides" python -m oiltech_digest.cli apply-source-overrides
fi

cycle=0
while true; do
  log "Cycle ${cycle} started"

  if [ "$RUN_MAINTENANCE_ON_START" = "1" ] && [ "$cycle" -eq 0 ]; then
    run_step "maintenance-cleanup" python -m oiltech_digest.cli maintenance-cleanup
  elif [ "$MAINTENANCE_EVERY_CYCLES" -gt 0 ] && [ $((cycle % MAINTENANCE_EVERY_CYCLES)) -eq 0 ]; then
    run_step "maintenance-cleanup" python -m oiltech_digest.cli maintenance-cleanup
  fi

  if [ "$RUN_DISCOVER_ON_START" = "1" ] && [ "$cycle" -eq 0 ]; then
    run_step "discover-rss" python -m oiltech_digest.cli discover-rss --workers "$DISCOVER_WORKERS" --timeout "$DISCOVER_TIMEOUT"
  elif [ "$DISCOVER_EVERY_CYCLES" -gt 0 ] && [ $((cycle % DISCOVER_EVERY_CYCLES)) -eq 0 ]; then
    run_step "discover-rss" python -m oiltech_digest.cli discover-rss --workers "$DISCOVER_WORKERS" --timeout "$DISCOVER_TIMEOUT"
  fi

  if [ "$STREAMING_PIPELINE" = "1" ]; then
    # Стриминг: parse + AI-обработка параллельно в одном процессе.
    if [ "$AI_PROCESS_LIMIT" -gt 0 ] && { [ "$AI_OFFLINE" = "1" ] || [ -n "${OPENAI_API_KEY:-}" ]; }; then
      _offline_flag=""
      [ "$AI_OFFLINE" = "1" ] && _offline_flag="--offline"
      run_step "parse-process" python -m oiltech_digest.cli parse-process \
        --workers "$PARSE_WORKERS" \
        --process-limit "$STREAM_PROCESS_BATCH" \
        --poll-interval "$STREAM_POLL_INTERVAL" \
        ${_offline_flag}
    else
      run_step "parse" python -m oiltech_digest.cli parse --workers "$PARSE_WORKERS"
      log "SKIP process: OPENAI_API_KEY is empty and AI_OFFLINE!=1"
    fi
  else
    # Классический последовательный режим.
    run_step "parse" python -m oiltech_digest.cli parse --workers "$PARSE_WORKERS"
    _retry_flag=""
    [ "$FULLTEXT_RETRY_TOO_SHORT" = "1" ] && _retry_flag="--retry-too-short"
    run_step "fetch-full-text" python -m oiltech_digest.cli fetch-full-text --limit "$FULL_TEXT_LIMIT" --min-chars "$FULL_TEXT_MIN_CHARS" ${_retry_flag}

    if [ "$AI_PROCESS_LIMIT" -gt 0 ]; then
      if [ "${AI_EXECUTION_REGION:-ru}" = "external" ]; then
        # Внешний контур: РФ-core не зовёт OpenAI сам, а ставит задачу в external-ai —
        # её заберёт зарубежный worker.
        run_step "enqueue-process" python -m oiltech_digest.cli enqueue-process --limit "$AI_PROCESS_LIMIT"
      elif [ "$AI_OFFLINE" = "1" ]; then
        run_step "process-offline" python -m oiltech_digest.cli process --offline --limit "$AI_PROCESS_LIMIT"
      elif [ -n "${OPENAI_API_KEY:-}" ]; then
        run_step "process" python -m oiltech_digest.cli process --limit "$AI_PROCESS_LIMIT"
      else
        log "SKIP process: OPENAI_API_KEY is empty"
      fi
    fi
  fi

  if [ "$FETCH_EXTERNAL_ENABLED" = "1" ]; then
    # Западные источники (network_region='external') фетчим через зарубежный воркер —
    # с РФ-сервера к ним нет доступа. Задачи разберёт NL external-worker.
    run_step "enqueue-external-scrape" python -m oiltech_digest.cli enqueue-external-scrape
    # Обрывки у тех же источников: лента даёт анонс, а локальная дозагрузка их не
    # берёт (403 с РФ-адреса, попытка одна навсегда). Тело добирает воркер.
    run_step "enqueue-external-refetch" python -m oiltech_digest.cli enqueue-external-refetch \
      --limit "$EXTERNAL_REFETCH_LIMIT"
  fi

  # Срок — от прошлого прогона с записью в базе, а не «каждый 24-й цикл»: счётчик
  # обнулялся при каждом перезапуске, а цикл идёт ~41 мин, а не 30 — «дважды в сутки»
  # на деле выходило раз в 16,5 ч и сдвигалось каждым выкатом (19.09). Повтор при
  # перезапуске исключает та же проверка по базе.
  if [ "$REPRINTS_INTERVAL_HOURS" != "0" ]; then
    if [ "$AI_OFFLINE" = "1" ] || [ -n "${OPENAI_API_KEY:-}" ]; then
      run_step "find-reprints" python -m oiltech_digest.cli find-reprints \
        --days "$REPRINTS_DAYS" --limit "$REPRINTS_LIMIT" --apply \
        --min-interval-hours "$REPRINTS_INTERVAL_HOURS"
    else
      log "SKIP find-reprints: OPENAI_API_KEY is empty"
    fi
  fi

  if [ "$SOURCE_DISCOVERY_ENABLED" = "1" ]; then
    if [ "$SOURCE_DISCOVERY_EVERY_CYCLES" -gt 0 ] && [ $((cycle % SOURCE_DISCOVERY_EVERY_CYCLES)) -eq 0 ]; then
      _source_discovery_offline_flag=""
      if [ "$SOURCE_DISCOVERY_OFFLINE" = "1" ]; then
        _source_discovery_offline_flag="--offline"
      fi

      _source_discovery_evaluate_flag="--no-evaluate"
      if [ "$SOURCE_DISCOVERY_EVALUATE" = "1" ]; then
        _source_discovery_evaluate_flag="--evaluate"
      fi

      if [ "$SOURCE_DISCOVERY_MODE" = "loop" ] && [ -z "$SOURCE_DISCOVERY_TOPICS" ]; then
        run_step "enqueue-agent-loop" python -m oiltech_digest.cli enqueue-agent-loop \
          --target-per-topic "$SOURCE_DISCOVERY_TARGET_PER_TOPIC" \
          --topic-limit "$SOURCE_DISCOVERY_TOPIC_LIMIT" \
          --candidate-limit "$SOURCE_DISCOVERY_LIMIT" \
          --max-actions "$SOURCE_DISCOVERY_MAX_ACTIONS" \
          --max-iterations "$SOURCE_DISCOVERY_MAX_ITERATIONS" \
          --article-limit "$SOURCE_DISCOVERY_ARTICLE_LIMIT" \
          --max-daily-loop-runs "$SOURCE_DISCOVERY_MAX_DAILY_LOOP_RUNS" \
          --max-daily-candidates "$SOURCE_DISCOVERY_MAX_DAILY_CANDIDATES" \
          --max-daily-evaluations "$SOURCE_DISCOVERY_MAX_DAILY_EVALUATIONS" \
          $_source_discovery_offline_flag \
          $_source_discovery_evaluate_flag
      elif [ "$SOURCE_DISCOVERY_PLANNER_ENABLED" = "1" ] && [ -z "$SOURCE_DISCOVERY_TOPICS" ]; then
        run_step "enqueue-agent-plan" python -m oiltech_digest.cli enqueue-agent-plan \
          --target-per-topic "$SOURCE_DISCOVERY_TARGET_PER_TOPIC" \
          --topic-limit "$SOURCE_DISCOVERY_TOPIC_LIMIT" \
          --candidate-limit "$SOURCE_DISCOVERY_LIMIT" \
          --max-actions "$SOURCE_DISCOVERY_MAX_ACTIONS" \
          $_source_discovery_offline_flag \
          $_source_discovery_evaluate_flag
      else
        set -- \
          --topic-limit "$SOURCE_DISCOVERY_TOPIC_LIMIT" \
          --limit "$SOURCE_DISCOVERY_LIMIT" \
          --article-limit "$SOURCE_DISCOVERY_ARTICLE_LIMIT" \
          $_source_discovery_offline_flag \
          $_source_discovery_evaluate_flag

        if [ -n "$SOURCE_DISCOVERY_TOPICS" ]; then
          old_ifs="$IFS"
          IFS=","
          for topic in $SOURCE_DISCOVERY_TOPICS; do
            topic="$(printf '%s' "$topic" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
            if [ -n "$topic" ]; then
              set -- "$@" --topic "$topic"
            fi
          done
          IFS="$old_ifs"
        fi

        run_step "enqueue-source-discovery" python -m oiltech_digest.cli enqueue-source-discovery "$@"
      fi
    fi
  fi

  if [ "$SIGNAL_DISCOVERY_DAILY_ENABLED" = "1" ]; then
    run_step "enqueue-daily-signal-discovery" python -m oiltech_digest.cli enqueue-daily-signal-discovery
  fi

  if [ "$SIGNAL_DISCOVERY_ENABLED" = "1" ]; then
    if [ "$SIGNAL_DISCOVERY_EVERY_CYCLES" -gt 0 ] && [ $((cycle % SIGNAL_DISCOVERY_EVERY_CYCLES)) -eq 0 ]; then
      _signal_offline_flag="--offline"
      if [ "$SIGNAL_DISCOVERY_OFFLINE" != "1" ]; then
        _signal_offline_flag="--no-offline"
      fi
      _signal_web_flag=""
      if [ "$SIGNAL_DISCOVERY_WEB" = "1" ]; then
        _signal_web_flag="--web"
      fi
      _signal_web_only_flag=""
      if [ "$SIGNAL_DISCOVERY_WEB_ONLY" = "1" ]; then
        _signal_web_only_flag="--web-only"
      fi
      run_step "enqueue-signal-discovery" python -m oiltech_digest.cli enqueue-signal-discovery \
        --days "$SIGNAL_DISCOVERY_DAYS" \
        --limit "$SIGNAL_DISCOVERY_LIMIT" \
        --min-score "$SIGNAL_DISCOVERY_MIN_SCORE" \
        --max-signals "$SIGNAL_DISCOVERY_MAX_SIGNALS" \
        --web-query-limit "$SIGNAL_DISCOVERY_WEB_QUERY_LIMIT" \
        --research-rounds "$SIGNAL_DISCOVERY_RESEARCH_ROUNDS" \
        --web-fulltext-limit "$SIGNAL_DISCOVERY_WEB_FULLTEXT_LIMIT" \
        $_signal_web_flag \
        $_signal_web_only_flag \
        $_signal_offline_flag
    fi
  fi

  # Сторож полос: застой внешней очереди или очередь без живого воркера — «FAIL
  # check-lanes» и строки ТРЕВОГА в логе (цикл не прерывается).
  run_step "check-lanes" python -m oiltech_digest.cli check-lanes

  run_step "stats" python -m oiltech_digest.cli stats
  cycle=$((cycle + 1))
  log "Cycle finished. Sleeping ${CYCLE_INTERVAL_SECONDS}s"
  sleep "$CYCLE_INTERVAL_SECONDS"
done
