import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { api, apiDownload, apiSend, type Row } from "./api";

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

function text(value: string | null | undefined): string {
  return value || "Нет данных";
}

export function Collection(): ReactNode {
  const meta = useContext(MetaContext);
  const platforms = meta?.platforms || [];
  const rule = meta?.limit || { min: 1, max: 500, default: 20 };
  const [platform, setPlatform] = useState("");
  const [city, setCity] = useState("");
  const [query, setQuery] = useState("салон красоты");
  const [limit, setLimit] = useState(String(rule.default));
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const selected = platforms.find((item) => item.id === platform);
  const cities = selected?.cities || [];

  useEffect(() => {
    if (!platforms.length || platform) return;
    const first = platforms.find((item) => item.available) || platforms[0];
    setPlatform(first.id);
    setCity(first.cities[0] || "");
    setLimit(String(rule.default));
  }, [platforms, platform, rule.default]);

  useEffect(() => {
    if (!job || !["queued", "running", "paused", "awaiting_manual_verification"].includes(job.status)) return;
    const timer = window.setTimeout(() => {
      void api<Job>(`/api/v1/jobs/${job.id}`).then(setJob).catch((reason: Error) => setError(reason.message));
    }, 800);
    return () => window.clearTimeout(timer);
  }, [job]);

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
      setJob(created);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось начать сбор.");
    } finally {
      setBusy(false);
    }
  }

  async function act(action: "pause" | "resume" | "stop"): Promise<void> {
    if (!job) return;
    setError("");
    try {
      setJob(await apiSend<Job>(`/api/v1/jobs/${job.id}/${action}`));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Действие не выполнено.");
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
      {job && (
        <div className="status-card">
          <p>
            <strong>{STATUS[job.status] || job.status}</strong>
            {job.message ? `. ${job.message}` : ""}
          </p>
          <p>
            Собрано компаний: {job.collected ?? 0}
            {job.limit ? ` из ${job.limit}` : ""}
          </p>
          {typeof job.progress === "number" && (
            <div className="bar" aria-label="Прогресс">
              <span style={{ width: `${Math.round(job.progress * 100)}%` }} />
            </div>
          )}
          {job.error && <p className="error">{job.error}</p>}
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
            <a className="button secondary" href="#/companies">
              К компаниям
            </a>
          </div>
        </div>
      )}
    </section>
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
