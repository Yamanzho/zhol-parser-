import { ru } from "./text";

const TOKEN_KEY = "zhol.admin.token";

export type Row = Record<string, unknown>;

let onUnauthorized = () => {};

export function setUnauthorized(handler: () => void): void {
  onUnauthorized = handler;
}

export function isLocalHost(): boolean {
  const host = window.location.hostname;
  return host === "127.0.0.1" || host === "localhost" || host === "::1";
}

export function getToken(): string {
  return sessionStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(token: string): void {
  sessionStorage.setItem(TOKEN_KEY, token.trim());
}

export function clearToken(): void {
  sessionStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function authHeaders(json = false): Headers {
  const headers = new Headers();
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (json) headers.set("Content-Type", "application/json");
  return headers;
}

function failureText(status: number, detail: string): string {
  return ru(detail) || ru("request failed") || `Ошибка ${status}`;
}

async function readBody<T>(response: Response): Promise<T> {
  if (response.status === 401) {
    clearToken();
    onUnauthorized();
    throw new ApiError(401, ru("authentication required"));
  }
  if (!response.ok) {
    let detail = response.statusText || "request failed";
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail)) {
        detail = body.detail
          .map((item) => (item && typeof item === "object" && "msg" in item ? String(item.msg) : JSON.stringify(item)))
          .join("; ");
      }
    } catch {
      detail = response.statusText || "request failed";
    }
    throw new ApiError(response.status, failureText(response.status, detail));
  }
  if (response.status === 204) return undefined as T;
  const type = response.headers.get("content-type") || "";
  if (!type.includes("json")) return undefined as T;
  return (await response.json()) as T;
}

export async function ensureLocalSession(): Promise<boolean> {
  if (!isLocalHost()) return false;
  try {
    const response = await fetch("/api/v1/admin/local-session", {
      method: "POST",
      credentials: "include",
      headers: { "X-Zhol-Request": "admin" },
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function endLocalSession(): Promise<void> {
  await fetch("/api/v1/admin/local-session/logout", {
    method: "POST",
    credentials: "include",
    headers: { "X-Zhol-Request": "admin" },
  }).catch(() => undefined);
}

export async function api<T>(path: string): Promise<T> {
  return readBody<T>(await fetch(path, { headers: authHeaders(), credentials: "include" }));
}

export async function apiSend<T>(path: string, body?: unknown): Promise<T> {
  const headers = authHeaders(body !== undefined);
  headers.set("X-Zhol-Request", "admin");
  return readBody<T>(
    await fetch(path, {
      method: "POST",
      credentials: "include",
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  );
}

export async function apiUploadForm<T>(path: string, data: FormData): Promise<T> {
  const headers = authHeaders();
  headers.set("X-Zhol-Request", "admin");
  return readBody<T>(await fetch(path, { method: "POST", credentials: "include", headers, body: data }));
}

export async function apiUpload<T>(path: string, file: File, fields?: Record<string, string>): Promise<T> {
  const data = new FormData();
  data.append("file", file);
  for (const [key, value] of Object.entries(fields || {})) data.append(key, value);
  const headers = authHeaders();
  headers.set("X-Zhol-Request", "admin");
  return readBody<T>(await fetch(path, { method: "POST", credentials: "include", headers, body: data }));
}

export async function apiDownload(path: string, filename: string): Promise<void> {
  const response = await fetch(path, { headers: authHeaders(), credentials: "include" });
  if (!response.ok) {
    await readBody(response);
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}
