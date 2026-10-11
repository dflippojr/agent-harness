import type { paths, components } from "./api.js";

export type Surface = "app" | "admin" | "legacy" | "";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export interface RequestOptions<M extends HttpMethod = HttpMethod> {
  method?: M;
  body?: unknown;
  surface?: Surface;
}
// api() selects the surface at runtime: shared fields are typed; surface-specific fields require narrowing.
export type SessionDetail =
  paths["/api/v1/sessions/{ref}"]["get"]["responses"][200]["content"]["application/json"] |
  paths["/api/admin/v1/sessions/{ref}"]["get"]["responses"][200]["content"]["application/json"];
export type SessionList =
  paths["/api/v1/sessions"]["get"]["responses"][200]["content"]["application/json"] |
  paths["/api/admin/v1/sessions"]["get"]["responses"][200]["content"]["application/json"];
export type JobList = paths["/api/admin/v1/jobs"]["get"]["responses"][200]["content"]["application/json"];
export type AdminDiscovery = paths["/api/admin/v1"]["get"]["responses"][200]["content"]["application/json"];
export type CronPreview = paths["/api/admin/v1/jobs/preview"]["get"]["responses"][200]["content"]["application/json"];

// Only the first response contracts are opted in. Other endpoints remain incremental work.
type Route<P extends string> = P extends `${infer Path}?${string}` ? Path : P;
type ResponseFor<P extends string, M extends HttpMethod> =
  P extends "/sessions" ? (M extends "GET" ? SessionList : M extends "POST" ? SessionDetail : any) :
  P extends `/sessions/${infer Ref}` ? (Ref extends `${string}/${string}` ? any : M extends "GET" | "PATCH" | "PUT" ? SessionDetail : any) :
  P extends "/jobs" ? (M extends "GET" ? JobList : M extends "POST" ? components["schemas"]["JobResponse"] : any) :
  P extends "/jobs/preview" ? (M extends "GET" ? CronPreview : any) :
  P extends `/jobs/${infer Ref}` ? (Ref extends `${string}/${string}` ? any : M extends "DELETE" ? null : components["schemas"]["JobResponse"]) :
  P extends "" ? AdminDiscovery : any;
export type ApiResponse<P extends string, M extends HttpMethod = "GET"> = ResponseFor<Route<P>, M>;

export interface Api {
  <P extends string, M extends HttpMethod = "GET">(path: P, options?: RequestOptions<M>): Promise<ApiResponse<P, M>>;
}
export interface ClientError extends Error {
  status?: number;
  code?: string;
  keys?: Record<string, unknown>;
  details?: Record<string, unknown>;
  data?: unknown;
}
export interface Identity {
  role: "owner" | "member" | "guest" | "signin" | "offline";
  offline?: boolean;
  [key: string]: unknown;
}
export interface WebAuth {
  csrf?: string;
  [key: string]: unknown;
}
export type ConnectionState = "live" | "reconnecting" | "offline";
export type StreamStop = (() => void) & { nudge(): void };
// SSE event payload contracts are not yet in OpenAPI; type the transport envelope for now.
export interface StreamEvent { type: string; seq?: number | null; ts?: number; data: any }
export type StreamHandlers = Record<string, (event: StreamEvent) => void>;
export interface StreamOptions {
  authorized?: boolean;
  indicate?: boolean;
  onState?: ((state: ConnectionState) => void) | null;
}
export type OpenStream = (urlFor: () => string | Promise<string>, handlers: StreamHandlers, options?: StreamOptions) => StreamStop;

export interface SheetOptions {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
  dismissOnRoute?: boolean;
}
export type ConfirmSheet = (options: SheetOptions) => Promise<boolean>;
export type PromptSheet = (options: SheetOptions & {
  label?: string;
  value?: string;
  type?: string;
  inputmode?: string;
  placeholder?: string;
  validate?: (value: string) => string | undefined;
}) => Promise<string | null>;
