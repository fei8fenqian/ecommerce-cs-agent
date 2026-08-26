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

export interface TicketEscalation {
  status: "PENDING" | "DELIVERING" | "DELIVERED" | "RETRY_WAIT" | "DLQ" | null;
  attempts: number;
  next_attempt_at: string | null;
  delivered_at: string | null;
  last_error_code: string | null;
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

export interface ChatStreamEvent {
  event: string;
  session_id?: string;
  content?: string;
  answer?: string;
  data?: { answer?: string };
  code?: string;
  message?: string;
}

export interface SessionItem {
  session_id: string;
  title: string;
  created_at: number;
  last_active: number;
  message_count: number;
}

export interface SessionDetail {
  session_id: string;
  title: string;
  messages: Array<{ role: string; content?: string; sequence_no?: number }>;
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
  image_url: string | null;
}

export interface ProductCatalogPage {
  category: "laptops" | "phones" | "components";
  products: Product[];
  total: number;
  page: number;
  page_size: number;
  brands: string[];
  component_categories: Record<string, string>;
}

export interface ProductDetail extends Product {
  specifications: Array<{ name: string; value: string }>;
}

export interface CheckoutSession {
  order_no: string;
  amount_cents: number;
  payment_url: string;
  payment_form_action?: string | null;
  payment_form_fields?: Record<string, string> | null;
  payment_qr_code?: string | null;
}

export interface PublicAssistantResponse {
  answer: string;
}

/** 详情页传给服务端的商品定位；服务端会重新读取公开商品事实。 */
export interface ProductContextRef {
  category: "laptops" | "phones" | "components";
  productId: string;
}

export interface CheckoutOrder {
  order_no: string;
  status: string;
  total_amount_cents: number;
  product_name: string;
  quantity: number;
  payment_status: string;
  fulfillment_status: string | null;
  tracking_company: string | null;
  tracking_number: string | null;
  created_at: string;
  refund_id: string | null;
  refund_status: string | null;
}

/** 新商城订单的全额退款状态；支付网关原始字段不会发送给浏览器。 */
export interface CheckoutRefund {
  refund_id: string;
  order_no: string;
  status: "PENDING_CONFIRMATION" | "PENDING_FINANCE_APPROVAL" | "PROCESSING" | "SUCCEEDED" | "FAILED" | "REJECTED";
  amount_cents: number;
  currency: "CNY";
  reason: string;
  requested_at: string;
  idempotent_replay: boolean;
}

export interface FinanceRefund {
  refund_id: string;
  order_no: string;
  status: string;
  amount_cents: number;
  currency: string;
  reason: string;
  requested_at: string;
  finance_decision_note: string;
  finance_decided_at: string | null;
}

export interface FinanceAnomaly {
  anomaly_type: string;
  reference_id: string;
  order_no: string;
  status: string;
  amount_cents: number;
  currency: string;
  reason: string;
  occurred_at: string;
  age_seconds: number;
}

export interface CartItem {
  item_id: number;
  category: "laptops" | "phones";
  product_id: string;
  product_name: string;
  brand: string;
  price: number | null;
  stock: number;
  quantity: number;
  available: boolean;
}

export interface Cart {
  items: CartItem[];
}

export interface Fulfillment {
  order_no: string;
  product_name: string;
  quantity: number;
  status: string;
  carrier: string | null;
  tracking_number: string | null;
  created_at: string;
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

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function notifyAuthenticationExpired(): void {
  window.dispatchEvent(new Event("ecommerce-agent.auth-invalid"));
}

/** 将统一 API 错误转换为可直接显示给用户的短消息。 */
async function api<T>(path: string, options: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(path, { ...options, credentials: "same-origin", headers });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    if (token && response.status === 401) notifyAuthenticationExpired();
    throw new ApiError(
      body.error?.message ?? body.detail ?? "请求暂时无法完成",
      response.status,
      body.error?.code,
    );
  }
  return response.json() as Promise<T>;
}

export async function signIn(username: string, password: string): Promise<AuthState> {
  return api<AuthState>("/api/v1/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export async function register(username: string, password: string): Promise<AuthState> {
  return api<AuthState>("/api/v1/auth/register", {
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

/** 消费 FastAPI 的 SSE 聊天流；每条事件在到达浏览器时立即交给页面渲染。 */
export async function streamChat(
  token: string,
  query: string,
  sessionId: string | undefined,
  onEvent: (event: ChatStreamEvent) => void,
  replaceFromSequence?: number,
  product?: ProductContextRef,
): Promise<void> {
  const response = await fetch("/api/v1/chat/stream", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, Accept: "text/event-stream" },
    body: JSON.stringify({
      query,
      session_id: sessionId,
      replace_from_sequence: replaceFromSequence,
      product_category: product?.category,
      product_id: product?.productId,
    }),
  });
  if (!response.ok || !response.body) {
    const body = (await response.json().catch(() => ({}))) as ErrorBody;
    if (response.status === 401) notifyAuthenticationExpired();
    throw new ApiError(
      body.error?.message ?? body.detail ?? "智能客服暂时不可用",
      response.status,
      body.error?.code,
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  while (true) {
    const chunk = await reader.read();
    if (chunk.done) break;
    pending += decoder.decode(chunk.value, { stream: true });
    const messages = pending.split("\n\n");
    pending = messages.pop() ?? "";
    for (const message of messages) {
      const data = message.split("\n").find((line) => line.startsWith("data: "))?.slice(6);
      if (!data) continue;
      const event = JSON.parse(data) as ChatStreamEvent;
      onEvent(event);
      if (event.event === "error") throw new Error(event.message ?? "智能客服暂时不可用");
    }
  }
}

export async function listProducts(
  token: string | undefined,
  category: "laptops" | "phones" | "components",
  options: { query?: string; brand?: string; componentCategory?: string; page?: number } = {},
): Promise<ProductCatalogPage> {
  const parameters = new URLSearchParams({ category, page_size: "24", page: String(options.page ?? 1) });
  if (options.query?.trim()) parameters.set("query", options.query.trim());
  if (options.brand?.trim()) parameters.set("brand", options.brand.trim());
  if (options.componentCategory?.trim()) parameters.set("component_category", options.componentCategory.trim());
  return api<ProductCatalogPage>(`/api/v1/products?${parameters}`, {}, token);
}

/** 读取单件商品的公开规格，用于目录中的详情页。 */
export function getProductDetail(
  token: string | undefined,
  category: "laptops" | "phones" | "components",
  productId: string,
): Promise<ProductDetail> {
  return api<ProductDetail>(`/api/v1/products/${category}/${encodeURIComponent(productId)}`, {}, token);
}

/** 匿名访客可用的公开导购，不创建服务端会话，也不会访问个人订单。 */
export function askPublicAssistant(query: string, product?: ProductContextRef): Promise<PublicAssistantResponse> {
  return api<PublicAssistantResponse>("/api/v1/products/assistant", {
    method: "POST",
    body: JSON.stringify({
      query,
      product_category: product?.category,
      product_id: product?.productId,
    }),
  });
}

/** 创建待支付订单后返回支付宝沙箱的浏览器跳转地址。 */
export function createCheckout(
  token: string,
  category: "laptops" | "phones" | "components",
  productId: string,
  returnOrigin: string,
): Promise<CheckoutSession> {
  return api<CheckoutSession>("/api/v1/checkout/orders", {
    method: "POST",
    body: JSON.stringify({ category, product_id: productId, quantity: 1, return_origin: returnOrigin }),
  }, token);
}

/** 将商品加入当前客户的持久化购物车；价格和库存会在结算时再次核验。 */
export function addCartItem(
  token: string,
  category: "laptops" | "phones" | "components",
  productId: string,
  quantity = 1,
): Promise<CartItem> {
  return api<CartItem>("/api/v1/cart/items", {
    method: "POST",
    body: JSON.stringify({ category, product_id: productId, quantity }),
  }, token);
}

/** 读取当前客户的购物车。 */
export function getCart(token: string): Promise<Cart> {
  return api<Cart>("/api/v1/cart", {}, token);
}

/** 覆盖购物车单项数量。 */
export function updateCartItem(token: string, itemId: number, quantity: number): Promise<CartItem> {
  return api<CartItem>(`/api/v1/cart/items/${itemId}`, {
    method: "PATCH",
    body: JSON.stringify({ quantity }),
  }, token);
}

/** 删除购物车单项，并返回最新购物车。 */
export function deleteCartItem(token: string, itemId: number): Promise<Cart> {
  return api<Cart>(`/api/v1/cart/items/${itemId}`, { method: "DELETE" }, token);
}

/** 以购物车当前内容创建或复用一笔待支付订单。 */
export function checkoutCart(token: string, returnOrigin: string): Promise<CheckoutSession> {
  return api<CheckoutSession>("/api/v1/cart/checkout", {
    method: "POST",
    body: JSON.stringify({ return_origin: returnOrigin }),
  }, token);
}

/** 为同一笔待支付订单重新打开支付宝收银台，不新建订单。 */
export function resumeCheckout(token: string, orderNo: string, returnOrigin: string): Promise<CheckoutSession> {
  return api<CheckoutSession>(`/api/v1/checkout/orders/${encodeURIComponent(orderNo)}/resume-payment`, {
    method: "POST",
    body: JSON.stringify({ return_origin: returnOrigin }),
  }, token);
}

/** 关闭尚未付款的沙箱订单；已付款或状态变化的订单会被拒绝。 */
export function cancelCheckout(token: string, orderNo: string): Promise<{ order_no: string; cancelled: boolean }> {
  return api(`/api/v1/checkout/orders/${encodeURIComponent(orderNo)}/cancel`, { method: "POST" }, token);
}

export async function listMyCheckoutOrders(token: string): Promise<CheckoutOrder[]> {
  const response = await api<{ orders: CheckoutOrder[] }>("/api/v1/checkout/orders/my", {}, token);
  return response.orders;
}

/** 以支付宝网关的交易查询结果刷新一笔待支付订单。 */
export function refreshCheckoutPayment(token: string, orderNo: string): Promise<CheckoutOrder> {
  return api<CheckoutOrder>(`/api/v1/checkout/orders/${encodeURIComponent(orderNo)}/refresh-payment`, {
    method: "POST",
  }, token);
}

/** 创建待确认退款；付款金额由服务端从已支付交易读取，浏览器不能传金额。 */
export function requestCheckoutRefund(
  token: string,
  orderNo: string,
  reason: string,
  idempotencyKey: string,
): Promise<CheckoutRefund> {
  return api<CheckoutRefund>(`/api/v1/checkout/orders/${encodeURIComponent(orderNo)}/refunds`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ reason }),
  }, token);
}

/** 客户明确确认后才会提交一次支付宝沙箱退款。 */
export function confirmCheckoutRefund(
  token: string,
  refundId: string,
  idempotencyKey: string,
): Promise<CheckoutRefund> {
  return api<CheckoutRefund>(`/api/v1/checkout/refunds/${encodeURIComponent(refundId)}/confirm`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
  }, token);
}

/** 查询已提交退款的支付宝状态；不会再次请求退款。 */
export function refreshCheckoutRefund(token: string, refundId: string): Promise<CheckoutRefund> {
  return api<CheckoutRefund>(`/api/v1/checkout/refunds/${encodeURIComponent(refundId)}/refresh`, {
    method: "POST",
  }, token);
}

/** 运营查看应用自有订单的发货队列。 */
export async function listOperatorFulfillments(token: string): Promise<Fulfillment[]> {
  const response = await api<{ fulfillments: Fulfillment[] }>("/api/v1/fulfillments", {}, token);
  return response.fulfillments;
}

/** 运营登记一次真实或演示物流发货事件。 */
export function shipFulfillment(
  token: string,
  orderNo: string,
  carrier: string,
  trackingNumber: string,
): Promise<Fulfillment> {
  return api<Fulfillment>(`/api/v1/fulfillments/${encodeURIComponent(orderNo)}/ship`, {
    method: "POST",
    body: JSON.stringify({ carrier, tracking_number: trackingNumber }),
  }, token);
}

export async function listMyOrders(token: string): Promise<CustomerOrder[]> {
  const response = await api<{ orders: CustomerOrder[] }>("/api/v1/orders/my", {}, token);
  return response.orders;
}

export async function listFinanceRefunds(token: string): Promise<FinanceRefund[]> {
  const response = await api<{ refunds: FinanceRefund[] }>("/api/v1/checkout/finance/refunds", {}, token);
  return response.refunds;
}

export async function listFinanceAnomalies(token: string): Promise<FinanceAnomaly[]> {
  const response = await api<{ anomalies: FinanceAnomaly[] }>("/api/v1/checkout/finance/anomalies", {}, token);
  return response.anomalies;
}

export function approveFinanceRefund(
  token: string,
  refundId: string,
  decisionNote: string,
  idempotencyKey: string,
): Promise<CheckoutRefund> {
  return api<CheckoutRefund>(`/api/v1/checkout/finance/refunds/${encodeURIComponent(refundId)}/approve`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ decision_note: decisionNote }),
  }, token);
}

export function rejectFinanceRefund(
  token: string,
  refundId: string,
  decisionNote: string,
  idempotencyKey: string,
): Promise<CheckoutRefund> {
  return api<CheckoutRefund>(`/api/v1/checkout/finance/refunds/${encodeURIComponent(refundId)}/reject`, {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({ decision_note: decisionNote }),
  }, token);
}

export async function listSessions(token: string): Promise<SessionItem[]> {
  const response = await api<{ sessions: SessionItem[] }>("/api/v1/sessions", {}, token);
  return response.sessions;
}

export function getSession(token: string, sessionId: string): Promise<SessionDetail> {
  return api(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, {}, token);
}

export function deleteSession(token: string, sessionId: string): Promise<{ ok: boolean }> {
  return api(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" }, token);
}

export async function listTickets(token: string): Promise<Ticket[]> {
  const response = await api<{ tickets: Ticket[] }>("/api/v1/tickets", {}, token);
  return response.tickets;
}

export function getTicket(token: string, ticketId: string): Promise<Ticket> {
  return api(`/api/v1/tickets/${encodeURIComponent(ticketId)}`, {}, token);
}

export function getTicketEscalation(token: string, ticketId: string): Promise<TicketEscalation> {
  return api(`/api/v1/tickets/${encodeURIComponent(ticketId)}/escalation`, {}, token);
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

export function createCustomerTicket(token: string, issue: string): Promise<Ticket> {
  return api("/api/v1/tickets", {
    method: "POST",
    body: JSON.stringify({ issue }),
  }, token);
}
