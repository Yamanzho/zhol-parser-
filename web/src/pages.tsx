import { useEffect, useState, type ReactNode } from "react";
import type { Row } from "./api";
import { BatchSummary, VerificationControls, collectionStatus, verificationPassed, verificationUnavailable } from "./collect";
import { COUNT_LABEL, rawCell } from "./text";
import {
  Banner,
  Feedback,
  Pill,
  StateView,
  Table,
  cell,
  downloadAction,
  rowsOf,
  sendAction,
  uploadAction,
  useGet,
  useAction,
} from "./ui";

export function Dashboard(): ReactNode {
  const summary = useGet<Row>("/api/v1/admin/summary");
  const health = useGet<Row>("/health");
  const counts = (summary.data?.counts || {}) as Row;
  const modes = (summary.data?.scout_source_modes || {}) as Row;
  const phases = (summary.data?.listing_phases || {}) as Row;
  const blocked = StateView({ loading: summary.loading, error: summary.error });
  return (
    <section>
      <h1>Главная</h1>
      <Banner />
      <p className="hint">Здесь только уже сохраненные записи. Новые площадки отсюда не открываются. Заказы, тексты, правки и отчеты открываются внутри компании.</p>
      {blocked || (
        <>
          <p>
            Состояние API: {health.loading ? "Загрузка." : health.error || cell(health.data?.status)}. Реальная публикация: нет.
            Бек подключен: нет. Сбор компаний идёт по поисковым запросам, обход всего города не выполняется.
          </p>
          <div className="cards">
            {Object.entries(counts).map(([key, value]) => (
              <div className="card" key={key}>
                <span>{COUNT_LABEL[key] || key}</span>
                <strong>{cell(value)}</strong>
              </div>
            ))}
          </div>
          <h2>Режимы сбора</h2>
          {Object.keys(modes).length === 0 ? <p>Сборов пока нет.</p> : <KeyList data={modes} />}
          <h2>Фазы карточек</h2>
          {Object.keys(phases).length === 0 ? <p>Отправок карточек пока нет.</p> : <KeyList data={phases} />}
          <p className="muted">{cell(summary.data?.note)}</p>
          <OpenTasks />
        </>
      )}
    </section>
  );
}

function OpenTasks(): ReactNode {
  const data = useGet<Row>("/api/v1/admin/tasks?status=open&limit=20&offset=0");
  const rows = rowsOf(data.data);
  return (
    <>
      <h2>Нужен человек</h2>
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Открытых задач человеку нет." />
      {rows.length > 0 && (
        <ul>
          {rows.map((row) => {
            const companyId = row.object_type === "company" && row.object_id != null ? cell(row.object_id) : "";
            const title = row.reason === "bek_audit_ambiguous" ? "В книге аудита неоднозначная строка" : row.reason === "bek_audit_unmatched" ? "Строка аудита не сопоставилась с компанией" : "Нужен человек";
            return (
              <li key={cell(row.id)}>
                {title}. {companyId ? <a href={`#/companies/${companyId}`}>Открыть компанию</a> : "Компания в задаче не указана."}
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

function KeyList(props: { data: Row }): ReactNode {
  return (
    <ul>
      {Object.entries(props.data).map(([key, value]) => (
        <li key={key}>
          <Pill value={key} /> {cell(value)}
        </li>
      ))}
    </ul>
  );
}

export { Companies, CompanyDetail } from "./companies";
export { Scout } from "./collect";

export function ScoutDetail(props: { id: string }): ReactNode {
  const [tick, setTick] = useState(0);
  const action = useAction(() => setTick((value) => value + 1));
  const data = useGet<Row>(`/api/v1/admin/scout/batches/${props.id}?refresh=${tick}`);
  const items = (data.data?.items || []) as Row[];
  const status = String(data.data?.status || "");
  useEffect(() => {
    if (status !== "running" && status !== "interrupted" && status !== "awaiting_manual_verification") return;
    const timer = window.setInterval(() => setTick((value) => value + 1), 2000);
    return () => window.clearInterval(timer);
  }, [status]);
  return (
    <section>
      <h1>Сбор {props.id}</h1>
      <p className="hint">Пауза и остановка не удаляют уже собранные компании. Excel можно скачать после подготовки.</p>
      <p><a href="#/scout">К списку сборов</a></p>
      <StateView loading={data.loading} error={data.error} />
      {data.data && (
        <>
          <BatchSummary batch={data.data} />
          <VerificationControls
            batch={data.data}
            pending={action.pending}
            onSignal={(path, confirmText) => void action.run(() => sendAction(path).then((body) => {
              const message = typeof body.message === "string" ? body.message : "";
              const status = `Статус: ${collectionStatus(body.status)}.`;
              return message ? `${message} ${status}` : status;
            }), confirmText)}
          />
          <div className="actions">
            <button type="button" disabled={action.pending || status !== "running"} onClick={() => void action.run(() => sendAction(`/api/v1/admin/scout/batches/${props.id}/pause`).then((body) => `Статус: ${collectionStatus(body.status)}.`), "Поставить сбор на паузу?")}>
              Пауза
            </button>
            <button type="button" disabled={action.pending || verificationUnavailable(data.data) || (status !== "paused" && status !== "interrupted" && !verificationPassed(data.data))} onClick={() => void action.run(() => sendAction(`/api/v1/admin/scout/batches/${props.id}/resume`).then((body) => `Статус: ${collectionStatus(body.status)}.`), "Продолжить сбор?")}>
              Продолжить сбор
            </button>
            <button type="button" disabled={action.pending || status === "stopped" || status === "completed"} onClick={() => void action.run(() => sendAction(`/api/v1/admin/scout/batches/${props.id}/stop`).then((body) => `Статус: ${collectionStatus(body.status)}.`), "Остановить сбор? Уже собранные компании останутся.")}>
              Остановить сбор
            </button>
            <button type="button" disabled={action.pending} onClick={() => void action.run(() => sendAction(`/api/v1/admin/scout/batches/${props.id}/export`).then((body) => `Строк в Excel: ${cell(body.rows)}.`))}>
              Подготовить Excel
            </button>
            <button type="button" disabled={action.pending} onClick={() => void action.run(() => downloadAction(`/api/v1/admin/scout/batches/${props.id}/export.xlsx`, `saved-batch-${props.id}.xlsx`))}>
              Скачать сохраненный файл сбора
            </button>
          </div>
          <Feedback message={action.message} error={action.error} />
          {items.length === 0 ? (
            <p>В сборе пока нет компаний.</p>
          ) : (
            <Table columns={["id", "id_2gis", "company_id", "status", "card_state", "verification_status", "collected_at", "attempts"]} rows={items} />
          )}
        </>
      )}
    </section>
  );
}

export function Audits(): ReactNode {
  const [tick, setTick] = useState(0);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<Row | null>(null);
  const [imported, setImported] = useState<Row | null>(null);
  const [over, setOver] = useState(false);
  const action = useAction(() => setTick((value) => value + 1));
  const data = useGet<Row>(`/api/v1/admin/audits?limit=50&offset=0&refresh=${tick}`);
  const rows = rowsOf(data.data);
  const summary = (preview?.preview || null) as Row | null;

  function choose(next: File | null): void {
    setImported(null);
    setPreview(null);
    if (!next) {
      setFile(null);
      return;
    }
    if (!next.name.toLowerCase().endsWith(".xlsx")) {
      setFile(null);
      void action.run(async () => {
        throw new Error("Нужен файл Excel (.xlsx).");
      });
      return;
    }
    setFile(next);
  }

  return (
    <section>
      <h1>Загрузка и анализ аудита</h1>
      <Banner />
      <p className="hint">Файл сначала проверяется. В базу он записывается только после вашего подтверждения. Программа Бека не вызывается.</p>
      <ol className="steps">
        <li className={file ? "done" : "active"}>Файл</li>
        <li className={summary ? "done" : file ? "active" : ""}>Проверка</li>
        <li className={imported ? "done" : summary ? "active" : ""}>Запись</li>
      </ol>
      <div
        className={over ? "drop over" : "drop"}
        onDragOver={(event) => {
          event.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(event) => {
          event.preventDefault();
          setOver(false);
          choose(event.dataTransfer.files?.[0] || null);
        }}
      >
        <p>{file ? file.name : "Перетащите сюда книгу Excel или выберите файл."}</p>
        <label>
          Файл Excel (.xlsx)
          <input
            type="file"
            accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            onChange={(event) => choose(event.target.files?.[0] || null)}
          />
        </label>
      </div>
      <div className="actions">
        {file ? (
        <button
          type="button"
          disabled={action.pending}
          onClick={() => {
            const chosen = file;
            if (!chosen) return;
            void action.run(async () => {
              const checked = await uploadAction("/api/v1/admin/audits/validate", chosen);
              if (checked.imported === true) throw new Error("Проверка не должна записывать файл.");
              setPreview(checked);
              setImported(null);
              const body = (checked.preview || {}) as Row;
              return `Файл проверен, в базу еще не записан. Компаний: ${cell(body.companies)}. Повторов: ${cell(body.duplicates)}. Вопросов: ${cell(body.issues)}.`;
            });
          }}
        >
          {action.pending && !imported ? "Проверка." : "Проверить файл"}
        </button>
        ) : null}
        {summary && file ? (
          <button
            type="button"
            disabled={action.pending}
            onClick={() => {
              const chosen = file;
              if (!chosen) return;
              void action.run(async () => {
                const body = await uploadAction("/api/v1/admin/audits/import", chosen);
                setImported(body);
                return `Записано. Новых компаний: ${cell(body.companies)}. Аудитов: ${cell(body.audits)}. Планов: ${cell(body.plans)}. Бек не вызывался.`;
              }, "Записать проверенный файл в базу? Бек не будет вызван.");
            }}
          >
            Записать в базу
          </button>
        ) : (
          <p className="hint">Кнопка записи появится после проверки файла.</p>
        )}
      </div>
      <Feedback message={action.message} error={action.error} />
      {summary && (
        <article className="panel">
          <h2>Просмотр файла</h2>
          <p>
            Компаний в файле: {cell(summary.companies)}. Повторов номера: {cell(summary.duplicates)}. Строк с вопросами: {cell(summary.issues)}. Лист: {cell(preview?.sheet)}.
          </p>
          <IssueList rows={summary.issue_rows} empty="Вопросов в файле нет." />
          <PreviewRows rows={summary.rows} />
        </article>
      )}
      {imported && (
        <article className="panel">
          <h2>Что записано</h2>
          <p>
            Новых компаний: {cell(imported.companies)}. Снимков: {cell(imported.snapshots)}. Аудитов: {cell(imported.audits)}. Планов улучшений: {cell(imported.plans)}.
          </p>
          <h3>Не сопоставилось</h3>
          <IssueList rows={imported.unmatched} empty="Все подходящие строки сопоставились." />
          <h3>Неоднозначные строки</h3>
          <IssueList rows={imported.ambiguous} empty="Неоднозначных строк нет." />
        </article>
      )}
      <h2>Компании и баллы</h2>
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Аудитов пока нет." />
      {rows.length > 0 && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Компания</th>
                <th>Балл</th>
                <th>Проверка</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={cell(row.id)}>
                  <td>
                    {row.company_id != null ? <a href={`#/companies/${cell(row.company_id)}`}>{cell(row.company_name)}</a> : cell(row.company_name)}
                  </td>
                  <td>{cell(row.score_total)}</td>
                  <td>
                    <Pill value={row.verification_status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function IssueList(props: { rows: unknown; empty: string }): ReactNode {
  const rows = Array.isArray(props.rows) ? (props.rows as Row[]) : [];
  if (rows.length === 0) return <p>{props.empty}</p>;
  return (
    <ul>
      {rows.map((row, index) => (
        <li key={`${cell(row.name)}-${index}`}>
          {cell(row.name || row.row_key)}: {cell(row.reason)}
        </li>
      ))}
    </ul>
  );
}

function PreviewRows(props: { rows: unknown }): ReactNode {
  const rows = Array.isArray(props.rows) ? (props.rows as Row[]) : [];
  if (rows.length === 0) return <p>В просмотре нет строк компаний.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Название</th>
            <th>Город</th>
            <th>Балл</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${cell(row.name)}-${index}`}>
              <td>{cell(row.name)}</td>
              <td>{cell(row.city)}</td>
              <td>{cell(row.score)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function observationLines(value: unknown): string[] {
  if (!value || typeof value !== "object") return [];
  const texts = (value as Row).texts;
  if (Array.isArray(texts)) return texts.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
  if (typeof texts === "string" && texts.trim()) return [texts.trim()];
  return [];
}

export function AuditDetail(props: { id: string }): ReactNode {
  const data = useGet<Row>(`/api/v1/admin/audits/${props.id}`);
  const audit = (data.data?.audit || {}) as Row;
  const plan = (data.data?.plan || null) as Row | null;
  const tasks = (plan?.tasks || []) as Row[];
  const notes = observationLines(audit.observations);
  return (
    <section>
      <h1>Аудит</h1>
      <Banner />
      <p>
        <a href="#/audits">К загрузке аудита</a>
        {audit.company_id != null ? (
          <>
            {" "}
            <a href={`#/companies/${cell(audit.company_id)}`}>Открыть компанию</a>
          </>
        ) : null}
      </p>
      <StateView loading={data.loading} error={data.error} />
      {data.data && (
        <>
          <p>
            {cell(audit.company_name)}. Балл {cell(audit.score_total)}. Проверка <Pill value={audit.verification_status} />. Карточка не менялась.
          </p>
          <h2>Наблюдения</h2>
          {notes.length === 0 ? <p>Текста наблюдений нет.</p> : <ul>{notes.map((text) => <li key={text}>{text}</li>)}</ul>}
          <h2>План улучшений</h2>
          {plan == null ? (
            <p>План не сохранен.</p>
          ) : tasks.length === 0 ? (
            <p>В сохраненном плане нет задач.</p>
          ) : (
            <ul>
              {tasks.map((task, index) => (
                <li key={`${cell(task.type)}-${index}`}>
                  {cell(task.platform)}. {cell(task.field)}. {cell(task.status)}.
                </li>
              ))}
            </ul>
          )}
          <details className="technical">
            <summary>Технические данные (для разработчика)</summary>
            <pre>{JSON.stringify(audit.observations ?? {}, null, 2)}</pre>
          </details>
        </>
      )}
    </section>
  );
}

const ORDER_EXAMPLE = `{
  "key": "local-order-1",
  "company": {"id_2gis": "70000000000000921", "name": "Salon Test", "city": "Алматы"},
  "platforms": [{"platform": "2GIS", "action": "update", "external_id": "70000000000000921", "owner_authorization": "verified"}]
}`;

export function Orders(): ReactNode {
  const [tick, setTick] = useState(0);
  const [spec, setSpec] = useState(ORDER_EXAMPLE);
  const action = useAction(() => setTick((value) => value + 1));
  const data = useGet<Row>(`/api/v1/admin/orders?limit=50&offset=0&refresh=${tick}`);
  const rows = rowsOf(data.data);
  return (
    <section>
      <h1>Заказы</h1>
      <Banner />
      <p className="hint">
        JSON ниже создает локальный заказ. Поля key, company, platforms и owner_authorization остаются как в API.
        Оплата, WhatsApp и Бек не вызываются. Повтор того же key не создает второй заказ.
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void action.run(async () => {
            let parsed: Row;
            try {
              parsed = JSON.parse(spec) as Row;
            } catch {
              throw new Error("JSON заказа не читается. Проверьте кавычки и скобки.");
            }
            const body = await sendAction("/api/v1/admin/orders", parsed);
            const readiness = (body.readiness || {}) as Row;
            return `Заказ ${cell(body.order_id)}, этап ${cell(body.stage)}. Готовность: ${cell(readiness.status)}. Бек не вызывался.`;
          }, "Создать этот локальный заказ? Бек, оплата и WhatsApp не будут вызваны.");
        }}
      >
        <label>
          JSON локального заказа
          <textarea value={spec} onChange={(event) => setSpec(event.target.value)} />
        </label>
        <button type="submit" disabled={action.pending}>
          {action.pending ? "Создание." : "Создать локальный заказ"}
        </button>
      </form>
      <Feedback message={action.message} error={action.error} />
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Заказов пока нет." />
      {rows.length > 0 && (
        <Table
          columns={["order_id", "company_name", "stage", "readiness", "amount_kzt"]}
          rows={rows}
          link={(row) => `#/orders/${cell(row.order_id)}`}
        />
      )}
    </section>
  );
}

export function OrderDetail(props: { id: string }): ReactNode {
  const data = useGet<Row>(`/api/v1/admin/orders/${props.id}`);
  const readiness = (data.data?.readiness || {}) as Row;
  const status = (data.data?.status || {}) as Row;
  const platforms = (status.platforms || []) as Row[];
  return (
    <section>
      <h1>Заказ {props.id}</h1>
      <Banner />
      <p className="hint">Список «Не хватает» показывает, чего не хватает до работы. Сумма в тенге. Публикации нет.</p>
      <p><a href="#/orders">К списку заказов</a></p>
      <StateView loading={data.loading} error={data.error} />
      {data.data && (
        <>
          <p>
            {cell(data.data.company_name)}. Этап <Pill value={data.data.stage} />. Готовность <Pill value={readiness.status} />.
            Сумма {cell(data.data.amount_kzt)} тенге. Реальная публикация: нет.
          </p>
          <h2>Не хватает</h2>
          <ul>
            <li>Данные компании: {cell(readiness.missing_company_information)}</li>
            <li>Материалы: {cell(readiness.missing_materials)}</li>
            <li>Допуски владельца: {cell(readiness.missing_owner_permissions)}</li>
            <li>Неясности: {cell(readiness.unresolved_ambiguities)}</li>
          </ul>
          <h2>Площадки</h2>
          {platforms.length === 0 ? <p>Работы по площадкам не сохранены.</p> : <Table columns={["platform", "action", "owner_authorization"]} rows={platforms} />}
        </>
      )}
    </section>
  );
}

export function Content(): ReactNode {
  const [tick, setTick] = useState(0);
  const [orderId, setOrderId] = useState("");
  const action = useAction(() => setTick((value) => value + 1));
  const data = useGet<Row>(`/api/v1/admin/content?limit=50&offset=0&refresh=${tick}`);
  const rows = rowsOf(data.data);
  return (
    <section>
      <h1>Тексты и картинки</h1>
      <Banner />
      <p className="hint">
        Укажите номер заказа и подготовьте офлайн-черновики. Утверждение версии не публикует текст. Картинки помечены как иллюстрации, это не фото компании.
      </p>
      <form
        className="toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run(
            () => sendAction(`/api/v1/admin/orders/${orderId}/content/prepare`).then((body) => `Офлайн-черновики для заказа ${cell(body.order_id)}. Публикация: нет.`),
            "Подготовить офлайн-черновики для этого заказа? Ничего не публикуется.",
          );
        }}
      >
        <label>
          Номер заказа
          <input value={orderId} placeholder="Например, 1" onChange={(event) => setOrderId(event.target.value)} />
        </label>
        <button type="submit" disabled={action.pending || !orderId.trim()}>
          {action.pending ? "Подготовка." : "Подготовить офлайн-черновики"}
        </button>
      </form>
      <p>
        Поставщик <Pill value="offline" />. Иллюстрации подписаны и не являются фотографиями компании.
      </p>
      <Feedback message={action.message} error={action.error} />
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Черновиков пока нет." />
      {rows.map((row) => (
        <article className="panel" key={cell(row.id)}>
          <p>
            #{cell(row.id)} заказ {cell(row.order_id)} {cell(row.kind)} версия {cell(row.version)} <Pill value={row.approval_status} />{" "}
            публикация {cell(row.publication)}
          </p>
          <p className="muted">Факты-источники: {cell(row.facts || row.source_refs)}</p>
          {typeof row.text === "string" && row.text ? <p>{row.text}</p> : null}
          {row.illustrative === true && <p>Иллюстрация, не фотография компании. Происхождение: {cell(row.origin)}.</p>}
          {row.approval_status === "done" && (
            <button
              type="button"
              disabled={action.pending}
              onClick={() =>
                void action.run(
                  () =>
                    sendAction(`/api/v1/admin/content/${cell(row.id)}/approve`, { version: row.version }).then(
                      (body) => `Материал ${cell(body.id)}, версия ${cell(body.version)}: ${cell(body.status)}. Публикация: нет.`,
                    ),
                  `Утвердить материал ${cell(row.id)}, версию ${cell(row.version)}? Это не публикация.`,
                )
              }
            >
              Утвердить эту версию
            </button>
          )}
        </article>
      ))}
    </section>
  );
}

function ListingCard(props: { row: Row; pageFile: File | null; pending: boolean; run: (work: () => Promise<string>, confirmText?: string) => Promise<void> }): ReactNode {
  const targets = ((props.row.targets || []) as Row[]).filter((item) => typeof item.platform === "string");
  const first = targets[0] || {};
  const [platform, setPlatform] = useState(typeof first.platform === "string" ? first.platform : "");
  const [operation, setOperation] = useState(typeof first.operation === "string" ? first.operation : "update");
  const selected = targets.find((item) => item.platform === platform) || first;
  const fields = {
    package_id: rawCell(props.row.package_id),
    version: rawCell(props.row.version),
    platform,
    operation,
    mode: "offline",
  };
  const items = ((props.row.items || []) as Row[]).filter((item) => item.platform === platform);
  const submissions = ((props.row.submissions || []) as Row[]).filter((item) => item.platform === platform);

  function choosePlatform(value: string): void {
    setPlatform(value);
    const match = targets.find((item) => item.platform === value);
    if (match && typeof match.operation === "string") setOperation(match.operation);
  }

  return (
    <article className="panel">
      <p>
        Пакет {cell(props.row.package_id)}, заказ {cell(props.row.order_id)}, версия {cell(props.row.version)} <Pill value={props.row.phase} />
      </p>
      <p>{cell(props.row.phase_label)} Режим доказательств: офлайн. Реальная публикация: нет. Утверждение одной площадки не утверждает другую.</p>
      {targets.length === 0 ? (
        <p>В пакете нет площадки.</p>
      ) : (
        targets.map((item) => (
          <p key={`${cell(item.platform)}-${cell(item.operation)}`}>
            {cell(item.platform)} {cell(item.operation)} подготовка <Pill value={item.preparation} /> утверждение <Pill value={item.approval} /> отправка{" "}
            <Pill value={item.submission} />. Опубликовано: нет.
          </p>
        ))
      )}
      <Table columns={["n", "platform", "field", "before", "after", "source_ref", "status"]} rows={items.length > 0 ? items : ((props.row.items || []) as Row[])} />
      {targets.length === 0 ? null : (
        <div className="actions">
          <label>
            Площадка
            <select value={platform} onChange={(event) => choosePlatform(event.target.value)}>
              {targets.map((item) => (
                <option key={cell(item.platform)} value={cell(item.platform)}>
                  {cell(item.platform)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Операция
            <select value={operation} onChange={(event) => setOperation(event.target.value)}>
              <option value="update">обновление (update)</option>
              <option value="create">создание (create)</option>
            </select>
          </label>
          <button
            type="button"
            disabled={props.pending || !props.pageFile || !platform}
            onClick={() => {
              const chosen = props.pageFile;
              if (!chosen) return;
              void props.run(
                () =>
                  uploadAction(`/api/v1/admin/orders/${cell(props.row.order_id)}/listings/prepare`, chosen, fields).then(
                    (body) =>
                      `Офлайн-подготовка ${cell(body.platform)} ${cell(body.operation)}: ${cell(body.status)}. Опубликовано: нет. Живой кабинет не открывался.`,
                  ),
                `Подготовить офлайн-черновик ${platform} (${operation}) для пакета ${cell(props.row.package_id)}, версии ${cell(props.row.version)}?`,
              );
            }}
          >
            Подготовить эту площадку
          </button>
          <button
            type="button"
            disabled={props.pending || !props.pageFile || !platform}
            onClick={() => {
              const chosen = props.pageFile;
              if (!chosen) return;
              void props.run(
                () =>
                  uploadAction(`/api/v1/admin/orders/${cell(props.row.order_id)}/listings/approve`, chosen, fields).then(
                    (body) => `Утверждение ${cell(body.platform)} ${cell(body.operation)}: ${cell(body.status)}. Опубликовано: ${cell(body.published)}. Это не реальная публикация.`,
                  ),
                `Утвердить пакет ${cell(props.row.package_id)}, версию ${cell(props.row.version)}, только для офлайн ${platform} (${operation})?`,
              );
            }}
          >
            Утвердить эту версию
          </button>
          <button
            type="button"
            disabled={props.pending || !props.pageFile || !platform}
            onClick={() => {
              const chosen = props.pageFile;
              if (!chosen) return;
              void props.run(
                () =>
                  uploadAction(`/api/v1/admin/orders/${cell(props.row.order_id)}/listings/simulate`, chosen, fields).then(
                    (body) =>
                      `Офлайн-симуляция ${cell(body.platform)} ${cell(body.operation)}: ${cell(body.status)}. Флаг отправки: ${cell(body.submitted)}. Опубликовано: нет. В живой кабинет ничего не ушло.`,
                  ),
                `Симулировать офлайн ${platform} (${operation})? Карточка не будет опубликована.`,
              );
            }}
          >
            Симулировать офлайн-отправку
          </button>
        </div>
      )}
      {submissions.length === 0 ? (
        <p>Для {cell(selected.platform || platform)} отправка не сохранена.</p>
      ) : (
        submissions.map((item) => (
          <p key={cell(item.id)}>
            {cell(item.platform)} {cell(item.operation)} <Pill value={item.phase} /> доказательства {cell(item.evidence_mode)}. {cell(item.phase_label)} Модерация{" "}
            {cell(item.moderation_status)}. Реальная публикация: нет.
          </p>
        ))
      )}
    </article>
  );
}

export function Listings(): ReactNode {
  const [tick, setTick] = useState(0);
  const [orderId, setOrderId] = useState("");
  const [pageFile, setPageFile] = useState<File | null>(null);
  const action = useAction(() => setTick((value) => value + 1));
  const data = useGet<Row>(`/api/v1/admin/listings?limit=50&offset=0&refresh=${tick}`);
  const rows = rowsOf(data.data);
  return (
    <section>
      <h1>Правки карточек</h1>
      <Banner />
      <p className="hint">
        Сухой прогон только считает план. Для подготовки, утверждения и симуляции приложите сохраненную HTML-страницу кабинета.
        Это офлайн-симуляция: форма на площадку не отправляется. Утверждение одной площадки не покрывает остальные.
      </p>
      <form
        className="toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run(
            () =>
              sendAction(`/api/v1/admin/orders/${orderId}/listings/dry-run`, {}).then(
                (body) => `Сухой прогон заказа ${cell(body.order_id)}. Режим ${cell(body.mode)}. Опубликовано: нет.`,
              ),
            "Сделать сухой прогон? Форма кабинета не отправляется.",
          );
        }}
      >
        <label>
          Номер заказа
          <input value={orderId} placeholder="Например, 1" onChange={(event) => setOrderId(event.target.value)} />
        </label>
        <button type="submit" disabled={action.pending || !orderId.trim()}>
          {action.pending ? "Расчет." : "Сухой прогон"}
        </button>
      </form>
      <label>
        Сохраненная офлайн-страница (.html)
        <input type="file" accept=".html,text/html" onChange={(event) => setPageFile(event.target.files?.[0] || null)} />
      </label>
      <p className="hint">Отправка в живой кабинет выключена. Симуляция не публикует карточку.</p>
      <Feedback message={action.message} error={action.error} />
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Пакетов правок пока нет." />
      {rows.map((row) => (
        <ListingCard key={cell(row.package_id)} row={row} pageFile={pageFile} pending={action.pending} run={action.run} />
      ))}
    </section>
  );
}

export function Tasks(): ReactNode {
  const data = useGet<Row>("/api/v1/admin/tasks?limit=50&offset=0");
  const rows = rowsOf(data.data);
  return (
    <section>
      <h1>Задачи человеку</h1>
      <Banner />
      <p className="hint">Сюда попадают остановки, которые программа не решает сама. Статус в таблице берется из базы как есть.</p>
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Задач человеку нет." />
      {rows.length > 0 && <Table columns={["id", "reason", "object_type", "object_id", "status", "created_at"]} rows={rows} />}
    </section>
  );
}

export function Reports(): ReactNode {
  const [tick, setTick] = useState(0);
  const [orderId, setOrderId] = useState("");
  const action = useAction(() => setTick((value) => value + 1));
  const data = useGet<Row>(`/api/v1/admin/reports?limit=50&offset=0&refresh=${tick}`);
  const rows = rowsOf(data.data);
  return (
    <section>
      <h1>Отчеты</h1>
      <Banner />
      <p className="hint">Отчет собирается из сохраненных данных заказа. Карточка на площадке не открывается. Скачивание доступно на странице отчета.</p>
      <form
        className="toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          void action.run(
            () =>
              sendAction(`/api/v1/admin/orders/${orderId}/reports/completion`).then(
                (body) => `Отчет ${cell(body.id)}. Итог: ${cell(body.outcome)}. Живая проверка: нет.`,
              ),
            "Собрать отчет о завершении из сохраненных данных? Карточка не откроется.",
          );
        }}
      >
        <label>
          Номер заказа
          <input value={orderId} placeholder="Например, 1" onChange={(event) => setOrderId(event.target.value)} />
        </label>
        <button type="submit" disabled={action.pending || !orderId.trim()}>
          {action.pending ? "Сборка." : "Собрать отчет о завершении"}
        </button>
      </form>
      <Feedback message={action.message} error={action.error} />
      <StateView loading={data.loading} error={data.error} empty={!data.loading && !data.error && rows.length === 0} emptyText="Отчетов пока нет." />
      {rows.length > 0 && (
        <Table
          columns={["id", "order_id", "period", "status", "verification_mode", "outcome", "stored_live_verified"]}
          rows={rows}
          link={(row) => `#/reports/${cell(row.id)}`}
        />
      )}
    </section>
  );
}

export function ReportDetail(props: { id: string }): ReactNode {
  const action = useAction();
  const data = useGet<Row>(`/api/v1/admin/reports/${props.id}`);
  const report = (data.data?.report || {}) as Row;
  return (
    <section>
      <h1>Отчет {props.id}</h1>
      <Banner />
      <p className="hint">JSON и HTML скачиваются из сохраненного отчета. PDF здесь не создается.</p>
      <p><a href="#/reports">К списку отчетов</a></p>
      <StateView loading={data.loading} error={data.error} />
      {data.data && (
        <>
          <p>
            Заказ {cell(report.order_id)}. Статус <Pill value={report.status} />. Режим {cell(report.verification_mode)}. Живой флаг в данных{" "}
            {cell(report.stored_live_verified)}. Итог {cell(report.outcome)}. Завершено {cell(report.completed)}. Эта страница читает
            сохраненный отчет и не делает живую проверку. PDF сохранен: {cell(report.has_pdf)}. Книга Excel сохранена: {cell(report.has_xlsx)}.
          </p>
          <div className="actions">
            <button type="button" disabled={action.pending} onClick={() => void action.run(() => downloadAction(`/api/v1/admin/reports/${props.id}/completion.json`, "completion.json"))}>
              Скачать JSON
            </button>
            <button type="button" disabled={action.pending} onClick={() => void action.run(() => downloadAction(`/api/v1/admin/reports/${props.id}/completion.html`, "completion.html"))}>
              Скачать HTML
            </button>
          </div>
          <Feedback message={action.message} error={action.error} />
        </>
      )}
    </section>
  );
}
