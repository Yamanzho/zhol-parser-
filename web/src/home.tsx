import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, apiDownload, apiSend, type Row } from "./api";
import { duration, TimingList, type Timings } from "./ui";

export type Platform = { id: string; label: string; available: boolean; note: string; cities: string[] };
export type Meta = {
  demo?: boolean;
  label?: string;
  platforms?: Platform[];
  limit?: { min: number; max: number; default: number };
  export_notes?: string[];
  export_formats?: string[];
};

type Job = {
  id: number;
  status: string;
  platform_label?: string | null;
  city?: string | null;
  query?: string | null;
  limit?: number | null;
  collected?: number;
  progress?: number | null;
  message?: string | null;
  error?: string | null;
  controls?: { pause?: boolean; resume?: boolean; stop?: boolean };
  results_url?: string;
  batch_id?: number | null;
  result?: JobResult | null;
  collection_mode?: { code: string; label: string; scope_label?: string } | null;
  timings?: Timings | null;
  created_at?: string | null;
  finished_at?: string | null;
  duration_seconds?: number | null;
};

type JobResult = {
  code: string;
  label: string;
  note?: string;
  limit_reached?: boolean;
  search_exhausted?: boolean;
  warnings?: string[];
};

type Company = {
  id: number;
  name?: string | null;
  phone?: string | null;
  address?: string | null;
  category?: string | null;
  city?: string | null;
  platform_label?: string | null;
  source_label?: string | null;
  review_note?: string | null;
};

type Page<T> = { items?: T[]; total?: number };

const MetaContext = createContext<Meta | null>(null);

export function MetaProvider(props: { value: Meta | null; children: ReactNode }): ReactNode {
  return <MetaContext.Provider value={props.value}>{props.children}</MetaContext.Provider>;
}

const STATUS: Record<string, string> = {
  queued: "Ожидает запуска",
  running: "Идёт сбор",
  paused: "Пауза",
  stopped: "Остановлен",
  completed: "Завершён",
  failed: "Ошибка",
  awaiting_manual_verification: "Ждёт проверку в браузере",
};

const ACTIVE = ["queued", "running", "paused", "awaiting_manual_verification"];

function text(value: string | null | undefined): string {
  return value || "Нет данных";
}

function jobLabel(job: Job): string {
  return job.result?.label || STATUS[job.status] || job.status;
}

function when(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString("ru-RU") : "нет данных";
}

export function Collection(props: { jobId?: string }): ReactNode {
  const meta = useContext(MetaContext);
  const platforms = meta?.platforms || [];
  const rule = meta?.limit || { min: 1, max: 500, default: 20 };
  const [platform, setPlatform] = useState("");
  const [city, setCity] = useState("");
  const [query, setQuery] = useState("салон красоты");
  const [limit, setLimit] = useState(String(rule.default));
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [listError, setListError] = useState("");
  const [reload, setReload] = useState(0);
  const selected = platforms.find((item) => item.id === platform);
  const cities = selected?.cities || [];
  const openId = props.jobId && /^\d+$/.test(props.jobId) ? Number(props.jobId) : null;
  const anyActive = jobs.some((item) => ACTIVE.includes(item.status));

  useEffect(() => {
    if (!platforms.length || platform) return;
    const first = platforms.find((item) => item.available) || platforms[0];
    setPlatform(first.id);
    setCity(first.cities[0] || "");
    setLimit(String(rule.default));
  }, [platforms, platform, rule.default]);

  useEffect(() => {
    let cancel = false;
    void api<Page<Job>>("/api/v1/jobs?limit=20&offset=0")
      .then((body) => {
        if (cancel) return;
        setJobs(body.items || []);
        setListError("");
      })
      .catch((reason: Error) => {
        if (!cancel) setListError(reason.message);
      });
    return () => {
      cancel = true;
    };
  }, [reload]);

  useEffect(() => {
    if (!anyActive) return;
    const timer = window.setTimeout(() => setReload((value) => value + 1), 3000);
    return () => window.clearTimeout(timer);
  }, [anyActive, jobs]);

  async function start(): Promise<void> {
    setError("");
    setBusy(true);
    try {
      const created = await apiSend<Job>("/api/v1/jobs", {
        platform,
        city,
        query: query.trim(),
        limit: Number(limit),
      });
      window.location.hash = `#/collect/${created.id}`;
      setReload((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось начать сбор.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="simple">
      <h1>Сбор</h1>
      <p className="lede">Выберите площадку, город и запрос. Сбор можно поставить на паузу или остановить.</p>
      <div className="simple-form">
        <label>
          Площадка
          <select
            value={platform}
            onChange={(event) => {
              const next = platforms.find((item) => item.id === event.target.value);
              setPlatform(event.target.value);
              setCity(next?.cities[0] || "");
            }}
          >
            {platforms.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
                {item.available ? "" : " (недоступна)"}
              </option>
            ))}
          </select>
        </label>
        <label>
          Город
          <select value={city} onChange={(event) => setCity(event.target.value)}>
            {cities.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Категория или запрос
          <input value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
        <label>
          Максимум компаний
          <input min={rule.min} max={rule.max} type="number" value={limit} onChange={(event) => setLimit(event.target.value)} />
        </label>
      </div>
      {selected && !selected.available && <p className="error">{selected.note}</p>}
      {selected?.available && selected.note && <p className="note">{selected.note}</p>}
      <div className="actions">
        <button type="button" disabled={busy || !selected?.available || !city || !query.trim()} onClick={() => void start()}>
          Начать сбор
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {openId !== null && <JobDetails id={openId} onChange={() => setReload((value) => value + 1)} />}
      <h2>Последние задания</h2>
      <p className="note">Здесь видны все задания сбора, в том числе созданные через API.</p>
      <div className="actions">
        <button type="button" className="secondary" onClick={() => setReload((value) => value + 1)}>
          Обновить список
        </button>
      </div>
      {listError && <p className="error">{listError}</p>}
      {!listError && !jobs.length && <p className="note">Заданий сбора пока нет.</p>}
      {jobs.length > 0 && (
        <table className="company-list jobs-list">
          <thead>
            <tr>
              <th>№</th>
              <th>Площадка, город, запрос</th>
              <th>Статус</th>
              <th>Собрано</th>
              <th>Замечания</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {jobs.map((item) => (
              <tr key={item.id} className={item.id === openId ? "selected" : ""}>
                <td>{item.id}</td>
                <td>
                  {text(item.platform_label)}, {text(item.city)}, «{text(item.query)}»
                </td>
                <td>
                  <span className={`pill ${item.result?.code || item.status}`}>{jobLabel(item)}</span>
                  {typeof item.progress === "number" && ACTIVE.includes(item.status) && (
                    <div className="bar" aria-label="Прогресс">
                      <span style={{ width: `${Math.round(item.progress * 100)}%` }} />
                    </div>
                  )}
                </td>
                <td>
                  {item.collected ?? 0}
                  {item.limit ? ` из ${item.limit}` : ""}
                </td>
                <td>{item.error ? "Ошибка" : item.result?.warnings?.length ? item.result.warnings.join(" ") : "Нет"}</td>
                <td>
                  <a href={`#/collect/${item.id}`}>Открыть</a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function JobDetails(props: { id: number; onChange: () => void }): ReactNode {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [results, setResults] = useState<Page<Company> | null>(null);
  const [showResults, setShowResults] = useState(false);
  const { id, onChange } = props;

  useEffect(() => {
    let cancel = false;
    setJob(null);
    setResults(null);
    setShowResults(false);
    void api<Job>(`/api/v1/jobs/${id}`)
      .then((body) => {
        if (!cancel) setJob(body);
      })
      .catch((reason: Error) => {
        if (!cancel) setError(reason.message);
      });
    return () => {
      cancel = true;
    };
  }, [id]);

  useEffect(() => {
    if (!job || !ACTIVE.includes(job.status)) return;
    const timer = window.setTimeout(() => {
      void api<Job>(`/api/v1/jobs/${job.id}`).then(setJob).catch((reason: Error) => setError(reason.message));
    }, 800);
    return () => window.clearTimeout(timer);
  }, [job]);

  useEffect(() => {
    if (!showResults || !job?.results_url) return;
    let cancel = false;
    void api<Page<Company>>(`${job.results_url}?limit=50&offset=0`)
      .then((body) => {
        if (!cancel) setResults(body);
      })
      .catch((reason: Error) => {
        if (!cancel) setError(reason.message);
      });
    return () => {
      cancel = true;
    };
  }, [showResults, job?.results_url, job?.collected]);

  async function act(action: "pause" | "resume" | "stop"): Promise<void> {
    if (!job) return;
    if (action === "stop" && !window.confirm("Остановить сбор? Уже собранные компании сохранятся.")) return;
    setError("");
    try {
      setJob(await apiSend<Job>(`/api/v1/jobs/${job.id}/${action}`));
      onChange();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Действие не выполнено.");
    }
  }

  if (!job) {
    return <div className="status-card">{error ? <p className="error">{error}</p> : <p>Загружаем задание {id}.</p>}</div>;
  }
  const warnings = job.result?.warnings || [];
  return (
    <div className="status-card job-details">
      <h2>
        Задание {job.id}: {text(job.platform_label)}, {text(job.city)}, «{text(job.query)}»
      </h2>
      <p>
        <strong>{jobLabel(job)}</strong>
        {job.message ? `. ${job.message}` : ""}
      </p>
      {job.result?.note && <p>{job.result.note}</p>}
      {job.collection_mode && (
        <p className="note">
          Источник: {job.collection_mode.label}. {job.collection_mode.scope_label || ""}
        </p>
      )}
      <p>
        Собрано компаний: {job.collected ?? 0}
        {job.limit ? ` из ${job.limit}` : ""}
      </p>
      {typeof job.progress === "number" && (
        <div className="bar" aria-label="Прогресс">
          <span style={{ width: `${Math.round(job.progress * 100)}%` }} />
        </div>
      )}
      {warnings.length > 0 && (
        <ul className="warnings">
          {warnings.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      )}
      {job.error && <p className="error">{job.error}</p>}
      <p className="note">
        Создано: {when(job.created_at)}. Завершено: {job.finished_at ? when(job.finished_at) : "ещё нет"}.
        {typeof job.duration_seconds === "number" ? ` Длительность с постановки в очередь: ${duration(job.duration_seconds * 1000)}.` : ""}
      </p>
      <TimingList timings={job.timings} />
      {error && <p className="error">{error}</p>}
      <div className="actions">
        <button type="button" className="secondary" disabled={!job.controls?.pause} onClick={() => void act("pause")}>
          Пауза
        </button>
        <button type="button" className="secondary" disabled={!job.controls?.resume} onClick={() => void act("resume")}>
          Продолжить
        </button>
        <button type="button" className="secondary" disabled={!job.controls?.stop} onClick={() => void act("stop")}>
          Остановить
        </button>
        <button type="button" className="secondary" onClick={() => setShowResults((value) => !value)}>
          {showResults ? "Скрыть компании" : "Собранные компании"}
        </button>
        {job.batch_id ? (
          <a className="button secondary" href={`#/scout/${job.batch_id}`}>
            Подробно в истории
          </a>
        ) : null}
        <a className="button secondary" href="#/export">
          Выгрузка
        </a>
      </div>
      {showResults && results && (
        <>
          <p className="note">
            Показано {results.items?.length || 0} из {results.total ?? 0}.
          </p>
          <table className="company-list">
            <thead>
              <tr>
                <th>Название</th>
                <th>Телефон</th>
                <th>Адрес</th>
                <th>Категория</th>
              </tr>
            </thead>
            <tbody>
              {(results.items || []).map((item) => (
                <tr key={item.id}>
                  <td>{text(item.name)}</td>
                  <td>{text(item.phone)}</td>
                  <td>{text(item.address)}</td>
                  <td>{text(item.category)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

export function Companies(): ReactNode {
  const meta = useContext(MetaContext);
  const [q, setQ] = useState("");
  const [platform, setPlatform] = useState("");
  const [city, setCity] = useState("");
  const [applied, setApplied] = useState({ q: "", platform: "", city: "" });
  const [page, setPage] = useState<Page<Company> | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [opened, setOpened] = useState<Row | null>(null);
  const cities = Array.from(new Set((meta?.platforms || []).flatMap((item) => item.cities)));

  useEffect(() => {
    const params = new URLSearchParams();
    if (applied.q) params.set("q", applied.q);
    if (applied.platform) params.set("platform", applied.platform);
    if (applied.city) params.set("city", applied.city);
    params.set("limit", "50");
    let cancel = false;
    setLoading(true);
    void api<Page<Company>>(`/api/v1/companies?${params.toString()}`)
      .then((body) => {
        if (!cancel) {
          setPage(body);
          setError("");
        }
      })
      .catch((reason: Error) => {
        if (!cancel) setError(reason.message);
      })
      .finally(() => {
        if (!cancel) setLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, [applied]);

  return (
    <section className="simple">
      <h1>Компании</h1>
      <p className="lede">Найдите компанию по названию, телефону или адресу.</p>
      <div className="filters">
        <label>
          Поиск
          <input value={q} onChange={(event) => setQ(event.target.value)} />
        </label>
        <label>
          Площадка
          <select value={platform} onChange={(event) => setPlatform(event.target.value)}>
            <option value="">Все</option>
            {(meta?.platforms || []).map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          Город
          <select value={city} onChange={(event) => setCity(event.target.value)}>
            <option value="">Все</option>
            {cities.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="actions">
        <button type="button" onClick={() => setApplied({ q: q.trim(), platform, city })}>
          Найти
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      {loading && <p>Загружаем список.</p>}
      {!loading && !error && (page?.total || 0) === 0 && <p>Пока нет компаний. Запустите сбор или измените фильтр.</p>}
      {(page?.items || []).length > 0 && (
        <table className="company-list">
          <thead>
            <tr>
              <th>Название</th>
              <th>Телефон</th>
              <th>Адрес</th>
              <th>Категория</th>
              <th>Площадка</th>
              <th>Источник</th>
            </tr>
          </thead>
          <tbody>
            {(page?.items || []).map((company) => (
              <tr key={company.id}>
                <td>
                  <button
                    type="button"
                    className="link"
                    onClick={() => {
                      setOpened(null);
                      void api<Row>(`/api/v1/companies/${company.id}`)
                        .then(setOpened)
                        .catch((reason: Error) => setError(reason.message));
                    }}
                  >
                    {text(company.name)}
                  </button>
                </td>
                <td>{text(company.phone)}</td>
                <td>{text(company.address)}</td>
                <td>{text(company.category)}</td>
                <td>{text(company.platform_label)}</td>
                <td>{text(company.source_label)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {opened && <CompanyFacts body={opened} />}
    </section>
  );
}

function CompanyFacts(props: { body: Row }): ReactNode {
  const facts = Array.isArray(props.body.facts) ? props.body.facts : [];
  const note = typeof props.body.note === "string" ? props.body.note : "";
  return (
    <div className="status-card">
      <h2>Карточка</h2>
      {facts.map((fact) => {
        const row = fact as { label?: string; text?: string };
        return (
          <p key={row.label}>
            <strong>{row.label}: </strong>
            {row.text}
          </p>
        );
      })}
      {note && <p className="note">{note}</p>}
    </div>
  );
}

export function ExportPanel(): ReactNode {
  const meta = useContext(MetaContext);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobId, setJobId] = useState("all");
  const [format, setFormat] = useState("xlsx");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancel = false;
    void api<Page<Job>>("/api/v1/jobs?limit=50&offset=0")
      .then((body) => {
        if (!cancel) setJobs(body.items || []);
      })
      .catch((reason: Error) => {
        if (!cancel) setError(reason.message);
      })
      .finally(() => {
        if (!cancel) setLoading(false);
      });
    return () => {
      cancel = true;
    };
  }, []);

  const names: Record<string, string> = { xlsx: "zhol-companies.xlsx", csv: "zhol-companies.csv", json: "zhol-companies.json" };

  return (
    <section className="simple">
      <h1>Выгрузка</h1>
      <p className="lede">Скачайте компании в Excel, CSV или JSON.</p>
      {(meta?.export_notes || []).map((note) => (
        <p key={note} className="note">
          {note}
        </p>
      ))}
      {loading && <p>Загружаем задания.</p>}
      {error && <p className="error">{error}</p>}
      <div className="simple-form">
        <label>
          Что выгрузить
          <select value={jobId} onChange={(event) => setJobId(event.target.value)}>
            <option value="all">Все компании в текущей базе</option>
            {jobs.map((job) => (
              <option key={job.id} value={String(job.id)}>
                #{job.id} {job.platform_label || ""} {job.city || ""} {job.query || ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          Формат
          <select value={format} onChange={(event) => setFormat(event.target.value)}>
            <option value="xlsx">Excel</option>
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </select>
        </label>
      </div>
      <div className="actions">
        <button
          type="button"
          onClick={() => {
            setError("");
            const path = jobId === "all" ? `/api/v1/export?format=${format}` : `/api/v1/jobs/${jobId}/export?format=${format}`;
            void apiDownload(path, names[format] || "zhol-export").catch((reason: Error) => setError(reason.message));
          }}
        >
          Скачать
        </button>
      </div>
    </section>
  );
}

export function Advanced(): ReactNode {
  const links = [
    ["#/history", "История сбора"],
    ["#/dashboard", "Сводка"],
    ["#/audits", "Аудит"],
    ["#/orders", "Заказы"],
    ["#/content", "Материалы"],
    ["#/listings", "Правки карточек"],
    ["#/tasks", "Задачи человеку"],
    ["#/reports", "Отчёты"],
  ];
  return (
    <section className="simple">
      <h1>Дополнительно</h1>
      <p className="lede">Здесь прежние разделы. Для обычного сбора они не нужны.</p>
      <div className="advanced-list">
        {links.map(([href, label]) => (
          <a key={href} href={href}>
            {label}
          </a>
        ))}
      </div>
    </section>
  );
}
