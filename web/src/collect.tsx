import { CollectionForm } from "./collect-form";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { Row } from "./api";
import { api, apiUploadForm } from "./api";
import {
  Feedback,
  Pill,
  StateView,
  Table,
  TimingList,
  type Timings,
  cell,
  downloadAction,
  downloadPlatformWorkbook,
  rowsOf,
  sendAction,
  useAction,
  useGet,
} from "./ui";

const RUN_KEY = "zhol.scout.run";

type Category = { label: string; sector: string; counts: Record<string, number> };
type Source = {
  id: string;
  label: string;
  available: boolean;
  note: string;
  live_available?: boolean;
  live_note?: string;
  collection_state?: "available" | "restricted" | "unauthorized";
  limit_note?: string;
};
type Mode = { id: string; label: string; available: boolean; note: string; kind?: string; cities?: string[] };
type Setup = {
  ready?: boolean;
  error?: string;
  default_city?: string;
  cities?: string[];
  categories?: Category[];
  sources?: Source[];
  collection_modes?: Mode[];
  live_2gis?: { available?: boolean; cities?: string[]; blocker?: string | null };
  browser_2gis?: {
    available?: boolean;
    cities?: string[];
    blocker?: string | null;
    source_kind?: string;
    engine?: boolean;
    origin?: string | null;
  };
  limit?: { min: number; max: number; default: number };
  skip_existing?: { note?: string };
  stop?: { supported?: boolean };
  resume?: { supported?: boolean };
  note?: string;
  browser_note?: string;
};
type RunJob = Row & { batch?: Row | null };

export function collectionStatus(value: unknown): string {
  const raw = typeof value === "string" ? value : "";
  const labels: Record<string, string> = {
    queued: "Ожидает запуска",
    running: "Выполняется",
    done: "Завершён",
    completed: "Завершён",
    completed_with_warnings: "Завершён с предупреждениями",
    limit_reached: "Достигнут лимит",
    partial: "Частичный результат",
    blocked: "Нужна проверка или доступ ограничен",
    stopped: "Остановлен",
    failed: "Ошибка",
    dead: "Ошибка",
    paused: "Пауза",
    interrupted: "Прерван",
    awaiting_manual_verification: "Ждет проверку в браузере",
  };
  return labels[raw] || cell(value);
}

function readRun(): number[] {
  try {
    const body = JSON.parse(sessionStorage.getItem(RUN_KEY) || "") as { ids?: unknown };
    return Array.isArray(body.ids) ? body.ids.filter((item): item is number => typeof item === "number") : [];
  } catch {
    return [];
  }
}

function writeRun(ids: number[]): void {
  sessionStorage.setItem(RUN_KEY, JSON.stringify({ ids }));
}

export function sourceState(source: Source | undefined): "available" | "restricted" | "unauthorized" {
  if (source?.collection_state) return source.collection_state;
  return source?.live_available ? "available" : "unauthorized";
}

export function browserReady(source: Source | undefined): boolean {
  return sourceState(source) === "available";
}

export function Scout(): ReactNode {
  const setup = useGet<Setup>("/api/v1/admin/scout/setup");
  const options = setup.data;
  const cities = options?.cities || [];
  const categories = options?.categories || [];
  const sources = options?.sources || [];
  const limitRule = options?.limit || { min: 1, max: 500, default: 20 };
  const [tick, setTick] = useState(0);
  const [mode, setMode] = useState("fixture");
  const [paired, setPaired] = useState<string[]>([]);
  const [city, setCity] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const [queryText, setQueryText] = useState("");
  const [platforms, setPlatforms] = useState<string[]>(["2gis"]);
  const [limit, setLimit] = useState(String(limitRule.default));
  const [filter, setFilter] = useState("");
  const [errors, setErrors] = useState<string[]>([]);
  const [summary, setSummary] = useState<string[]>([]);
  const [jobs, setJobs] = useState<RunJob[]>([]);
  const action = useAction(() => setTick((value) => value + 1));
  useEffect(() => {
    const status = overall(jobs);
    if (status === "failed" || status === "dead" || status === "interrupted") action.clearMessage();
  }, [jobs, action.clearMessage]);
  const data = useGet<Row>(`/api/v1/admin/scout/batches?limit=50&offset=0&refresh=${tick}`);
  const rows = rowsOf(data.data);
  const availableSources = sources.filter((source) => source.available).map((source) => source.id);
  const modes = options?.collection_modes || [];
  const selected = modes.find((item) => item.id === mode);
  const liveCities = options?.live_2gis?.cities || [];
  const browserCities = options?.browser_2gis?.cities || [];
  const pairOn = paired.length > 0;
  const activeMode = pairOn ? (paired.length === 1 ? paired[0] : "browser_pair") : mode;
  const live = activeMode === "live_2gis";
  const browser = activeMode.startsWith("browser_");
  const catalogFree = live || browser;
  const pairCities = paired.map((id) => modes.find((item) => item.id === id)?.cities || []).filter((names) => names.length > 0);
  const sharedCities = pairCities.length === 0 ? [] : pairCities.reduce((names, next) => names.filter((name) => next.includes(name)));
  const cityChoices = pairOn ? (sharedCities.length > 0 ? sharedCities : ["Алматы"]) : live ? liveCities : browser ? browserCities : cities;

  useEffect(() => {
    if (!options?.ready || city) return;
    setCity(options.default_city || (options.cities || [])[0] || "");
    setLimit(String(options.limit?.default || 20));
    setPlatforms((options.sources || []).filter((source) => source.available).map((source) => source.id));
  }, [options, city]);

  useEffect(() => {
    if (paired.length > 0) {
      setPlatforms(paired.map((id) => (id === "browser_google" ? "google" : "2gis")));
      return;
    }
    if (mode === "browser_yandex") setPlatforms(["yandex"]);
    else if (mode === "browser_google") setPlatforms(["google"]);
    else if (mode === "browser_2gis" || mode === "live_2gis") setPlatforms(["2gis"]);
  }, [mode, paired]);

  useEffect(() => {
    const ids = readRun();
    if (ids.length === 0) return;
    let cancel = false;
    void refreshJobs(ids).then((next) => {
      if (!cancel) setJobs(next);
    });
    return () => {
      cancel = true;
    };
  }, []);

  const active = jobs.some((job) => isOpen(job));
  const jobKey = jobs.map((job) => String(job.id)).join(",");
  useEffect(() => {
    if (!active) return;
    const ids = jobKey.split(",").map((item) => Number(item)).filter((item) => Number.isInteger(item));
    const timer = window.setInterval(() => {
      void refreshJobs(ids).then(setJobs);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [active, jobKey]);

  const visible = useMemo(() => {
    const needle = filter.trim().toLocaleLowerCase("ru");
    return categories.filter((item) => !needle || item.label.toLocaleLowerCase("ru").includes(needle) || item.sector.toLocaleLowerCase("ru").includes(needle));
  }, [categories, filter]);

  const queries = queryLines(queryText);
  const enabledLabels = categories.filter((item) => catalogFree || countFor(item, city) > 0).map((item) => item.label);
  const enabledKey = enabledLabels.join("\n");

  useEffect(() => {
    const allowed = new Set(enabledKey ? enabledKey.split("\n") : []);
    setPicked((current) => {
      const next = current.filter((label) => allowed.has(label));
      return next.length === current.length ? current : next;
    });
  }, [enabledKey]);

  useEffect(() => {
    if (cityChoices.length > 0 && !cityChoices.includes(city)) setCity(cityChoices[0]);
  }, [cityChoices, city]);

  useEffect(() => {
    setSummary([]);
  }, [city, enabledKey, queryText, limit, mode, paired]);

  function chooseSummary(): void {
    const blocked = paired.length
      ? modes.filter((item) => paired.includes(item.id) && !item.available)
      : modes.filter((item) => item.id === mode && !item.available);
    if (blocked.length > 0) {
      setErrors(blocked.map((item) => item.note));
      setSummary([]);
      return;
    }
    const next = validate(city, picked, queries, platforms, limit, limitRule, allowedPlatforms(activeMode, availableSources, paired), categories, catalogFree);
    setErrors(next);
    if (next.length > 0) {
      setSummary([]);
      return;
    }
    const parsed = Number(limit);
    setSummary([
      `Город: ${city}.`,
      `Режим: ${pairOn && paired.length > 1 ? "2ГИС и Google Maps, отдельные сборы" : modes.find((item) => item.id === activeMode)?.label || "Тестовый каталог"}.`,
      `Категории: ${picked.length > 0 ? picked.join(", ") : "не выбраны"}.`,
      `Дополнительные запросы: ${queries.length}.`,
      `Источники: ${sourceNames(platforms, sources)}.`,
      `Поисковых задач: ${picked.length + queries.length}.`,
      pairOn && paired.length > 1
        ? `Лимит: ${parsed} компаний на категорию для каждого источника отдельно.`
        : `Лимит: ${parsed} компаний на категорию.`,
      options?.skip_existing?.note || "Компания с тем же номером в 2ГИС не создается второй раз.",
      live
        ? "Будет вызван официальный Catalog API 2ГИС. Сайт 2ГИС не открывается."
        : browser
          ? activeMode === "browser_pair"
            ? "Будут созданы два отдельных сбора: 2ГИС и Google Maps. Лимит у каждого свой. Ошибка одного сбора не отменяет другой. Chrome открывается по очереди, чтобы не делить один профиль. Проверка «не робот» не обходится."
            : activeMode === "browser_yandex"
            ? "Будет запущен сборщик Яндекс Карт. Проверка «не робот» не обходится. Результат появится только после реального сбора."
            : activeMode === "browser_google"
              ? "Будет запущен сборщик Google Maps. Проверка «не робот» не обходится. Результат появится только после реального сбора."
              : options?.browser_2gis?.source_kind && options.browser_2gis.source_kind !== "live_2gis"
                ? "Будет запущен существующий сборщик на локальном HTML или localhost. Сайт 2ГИС не открывается. Ключ Catalog API не используется. Результат появится только после реального сбора."
                : "Будет запущен существующий сборщик публичных карточек 2ГИС. Ключ Catalog API не используется. Результат появится только после реального сбора."
          : "Живой обход площадок не запускается.",
    ]);
  }

  return (
    <section>
      <h1>Сбор компаний</h1>
      <p className="hint">Выберите города, категории и источники. Кнопка «Начать сбор» ставит задачи в очередь и сама площадку не открывает.</p>
      <StateView loading={setup.loading} error={setup.error} />
      {options && options.ready === false ? <p className="error">{options.error || "Сбор из формы недоступен."}</p> : null}
      {options ? (
        <CollectionForm
          options={options}
          pending={action.pending}
          onRun={(work) => void action.run(work)}
          onStarted={(ids) => {
            writeRun(ids);
            void refreshJobs(ids).then(setJobs);
            setTick((value) => value + 1);
          }}
        />
      ) : null}
      <div className="actions">
        <button type="button" disabled={action.pending} onClick={() => void action.run(() => downloadPlatformWorkbook())}>
          Скачать Excel
        </button>
        <a className="button-link" href="#/companies">
          Открыть результаты
        </a>
      </div>
      <Feedback message={action.message} error={action.error} />
      <RunProgress
        jobs={jobs}
        pending={action.pending}
        canStop={options?.stop?.supported !== false}
        canResume={options?.resume?.supported !== false}
        onSignal={(path, confirmText) => void action.run(() => sendAction(path).then((body) => `Статус: ${collectionStatus(body.status)}.`), confirmText)}
      />
      <details className="extra">
        <summary>Дополнительно</summary>
        {options?.note ? <p className="hint">{options.note}</p> : null}
        {options?.browser_note ? <p className="hint">{options.browser_note}</p> : null}
      {options?.ready ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            chooseSummary();
          }}
        >
          <div className="collect-grid">
            <section>
              <h2>Режим</h2>
              {pairOn ? <p className="hint">2ГИС и Google Maps можно выбрать вместе. Каждый источник получит свой сбор и свой лимит.</p> : null}
              {modes.map((item) => {
                const pairable = item.id === "browser_2gis" || item.id === "browser_google";
                return (
                  <label className={item.available ? "choice" : "choice source-off"} key={item.id}>
                    {pairable ? (
                      <input
                        type="checkbox"
                        checked={paired.includes(item.id)}
                        disabled={!item.available}
                        onChange={() => {
                          const next = paired.includes(item.id) ? paired.filter((id) => id !== item.id) : [...paired, item.id];
                          setPaired(next);
                          setMode(next.length > 0 ? "browser_pair" : "fixture");
                        }}
                      />
                    ) : (
                      <input
                        type="radio"
                        name="collection-mode"
                        checked={mode === item.id && paired.length === 0}
                        disabled={!item.available}
                        onChange={() => {
                          setPaired([]);
                          setMode(item.id);
                        }}
                      />
                    )}
                    <span>
                      {item.label}
                      {item.available ? "" : " (недоступно)"}
                      <span className="mark">{item.note}</span>
                    </span>
                  </label>
                );
              })}
            </section>
            <section>
              <h2>Город</h2>
              <label>
                Город сбора
                <select value={city} onChange={(event) => setCity(event.target.value)}>
                  {cityChoices.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
            </section>
            <section>
              <h2>Источники</h2>
              {sources.map((source) => {
                const forced = pairOn && (source.id === "2gis" || source.id === "google");
                return (
                <label className={source.available || forced ? "choice" : "choice source-off"} key={source.id}>
                  <input
                    type="checkbox"
                    checked={(source.available || forced) && platforms.includes(source.id)}
                    disabled={forced || !source.available}
                    onChange={(event) => {
                      setPlatforms((current) =>
                        event.target.checked ? [...current, source.id] : current.filter((item) => item !== source.id),
                      );
                    }}
                  />
                  <span>
                    {source.label}
                    {source.available || forced ? "" : " (недоступно)"}
                    <span className="mark">{source.note}</span>
                    <span className="mark">
                      {source.live_available ? "Живой сбор: доступен. " : "Живой сбор: недоступен. "}
                      {source.live_note || ""}
                    </span>
                  </span>
                </label>
                );
              })}
            </section>
            <section>
              <h2>Категории</h2>
              <label>
                Поиск категории
                <input value={filter} placeholder="Название категории" onChange={(event) => setFilter(event.target.value)} />
              </label>
              <div className="actions">
                <button type="button" onClick={() => setPicked(enabledLabels)}>
                  Выбрать все
                </button>
                <button type="button" onClick={() => setPicked([])}>
                  Очистить
                </button>
              </div>
              <div className="choices">
                {visible.length === 0 ? <p>Нет таких категорий.</p> : null}
                {visible.map((item) => {
                  const count = countFor(item, city);
                  const disabled = !catalogFree && count < 1;
                  return (
                    <label className={disabled ? "choice source-off" : "choice"} key={item.label}>
                      <input
                        type="checkbox"
                        checked={picked.includes(item.label)}
                        disabled={disabled}
                        onChange={(event) => {
                          setPicked((current) =>
                            event.target.checked ? [...current, item.label] : current.filter((name) => name !== item.label),
                          );
                        }}
                      />
                      <span>
                        {item.label}
                        <span className="mark">
                          {item.sector ? `${item.sector}. ` : ""}
                          {live
                            ? "Запрос в каталог 2ГИС."
                            : browser
                              ? activeMode === "browser_pair"
                                ? "Поисковая фраза для 2ГИС и для Google Maps."
                                : activeMode === "browser_yandex"
                                ? "Поисковая фраза Яндекс Карт."
                                : activeMode === "browser_google"
                                  ? "Поисковая фраза Google Maps."
                                  : "Поисковая фраза существующего сборщика 2ГИС."
                              : disabled
                                ? "В каталоге для этого города нет компаний."
                                : `В каталоге: ${count}.`}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </div>
            </section>
            <section>
              <h2>Параметры</h2>
              <label>
                Дополнительные поисковые запросы
                <textarea
                  value={queryText}
                  placeholder={"Детские стоматологии\nРесторан"}
                  onChange={(event) => setQueryText(event.target.value)}
                />
              </label>
              <p className="hint">
                Каждый запрос с новой строки. Город подставляется отдельно, повторять его в запросе не нужно.
                {live || browser
                  ? activeMode === "browser_pair"
                    ? " Фраза уходит отдельно в 2ГИС и в Google Maps."
                    : activeMode === "browser_yandex"
                    ? " Поисковая фраза Яндекс Карт."
                    : activeMode === "browser_google"
                      ? " Поисковая фраза Google Maps."
                      : " Если категории нет в списке, напишите её здесь. Например: Косметология."
                  : ""}
              </p>
              <label>
                Максимум компаний на категорию
                <input value={limit} inputMode="numeric" onChange={(event) => setLimit(event.target.value)} />
              </label>
              <p className="hint">
                От {limitRule.min} до {limitRule.max}. {options.skip_existing?.note}
              </p>
            </section>
          </div>
          {errors.length > 0 ? (
            <ul>
              {errors.map((error) => (
                <li className="field-error" key={error}>
                  {error}
                </li>
              ))}
            </ul>
          ) : null}
          {summary.length > 0 ? (
            <div className="summary">
              <h2>Проверьте сбор</h2>
              <ul>
                {summary.map((line) => (
                  <li key={line}>{line}</li>
                ))}
              </ul>
              <div className="actions">
                <button
                  type="button"
                  disabled={action.pending}
                  onClick={() => {
                    const blocked = paired.length
                      ? modes.filter((item) => paired.includes(item.id) && !item.available)
                      : modes.filter((item) => item.id === mode && !item.available);
                    if (blocked.length > 0) {
                      setErrors(blocked.map((item) => item.note));
                      return;
                    }
                    const next = validate(city, picked, queries, platforms, limit, limitRule, allowedPlatforms(activeMode, availableSources, paired), categories, catalogFree);
                    setErrors(next);
                    if (next.length > 0) return;
                    void action.run(async () => {
                      const payload: Row = pairOn
                        ? {
                            city,
                            categories: picked,
                            queries,
                            platforms,
                            limit: Number(limit),
                            source_mode: "fixture",
                            source_modes: paired,
                          }
                        : {
                            city,
                            categories: picked,
                            queries,
                            platforms,
                            limit: Number(limit),
                            source_mode: live || browser ? mode : "fixture",
                          };
                      const body = await sendAction("/api/v1/admin/scout/collect", payload);
                      const started = Array.isArray(body.jobs) ? (body.jobs as RunJob[]) : [];
                      const ids = started.map((job) => job.id).filter((id): id is number => typeof id === "number");
                      writeRun(ids);
                      setJobs(await refreshJobs(ids));
                      setSummary([]);
                      setTick((value) => value + 1);
                      const problems = Array.isArray(body.errors)
                        ? (body.errors as Row[]).map((item) => String(item.detail || "")).filter(Boolean)
                        : [];
                      const lead = pairOn && paired.length > 1
                        ? `Отдельных сборов: ${ids.length}. Лимит у каждого источника свой.`
                        : live
                          ? `Задач в очереди: ${ids.length}. Источник: каталог 2ГИС.`
                          : browser
                            ? `Задач в очереди: ${ids.length}. Источник: существующий сборщик.`
                            : `Задач в очереди: ${ids.length}. Живой сбор: нет.`;
                      if (problems.length > 0) {
                        throw new Error([lead, ...problems].join(" "));
                      }
                      return lead;
                    });
                  }}
                >
                  {action.pending ? "Запуск." : "Подтвердить и начать"}
                </button>
                <button type="button" onClick={() => setSummary([])}>
                  Изменить
                </button>
              </div>
            </div>
          ) : (
            <button
              type="submit"
              disabled={
                action.pending ||
                !options.ready ||
                (pairOn
                  ? modes.some((item) => paired.includes(item.id) && !item.available)
                  : Boolean(selected && !selected.available))
              }
            >
              Проверить выбранный режим
            </button>
          )}
        </form>
      ) : null}
      <div className="actions">
        <button
          type="button"
          disabled={action.pending}
          onClick={() =>
            void action.run(async () => {
              const body = await sendAction("/api/v1/admin/scout/export");
              return `Строк в Excel: ${cell(body.rows)}.`;
            }, "Выгрузить собранные компании в Excel?")
          }
        >
          Подготовить файл рассылки
        </button>
        <button type="button" disabled={action.pending} onClick={() => void action.run(() => downloadAction("/api/v1/admin/scout/export.xlsx", "mailing.xlsx"))}>
          Скачать файл рассылки
        </button>
        <button
          type="button"
          disabled={action.pending}
          onClick={() =>
            void action.run(async () => {
              const body = await sendAction("/api/v1/admin/scout/platform-export");
              const sheets = (body.sheets || {}) as Row;
              return `Лист 2ГИС: ${cell(sheets["2gis"])}. Лист Google Maps: ${cell(sheets.google)}.`;
            }, "Собрать Excel с отдельными листами 2ГИС и Google Maps?")
          }
        >
          Excel 2ГИС и Google
        </button>
        <button type="button" disabled={action.pending} onClick={() => void action.run(() => downloadPlatformWorkbook())}>
          Скачать Excel 2ГИС и Google
        </button>
      </div>
      <SavedPageImport limit={limitRule} />
      </details>
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Сборов пока нет." />
      {rows.length > 0 && (
        <section className="summary job-history" id="history">
          <h2>История сборов</h2>
          <Table
            columns={["id", "city", "search_query", "recorded_source_mode", "display_status"]}
            rows={rows.map((row) => ({ ...row, search_query: row.category }))}
            link={(row) => `#/scout/${cell(row.id)}`}
          />
        </section>
      )}
    </section>
  );
}

export function RunProgress(props: {
  jobs: RunJob[];
  pending?: boolean;
  canStop: boolean;
  canResume: boolean;
  onSignal: (path: string, confirmText: string) => void;
}): ReactNode {
  if (props.jobs.length === 0) return null;
  const batches = props.jobs.map((job) => job.batch).filter((batch): batch is Row => Boolean(batch));
  const running = batches.filter((batch) => batch.status === "running" || batch.status === "awaiting_manual_verification");
  const resumable = batches.filter((batch) => !verificationUnavailable(batch) && (batch.status === "paused" || batch.status === "interrupted" || verificationPassed(batch)));
  const finished = props.jobs.length > 0 && props.jobs.every((job) => isFinished(job));
  return (
    <div className="summary progress-panel">
      <h2>Ход сбора</h2>
      <section className="progress-block">
        <h3>Статус</h3>
        <dl className="job-facts">
          <div>
            <dt>Состояние</dt>
            <dd>{collectionStatus(overall(props.jobs))}</dd>
          </div>
          <div>
            <dt>Текущий запрос</dt>
            <dd>{activeCategory(props.jobs)}</dd>
          </div>
        </dl>
      </section>
      <section className="progress-block">
        <h3>Задания</h3>
        <div className="job-list">
          {props.jobs.map((job) => (
            <JobLine key={cell(job.id)} job={job} />
          ))}
        </div>
      </section>
      <div className="actions">
        {props.canStop && running.length > 0
          ? running.map((batch) => (
              <span className="actions" key={cell(batch.id)}>
                {batch.status === "running" ? (
                  <button
                    type="button"
                    disabled={props.pending}
                    onClick={() => props.onSignal(`/api/v1/admin/scout/batches/${cell(batch.id)}/pause`, "Поставить этот сбор на паузу?")}
                  >
                    Пауза {batchTitle(batch)}
                  </button>
                ) : null}
                <button
                  type="button"
                  disabled={props.pending}
                  onClick={() => props.onSignal(`/api/v1/admin/scout/batches/${cell(batch.id)}/stop`, "Остановить этот сбор? Уже собранные компании останутся.")}
                >
                  Остановить {batchTitle(batch)}
                </button>
              </span>
            ))
          : null}
        {running
          .filter((batch) => batch.status === "awaiting_manual_verification")
          .map((batch) => (
            <VerificationControls
              key={`verify-${cell(batch.id)}`}
              batch={batch}
              pending={props.pending}
              onSignal={props.onSignal}
            />
          ))}
        {props.canResume && resumable.length > 0
          ? resumable.map((batch) => (
              <button
                key={`resume-${cell(batch.id)}`}
                type="button"
                disabled={props.pending}
                onClick={() => props.onSignal(`/api/v1/admin/scout/batches/${cell(batch.id)}/resume`, "Продолжить сбор?")}
              >
                Продолжить {batchTitle(batch)}
              </button>
            ))
          : null}
        {finished ? <a href="#/companies">Открыть компании</a> : null}
      </div>
    </div>
  );
}

function JobLine(props: { job: RunJob }): ReactNode {
  const batch = props.job.batch || null;
  const progress = (batch?.progress || {}) as Row;
  const outcome = (batch?.outcome || {}) as Row;
  const total = typeof progress.total === "number" ? progress.total : null;
  const done = ["completed", "failed", "blocked"].reduce((sum, key) => sum + (typeof progress[key] === "number" ? progress[key] : 0), 0);
  const status = shownStatus(props.job);
  return (
    <article className="job-card">
      <h3 className="job-title">{cell(batch?.category || props.job.category)}</h3>
      <section className="progress-block">
        <h4>Статус</h4>
        <dl className="job-facts">
          <div>
            <dt>Состояние</dt>
            <dd>
              <Pill value={status} /> {collectionStatus(status)}
            </dd>
          </div>
          <div>
            <dt>Город</dt>
            <dd>{cell(batch?.city || props.job.city)}</dd>
          </div>
          <div>
            <dt>Источник</dt>
            <dd>{platformLine(batch?.platforms || props.job.platforms)}</dd>
          </div>
          <div>
            <dt>Текущий запрос</dt>
            <dd>{currentQuery(batch, props.job)}</dd>
          </div>
          {batch?.collection_plan ? (
            <div>
              <dt>Сочетания</dt>
              <dd>
                {cell((batch.collection_plan as Row).completed)} из {cell((batch.collection_plan as Row).total)}. Город: {cell((batch.collection_plan as Row).current_city)}. Пропущено по лимиту: {cell((batch.collection_plan as Row).skipped)}.
              </dd>
            </div>
          ) : null}
        </dl>
      </section>
      <CollectionMetrics batch={batch} />
      <section className="progress-block">
        <h4>Итоги</h4>
        <dl className="job-facts">
          {batch?.metrics ? null : (
            <div>
              <dt>Обработано</dt>
              <dd>
                {total !== null && total > 0
                  ? `${done} из ${total}`
                  : "Число компаний появится, когда сбор запишет список. Процент заранее не считается."}
              </dd>
            </div>
          )}
          <div>
            <dt>Найдено</dt>
            <dd>{numberOrWait(outcome.discovered)}</dd>
          </div>
          <div>
            <dt>Новые компании</dt>
            <dd>{numberOrWait(outcome.added)}</dd>
          </div>
          <div>
            <dt>Уже были в базе</dt>
            <dd>{numberOrWait(outcome.already_present)}</dd>
          </div>
          <div>
            <dt>Ошибки</dt>
            <dd>{numberOrWait(outcome.failed)}</dd>
          </div>
          {typeof outcome.skipped === "number" ? (
            <div>
              <dt>Пропущено записей</dt>
              <dd>{outcome.skipped}</dd>
            </div>
          ) : null}
          {typeof outcome.duplicates === "number" ? (
            <div>
              <dt>Повторы при обходе</dt>
              <dd>{outcome.duplicates}</dd>
            </div>
          ) : null}
        </dl>
      </section>
      {savedRunNote(batch?.recorded_source_mode)}
      {screenshotNote(batch)}
      <VerificationNotice batch={batch} />
      {props.job.error ? <p className="error">{cell(props.job.error)}</p> : null}
      {typeof batch?.error === "string" && batch.error && !sameVerification(batch, batch.error) ? <p className="error">{cell(batch.error)}</p> : null}
      {batch?.id ? <p><a href={`#/scout/${cell(batch.id)}`}>Открыть сбор</a></p> : <p>Ждем номер сбора.</p>}
    </article>
  );
}

export function BatchSummary(props: { batch: Row }): ReactNode {
  const progress = (props.batch.progress || {}) as Row;
  const outcome = (props.batch.outcome || {}) as Row;
  const total = typeof progress.total === "number" ? progress.total : null;
  const done = ["completed", "failed", "blocked"].reduce((sum, key) => sum + (typeof progress[key] === "number" ? progress[key] : 0), 0);
  return (
    <div className="summary">
      <p>
        Статус: {collectionStatus(props.batch.display_status || props.batch.status)}. Текущий запрос: {currentQuery(props.batch)}. Город:{" "}
        {cell(props.batch.city)}. Источник: {platformLine(props.batch.platforms)}.
      </p>
      <ResultNote batch={props.batch} />
      {savedRunNote(props.batch.recorded_source_mode)}
      {screenshotNote(props.batch)}
      {props.batch.metrics ? null : total !== null && total > 0 ? <p>Обработано {done} из {total}.</p> : <p>Доля выполнения пока не считается: список компаний еще не записан.</p>}
      <CollectionMetrics batch={props.batch} />
      <VerificationNotice batch={props.batch} />
      {typeof props.batch.error === "string" && props.batch.error && !sameVerification(props.batch, props.batch.error) ? <p className="error">{cell(props.batch.error)}</p> : null}
      <p>
        Найдено: {numberOrWait(outcome.discovered)}. Новые компании: {numberOrWait(outcome.added)}. Уже были в базе: {numberOrWait(outcome.already_present)}.
        Ошибки: {numberOrWait(outcome.failed)}.
        {typeof outcome.skipped === "number" ? ` Пропущено записей: ${outcome.skipped}.` : ""}
      </p>
      <p>
        <a href="#/companies">Открыть компании</a>
      </p>
    </div>
  );
}

function ResultNote(props: { batch: Row }): ReactNode {
  const raw = props.batch.result;
  const result = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Row) : null;
  const rawMode = props.batch.collection_mode;
  const mode = rawMode && typeof rawMode === "object" && !Array.isArray(rawMode) ? (rawMode as Row) : null;
  const warnings = Array.isArray(result?.warnings) ? (result?.warnings as unknown[]).map(String) : [];
  const shownMode = mode && mode.code !== "saved" && mode.code !== "fixture";
  return (
    <>
      {typeof result?.note === "string" && result.note ? <p>{result.note}</p> : null}
      {warnings.length > 0 ? (
        <ul className="warnings">
          {warnings.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : null}
      {shownMode ? (
        <p className="hint">
          Режим: {cell(mode.label)}. {typeof mode.scope_label === "string" ? mode.scope_label : ""}
        </p>
      ) : null}
    </>
  );
}

function allowedPlatforms(mode: string, available: string[], paired: string[] = []): string[] {
  if (paired.length > 0) return paired.map((id) => (id === "browser_google" ? "google" : "2gis"));
  if (mode === "browser_google") return ["google"];
  if (mode === "browser_yandex") return ["yandex"];
  if (mode === "browser_2gis" || mode === "live_2gis" || mode === "browser_pair") return ["2gis"];
  return available;
}

function validate(
  city: string,
  picked: string[],
  queries: string[],
  platforms: string[],
  limit: string,
  rule: { min: number; max: number },
  available: string[],
  categories: Category[],
  live = false,
): string[] {
  const errors: string[] = [];
  if (!city.trim()) errors.push("Выберите город.");
  if (picked.length === 0 && queries.length === 0) errors.push("Выберите хотя бы одну категорию или укажите дополнительный запрос.");
  const known = new Set(categories.map((item) => item.label));
  for (const label of picked) {
    const row = categories.find((item) => item.label === label);
    if (!known.has(label) || !row || (!live && countFor(row, city) < 1)) {
      errors.push(`Категория «${label}» недоступна для выбранного города.`);
    }
  }
  const selected = platforms.filter((item) => available.includes(item));
  if (selected.length === 0) errors.push("Выберите хотя бы один доступный источник. Сейчас это только 2ГИС.");
  if (platforms.some((item) => !available.includes(item))) errors.push("Недоступный источник выбрать нельзя.");
  const parsed = Number(limit);
  if (!Number.isInteger(parsed) || parsed < rule.min || parsed > rule.max) {
    errors.push(`Лимит должен быть целым числом от ${rule.min} до ${rule.max}.`);
  }
  return errors;
}

function queryLines(value: string): string[] {
  return value
    .split(/\n+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function countFor(item: Category, city: string): number {
  const value = item.counts?.[city];
  return typeof value === "number" ? value : 0;
}

function sourceNames(ids: string[], sources: Source[]): string {
  const names = ids.map((id) => sources.find((source) => source.id === id)?.label || id);
  return names.join(", ");
}

function batchTitle(batch: Row): string {
  return `${platformLine(batch.platforms)}: ${cell(batch.category)}`;
}

function platformLine(value: unknown): string {
  const labels: Record<string, string> = { "2gis": "2ГИС", yandex: "Яндекс Карты", google: "Google Maps" };
  if (!Array.isArray(value) || value.length === 0) return "нет";
  return value.map((item) => labels[String(item)] || String(item)).join(", ");
}

function numberOrWait(value: unknown): string {
  return typeof value === "number" ? String(value) : "еще нет";
}

const METRIC_LABELS: Array<{ key: string; label: string }> = [
  { key: "companies_processed", label: "Обработано компаний" },
  { key: "companies_committed", label: "Сохранено компаний" },
  { key: "duplicate_companies", label: "Уже были в базе" },
  { key: "snapshots_written", label: "Новые снимки" },
  { key: "failed_records", label: "Ошибки карточек" },
  { key: "average_persistence_ms", label: "Запись карточки в базу, в среднем" },
  { key: "browser_restarts", label: "Перезапуски браузера" },
  { key: "retries", label: "Повторы задания" },
];

function fullTimings(batch: Row): boolean {
  const timings = batch.timings;
  return !!timings && typeof timings === "object" && !Array.isArray(timings) && (timings as Row).persistence_scope === "commit";
}

function metricText(metrics: Row, key: string, batch: Row): string {
  if (key === "average_persistence_ms" && !fullTimings(batch)) return "измерено не полностью";
  if (!Object.prototype.hasOwnProperty.call(metrics, key)) return "еще нет";
  const value = metrics[key];
  if (typeof value !== "number") return "еще нет";
  if (key === "average_persistence_ms") {
    return `${value.toLocaleString("ru-RU", { maximumFractionDigits: 1 })} мс`;
  }
  return String(value);
}

export function CollectionMetrics(props: { batch: Row | null }): ReactNode {
  const batch = props.batch;
  if (!batch) return null;
  const metrics = batch.metrics;
  const states = batch.card_states;
  const hasMetrics = metrics !== null && typeof metrics === "object" && !Array.isArray(metrics);
  const hasStates = states !== null && typeof states === "object" && !Array.isArray(states);
  if (!hasMetrics && !hasStates && batch.status !== "interrupted") return null;
  const metricRow = hasMetrics ? (metrics as Row) : null;
  const stateRow = hasStates ? (states as Row) : null;
  return (
    <section className="progress-block">
      <h4>Метрики сбора</h4>
      {metricRow ? (
        <dl className="job-facts">
          {METRIC_LABELS.map((item) => (
            <div key={item.key}>
              <dt>{item.label}</dt>
              <dd>{metricText(metricRow, item.key, batch)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <p>Счетчики браузерного сбора еще не записаны.</p>
      )}
      {batch.timings ? (
        <>
          <h4>Время этапов</h4>
          <TimingList timings={batch.timings as Timings} />
        </>
      ) : null}
      {stateRow ? (
        <>
          <h4>Состояние карточек</h4>
          <dl className="job-facts">
            <div>
              <dt>Сохранено</dt>
              <dd>{numberOrWait(stateRow.saved)}</dd>
            </div>
            <div>
              <dt>Уже была</dt>
              <dd>{numberOrWait(stateRow.existed)}</dd>
            </div>
            <div>
              <dt>Ждет повтора</dt>
              <dd>{numberOrWait(stateRow.retry_wait)}</dd>
            </div>
            <div>
              <dt>Отклонено</dt>
              <dd>{numberOrWait(stateRow.rejected)}</dd>
            </div>
          </dl>
          <ul className="state-notes">
            <li>Сохранено: компания и снимок записаны в этом сборе.</li>
            <li>Уже была: компания уже была в базе, новый снимок этого сбора не создан.</li>
            <li>Ждет повтора: временная ошибка, число попыток еще не исчерпано.</li>
            <li>Отклонено: данные карточки неверны, доступ закрыт или попытки исчерпаны.</li>
          </ul>
        </>
      ) : null}
      {batch.status === "interrupted" ? <p>{interruptionText(batch)}</p> : null}
    </section>
  );
}

function verificationOf(batch: Row | null | undefined): Row | null {
  const raw = batch?.manual_verification;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  return raw as Row;
}

function sameVerification(batch: Row | null | undefined, error: string): boolean {
  const message = verificationOf(batch)?.message;
  return typeof message === "string" && message === error;
}

export function verificationPassed(batch: Row | null | undefined): boolean {
  return verificationOf(batch)?.outcome === "passed";
}

export function verificationUnavailable(batch: Row | null | undefined): boolean {
  return verificationOf(batch)?.outcome === "unavailable";
}

export function VerificationNotice(props: { batch: Row | null | undefined }): ReactNode {
  const message = verificationOf(props.batch)?.message;
  if (typeof message !== "string" || !message) return null;
  const outcome = String(verificationOf(props.batch)?.outcome || "");
  return <p className={outcome === "passed" ? "hint" : "error"}>{message}</p>;
}

export function VerificationControls(props: {
  batch: Row;
  pending?: boolean;
  onSignal: (path: string, confirmText: string) => void;
}): ReactNode {
  if (props.batch.status !== "awaiting_manual_verification" || verificationPassed(props.batch)) return null;
  const id = cell(props.batch.id);
  const browser = String(verificationOf(props.batch)?.browser || "closed");
  return (
    <div className="actions">
      {browser === "open" ? (
        <>
          <button
            type="button"
            disabled={props.pending}
            onClick={() => props.onSignal(`/api/v1/admin/scout/batches/${id}/verification-passed`, "Подтвердить, что проверка в открытом окне пройдена?")}
          >
            Проверка пройдена
          </button>
          <button
            type="button"
            disabled={props.pending}
            onClick={() => props.onSignal(`/api/v1/admin/scout/batches/${id}/verification-failed`, "Остановить сбор, потому что проверка не пройдена? Уже собранные компании останутся.")}
          >
            Проверка не пройдена
          </button>
        </>
      ) : (
        <button
          type="button"
          disabled={props.pending}
          onClick={() => props.onSignal(`/api/v1/admin/scout/batches/${id}/verification-browser`, "Открыть видимый Chrome для ручной проверки? Программа её не решает.")}
        >
          Открыть видимый Chrome
        </button>
      )}
    </div>
  );
}

function interruptionText(batch: Row): string {
  const interruption = batch.interruption;
  const kind = interruption && typeof interruption === "object" && !Array.isArray(interruption)
    ? String((interruption as Row).classification || "")
    : "";
  if (kind === "browser_session") {
    return "Сбор прерван: сессия браузера оборвалась. Сохраненные компании остаются. Повтор откроет только незавершенные карточки.";
  }
  if (kind === "captcha") {
    return "Сбор прерван: площадка попросила проверку. Автоматический повтор этой проверки не выполняется.";
  }
  if (batch.stop_reason === "stall") {
    return "Сбор прерван: лента результатов перестала пополняться. Это не конец списка. Сохраненные компании остаются. Продолжение откроет только новые карточки.";
  }
  if (batch.retry_pending === true) {
    return "Сбор прерван: часть карточек ждет повтора. Сохраненные компании остаются.";
  }
  return "Сбор прерван. Сохраненные компании остаются. Повтор не создает второй снимок для уже записанной карточки.";
}

function shownStatus(job: RunJob): string {
  const display = job.batch?.display_status;
  if (typeof display === "string" && display) return display;
  return String(job.batch?.status || job.status || "");
}

function isOpen(job: RunJob): boolean {
  const status = String(job.batch?.status || job.status || "");
  return status === "queued" || status === "running" || status === "paused" || status === "interrupted" || status === "awaiting_manual_verification";
}

function isFinished(job: RunJob): boolean {
  const status = String(job.batch?.status || job.status || "");
  return status === "completed" || status === "done" || status === "stopped" || status === "failed" || status === "dead";
}

function overall(jobs: RunJob[]): string {
  const statuses = jobs.map((job) => shownStatus(job));
  if (statuses.some((status) => status === "running")) return "running";
  if (statuses.some((status) => status === "queued")) return "queued";
  if (statuses.some((status) => status === "paused")) return "paused";
  if (statuses.some((status) => status === "awaiting_manual_verification" || status === "blocked")) return "blocked";
  if (statuses.some((status) => status === "interrupted")) return "interrupted";
  if (statuses.some((status) => status === "failed" || status === "dead")) return "failed";
  if (statuses.some((status) => status === "stopped")) return "stopped";
  if (statuses.some((status) => status === "partial" || status === "completed_with_warnings")) return "partial";
  if (statuses.every((status) => status === "completed" || status === "done" || status === "limit_reached")) {
    return statuses.some((status) => status === "limit_reached") ? "limit_reached" : "completed";
  }
  return statuses[0] || "";
}

function activeCategory(jobs: RunJob[]): string {
  const current = jobs.find((job) => String(job.batch?.status || job.status) === "running") || jobs.find((job) => job.status === "queued") || jobs[0];
  return currentQuery(current?.batch || null, current);
}

function currentQuery(batch: Row | null | undefined, fallback?: Row | null): string {
  const query = batch?.active_query;
  if (typeof query === "string" && query.trim()) return query;
  return cell(batch?.category || fallback?.category);
}

function savedRunNote(mode: unknown): ReactNode {
  if (mode === "manual") return <p>Импорт сохранённых страниц. Живой сбор не выполнялся.</p>;
  if (mode === "fixture") return <p>Живой сбор не выполнялся.</p>;
  return null;
}

function screenshotNote(batch: Row | null | undefined): ReactNode {
  const value = batch?.optional_screenshot;
  if (value === "missing") return <p>Скриншот не приложен. Запись сбора сохранена без этого файла.</p>;
  if (value === "unreadable") return <p>Файл скриншота не читается. Он не прикреплён, запись сбора сохранена.</p>;
  if (value === "present") return <p>Файл скриншота приложен.</p>;
  return null;
}

const SAVED_PLATFORMS: Array<{ id: string; label: string }> = [
  { id: "2gis", label: "2ГИС" },
  { id: "yandex", label: "Яндекс Карты" },
  { id: "google", label: "Google Maps" },
];

export function validateSavedImport(
  city: string,
  category: string,
  platforms: string[],
  files: Record<string, File | null>,
  limit: string,
  rule: { min: number; max: number },
): string[] {
  const errors: string[] = [];
  if (!city.trim()) errors.push("Укажите город.");
  if (!category.trim()) errors.push("Укажите категорию.");
  if (platforms.length === 0) errors.push("Выберите хотя бы одну площадку сохранённой страницы.");
  for (const id of platforms) {
    const file = files[id];
    const label = SAVED_PLATFORMS.find((item) => item.id === id)?.label || id;
    if (!file) {
      errors.push(`Выберите HTML-файл для ${label}.`);
      continue;
    }
    const name = file.name || "";
    if (name.includes("/") || name.includes("\\") || name.includes("..")) {
      errors.push(`Имя файла для ${label} не принято.`);
    }
    if (!name.toLowerCase().endsWith(".html") && !name.toLowerCase().endsWith(".htm")) {
      errors.push(`Для ${label} нужен файл HTML, а не адрес сайта.`);
    }
    if (file.size < 1) errors.push(`Файл для ${label} пустой.`);
  }
  const parsed = Number(limit);
  if (!Number.isInteger(parsed) || parsed < rule.min || parsed > rule.max) {
    errors.push(`Лимит должен быть целым числом от ${rule.min} до ${rule.max}.`);
  }
  return errors;
}

export function SavedPageImport(props: { limit: { min: number; max: number; default: number } }): ReactNode {
  const [city, setCity] = useState("Алматы");
  const [category, setCategory] = useState("");
  const [picked, setPicked] = useState<string[]>(["2gis"]);
  const [files, setFiles] = useState<Record<string, File | null>>({ "2gis": null, yandex: null, google: null });
  const [shot, setShot] = useState<File | null>(null);
  const [limit, setLimit] = useState(String(props.limit.default));
  const [errors, setErrors] = useState<string[]>([]);
  const [summary, setSummary] = useState<string[]>([]);
  const [jobs, setJobs] = useState<RunJob[]>([]);
  const action = useAction();
  const active = jobs.some((job) => isOpen(job));
  const jobKey = jobs.map((job) => String(job.id)).join(",");

  useEffect(() => {
    if (!active) return;
    const ids = jobKey.split(",").map((item) => Number(item)).filter((item) => Number.isInteger(item));
    const timer = window.setInterval(() => {
      void refreshJobs(ids).then(setJobs);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [active, jobKey]);

  function choose(): void {
    const next = validateSavedImport(city, category, picked, files, limit, props.limit);
    setErrors(next);
    if (next.length > 0) {
      setSummary([]);
      return;
    }
    const names = picked.map((id) => `${SAVED_PLATFORMS.find((item) => item.id === id)?.label}: ${files[id]?.name}`).join("; ");
    setSummary([
      "Импорт сохранённых страниц. Это не живой сбор с сайта.",
      `Город: ${city.trim()}.`,
      `Категория: ${category.trim()}.`,
      `Файлы: ${names}.`,
      shot ? `Скриншот: ${shot.name}.` : "Скриншот не выбран. Сбор сохранится и без него.",
      `Лимит: ${Number(limit)} компаний.`,
      "Браузер 2ГИС, Яндекс Карт и Google Maps не открывается.",
    ]);
  }

  const selectedPlatforms = SAVED_PLATFORMS.filter((item) => picked.includes(item.id));

  return (
    <section className="summary saved-import">
      <h2>Импорт сохранённых страниц</h2>
      <p className="hint">
        Загрузите уже сохранённые HTML-страницы 2ГИС, Яндекс Карт или Google Maps. Это не живой сбор: сайт площадки не открывается.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          choose();
        }}
      >
        <div className="saved-import-layout">
          <section className="saved-panel">
            <h3>Площадки</h3>
            <div className="saved-choices">
              {SAVED_PLATFORMS.map((item) => (
                <label className="choice" key={item.id}>
                  <input
                    type="checkbox"
                    checked={picked.includes(item.id)}
                    onChange={(event) => {
                      setPicked((current) =>
                        event.target.checked ? [...current, item.id] : current.filter((id) => id !== item.id),
                      );
                    }}
                  />
                  <span>
                    {item.label}
                    <span className="mark">Сохранённая страница, не живой сайт</span>
                  </span>
                </label>
              ))}
            </div>
          </section>
          <section className="saved-panel">
            <h3>Файлы</h3>
            <div className="file-list">
              {selectedPlatforms.map((item) => (
                <label className="field file-row" key={item.id}>
                  <span className="field-name">HTML {item.label}</span>
                  <input
                    type="file"
                    accept=".html,.htm,text/html"
                    onChange={(event) => {
                      const file = event.target.files?.[0] || null;
                      setFiles((current) => ({ ...current, [item.id]: file }));
                    }}
                  />
                  <span className="hint">{files[item.id] ? `Выбран файл: ${files[item.id]?.name}` : "Файл HTML ещё не выбран."}</span>
                </label>
              ))}
              {selectedPlatforms.length === 0 ? <p className="hint">Выберите площадку, чтобы загрузить её HTML.</p> : null}
              <label className="field file-row">
                <span className="field-name">Скриншот PNG, если он есть</span>
                <input
                  type="file"
                  accept="image/png,.png"
                  onChange={(event) => setShot(event.target.files?.[0] || null)}
                />
                <span className="hint">{shot ? `Выбран файл: ${shot.name}` : "Скриншот необязателен. Если файла нет, компании всё равно сохраняются."}</span>
              </label>
            </div>
          </section>
          <section className="saved-panel saved-params">
            <h3>Параметры импорта</h3>
            <div className="saved-fields">
              <label className="field">
                <span className="field-name">Город</span>
                <input value={city} onChange={(event) => setCity(event.target.value)} />
              </label>
              <label className="field">
                <span className="field-name">Категория</span>
                <input value={category} onChange={(event) => setCategory(event.target.value)} />
              </label>
              <label className="field">
                <span className="field-name">Максимум компаний</span>
                <input value={limit} inputMode="numeric" onChange={(event) => setLimit(event.target.value)} />
              </label>
            </div>
          </section>
        </div>
        {errors.length > 0 ? (
          <ul className="saved-errors" role="alert">
            {errors.map((error) => (
              <li className="field-error" key={error}>
                {error}
              </li>
            ))}
          </ul>
        ) : null}
        {summary.length > 0 ? (
          <div className="saved-review">
            <h3>Проверьте импорт</h3>
            <ul>
              {summary.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <div className="saved-actions">
              <button
                className="primary"
                type="button"
                disabled={action.pending}
                onClick={() => {
                  const next = validateSavedImport(city, category, picked, files, limit, props.limit);
                  setErrors(next);
                  if (next.length > 0) return;
                  void action.run(async () => {
                    const data = new FormData();
                    data.append("city", city.trim());
                    data.append("category", category.trim());
                    data.append("platforms", picked.join(","));
                    data.append("limit", String(Number(limit)));
                    for (const id of picked) {
                      const file = files[id];
                      if (file) data.append(`file_${id}`, file);
                    }
                    if (shot) data.append("screenshot", shot);
                    const body = await apiUploadForm<Row>("/api/v1/admin/scout/saved-pages", data);
                    const id = typeof body.id === "number" ? body.id : null;
                    const started = id === null ? [] : await refreshJobs([id]);
                    setJobs(started);
                    setSummary([]);
                    return "Импорт сохранённых страниц поставлен в очередь. Живой сбор не запускался.";
                  });
                }}
              >
                {action.pending ? "Запуск." : "Подтвердить импорт"}
              </button>
              <button type="button" onClick={() => setSummary([])}>
                Изменить
              </button>
            </div>
          </div>
        ) : (
          <div className="saved-actions">
            <button className="primary" type="submit">Проверить файлы</button>
          </div>
        )}
      </form>
      <Feedback message={action.message} error={action.error} />
      <RunProgress
        jobs={jobs}
        pending={action.pending}
        canStop
        canResume
        onSignal={(path, confirmText) => void action.run(() => sendAction(path).then((body) => `Статус: ${collectionStatus(body.status)}.`), confirmText)}
      />
    </section>
  );
}

async function refreshJobs(ids: number[]): Promise<RunJob[]> {
  const jobs = await Promise.all(ids.map((id) => api<Row>(`/api/v1/admin/jobs/${id}`).catch(() => null)));
  const found = jobs.filter((job): job is Row => job !== null);
  return Promise.all(
    found.map(async (job) => {
      if (typeof job.batch_id !== "number") return job;
      const batch = await api<Row>(`/api/v1/admin/scout/batches/${job.batch_id}`).catch(() => null);
      return { ...job, batch };
    }),
  );
}
