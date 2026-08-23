export type UserRole = "customer" | "agent" | "operator" | "admin" | "finance";

export interface SignedInUser {
  id: number;
  username: string;
  role: UserRole;
}

export interface AuthState {
  token: string;
  user: SignedInUser;
}

export interface Ticket {
  ticket_id: string;
  customer_name?: string;
  phone?: string;
  issue?: string;
  urgency: string;
  status: string;
  created_at: string;
  assigned_agent_id?: number;
}

export interface TicketMessage {
  message_id: number;
  author_role: "customer" | "agent" | "ai";
  content: string;
  ai_assisted: boolean;
  created_at: string;
}

export interface ChatResponse {
  answer: string;
  session_id: string;
  total_steps: number;
  total_tokens: number;
}

export interface SessionItem {
  session_id: string;
  title: string;
  created_at: number;
  last_active: number;
  message_count: number;
}

export interface Product {
  id: string;
  product_name: string;
  brand: string;
  price: number | null;
  description: string;
  product_type: string;
  status: string;
  stock: number;
  warehouse: string;
  image_url: string | null;
}

export interface CustomerOrder {
  order_id: string;
  status: string | null;
  tracking: { company: string | null; number: string | null };
  total_amount: number;
  paid_amount: number;
  payment_method: string | null;
  order_date: string;
  delivered_at: string | null;
  items: Array<{ product_name: string; brand: string | null; price: number; quantity: number | null }>;
}

export interface SupportReplyDraft {
  ticket_id: string;
  draft: string;
  knowledge_references: Array<{ title: string; reference: string }>;
  needs_human_follow_up: boolean;
}

interface ErrorBody {
  error?: { code?: string; message?: string };
  detail?: string;
}

/** 将统一 API 错误转换为可直接显示给用户的短消息。 */
async function api<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    throw new Error(body.error?.message ?? body.detail ?? "请求暂时无法完成");
  }
  return response.json() as Promise<T>;
}

export async function signIn(username: string, password: string): Promise<AuthState> {
  return api<AuthState>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function signOut(token: string): Promise<{ message: string }> {
  return api("/api/v1/auth/logout", { method: "POST" }, token);
}

export function sendChat(token: string, query: string, sessionId?: string): Promise<ChatResponse> {
  return api("/api/v1/chat", {
    method: "POST",
    body: JSON.stringify({ query, session_id: sessionId }),
  }, token);
}

export async function listProducts(token: string, category: "laptops" | "phones", query = ""): Promise<Product[]> {
  const parameters = new URLSearchParams({ category });
  if (query.trim()) parameters.set("query", query.trim());
  const response = await api<{ products: Product[] }>(`/api/v1/products?${parameters}`, {}, token);
  return response.products;
}

export async function listMyOrders(token: string): Promise<CustomerOrder[]> {
  const response = await api<{ orders: CustomerOrder[] }>("/api/v1/orders/my", {}, token);
  return response.orders;
}

export async function listSessions(token: string): Promise<SessionItem[]> {
  const response = await api<{ sessions: SessionItem[] }>("/api/v1/sessions", {}, token);
  return response.sessions;
}

export async function listTickets(token: string): Promise<Ticket[]> {
  const response = await api<{ tickets: Ticket[] }>("/api/v1/tickets", {}, token);
  return response.tickets;
}

export function getTicket(token: string, ticketId: string): Promise<Ticket> {
  return api(`/api/v1/tickets/${encodeURIComponent(ticketId)}`, {}, token);
}

export async function listTicketMessages(token: string, ticketId: string): Promise<TicketMessage[]> {
  const response = await api<{ messages: TicketMessage[] }>(
    `/api/v1/tickets/${encodeURIComponent(ticketId)}/messages`,
    {},
    token,
  );
  return response.messages;
}

export function claimTicket(token: string, ticketId: string): Promise<Ticket> {
  return api(`/api/v1/tickets/${encodeURIComponent(ticketId)}/claim`, { method: "POST" }, token);
}

export function requestReplyDraft(token: string, ticketId: string): Promise<SupportReplyDraft> {
  return api(`/api/v1/agent/support-reply-drafts/${encodeURIComponent(ticketId)}`, { method: "POST" }, token);
}

export function sendAgentTicketMessage(token: string, ticketId: string, content: string, aiAssisted: boolean): Promise<TicketMessage> {
  return api(`/api/v1/tickets/${encodeURIComponent(ticketId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ content, ai_assisted: aiAssisted }),
  }, token);
}

export function sendCustomerTicketMessage(token: string, ticketId: string, content: string): Promise<TicketMessage> {
  return api(`/api/v1/tickets/${encodeURIComponent(ticketId)}/customer-messages`, {
    method: "POST",
    body: JSON.stringify({ content }),
  }, token);
}
