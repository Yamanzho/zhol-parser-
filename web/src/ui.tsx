import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, apiDownload, apiSend, apiUpload, type Row } from "./api";
import { COLUMN_LABEL, rawCell, ru } from "./text";

export function useHash(): string {
  const [hash, setHash] = useState(window.location.hash || "#/collect");
  useEffect(() => {
    const onChange = () => setHash(window.location.hash || "#/collect");
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return hash;
}

export function parseRoute(hash: string): { page: string; id: string } {
  const path = hash.replace(/^#/, "").split("?")[0] || "/dashboard";
  const parts = path.split("/").filter(Boolean);
  return { page: parts[0] || "dashboard", id: parts[1] || "" };
}

export function useGet<T>(path: string | null): { data?: T; error?: string; loading: boolean } {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    if (!path) return;
    let cancel = false;
    setState({ loading: true });
    api<T>(path).then(
      (data) => {
        if (!cancel) setState({ loading: false, data });
      },
      (error: Error) => {
        if (!cancel) setState({ loading: false, error: error.message });
      },
    );
    return () => {
      cancel = true;
    };
  }, [path]);
  return state;
}

export function Banner(): ReactNode {
  return (
    <div className="banner">
      <strong>Офлайн-симуляция публикации, не реальная публикация.</strong> Правки и отправка карточек остаются в локальной базе
      и не публикуются на 2ГИС, Яндекс или Google. Живые кабинеты площадок и Бек не подключены. Сбор компаний с площадок к
      этой симуляции не относится: его режим показан на странице сбора.
    </div>
  );
}

export function useAction(onDone?: () => void): {
  pending: boolean;
  message: string;
  error: string;
  run: (work: () => Promise<string>, confirmText?: string) => Promise<void>;
  clearMessage: () => void;
} {
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function run(work: () => Promise<string>, confirmText?: string): Promise<void> {
    if (pending) return;
    if (confirmText && !window.confirm(confirmText)) return;
    setPending(true);
    setMessage("");
    setError("");
    try {
      setMessage(await work());
      onDone?.();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Запрос не выполнен.");
    } finally {
      setPending(false);
    }
  }

  const clearMessage = useCallback(() => setMessage(""), []);

  return { pending, message, error, run, clearMessage };
}

export function Feedback(props: { message?: string; error?: string }): ReactNode {
  return (
    <>
      {props.error ? <p className="error">{props.error}</p> : null}
      {props.message ? <p className="ok">{props.message}</p> : null}
    </>
  );
}

export function Unavailable(props: { label: string; reason: string }): ReactNode {
  return (
    <p>
      <button type="button" disabled>
        {props.label}
      </button>{" "}
      <span className="muted">{props.reason}</span>
    </p>
  );
}

export async function sendAction(path: string, body?: unknown): Promise<Row> {
  return apiSend<Row>(path, body);
}

export async function uploadAction(path: string, file: File, fields?: Record<string, string>): Promise<Row> {
  return apiUpload<Row>(path, file, fields);
}

export async function downloadAction(path: string, filename: string): Promise<string> {
  await apiDownload(path, filename);
  return `Файл скачивается: ${filename}`;
}

export async function downloadPlatformWorkbook(): Promise<string> {
  await sendAction("/api/v1/admin/scout/platform-export");
  await apiDownload(`/api/v1/admin/scout/platform-export.xlsx?fresh=${Date.now()}`, "2gis-google.xlsx");
  return "Файл скачивается: 2gis-google.xlsx. Это новая книга, не ранее сохраненный файл.";
}

export function StateView(props: { loading: boolean; error?: string; empty?: boolean; emptyText?: string }): ReactNode | null {
  if (props.loading) return <p>Загрузка.</p>;
  if (props.error) return <p className="error">{props.error}</p>;
  if (props.empty) return <p>{props.emptyText || "Пока ничего не сохранено."}</p>;
  return null;
}

export function cell(value: unknown): string {
  return ru(value);
}

export function Pill(props: { value: unknown }): ReactNode {
  const raw = rawCell(props.value);
  return <span className={`pill ${raw}`}>{ru(raw)}</span>;
}

export function rowsOf(data: Row | undefined): Row[] {
  const items = data?.items;
  return Array.isArray(items) ? (items as Row[]) : [];
}

export function Table(props: { columns: string[]; rows: Row[]; link?: (row: Row) => string }): ReactNode {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {props.columns.map((column) => (
              <th key={column}>{COLUMN_LABEL[column] || column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map((row, index) => (
            <tr key={String(row.id ?? row.order_id ?? index)}>
              {props.columns.map((column) => (
                <td key={column}>
                  {props.link && column === props.columns[0] ? (
                    <a href={props.link(row)}>{cell(row[column])}</a>
                  ) : column === "phase" || column === "recorded_source_mode" || column === "status" || column === "display_status" ? (
                    <Pill value={row[column]} />
                  ) : (
                    cell(row[column])
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Pager(props: { offset: number; total: number; limit: number; onChange: (offset: number) => void }): ReactNode | null {
  if (props.total <= props.limit) return null;
  return (
    <p className="pager">
      <button type="button" disabled={props.offset <= 0} onClick={() => props.onChange(Math.max(0, props.offset - props.limit))}>
        Назад
      </button>{" "}
      <button
        type="button"
        disabled={props.offset + props.limit >= props.total}
        onClick={() => props.onChange(props.offset + props.limit)}
      >
        Дальше
      </button>{" "}
      <span className="muted">
        {props.offset + 1}-{Math.min(props.offset + props.limit, props.total)} из {props.total}
      </span>
    </p>
  );
}

export type Timings = {
  persistence_scope: string;
  runs?: number;
  run_ms?: number;
  browser_start_ms?: number;
  link_discovery_ms?: number;
  card_read_ms?: number;
  card_pacing_ms?: number;
  verification_wait_ms?: number;
  persistence_ms?: number;
  cards_opened?: number;
  cards_persisted?: number;
  average_card_read_ms?: number;
  average_persistence_ms?: number;
};

export function duration(ms: number | null | undefined): string {
  if (typeof ms !== "number") return "нет данных";
  if (ms < 1000) return `${Math.round(ms)} мс`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1).replace(".", ",")} с`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} мин ${Math.round(seconds - minutes * 60)} с`;
}

export function TimingList(props: { timings?: Timings | null }): ReactNode {
  const timings = props.timings;
  if (!timings) return null;
  if (timings.persistence_scope !== "commit") {
    return <p className="note">Время этапов для этого сбора не измерялось. Прежний замер записи в базу был неполным и не показывается.</p>;
  }
  const rows: [string, string][] = [
    ["Весь прогон сборщика", duration(timings.run_ms)],
    ["Запуск браузера", duration(timings.browser_start_ms)],
  ];
  if (typeof timings.link_discovery_ms === "number") rows.push(["Поиск ссылок в выдаче", duration(timings.link_discovery_ms)]);
  if (typeof timings.card_read_ms === "number") rows.push(["Открытие и чтение карточек", duration(timings.card_read_ms)]);
  if (typeof timings.average_card_read_ms === "number") rows.push(["В среднем на карточку", duration(timings.average_card_read_ms)]);
  if (typeof timings.card_pacing_ms === "number") rows.push(["Паузы между карточками", duration(timings.card_pacing_ms)]);
  if (timings.verification_wait_ms) rows.push(["Ожидание проверки в браузере", duration(timings.verification_wait_ms)]);
  rows.push(["Запись в базу, включая фиксацию", duration(timings.persistence_ms)]);
  if (typeof timings.average_persistence_ms === "number") rows.push(["Запись одной карточки в среднем", duration(timings.average_persistence_ms)]);
  return (
    <dl className="facts">
      {rows.map(([label, value]) => (
        <div className="fact" key={label}>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}
