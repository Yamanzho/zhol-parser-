import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { Row } from "./api";
import { sendAction } from "./ui";

const CITY_KEY = "zhol.collect.cities";
const EMPTY_SOURCES: Source[] = [];
const LARGE = 12;

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

type Locality = { id: string; name: string; region: string; sources: Record<string, boolean> };
type Category = { label: string; sector: string };
type Group = { sector: string; labels: string[] };
type Setup = {
  localities?: Locality[];
  categories?: Category[];
  sources?: Source[];
  category_catalog?: { ready?: boolean; count?: number; error?: string | null; groups?: Group[] };
  limit?: { min: number; max: number; default: number };
};

type CityMode = "one" | "many" | "all";
type CategoryMode = "all" | "pick" | "manual";

export function CollectionForm(props: {
  options: Setup;
  pending: boolean;
  onRun: (work: () => Promise<string>) => void;
  onStarted: (ids: number[]) => void;
}): ReactNode {
  const localities = props.options.localities || [];
  const categories = props.options.categories || [];
  const groups = props.options.category_catalog?.groups || [];
  const catalogReady = props.options.category_catalog?.ready !== false && categories.length > 0;
  const catalogError = props.options.category_catalog?.error;
  const sources = props.options.sources ?? EMPTY_SOURCES;
  const limitRule = props.options.limit || { min: 1, max: 500, default: 20 };
  const [cityMode, setCityMode] = useState<CityMode>("one");
  const [cities, setCities] = useState<string[]>(() => readCities(localities));
  const [cityQuery, setCityQuery] = useState("");
  const [categoryMode, setCategoryMode] = useState<CategoryMode>("pick");
  const [picked, setPicked] = useState<string[]>([]);
  const [queryText, setQueryText] = useState("");
  const [filter, setFilter] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [limit, setLimit] = useState(String(limitRule.default));
  const [slice, setSlice] = useState("");
  const [skipUnsupported, setSkipUnsupported] = useState(false);
  const [confirmLarge, setConfirmLarge] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  useEffect(() => {
    const ready = sources.filter((source) => browserReady(source)).map((source) => source.id);
    setChosen(ready.filter((id) => id === "2gis" || id === "google").length > 0 ? ready.filter((id) => id === "2gis" || id === "google") : ready);
  }, [sources]);

  useEffect(() => {
    localStorage.setItem(CITY_KEY, JSON.stringify(cities));
  }, [cities]);

  const selectedCities = cityMode === "all" ? localities.map((item) => item.name) : cities;
  const queries = useMemo(() => normalizeLines(queryText), [queryText]);
  const phrases = categoryMode === "all" ? categories.map((item) => item.label) : categoryMode === "manual" ? queries : picked;
  const readySources = chosen.filter((id) => browserReady(sources.find((source) => source.id === id)));
  const skipped = useMemo(
    () => incompatible(selectedCities, readySources, localities),
    [selectedCities, readySources, localities],
  );
  const pairCount = countSupported(selectedCities, readySources, localities);
  const supported = pairCount * phrases.length;
  const parsedLimit = Number(limit);
  const upper = Number.isInteger(parsedLimit) ? parsedLimit * readySources.length : 0;
  const large = cityMode === "all" || categoryMode === "all" || supported > LARGE;

  const visibleCities = localities.filter((item) => {
    const needle = cityQuery.trim().toLocaleLowerCase("ru");
    return !needle || item.name.toLocaleLowerCase("ru").includes(needle) || item.region.toLocaleLowerCase("ru").includes(needle);
  });
  const visibleGroups = groups
    .map((group) => ({
      ...group,
      labels: group.labels.filter((label) => {
        const needle = filter.trim().toLocaleLowerCase("ru");
        return !needle || label.toLocaleLowerCase("ru").includes(needle) || group.sector.toLocaleLowerCase("ru").includes(needle);
      }),
    }))
    .filter((group) => group.labels.length > 0);

  function toggleCity(name: string): void {
    setCities((current) => {
      if (cityMode === "one") return [name];
      return current.includes(name) ? current.filter((item) => item !== name) : [...current, name];
    });
  }

  return (
    <form
      className="collect-form"
      onSubmit={(event) => {
        event.preventDefault();
        const problems: string[] = [];
        if (!catalogReady) problems.push(catalogError || "Каталог категорий пуст.");
        if (selectedCities.length === 0) problems.push("Выберите город.");
        if (phrases.length === 0) problems.push(categoryMode === "manual" ? "Введите хотя бы один запрос." : "Выберите категорию.");
        if (readySources.length === 0) problems.push("Выберите хотя бы один доступный источник.");
        if (!Number.isInteger(parsedLimit) || parsedLimit < limitRule.min || parsedLimit > limitRule.max) {
          problems.push(`Лимит на источник: от ${limitRule.min} до ${limitRule.max}.`);
        }
        const sliceValue = slice.trim() ? Number(slice) : null;
        if (sliceValue !== null && (!Number.isInteger(sliceValue) || sliceValue < 1 || sliceValue > parsedLimit)) {
          problems.push("Лимит на сочетание должен быть от 1 до лимита на источник.");
        }
        if (skipped.length > 0 && !skipUnsupported) problems.push("Есть неподдержанные сочетания. Пропустите их или снимите источник.");
        if (large && !confirmLarge) problems.push("Большой сбор нужно подтвердить.");
        setErrors(problems);
        if (problems.length > 0) return;
        props.onRun(async () => {
          const body = await sendAction("/api/v1/admin/scout/collect", {
            city: selectedCities[0] || "Алматы",
            cities: selectedCities,
            categories: categoryMode === "pick" ? picked : [],
            queries: categoryMode === "manual" ? queries : [],
            all_cities: cityMode === "all",
            all_categories: categoryMode === "all",
            platforms: readySources,
            limit: parsedLimit,
            source_mode: readySources[0] === "google" ? "browser_google" : readySources[0] === "yandex" ? "browser_yandex" : "browser_2gis",
            confirm: large,
            skip_unsupported: skipUnsupported,
            combination_limit: sliceValue,
          });
          const jobs = Array.isArray(body.jobs) ? (body.jobs as Row[]) : [];
          const ids = jobs.map((job) => job.id).filter((id): id is number => typeof id === "number");
          props.onStarted(ids);
          const notes = Array.isArray(body.errors) ? (body.errors as Row[]).map((item) => String(item.detail || "")).filter(Boolean) : [];
          const lead = `Сбор поставлен в очередь. Задач: ${ids.length}. Сочетаний: ${String(body.combination_count ?? body.query_count ?? "")}. Лимит на источник: ${parsedLimit}.`;
          if (notes.length > 0) throw new Error([lead, ...notes].join(" "));
          return lead;
        });
      }}
    >
      <div className="form-row">
        <section>
          <h2>Города</h2>
          <div className="modes">
            <ModeButton current={cityMode} id="one" label="Один город" onPick={(id) => setCityMode(id)} />
            <ModeButton current={cityMode} id="many" label="Несколько городов" onPick={(id) => setCityMode(id)} />
            <ModeButton current={cityMode} id="all" label="Все доступные города" onPick={(id) => setCityMode(id)} />
          </div>
          {cityMode === "all" ? (
            <p className="hint">Все города справочника: {localities.length}. Это не обход всей страны и сбор сам не стартует.</p>
          ) : (
            <>
              <label>
                Поиск города
                <input value={cityQuery} placeholder="Название" onChange={(event) => setCityQuery(event.target.value)} />
              </label>
              {cityMode === "many" ? (
                <div className="actions">
                  <button type="button" onClick={() => setCities(localities.map((item) => item.name))}>
                    Выбрать все
                  </button>
                  <button type="button" onClick={() => setCities([])}>
                    Очистить выбор
                  </button>
                </div>
              ) : null}
              <div className="chips">
                {cities.map((name) => (
                  <button type="button" key={name} onClick={() => setCities((current) => current.filter((item) => item !== name))}>
                    {name} ×
                  </button>
                ))}
              </div>
              <div className="choices">
                {visibleCities.map((item) => (
                  <label className="choice" key={item.id}>
                    <input type={cityMode === "one" ? "radio" : "checkbox"} name="city" checked={cities.includes(item.name)} onChange={() => toggleCity(item.name)} />
                    <span>
                      {item.name}
                      <span className="mark">{item.region}</span>
                    </span>
                  </label>
                ))}
              </div>
            </>
          )}
        </section>
        <section>
          <h2>Категории</h2>
          {!catalogReady ? <p className="error">{catalogError || "Каталог категорий пуст."}</p> : <p className="hint">В каталоге {categories.length} активных категорий. Это не все бизнесы Казахстана.</p>}
          <div className="modes">
            <ModeButton current={categoryMode} id="all" label="Все категории" onPick={(id) => setCategoryMode(id)} />
            <ModeButton current={categoryMode} id="pick" label="Выбрать категории" onPick={(id) => setCategoryMode(id)} />
            <ModeButton current={categoryMode} id="manual" label="Ввести запросы" onPick={(id) => setCategoryMode(id)} />
          </div>
          {categoryMode === "all" ? <p className="hint">В поиск уйдут названия из каталога, а не слово «Все».</p> : null}
          {categoryMode === "pick" ? (
            <>
              <label>
                Поиск категории
                <input value={filter} placeholder="Название" onChange={(event) => setFilter(event.target.value)} />
              </label>
              <div className="actions">
                <button type="button" onClick={() => setPicked(categories.map((item) => item.label))}>
                  Выбрать все
                </button>
                <button type="button" onClick={() => setPicked([])}>
                  Очистить выбор
                </button>
                <span className="hint">Выбрано: {picked.length}</span>
              </div>
              {visibleGroups.map((group) => (
                <details key={group.sector} open>
                  <summary>
                    {group.sector} ({group.labels.filter((label) => picked.includes(label)).length}/{group.labels.length})
                  </summary>
                  {group.labels.map((label) => (
                    <label className="choice" key={label}>
                      <input
                        type="checkbox"
                        checked={picked.includes(label)}
                        onChange={(event) =>
                          setPicked((current) => (event.target.checked ? [...current, label] : current.filter((item) => item !== label)))
                        }
                      />
                      <span>{label}</span>
                    </label>
                  ))}
                </details>
              ))}
            </>
          ) : null}
          {categoryMode === "manual" ? (
            <label>
              Один запрос на строку
              <textarea value={queryText} placeholder={"Салон красоты\nБарбершоп"} onChange={(event) => setQueryText(event.target.value)} />
              <span className="hint">После очистки повторов: {queries.length}</span>
            </label>
          ) : null}
        </section>
      </div>
      <div className="form-row">
        <section>
          <h2>Источники</h2>
          <div className="source-cards">
            {["2gis", "google", "yandex"].map((id) => {
              const source = sources.find((item) => item.id === id);
              const state = sourceState(source);
              const ready = state === "available";
              const title = id === "2gis" ? "2ГИС" : id === "google" ? "Google Maps" : "Яндекс Карты";
              const stateLabel = state === "available" ? "доступен" : state === "restricted" ? "ограничен" : "нет разрешения";
              return (
                <label className={ready ? "source-card" : "source-card source-off"} key={id}>
                  <input
                    type="checkbox"
                    checked={ready && chosen.includes(id)}
                    disabled={!ready}
                    onChange={(event) => setChosen((current) => (event.target.checked ? [...current, id] : current.filter((item) => item !== id)))}
                  />
                  <strong>{title}</strong>
                  <span>{stateLabel}</span>
                  {source?.limit_note ? <span className="mark">{source.limit_note}</span> : null}
                </label>
              );
            })}
          </div>
        </section>
        <section>
          <h2>Лимиты</h2>
          <label>
            Максимум уникальных компаний на источник
            <input value={limit} inputMode="numeric" onChange={(event) => setLimit(event.target.value)} />
          </label>
          <p className="hint">Общий потолок источника на все города и запросы. Число городов его не умножает.</p>
          <details>
            <summary>Дополнительно</summary>
            <label>
              Максимум на одно сочетание города и запроса
              <input value={slice} inputMode="numeric" placeholder="не задан" onChange={(event) => setSlice(event.target.value)} />
            </label>
          </details>
        </section>
      </div>
      <section className="summary compact">
        <h2>План</h2>
        <p>
          Городов: {selectedCities.length}. Запросов: {phrases.length}. Источников: {readySources.length}. Сочетаний: {supported}.
          Верхняя оценка компаний: {upper}.
        </p>
        {skipped.length > 0 ? (
          <>
            <p className="error">Не пойдут в очередь: {skipped.slice(0, 8).map((item) => `${item.city} / ${item.source}`).join(", ")}{skipped.length > 8 ? ` и ещё ${skipped.length - 8}` : ""}.</p>
            <label className="choice">
              <input type="checkbox" checked={skipUnsupported} onChange={(event) => setSkipUnsupported(event.target.checked)} />
              <span>Пропустить неподдержанные сочетания</span>
            </label>
          </>
        ) : null}
        {large ? (
          <label className="choice">
            <input type="checkbox" checked={confirmLarge} onChange={(event) => setConfirmLarge(event.target.checked)} />
            <span>Подтверждаю запуск этого сбора</span>
          </label>
        ) : null}
      </section>
      {errors.length > 0 ? (
        <ul>
          {errors.map((error) => (
            <li className="field-error" key={error}>
              {error}
            </li>
          ))}
        </ul>
      ) : null}
      <button type="submit" disabled={props.pending}>
        {props.pending ? "Запуск." : "Начать сбор"}
      </button>
    </form>
  );
}

function sourceState(source: Source | undefined): "available" | "restricted" | "unauthorized" {
  if (source?.collection_state) return source.collection_state;
  return source?.live_available ? "available" : "unauthorized";
}

function browserReady(source: Source | undefined): boolean {
  return sourceState(source) === "available";
}

function ModeButton<T extends string>(props: { current: T; id: T; label: string; onPick: (id: T) => void }): ReactNode {
  return (
    <button type="button" className={props.current === props.id ? "tab active" : "tab"} onClick={() => props.onPick(props.id)}>
      {props.label}
    </button>
  );
}

function readCities(localities: Locality[]): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(CITY_KEY) || "") as unknown;
    if (!Array.isArray(saved)) return ["Алматы"];
    const known = new Set(localities.map((item) => item.name));
    const names = saved.filter((item): item is string => typeof item === "string" && (known.size === 0 || known.has(item)));
    return names.length > 0 ? names : ["Алматы"];
  } catch {
    return ["Алматы"];
  }
}

function normalizeLines(value: string): string[] {
  const seen = new Set<string>();
  const lines: string[] = [];
  for (const raw of value.split("\n")) {
    const text = raw.split(/\s+/).filter(Boolean).join(" ");
    const key = text.toLocaleLowerCase("ru").split("ё").join("е");
    if (!text || seen.has(key)) continue;
    seen.add(key);
    lines.push(text);
  }
  return lines;
}

function incompatible(cities: string[], platforms: string[], localities: Locality[]): Array<{ city: string; source: string }> {
  const byName = new Map(localities.map((item) => [item.name, item]));
  const rows: Array<{ city: string; source: string }> = [];
  for (const city of cities) {
    const locality = byName.get(city);
    for (const platform of platforms) {
      if (!locality?.sources?.[platform]) rows.push({ city, source: platform === "2gis" ? "2ГИС" : platform === "google" ? "Google Maps" : "Яндекс Карты" });
    }
  }
  return rows;
}

function countSupported(cities: string[], platforms: string[], localities: Locality[]): number {
  const byName = new Map(localities.map((item) => [item.name, item]));
  let count = 0;
  for (const city of cities) {
    const locality = byName.get(city);
    for (const platform of platforms) {
      if (locality?.sources?.[platform]) count += 1;
    }
  }
  return count;
}
