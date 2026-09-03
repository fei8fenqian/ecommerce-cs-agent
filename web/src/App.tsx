import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  AuthState,
  Cart,
  ChatInteraction,
  CheckoutSession,
  CheckoutOrder,
  Fulfillment,
  FinanceAnomaly,
  FinanceAnomalySummary,
  FinanceRefund,
  Product,
  ProductContextRef,
  ProductCatalogPage,
  ProductDetail,
  PaymentProvider,
  CustomerOrder,
  CustomerAction,
  CustomerPresentation,
  SessionItem,
  SupportReplyDraft,
  Ticket,
  TicketEscalation,
  TicketMessage,
  addCartItem,
  approveFinanceRefund,
  askPublicAssistant,
  cancelCheckout,
  claimTicket,
  closeTicket,
  checkoutCart,
  confirmCheckoutRefund,
  createCheckout,
  deleteCartItem,
  deleteSession,
  getCart,
  getSession,
  getProductDetail,
  getTicket,
  getTicketEscalation,
  listSessions,
  listProducts,
  listMyOrders,
  listMyCheckoutOrders,
  listFinanceRefunds,
  listFinanceAnomalies,
  summarizeFinanceAnomalies,
  listOperatorFulfillments,
  listTicketMessages,
  listTickets,
  requestReplyDraft,
  requestCheckoutRefund,
  refreshCheckoutPayment,
  refreshFinanceRefund,
  refreshCheckoutRefund,
  rejectFinanceRefund,
  resumeCheckout,
  register,
  sendCustomerTicketMessage,
  sendAgentTicketMessage,
  shipFulfillment,
  signIn,
  streamChat,
  updateCartItem,
} from "./api";
import { PresentationRenderer } from "./chat/PresentationRenderer";
import { latestInteractiveChoiceMessageId } from "./chat/presentationState";

// 登录态只属于当前标签页；避免一个标签页退出时清空所有打开中的工作台。
const STORAGE_KEY = "ecommerce-agent.tab-auth";
const AUTH_TAB_ID_KEY = "ecommerce-agent.auth-tab-id";
const AUTH_HANDOFF_CHANNEL = "ecommerce-agent.auth-handoff";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  sequenceNo?: number;
  presentation?: CustomerPresentation | null;
};

type AuthHandoffRequest = {
  type: "request";
  sourceTabId: string;
  requestId: string;
};

type AuthHandoffResponse = {
  type: "response";
  sourceTabId: string;
  requestId: string;
  auth: AuthState;
};

type TypingQueue = {
  assistantId: string;
  pendingText: string;
  receivedText: string;
  renderedText: string;
  doneAnswer?: string;
  presentation?: CustomerPresentation | null;
  frameId: number | null;
  settled: boolean;
  resolve: () => void;
  drain: Promise<void>;
};

type CustomerPage = "service" | "catalog" | "product" | "orders" | "cart" | "tickets";

function toChatMessages(
  sessionId: string,
  messages: Array<{
    role: string;
    content?: string;
    sequence_no?: number;
    presentation?: CustomerPresentation | null;
  }>,
): ChatMessage[] {
  // 将会话 API 的持久化消息转换为聊天页面需要的显示数据。
  return messages
    .filter((message) => (message.role === "user" || message.role === "assistant") && (Boolean(message.content) || Boolean(message.presentation)))
    .map((message, index) => ({
      id: `history-${sessionId}-${message.sequence_no ?? index}`,
      role: message.role as "user" | "assistant",
      content: message.content ?? "",
      sequenceNo: message.sequence_no,
      presentation: message.presentation,
    }));
}

function loadAuth(): AuthState | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    const auth = raw ? (JSON.parse(raw) as AuthState) : null;
    if (!auth?.token || !auth.user || isJwtExpired(auth.token)) {
      window.sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return auth;
  } catch {
    window.sessionStorage.removeItem(STORAGE_KEY);
    return null;
  }
}

/** 仅用于避免把已过期的本地缓存渲染成“已登录”，不把前端解析当成认证依据。 */
function isJwtExpired(token: string): boolean {
  try {
    const encodedPayload = token.split(".")[1];
    if (!encodedPayload) return true;
    const base64 = encodedPayload.replace(/-/g, "+").replace(/_/g, "/");
    const paddedBase64 = base64.padEnd(Math.ceil(base64.length / 4) * 4, "=");
    const payload = JSON.parse(atob(paddedBase64)) as { exp?: unknown };
    return typeof payload.exp !== "number" || payload.exp * 1000 <= Date.now();
  } catch {
    return true;
  }
}

function saveAuth(auth: AuthState | null): void {
  if (auth) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(auth));
  else window.sessionStorage.removeItem(STORAGE_KEY);
}

function randomBrowserId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/** 同源新标签只传递一次性 tab id；JWT 永远不进入 URL。 */
function currentAuthTabId(): string {
  try {
    const existing = window.sessionStorage.getItem(AUTH_TAB_ID_KEY);
    if (existing) return existing;
    const created = randomBrowserId();
    window.sessionStorage.setItem(AUTH_TAB_ID_KEY, created);
    return created;
  } catch {
    return randomBrowserId();
  }
}

function removeAuthSourceFromUrl(): void {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("auth_source")) return;
  url.searchParams.delete("auth_source");
  window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
}

/** 只给同源站内链接添加认证接力提示；外链保持原样。 */
function decorateInternalHref(href?: string): string | undefined {
  if (!href) return href;
  try {
    const url = new URL(href, window.location.href);
    if (url.origin !== window.location.origin) return href;
    url.searchParams.set("auth_source", currentAuthTabId());
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return href;
  }
}

function isAuthHandoffResponse(value: unknown): value is AuthHandoffResponse {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AuthHandoffResponse>;
  const auth = candidate.auth;
  return candidate.type === "response"
    && typeof candidate.sourceTabId === "string"
    && typeof candidate.requestId === "string"
    && Boolean(auth)
    && typeof auth?.token === "string"
    && auth.token.length > 0
    && Boolean(auth.user)
    && typeof auth.user.id === "number"
    && typeof auth.user.username === "string"
    && ["customer", "agent", "operator", "admin", "finance"].includes(auth.user.role);
}

/** 认证后的 Markdown 站内链接支持右键新标签，并通过同源 tab handoff 恢复登录。 */
function CustomerMarkdown({ children }: { children: string }) {
  return <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    components={{
      a: ({ node: _node, href, children: linkChildren, ...props }) => (
        <a {...props} href={decorateInternalHref(href)}>{linkChildren}</a>
      ),
    }}
  >{children}</ReactMarkdown>;
}

function splitGraphemes(value: string): string[] {
  type SegmenterLike = new (locales?: string | string[], options?: { granularity: "grapheme" }) => {
    segment: (input: string) => Iterable<{ segment: string }>;
  };
  const segmenter = (Intl as unknown as { Segmenter?: SegmenterLike }).Segmenter;
  if (!segmenter) return Array.from(value);
  return Array.from(new segmenter("zh", { granularity: "grapheme" }).segment(value), (item) => item.segment);
}

function formatDate(value: string | number): string {
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString("zh-CN");
}

function sessionUrl(sessionId: string | undefined): string {
  const url = new URL(window.location.href);
  if (sessionId) url.searchParams.set("session", sessionId);
  else url.searchParams.delete("session");
  return `${url.pathname}${url.search}${url.hash}`;
}

function replaceSessionUrl(sessionId: string | undefined): void {
  window.history.replaceState(null, "", sessionUrl(sessionId));
}

/** MVP 入口：按登录身份加载客户服务台或内部客服工作台。 */
export function App() {
  const [auth, setAuth] = useState<AuthState | null>(loadAuth);
  const authSource = new URLSearchParams(window.location.search).get("auth_source");
  const [authHandoffPending, setAuthHandoffPending] = useState(() => Boolean(authSource && !window.sessionStorage.getItem(STORAGE_KEY)));

  useEffect(() => {
    const tabId = currentAuthTabId();
    if (!("BroadcastChannel" in window)) return;
    const channel = new BroadcastChannel(AUTH_HANDOFF_CHANNEL);
    const respondToHandoff = (event: MessageEvent<AuthHandoffRequest>): void => {
      const message = event.data;
      if (!message || message.type !== "request" || message.sourceTabId !== tabId || typeof message.requestId !== "string") return;
      if (!auth || isJwtExpired(auth.token)) return;
      const response: AuthHandoffResponse = { type: "response", sourceTabId: tabId, requestId: message.requestId, auth };
      channel.postMessage(response);
    };
    channel.addEventListener("message", respondToHandoff);
    return () => {
      channel.removeEventListener("message", respondToHandoff);
      channel.close();
    };
  }, [auth]);

  useEffect(() => {
    if (!authSource) return;
    if (auth || !authHandoffPending) {
      removeAuthSourceFromUrl();
      return;
    }
    if (!("BroadcastChannel" in window)) {
      const timer = globalThis.setTimeout(() => {
        removeAuthSourceFromUrl();
        setAuthHandoffPending(false);
      }, 1200);
      return () => globalThis.clearTimeout(timer);
    }

    const requestId = randomBrowserId();
    const channel = new BroadcastChannel(AUTH_HANDOFF_CHANNEL);
    let completed = false;
    const finish = (nextAuth?: AuthState): void => {
      if (completed) return;
      completed = true;
      if (nextAuth && !isJwtExpired(nextAuth.token)) {
        saveAuth(nextAuth);
        setAuth(nextAuth);
      }
      removeAuthSourceFromUrl();
      setAuthHandoffPending(false);
      channel.close();
    };
    const receiveHandoff = (event: MessageEvent<unknown>): void => {
      const message = event.data;
      if (!isAuthHandoffResponse(message) || message.sourceTabId !== authSource || message.requestId !== requestId) return;
      finish(message.auth);
    };
    channel.addEventListener("message", receiveHandoff);
    channel.postMessage({ type: "request", sourceTabId: authSource, requestId } satisfies AuthHandoffRequest);
    const timeout = window.setTimeout(() => finish(), 1500);
    return () => {
      window.clearTimeout(timeout);
      channel.removeEventListener("message", receiveHandoff);
      if (!completed) channel.close();
    };
  }, [auth, authHandoffPending, authSource]);

  const returnToPublicCatalog = (): void => {
    window.history.replaceState(null, "", window.location.pathname);
  };

  useEffect(() => {
    const discardExpiredAuth = (): void => {
      saveAuth(null);
      returnToPublicCatalog();
      setAuth(null);
    };
    window.addEventListener("ecommerce-agent.auth-invalid", discardExpiredAuth);
    return () => window.removeEventListener("ecommerce-agent.auth-invalid", discardExpiredAuth);
  }, []);

  if (authHandoffPending) {
    return <main className="login-page"><section className="login-card"><p className="eyebrow">GEEX DIGITAL</p><h1>正在恢复当前标签页</h1><p className="muted">正在通过原标签页确认登录状态…</p></section></main>;
  }

  const onSignedIn = (nextAuth: AuthState): void => {
    saveAuth(nextAuth);
    setAuth(nextAuth);
  };

  const onSignOut = async (): Promise<void> => {
    // 此处是“退出当前标签页”，不撤销服务端 token，避免同一账号的其他工作标签被登出。
    // 需要全端注销时应使用单独的“退出所有设备”命令，而不是复用本地退出按钮。
    saveAuth(null);
    returnToPublicCatalog();
    setAuth(null);
  };

  if (!auth) return <PublicStorefront onSignedIn={onSignedIn} />;
  if (auth.user.role === "customer") return <CustomerWorkspace auth={auth} onSignOut={onSignOut} />;
  if (auth.user.role === "agent") return <AgentWorkspace auth={auth} onSignOut={onSignOut} />;
  if (auth.user.role === "operator") return <OperatorWorkspace auth={auth} onSignOut={onSignOut} />;
  if (auth.user.role === "finance") return <FinanceWorkspace auth={auth} onSignOut={onSignOut} />;
  return <UnavailableWorkspace auth={auth} onSignOut={onSignOut} />;
}

/** 未登录用户先看到公开商城；交易和个人数据入口再要求认证。 */
function PublicStorefront({ onSignedIn }: { onSignedIn: (auth: AuthState) => void }) {
  const parameters = new URLSearchParams(window.location.search);
  const initialCategory = parameters.get("category");
  const initialComponentCategory = parameters.get("component_category") ?? "";
  const initialProduct = parameters.get("product");
  const [page, setPage] = useState<"catalog" | "product" | "assistant" | "login">(
    parameters.get("page") === "product" && initialCategory && initialProduct ? "product" : "catalog",
  );
  const [detailTarget, setDetailTarget] = useState<{ category: "laptops" | "phones" | "components"; productId: string } | null>(
    initialCategory === "laptops" || initialCategory === "phones" || initialCategory === "components"
      ? initialProduct ? { category: initialCategory, productId: initialProduct } : null
      : null,
  );
  const [assistantQuestion, setAssistantQuestion] = useState("");
  const [assistantProduct, setAssistantProduct] = useState<ProductContextRef | undefined>();
  const openLogin = (nextPage: "catalog" | "product" | "cart" | "orders" | "tickets"): void => {
    const nextParameters = new URLSearchParams({ page: "login", next: nextPage });
    if (nextPage === "product" && detailTarget) {
      nextParameters.set("category", detailTarget.category);
      nextParameters.set("product", detailTarget.productId);
    }
    window.history.pushState(null, "", `${window.location.pathname}?${nextParameters}`);
    setPage("login");
  };
  const finishSignIn = (nextAuth: AuthState): void => {
    const nextPage = new URLSearchParams(window.location.search).get("next");
    const destination = nextPage === "product" || nextPage === "cart" || nextPage === "orders" || nextPage === "tickets" ? nextPage : "catalog";
    const destinationParameters = new URLSearchParams({ page: destination });
    const loginParameters = new URLSearchParams(window.location.search);
    if (destination === "product" && loginParameters.get("category") && loginParameters.get("product")) {
      destinationParameters.set("category", loginParameters.get("category")!);
      destinationParameters.set("product", loginParameters.get("product")!);
    }
    window.history.replaceState(null, "", `${window.location.pathname}?${destinationParameters}`);
    onSignedIn(nextAuth);
  };
  const openProduct = (category: "laptops" | "phones" | "components", productId: string): void => {
    window.history.pushState(null, "", `${window.location.pathname}?page=product&category=${category}&product=${encodeURIComponent(productId)}`);
    setDetailTarget({ category, productId });
    setPage("product");
  };
  const openAssistant = (question = "", product?: ProductContextRef): void => {
    setAssistantQuestion(question);
    setAssistantProduct(product);
    setPage("assistant");
  };
  if (page === "login") return <LoginPage onSignedIn={finishSignIn} onBack={() => { window.history.replaceState(null, "", window.location.pathname); setPage("catalog"); }} />;

  return <main className="storefront">
    <header className="store-topbar">
      <button className="store-brand" onClick={() => setPage("catalog")}><span>G</span><strong>Geex Digital</strong></button>
      <nav aria-label="商城导航">{page !== "catalog" && <button onClick={() => setPage("catalog")}>商品目录</button>}<button onClick={() => openAssistant()}>智能客服</button><button onClick={() => openLogin("cart")}>购物车</button><button onClick={() => openLogin("orders")}>我的订单</button><button onClick={() => openLogin("tickets")}>售后服务</button><i /> <span>你好，</span><button className="store-login" onClick={() => openLogin("catalog")}>请登录</button><button onClick={() => openLogin("catalog")}>免费注册</button></nav>
    </header>
    <section className="store-content">
      {page === "catalog" ? <ProductCatalog onView={openProduct} initialComponentCategory={initialComponentCategory} /> : page === "product" && detailTarget ? <ProductDetailPage category={detailTarget.category} productId={detailTarget.productId} onBack={() => setPage("catalog")} onAsk={(product, question) => openAssistant(question || `我想了解 ${product.product_name}`, { category: detailTarget.category, productId: product.id })} onRequireLogin={() => openLogin("product")} /> : <PublicAssistantPage initialQuestion={assistantQuestion} initialProduct={assistantProduct} onBrowseProducts={() => setPage("catalog")} />}
    </section>
  </main>;
}

/** 已登录客户沿用公开商城外观，仅在交易与个人入口中使用认证身份。 */
function CustomerStorefront({
  auth,
  page,
  detailTarget,
  initialQuery,
  initialCategory,
  initialComponentCategory,
  onNavigate,
  onViewProduct,
  onBackToCatalog,
  onAskProduct,
  onSignOut,
}: {
  auth: AuthState;
  page: Exclude<CustomerPage, "service">;
  detailTarget: { category: "laptops" | "phones" | "components"; productId: string } | null;
  initialQuery: string;
  initialCategory: "laptops" | "phones" | "components";
  initialComponentCategory: string;
  onNavigate: (page: CustomerPage) => void;
  onViewProduct: (category: "laptops" | "phones" | "components", productId: string) => void;
  onBackToCatalog: () => void;
  onAskProduct: (product: Product, question?: string) => void;
  onSignOut: () => Promise<void>;
}) {
  const navigate = (nextPage: Exclude<CustomerPage, "product">): void => onNavigate(nextPage);
  return <main className="storefront authenticated-storefront">
    <header className="store-topbar">
      <button className="store-brand" onClick={() => navigate("catalog")}><span>G</span><strong>Geex Digital</strong></button>
      <nav aria-label="商城导航">
        {page !== "catalog" && <button className={page === "product" ? "store-nav-active" : ""} onClick={() => navigate("catalog")}>商品目录</button>}
        <button onClick={() => navigate("service")}>智能客服</button>
        <button className={page === "cart" ? "store-nav-active" : ""} onClick={() => navigate("cart")}>购物车</button>
        <button className={page === "orders" ? "store-nav-active" : ""} onClick={() => navigate("orders")}>我的订单</button>
        <button className={page === "tickets" ? "store-nav-active" : ""} onClick={() => navigate("tickets")}>我的售后</button>
        <i /> <span>你好，{auth.user.username}</span><button className="store-login" onClick={() => void onSignOut()}>退出</button>
      </nav>
    </header>
    <section className="store-content customer-store-content">
      {page === "catalog" ? <ProductCatalog auth={auth} initialQuery={initialQuery} initialCategory={initialCategory} initialComponentCategory={initialComponentCategory} onView={onViewProduct} />
        : page === "product" && detailTarget ? <ProductDetailPage auth={auth} category={detailTarget.category} productId={detailTarget.productId} onBack={onBackToCatalog} onAsk={onAskProduct} />
          : page === "cart" ? <CartPage auth={auth} />
              : page === "orders" ? <OrderList auth={auth} focusOrderId={new URLSearchParams(window.location.search).get("order_id") ?? new URLSearchParams(window.location.search).get("refund_order") ?? undefined} />
              : <CustomerTicketCenter auth={auth} focusTicketId={new URLSearchParams(window.location.search).get("ticket_id") ?? undefined} />}
    </section>
  </main>;
}

function LoginPage({ onSignedIn, onBack }: { onSignedIn: (auth: AuthState) => void; onBack?: () => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    setLoading(true);
    setError("");
    try {
      onSignedIn(mode === "login" ? await signIn(username.trim(), password) : await register(username.trim(), password));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败，请稍后重试");
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="login-page">
      <section className="login-card">
        {onBack && <button className="login-back" onClick={onBack}>← 返回商品页</button>}
        <p className="eyebrow">GEEX DIGITAL · AI SERVICE DESK</p>
        <h1>{mode === "login" ? "登录你的账号" : "创建客户账号"}</h1>
        <p className="muted">{mode === "login" ? "登录后可管理购物车、订单和售后服务。" : "注册后即可管理购物车、订单和售后服务。"}</p>
        <form onSubmit={submit} className="form-stack">
          <label>账号<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="3–64 位：字母、数字、_ 或 -" required /></label>
          <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={8} maxLength={72} required /></label>
          {error && <p className="error">{error}</p>}
          <button disabled={loading}>{loading ? "处理中…" : mode === "login" ? "进入服务台" : "注册并进入服务台"}</button>
        </form>
        <p className="hint">{mode === "login" ? "还没有客户账号？" : "已有账号？"} <button className="text-button" onClick={() => { setError(""); setMode(mode === "login" ? "register" : "login"); }}>{mode === "login" ? "注册" : "登录"}</button></p>
        <p className="hint">公开注册只会创建客户账号；页面不会保存密码。</p>
      </section>
    </main>
  );
}

function Shell({ title, subtitle, auth, onSignOut, children }: { title: string; subtitle: string; auth: AuthState; onSignOut: () => Promise<void>; children: React.ReactNode }) {
  return (
    <main className="workspace">
      <header className="topbar">
        <div><p className="eyebrow">GEEX DIGITAL</p><h1>{title}</h1><p className="muted">{subtitle}</p></div>
        <div className="account"><span>{auth.user.username}</span><span className="role-badge">{auth.user.role}</span><button className="secondary" onClick={() => void onSignOut()}>退出</button></div>
      </header>
      {children}
    </main>
  );
}

function CustomerWorkspace({ auth, onSignOut }: { auth: AuthState; onSignOut: () => Promise<void> }) {
  const pageFromUrl = new URLSearchParams(window.location.search).get("page");
  const catalogQueryFromUrl = new URLSearchParams(window.location.search).get("q") ?? "";
  const catalogComponentCategoryFromUrl = new URLSearchParams(window.location.search).get("component_category") ?? "";
  const catalogCategoryFromUrl = (() => {
    const category = new URLSearchParams(window.location.search).get("category");
    return category === "laptops" || category === "phones" || category === "components" ? category : "laptops";
  })();
  const detailCategoryFromUrl = new URLSearchParams(window.location.search).get("category");
  const detailProductIdFromUrl = new URLSearchParams(window.location.search).get("product");
  const [page, setPage] = useState<CustomerPage>(() =>
    pageFromUrl === "product" && detailCategoryFromUrl && detailProductIdFromUrl
      ? "product"
      : pageFromUrl === "catalog" || pageFromUrl === "orders" || pageFromUrl === "cart" || pageFromUrl === "tickets" ? pageFromUrl : "service",
  );
  const [detailTarget, setDetailTarget] = useState<{ category: "laptops" | "phones" | "components"; productId: string } | null>(
    detailCategoryFromUrl === "laptops" || detailCategoryFromUrl === "phones" || detailCategoryFromUrl === "components"
      ? detailProductIdFromUrl ? { category: detailCategoryFromUrl, productId: detailProductIdFromUrl } : null
      : null,
  );
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [query, setQuery] = useState("");
  const [selectedProductForChat, setSelectedProductForChat] = useState<ProductContextRef | undefined>();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sessionsExpanded, setSessionsExpanded] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [streamStatus, setStreamStatus] = useState("");
  const chatHistoryRef = useRef<HTMLDivElement>(null);
  const smoothScrollOnNextMessageRef = useRef(false);
  const scrollToOpenedSessionRef = useRef(false);
  const selectedSessionIdRef = useRef<string | undefined>(undefined);
  const sessionLoadRequestRef = useRef(0);
  const sessionCacheRef = useRef(new Map<string, ChatMessage[]>());
  const pendingUrlSessionRef = useRef(new URLSearchParams(window.location.search).get("session") ?? undefined);
  const typingQueueRef = useRef<TypingQueue | null>(null);

  const settleTypingQueue = (queue: TypingQueue): void => {
    if (queue.settled) return;
    queue.settled = true;
    queue.resolve();
    if (typingQueueRef.current === queue) typingQueueRef.current = null;
  };

  const cancelTyping = (): void => {
    const queue = typingQueueRef.current;
    if (!queue) return;
    if (queue.frameId !== null) window.cancelAnimationFrame(queue.frameId);
    queue.frameId = null;
    settleTypingQueue(queue);
  };

  const pumpTyping = (queue: TypingQueue): void => {
    if (typingQueueRef.current !== queue || queue.settled) return;
    if (queue.pendingText) {
      const units = splitGraphemes(queue.pendingText);
      // Keep short answers natural while preventing a long answer from
      // blocking the UI for an excessive amount of time.
      const count = units.length > 240 ? 8 : units.length > 80 ? 3 : 1;
      const rendered = units.slice(0, count).join("");
      queue.pendingText = queue.pendingText.slice(rendered.length);
      queue.renderedText += rendered;
      setChatMessages((items) => items.map((message) => message.id === queue.assistantId
        ? { ...message, content: queue.renderedText }
        : message));
      queue.frameId = window.requestAnimationFrame(() => pumpTyping(queue));
      return;
    }
    if (queue.doneAnswer !== undefined) {
      const finalAnswer = queue.doneAnswer;
      setChatMessages((items) => items.map((message) => message.id === queue.assistantId
        ? { ...message, content: finalAnswer, presentation: queue.presentation ?? message.presentation }
        : message));
      queue.frameId = null;
      settleTypingQueue(queue);
    } else {
      queue.frameId = null;
    }
  };

  const ensureTypingQueue = (assistantId: string): TypingQueue => {
    const existing = typingQueueRef.current;
    if (existing && existing.assistantId === assistantId) return existing;
    cancelTyping();
    let resolveDrain: () => void = () => undefined;
    const drain = new Promise<void>((resolve) => { resolveDrain = resolve; });
    const created: TypingQueue = {
      assistantId,
      pendingText: "",
      receivedText: "",
      renderedText: "",
      frameId: null,
      settled: false,
      resolve: resolveDrain,
      drain,
    };
    typingQueueRef.current = created;
    return created;
  };

  const queueAssistantToken = (assistantId: string, content: string): void => {
    if (!content) return;
    const queue = ensureTypingQueue(assistantId);
    queue.receivedText += content;
    queue.pendingText += content;
    if (queue.frameId === null) queue.frameId = window.requestAnimationFrame(() => pumpTyping(queue));
  };

  const finishAssistantTyping = (assistantId: string, answer: string, presentation?: CustomerPresentation | null): void => {
    const queue = ensureTypingQueue(assistantId);
    if (!queue.receivedText) queue.pendingText = answer;
    else if (answer.startsWith(queue.receivedText)) queue.pendingText += answer.slice(queue.receivedText.length);
    else {
      queue.pendingText = answer;
      queue.renderedText = "";
    }
    queue.doneAnswer = answer;
    queue.presentation = presentation;
    if (queue.frameId === null) queue.frameId = window.requestAnimationFrame(() => pumpTyping(queue));
  };

  const waitForTypingDrain = async (assistantId: string): Promise<void> => {
    const queue = typingQueueRef.current;
    if (queue && queue.assistantId === assistantId) await queue.drain;
  };

  useEffect(() => () => cancelTyping(), []);

  const navigatePage = (nextPage: CustomerPage): void => {
    cancelTyping();
    const parameters = new URLSearchParams();
    parameters.set("page", nextPage);
    const currentSession = new URLSearchParams(window.location.search).get("session");
    if (currentSession) parameters.set("session", currentSession);
    const nextUrl = `${window.location.pathname}?${parameters}`;
    if (`${window.location.pathname}${window.location.search}` !== nextUrl) {
      window.history.pushState(null, "", nextUrl);
    }
    setPage(nextPage);
  };

  useEffect(() => { selectedSessionIdRef.current = sessionId; }, [sessionId]);

  useEffect(() => { void listSessions(auth.token).then(setSessions).catch(() => undefined); }, [auth.token]);

  useEffect(() => {
    if (chatMessages.length === 0 || (!busy && !scrollToOpenedSessionRef.current)) return;
    const frame = window.requestAnimationFrame(() => {
      chatHistoryRef.current?.scrollTo({
        top: chatHistoryRef.current.scrollHeight,
        behavior: smoothScrollOnNextMessageRef.current ? "smooth" : "auto",
      });
      smoothScrollOnNextMessageRef.current = false;
      scrollToOpenedSessionRef.current = false;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [busy, chatMessages]);

  const startNewChat = (): void => {
    cancelTyping();
    sessionLoadRequestRef.current += 1;
    selectedSessionIdRef.current = undefined;
    replaceSessionUrl(undefined);
    setPage("service");
    setSessionId(undefined);
    setChatMessages([]);
    setQuery("");
    setSelectedProductForChat(undefined);
    setError("");
  };

  const openSession = async (nextSessionId: string): Promise<void> => {
    cancelTyping();
    setError("");
    const requestNumber = ++sessionLoadRequestRef.current;
    selectedSessionIdRef.current = nextSessionId;
    replaceSessionUrl(nextSessionId);
    setSessionId(nextSessionId);
    setPage("service");
    const cachedMessages = sessionCacheRef.current.get(nextSessionId);
    setChatMessages(cachedMessages ?? []);
    scrollToOpenedSessionRef.current = true;
    try {
      const session = await getSession(auth.token, nextSessionId);
      if (requestNumber !== sessionLoadRequestRef.current) return;
      const restoredMessages = toChatMessages(session.session_id, session.messages);
      sessionCacheRef.current.set(session.session_id, restoredMessages);
      setSessionId(session.session_id);
      setChatMessages(restoredMessages);
    } catch (reason) {
      if (requestNumber !== sessionLoadRequestRef.current) return;
      setError(reason instanceof Error ? reason.message : "无法读取历史会话");
    }
  };

  useEffect(() => {
    const urlSessionId = pendingUrlSessionRef.current;
    if (!urlSessionId) return;
    pendingUrlSessionRef.current = undefined;
    void openSession(urlSessionId);
  }, [auth.token]);

  useEffect(() => {
    const restoreBrowserRoute = (): void => {
      const parameters = new URLSearchParams(window.location.search);
      const routePage = parameters.get("page");
      const routeCategory = parameters.get("category");
      const routeProduct = parameters.get("product");
      const nextPage: CustomerPage =
        routePage === "product" && (routeCategory === "laptops" || routeCategory === "phones" || routeCategory === "components") && routeProduct
          ? "product"
          : routePage === "catalog" || routePage === "orders" || routePage === "cart" || routePage === "tickets"
            ? routePage
            : "service";
      if (nextPage === "product" && routeProduct && (routeCategory === "laptops" || routeCategory === "phones" || routeCategory === "components")) {
        setDetailTarget({ category: routeCategory, productId: routeProduct });
      } else if (nextPage !== "product") {
        setDetailTarget(null);
      }
      setPage(nextPage);

      const routeSessionId = parameters.get("session");
      if (nextPage === "service" && routeSessionId && routeSessionId !== selectedSessionIdRef.current) {
        void openSession(routeSessionId);
      } else if (nextPage === "service" && !routeSessionId && selectedSessionIdRef.current) {
        sessionLoadRequestRef.current += 1;
        selectedSessionIdRef.current = undefined;
        setSessionId(undefined);
        setChatMessages([]);
      }
    };
    window.addEventListener("popstate", restoreBrowserRoute);
    return () => window.removeEventListener("popstate", restoreBrowserRoute);
  }, [auth.token]);

  const submitChatMessage = async (
    submittedText = query,
    replaceFromSequence?: number,
    interaction?: ChatInteraction,
  ): Promise<void> => {
    const text = submittedText.trim();
    if ((!text && !interaction) || busy) return;
    const productContext = replaceFromSequence === undefined && !interaction ? selectedProductForChat : undefined;
    const displayedUserMessage = interaction ? `已选择订单：${interaction.subject_id}` : text;
    // 用户发送后将当前会话平滑带到新消息；后续 SSE token 继续贴住最新回复。
    smoothScrollOnNextMessageRef.current = true;
    setBusy(true); setError(""); setStreamStatus("正在思考…"); setQuery(""); setSelectedProductForChat(undefined);
    const assistantId = `assistant-${Date.now()}`;
    if (replaceFromSequence !== undefined) {
      setChatMessages((items) => items.filter((message) => (message.sequenceNo ?? -1) < replaceFromSequence));
    }
    setChatMessages((items) => [...items, { id: `user-${Date.now()}`, role: "user", content: displayedUserMessage }, { id: assistantId, role: "assistant", content: "" }]);
    const sourceSessionId = sessionId;
    let activeSessionId = sourceSessionId;
    try {
      await streamChat(auth.token, text, sessionId, (event) => {
        if (event.event === "start") {
          if (event.session_id) {
            activeSessionId = event.session_id;
            if (selectedSessionIdRef.current === sourceSessionId) {
              selectedSessionIdRef.current = event.session_id;
              replaceSessionUrl(event.session_id);
              setSessionId(event.session_id);
            }
          }
          if (selectedSessionIdRef.current === activeSessionId) setStreamStatus("正在思考…");
          return;
        }
        if (event.event === "tool_call") {
          if (selectedSessionIdRef.current === activeSessionId) setStreamStatus("正在思考…");
          return;
        }
        if (event.event === "token") {
          if (selectedSessionIdRef.current === activeSessionId) {
            setStreamStatus("正在思考…");
            queueAssistantToken(assistantId, event.content ?? "");
          }
          return;
        }
        if (event.event === "done") {
          const completedAnswer = event.answer ?? event.data?.answer;
          if (selectedSessionIdRef.current === activeSessionId) {
            finishAssistantTyping(assistantId, completedAnswer ?? "", event.presentation);
            setStreamStatus("");
          }
        }
      }, replaceFromSequence, productContext, interaction);
      await waitForTypingDrain(assistantId);
      setSessions(await listSessions(auth.token));
      if (activeSessionId && selectedSessionIdRef.current === activeSessionId) {
        const persisted = await getSession(auth.token, activeSessionId);
        const persistedMessages = toChatMessages(persisted.session_id, persisted.messages);
        sessionCacheRef.current.set(persisted.session_id, persistedMessages);
        setChatMessages(persistedMessages);
      }
    } catch (reason) {
      cancelTyping();
      if (selectedSessionIdRef.current === activeSessionId) {
        setError(reason instanceof Error ? reason.message : "智能客服暂时不可用");
        setChatMessages((items) => items.filter((message) => message.id !== assistantId || message.content));
      }
    } finally { setBusy(false); setStreamStatus(""); }
  };

  const submitChat = (event: FormEvent): void => {
    event.preventDefault();
    void submitChatMessage();
  };

  const handleChatKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (
      event.key !== "Enter"
      || event.shiftKey
      || event.altKey
      || event.nativeEvent.isComposing
    ) return;
    event.preventDefault();
    void submitChatMessage();
  };

  const handlePresentationAction = (action: CustomerAction): void => {
    if (action.type === "interaction") {
      void submitChatMessage("", undefined, action.interaction);
      return;
    }
    const parameters = new URLSearchParams();
    if (action.destination === "orders") {
      parameters.set("page", "orders");
      // ``refund_order`` remains a compatibility URL for existing self-service
      // links.  The Presentation contract itself is typed (order + focus), and
      // other customer actions no longer masquerade as refund navigation.
      if (action.target.focus === "refund") parameters.set("refund_order", action.target.order_id);
      else parameters.set("order_id", action.target.order_id);
      if (action.target.focus && action.target.focus !== "refund") parameters.set("focus", action.target.focus);
      window.history.pushState(null, "", `${window.location.pathname}?${parameters}`);
      setPage("orders");
      return;
    }
    parameters.set("page", "tickets");
    parameters.set("ticket_id", action.target.ticket_id);
    window.history.pushState(null, "", `${window.location.pathname}?${parameters}`);
    setPage("tickets");
  };

  const removeSession = async (targetSessionId: string): Promise<void> => {
    if (busy) return;
    try {
      await deleteSession(auth.token, targetSessionId);
      setSessions((items) => items.filter((item) => item.session_id !== targetSessionId));
      if (sessionId === targetSessionId) startNewChat();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "删除会话失败");
    }
  };
  const openProductDetail = (category: "laptops" | "phones" | "components", productId: string): void => {
    const parameters = new URLSearchParams({ page: "product", category, product: productId });
    window.history.pushState(null, "", `${window.location.pathname}?${parameters}`);
    setDetailTarget({ category, productId });
    setPage("product");
  };
  const returnToCatalog = (): void => {
    window.history.pushState(null, "", `${window.location.pathname}?page=catalog`);
    setPage("catalog");
  };
  const askAboutProduct = (product: Product, question = ""): void => {
    if (detailTarget) setSelectedProductForChat({ category: detailTarget.category, productId: product.id });
    navigatePage("service");
    setQuery(question.trim() || `我想了解 ${product.product_name}，请介绍它的配置、适用场景和库存情况。`);
  };

  // 商品浏览与交易入口使用与游客相同的商城壳；只有智能客服需要会话侧栏。
  const storefront = page !== "service" ? <CustomerStorefront
      auth={auth}
      page={page}
      detailTarget={detailTarget}
      initialQuery={catalogQueryFromUrl}
      initialCategory={catalogCategoryFromUrl}
      initialComponentCategory={catalogComponentCategoryFromUrl}
      onNavigate={navigatePage}
      onViewProduct={openProductDetail}
      onBackToCatalog={returnToCatalog}
      onAskProduct={askAboutProduct}
      onSignOut={onSignOut}
    /> : null;

  const interactiveChoiceMessageId = latestInteractiveChoiceMessageId(chatMessages);

  return storefront ?? <main className={`customer-chat-app${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
    <aside className="chat-sidebar">
      <div className="chat-brand"><span>G</span><strong>Geex AI</strong><button className="sidebar-toggle" aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"} onClick={() => setSidebarCollapsed((value) => !value)}>{sidebarCollapsed ? "›" : "‹"}</button></div>
      <button className="new-chat-button" onClick={startNewChat}>＋ 新建对话</button>
      <nav className="chat-page-nav" aria-label="客户服务导航">
        <button className={page === "catalog" ? "active" : "secondary"} onClick={() => navigatePage("catalog")}>商品目录</button>
        <button className={page === "cart" ? "active" : "secondary"} onClick={() => navigatePage("cart")}>购物车</button>
        <button className={page === "orders" ? "active" : "secondary"} onClick={() => navigatePage("orders")}>我的订单</button>
        <button className={page === "tickets" ? "active" : "secondary"} onClick={() => navigatePage("tickets")}>我的售后</button>
      </nav>
      <section className="sidebar-sessions"><button className="sidebar-section-toggle" onClick={() => setSessionsExpanded((value) => !value)}><span>最近对话</span><span>{sessionsExpanded ? "⌃" : "⌄"}</span></button>{sessionsExpanded && (sessions.length ? sessions.slice(0, 10).map((session) => <div className={`session-item ${sessionId === session.session_id ? "active" : ""}`} key={session.session_id}><a className="session-row" href={decorateInternalHref(sessionUrl(session.session_id))} onClick={(event) => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); void openSession(session.session_id); }}><span>{session.title || "新对话"}</span><small>{session.message_count} 条消息</small></a><button className="session-delete" aria-label={`删除会话：${session.title || "新对话"}`} onClick={() => void removeSession(session.session_id)}>×</button></div>) : <p className="sidebar-empty">暂无历史对话</p>)}</section>
      <div className="chat-account"><span>{auth.user.username}</span><button className="text-button" onClick={() => void onSignOut()}>退出</button></div>
    </aside>
    <section className="chat-main">
      <header className="chat-main-header"><div><strong>{page === "service" ? "智能客服" : page === "catalog" ? "商品目录" : page === "product" ? "商品详情" : page === "orders" ? "我的订单" : page === "cart" ? "购物车" : "我的售后"}</strong><span>{page === "service" ? (sessionId ? "当前会话" : "新对话") : "Geex Digital"}</span></div><span className="role-badge">客户服务台</span></header>
      {page === "catalog" ? <div className="customer-page-scroll"><ProductCatalog initialQuery={catalogQueryFromUrl} initialCategory={catalogCategoryFromUrl} initialComponentCategory={catalogComponentCategoryFromUrl} auth={auth} onView={openProductDetail} /></div> : page === "product" && detailTarget ? <div className="customer-page-scroll"><ProductDetailPage auth={auth} category={detailTarget.category} productId={detailTarget.productId} onBack={returnToCatalog} onAsk={askAboutProduct} /></div> : page === "orders" ? <div className="customer-page-scroll"><OrderList auth={auth} focusOrderId={new URLSearchParams(window.location.search).get("order_id") ?? new URLSearchParams(window.location.search).get("refund_order") ?? undefined} /></div> : page === "cart" ? <div className="customer-page-scroll"><CartPage auth={auth} /></div> : page === "tickets" ? <div className="customer-page-scroll"><CustomerTicketCenter auth={auth} focusTicketId={new URLSearchParams(window.location.search).get("ticket_id") ?? undefined} /></div> : <section className="chat-canvas">
        <div ref={chatHistoryRef} className="chat-history chatgpt-history">{chatMessages.length === 0 ? <div className="chat-welcome"><p className="eyebrow">GEEX DIGITAL · AI ASSISTANT</p><h1>今天想解决什么问题？</h1><p>我可以介绍商品、查询已归属订单，也能帮你发起售后工单。</p><div className="prompt-grid"><button className="prompt-card" onClick={() => setQuery("帮我推荐一台预算 5000 元左右的笔记本")}>推荐一台预算 5000 元的笔记本</button><button className="prompt-card" onClick={() => setQuery("帮我查询订单物流")}>查询我的订单物流</button><button className="prompt-card" onClick={() => setQuery("哪些手机目前有库存？")}>查询有库存的手机</button></div></div> : chatMessages.map((message) => <article className={`bubble ${message.role}${!message.content ? " thinking" : ""}`} key={message.id}><div className="message-content">{message.content ? message.role === "assistant" ? <CustomerMarkdown>{message.content}</CustomerMarkdown> : message.content : streamStatus || "正在思考…"}{message.role === "assistant" && message.presentation && <PresentationRenderer presentation={message.presentation} onAction={handlePresentationAction} disabled={busy} choiceEnabled={message.id === interactiveChoiceMessageId} />}</div></article>)}</div>
        <form className="composer chatgpt-composer" onSubmit={submitChat}><textarea value={query} onChange={(event) => setQuery(event.target.value)} onKeyDown={handleChatKeyDown} placeholder="给 Geex AI 发送消息" maxLength={2000} rows={1} /><button aria-label="发送消息" disabled={busy || !query.trim()}>{busy ? "…" : "↑"}</button></form><p className="chat-disclaimer">Enter 发送 · Shift / Alt + Enter 换行</p>
      </section>}
    </section>
    {error && <p className="toast error">{error}</p>}
  </main>;
}

const PRODUCT_CATEGORIES = [
  ["laptops", "笔记本"],
  ["phones", "手机"],
  ["components", "电脑配件"],
] as const;

function ProductImage({ product }: { product: Product }) {
  const [failed, setFailed] = useState(false);
  if (!product.image_url || failed) return <div className="product-visual product-image-missing" aria-label={`${product.product_name} 暂无图片`} />;
  return <img src={product.image_url} alt={product.product_name} onError={() => setFailed(true)} />;
}

/** 使用真实 href，让浏览器原生支持商品卡右键在新标签页打开。 */
function productDetailUrl(category: "laptops" | "phones" | "components", productId: string): string {
  const parameters = new URLSearchParams({ page: "product", category, product: productId });
  return `${window.location.pathname}?${parameters}`;
}

function ProductCatalog({ auth, onView, initialQuery = "", initialCategory = "laptops", initialComponentCategory = "" }: { auth?: AuthState; onView: (category: "laptops" | "phones" | "components", productId: string) => void; initialQuery?: string; initialCategory?: "laptops" | "phones" | "components"; initialComponentCategory?: string }) {
  const [category, setCategory] = useState<"laptops" | "phones" | "components">(initialCategory);
  const [query, setQuery] = useState(initialQuery);
  const [products, setProducts] = useState<Product[]>([]);
  const [catalogPage, setCatalogPage] = useState<ProductCatalogPage | null>(null);
  const [brand, setBrand] = useState("");
  const [componentCategory, setComponentCategory] = useState(initialComponentCategory);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const requestVersionRef = useRef(0);

  const load = async (
    nextCategory = category,
    nextQuery = query,
    nextPage = catalogPage?.page ?? 1,
    nextBrand = brand,
    nextComponentCategory = componentCategory,
  ): Promise<void> => {
    const requestVersion = ++requestVersionRef.current;
    setLoading(true); setError("");
    setProducts([]);
    try {
      const result = await listProducts(auth?.token, nextCategory, {
        query: nextQuery, brand: nextBrand, componentCategory: nextComponentCategory, page: nextPage,
      });
      if (requestVersion === requestVersionRef.current) {
        setProducts(result.products);
        setCatalogPage(result);
      }
    }
    catch (reason) {
      if (requestVersion === requestVersionRef.current) setError(reason instanceof Error ? reason.message : "商品目录暂时不可用");
    }
    finally {
      if (requestVersion === requestVersionRef.current) setLoading(false);
    }
  };
  useEffect(() => {
    setCategory(initialCategory);
    setQuery(initialQuery);
    setBrand("");
    setComponentCategory(initialComponentCategory);
    void load(initialCategory, initialQuery, 1, "", initialComponentCategory);
  }, [auth?.token, initialCategory, initialQuery, initialComponentCategory]);
  const selectCategory = (nextCategory: "laptops" | "phones" | "components"): void => {
    setCategory(nextCategory); setBrand(""); setComponentCategory("");
    void load(nextCategory, query, 1, "", "");
  };
  const search = (event: FormEvent): void => { event.preventDefault(); void load(category, query, 1, brand, componentCategory); };
  const selectBrand = (nextBrand: string): void => { setBrand(nextBrand); void load(category, query, 1, nextBrand, componentCategory); };
  const selectComponentCategory = (nextComponentCategory: string): void => { setComponentCategory(nextComponentCategory); void load(category, query, 1, brand, nextComponentCategory); };
  const hasPreviousPage = (catalogPage?.page ?? 1) > 1;
  const hasNextPage = catalogPage !== null && catalogPage.page * catalogPage.page_size < catalogPage.total;
  return <section className="catalog"><header className="catalog-header"><div><p className="eyebrow">PRODUCT CATALOG</p><h2>发现适合你的数码产品</h2><p className="muted">共 {catalogPage?.total ?? 0} 件商品；价格以当前系统数据为准。</p></div><form className="catalog-search" onSubmit={search}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索品牌或商品名称" maxLength={100} /><button>搜索</button></form></header><div className="catalog-tabs">{PRODUCT_CATEGORIES.map(([value, label]) => <button key={value} className={category === value ? "active" : "secondary"} onClick={() => selectCategory(value)}>{label}</button>)}</div>{catalogPage && category !== "components" && catalogPage.brands.length > 0 && <div className="catalog-filters"><button className={!brand ? "active" : "secondary"} onClick={() => selectBrand("")}>全部品牌</button>{catalogPage.brands.map((item) => <button key={item} className={brand === item ? "active" : "secondary"} onClick={() => selectBrand(item)}>{item}</button>)}</div>}{catalogPage && category === "components" && <div className="catalog-filters"><button className={!componentCategory ? "active" : "secondary"} onClick={() => selectComponentCategory("")}>全部配件</button>{Object.entries(catalogPage.component_categories).map(([value, label]) => <button key={value} className={componentCategory === value ? "active" : "secondary"} onClick={() => selectComponentCategory(value)}>{label}</button>)}</div>}{error && <p className="error">{error}</p>}<div className="product-grid">{loading ? <p className="empty">正在读取商品目录…</p> : products.length ? products.map((product) => <a className="product-card product-card-button product-card-link" key={product.id} href={decorateInternalHref(productDetailUrl(category, product.id))} onClick={(event) => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); onView(category, product.id); }}><ProductImage product={product} /><div className="product-info"><span className="product-type">{product.product_type || (category === "laptops" ? "笔记本" : category === "phones" ? "手机" : "电脑配件")}</span><h3>{product.product_name}</h3><p>{product.description.slice(0, 84) || "查看详细配置与 AI 咨询。"}</p><div className="product-bottom"><strong>{product.price === null ? "价格待询" : `¥${product.price.toLocaleString("zh-CN")}`}</strong>{product.stock <= 0 && <span className="out-stock">暂时缺货</span>}</div></div></a>) : <p className="empty">没有找到匹配商品，换个关键词试试。</p>}</div>{catalogPage && <nav className="catalog-pagination" aria-label="商品分页"><button className="secondary" disabled={!hasPreviousPage || loading} onClick={() => void load(category, query, catalogPage.page - 1, brand, componentCategory)}>上一页</button><span>第 {catalogPage.page} / {Math.max(1, Math.ceil(catalogPage.total / catalogPage.page_size))} 页</span><button className="secondary" disabled={!hasNextPage || loading} onClick={() => void load(category, query, catalogPage.page + 1, brand, componentCategory)}>下一页</button></nav>}</section>;
}

/** 商品详情承载参数、购买和面向该商品的 AI 咨询，目录卡只负责快速浏览。 */
/** 不登录也能提问的公开导购页；浏览器刷新后不会保留聊天内容。 */
function PublicAssistantPage({ initialQuestion, initialProduct, onBrowseProducts }: { initialQuestion: string; initialProduct?: ProductContextRef; onBrowseProducts: () => void }) {
  const [query, setQuery] = useState(initialQuestion);
  const [messages, setMessages] = useState<Array<{ role: "user" | "assistant"; content: string }>>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const historyRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setQuery(initialQuestion); }, [initialQuestion]);
  useEffect(() => { historyRef.current?.scrollTo({ top: historyRef.current.scrollHeight, behavior: "smooth" }); }, [messages, loading]);
  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    const text = query.trim();
    if (!text || loading) return;
    setQuery(""); setLoading(true); setError("");
    setMessages((current) => [...current, { role: "user", content: text }]);
    try {
      const result = await askPublicAssistant(text, initialProduct);
      setMessages((current) => [...current, { role: "assistant", content: result.answer }]);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "智能导购暂时不可用"); }
    finally { setLoading(false); }
  };
  return <section className="public-agent-chat">
    <header className="public-agent-header"><p className="eyebrow">GEEX AI</p><h1>智能客服</h1><p className="muted">咨询商品、配置和购买政策。</p></header>
    <div className="chatgpt-history public-chat-history" ref={historyRef}>
      {messages.length === 0 && !loading && <div className="public-agent-welcome"><h2>想了解哪款商品？</h2><p>例如：预算 8000 元，想买一台游戏本。</p><button className="text-button" onClick={onBrowseProducts}>浏览商品目录</button></div>}
      {messages.map((message, index) => <article className={`bubble ${message.role}`} key={`${message.role}-${index}-${message.content.slice(0, 24)}`}><div className="message-content"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown></div></article>)}
      {loading && <article className="bubble assistant thinking"><div className="message-content">正在思考…</div></article>}
      {error && <p className="error">{error}</p>}
    </div>
    <form className="composer chatgpt-composer public-assistant-form" onSubmit={submit}><textarea value={query} onChange={(event) => setQuery(event.target.value)} maxLength={800} placeholder="给 Geex AI 发送消息" rows={1} /><button aria-label="发送消息" disabled={loading || !query.trim()}>{loading ? "…" : "↑"}</button></form>
  </section>;
}

/** 原始商品名来自爬取数据，详情首屏只展示可读的短标题；完整标题仍可悬停查看。 */
function productDisplayName(productName: string): string {
  const primaryName = productName.split(/[，,。；;]/, 1)[0]?.trim() || productName.trim();
  return primaryName.length > 36 ? `${primaryName.slice(0, 36)}…` : primaryName;
}

/** 打开服务端生成的支付页面；具体渠道签名和表单字段只由后端决定。 */
function openPaymentCheckout(session: CheckoutSession): void {
  if (!session.payment_form_action || !session.payment_form_fields) {
    window.location.assign(session.payment_url);
    return;
  }
  const form = document.createElement("form");
  form.method = "post";
  form.action = session.payment_form_action;
  for (const [name, value] of Object.entries(session.payment_form_fields)) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = name;
    input.value = value;
    form.appendChild(input);
  }
  document.body.appendChild(form);
  form.submit();
}

function paymentProviderLabel(provider: PaymentProvider): string {
  return provider === "unionpay_test" ? "银联测试支付" : "支付宝沙箱";
}

/** 结算前选择支付渠道；前端只提交后端允许的 provider 标识。 */
function CheckoutMethodDialog({ onClose, onChooseProvider }: {
  onClose: () => void;
  onChooseProvider: (provider: PaymentProvider) => void;
}) {
  return <div className="modal-backdrop checkout-qr-backdrop" role="presentation">
    <section className="modal checkout-method-dialog" role="dialog" aria-modal="true" aria-label="选择付款方式">
      <button className="checkout-qr-close" aria-label="关闭结算" onClick={onClose}>×</button>
      <p className="eyebrow">CHECKOUT</p>
      <h2>选择付款方式</h2>
      <p className="muted">付款金额和商品信息会由服务端再次核验。</p>
      <button className="payment-method-option" onClick={() => onChooseProvider("unionpay_test")}>
        <span className="payment-method-icon">银</span>
        <span><strong>银联测试支付</strong><small>进入银联测试收银台完成付款</small></span>
        <b>›</b>
      </button>
      <button className="payment-method-option" onClick={() => onChooseProvider("alipay_sandbox")}>
        <span className="payment-method-icon">支</span>
        <span><strong>支付宝沙箱</strong><small>进入支付宝沙箱收银台完成付款</small></span>
        <b>›</b>
      </button>
      <button className="secondary checkout-method-cancel" onClick={onClose}>暂不付款</button>
    </section>
  </div>;
}

/** 在商城内展示支付宝沙箱二维码；付款结果仍由订单页查询确认。 */
function ProductDetailPage({
  auth,
  category,
  productId,
  onBack,
  onAsk,
  onRequireLogin,
}: {
  auth?: AuthState;
  category: "laptops" | "phones" | "components";
  productId: string;
  onBack: () => void;
  onAsk: (product: Product, question?: string) => void;
  onRequireLogin?: () => void;
}) {
  const [product, setProduct] = useState<ProductDetail | null>(null);
  const [question, setQuestion] = useState("");
  const [loading, setLoading] = useState(true);
  const [buying, setBuying] = useState(false);
  const [addingToCart, setAddingToCart] = useState(false);
  const [cartMessage, setCartMessage] = useState("");
  const [error, setError] = useState("");
  const [choosingPaymentMethod, setChoosingPaymentMethod] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true); setError("");
    void getProductDetail(auth?.token, category, productId)
      .then((result) => { if (active) setProduct(result); })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "商品详情暂时不可用"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [auth?.token, category, productId]);

  useEffect(() => {
    /** 从支付宝收银台返回（包括浏览器 bfcache 恢复）时，购买按钮必须可再次点击。 */
    const resetBuying = (): void => setBuying(false);
    window.addEventListener("pageshow", resetBuying);
    window.addEventListener("focus", resetBuying);
    return () => {
      window.removeEventListener("pageshow", resetBuying);
      window.removeEventListener("focus", resetBuying);
    };
  }, []);

  const buy = async (): Promise<void> => {
    if (!auth) { onRequireLogin?.(); return; }
    if (!product || buying) return;
    setError("");
    setChoosingPaymentMethod(true);
  };
  const startPayment = async (paymentProvider: PaymentProvider): Promise<void> => {
    if (!auth || !product || buying) return;
    setChoosingPaymentMethod(false);
    setBuying(true); setError("");
    try {
      const session = await createCheckout(
        auth.token,
        category,
        product.id,
        window.location.origin,
        paymentProvider,
      );
      openPaymentCheckout(session);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "暂时无法创建支付订单");
      setBuying(false);
    }
  };
  const addToCart = async (): Promise<void> => {
    if (!auth) { onRequireLogin?.(); return; }
    if (!product || addingToCart) return;
    setAddingToCart(true); setError(""); setCartMessage("");
    try {
      await addCartItem(auth.token, category, product.id);
      setCartMessage("已加入购物车");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "暂时无法加入购物车");
    } finally {
      setAddingToCart(false);
    }
  };
  const ask = (event: FormEvent): void => {
    event.preventDefault();
    if (product) onAsk(product, question);
  };

  if (loading) return <section className="product-detail panel"><p className="empty">正在读取商品详情…</p></section>;
  if (!product) return <section className="product-detail panel"><button className="secondary" onClick={onBack}>← 返回商品目录</button><p className="error">{error || "商品不可用或无法核验"}</p></section>;
  return <><section className="product-detail">
    <button className="detail-back secondary" onClick={onBack}>← 返回商品目录</button>
    {error && <p className="error">{error}</p>}
    <div className="product-detail-layout">
      <section className="product-detail-main panel">
        <div className="product-hero"><ProductImage product={product} /><div><p className="detail-category">{product.product_type || "商品详情"}</p><h1 title={product.product_name}>{productDisplayName(product.product_name)}</h1><p className="muted product-detail-summary">{product.brand ? `${product.brand} · ` : ""}{product.description || "查看以下详细规格。"}</p><div className="detail-price-row"><strong>{product.price === null ? "价格待询" : `¥${product.price.toLocaleString("zh-CN")}`}</strong>{product.stock <= 0 && <span className="out-stock">暂时缺货</span>}</div><div className="product-actions"><button className="secondary" disabled={product.stock <= 0 || addingToCart} onClick={() => void addToCart()}>{addingToCart ? "加入中…" : "加入购物车"}</button><button className="detail-buy" disabled={product.stock <= 0 || buying} onClick={() => void buy()}>{buying ? "正在打开支付…" : "立即结算"}</button></div>{!auth && <p className="muted cart-login-tip">加入购物车或结算时需要登录。</p>}{cartMessage && <p className="cart-success">{cartMessage}</p>}</div></div>
        <h2>商品参数</h2>
        <div className="specification-table">{product.specifications.length ? product.specifications.map((specification) => <div className="specification-row" key={specification.name}><span>{specification.name}</span><strong>{specification.value}</strong></div>) : <p className="empty">暂未提供详细参数。</p>}</div>
      </section>
      <aside className="product-detail-ai panel"><p className="eyebrow">AI PRODUCT EXPERT</p><h2>问问 AI</h2><p className="muted">围绕这款商品的配置、使用场景或搭配方案提问。</p><form onSubmit={ask}><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={`例如：${product.product_name} 适合玩 3A 游戏吗？`} maxLength={400} rows={5} /><button disabled={!question.trim()}>开始咨询</button></form><button className="text-button detail-default-question" onClick={() => onAsk(product)}>让 AI 介绍这款商品</button></aside>
    </div>
  </section>{choosingPaymentMethod && <CheckoutMethodDialog onClose={() => setChoosingPaymentMethod(false)} onChooseProvider={(provider) => { void startPayment(provider); }} />}</>;
}

/** 客户在付款前统一核对商品、数量和总额的轻量购物车页面。 */
function CartPage({ auth }: { auth: AuthState }) {
  const [cart, setCart] = useState<Cart>({ items: [] });
  const [loading, setLoading] = useState(true);
  const [updatingItem, setUpdatingItem] = useState<number | null>(null);
  const [checkingOut, setCheckingOut] = useState(false);
  const [error, setError] = useState("");
  const [choosingPaymentMethod, setChoosingPaymentMethod] = useState(false);

  const load = async (): Promise<void> => {
    setLoading(true); setError("");
    try { setCart(await getCart(auth.token)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "购物车暂时无法读取"); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [auth.token]);

  useEffect(() => {
    const resetCheckout = (): void => setCheckingOut(false);
    window.addEventListener("pageshow", resetCheckout);
    window.addEventListener("focus", resetCheckout);
    return () => { window.removeEventListener("pageshow", resetCheckout); window.removeEventListener("focus", resetCheckout); };
  }, []);

  const changeQuantity = async (itemId: number, quantity: number): Promise<void> => {
    if (quantity < 1) { await remove(itemId); return; }
    setUpdatingItem(itemId); setError("");
    try {
      const updated = await updateCartItem(auth.token, itemId, quantity);
      setCart((current) => ({ items: current.items.map((item) => item.item_id === itemId ? updated : item) }));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法更新商品数量"); }
    finally { setUpdatingItem(null); }
  };
  const remove = async (itemId: number): Promise<void> => {
    setUpdatingItem(itemId); setError("");
    try { setCart(await deleteCartItem(auth.token, itemId)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法删除商品"); }
    finally { setUpdatingItem(null); }
  };
  const checkout = async (paymentProvider: PaymentProvider): Promise<void> => {
    if (checkingOut || cart.items.length === 0 || cart.items.some((item) => !item.available)) return;
    setCheckingOut(true); setError("");
    try {
      const session = await checkoutCart(auth.token, window.location.origin, paymentProvider);
      openPaymentCheckout(session);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "暂时无法创建支付订单"); setCheckingOut(false); }
  };
  const openCheckout = (): void => {
    if (checkingOut || cart.items.length === 0 || cart.items.some((item) => !item.available)) return;
    setError("");
    setChoosingPaymentMethod(true);
  };
  const total = cart.items.reduce((sum, item) => sum + (item.price ?? 0) * item.quantity, 0);
  const canCheckout = cart.items.length > 0 && cart.items.every((item) => item.available && item.price !== null);

  return <><section className="cart-page panel"><div className="section-title"><div><p className="eyebrow">SHOPPING CART</p><h2>购物车</h2><p className="muted">结算前会再次核验商品价格与库存。</p></div><button className="secondary" onClick={() => void load()} disabled={loading}>刷新</button></div>{error && <p className="error">{error}</p>}{loading ? <p className="empty">正在读取购物车…</p> : cart.items.length === 0 ? <p className="empty">购物车还是空的。去商品详情页把想买的商品加入这里吧。</p> : <><div className="cart-items">{cart.items.map((item) => <article className={`cart-item${item.available ? "" : " unavailable"}`} key={item.item_id}><div><p className="product-type">{item.brand || "商品"}</p><h3>{item.product_name}</h3><p className="muted">{item.available ? `库存可用：${item.stock}` : "商品已下架或当前库存不足，请删除后重新选择。"}</p></div><div className="cart-item-price"><strong>{item.price === null ? "价格待询" : `¥${item.price.toLocaleString("zh-CN")}`}</strong><div className="quantity-control"><button className="secondary" disabled={updatingItem === item.item_id} onClick={() => void changeQuantity(item.item_id, item.quantity - 1)}>−</button><span>{item.quantity}</span><button className="secondary" disabled={updatingItem === item.item_id || item.quantity >= Math.min(5, item.stock)} onClick={() => void changeQuantity(item.item_id, item.quantity + 1)}>＋</button></div><button className="text-button cart-remove" disabled={updatingItem === item.item_id} onClick={() => void remove(item.item_id)}>删除</button></div></article>)}</div><footer className="cart-summary"><div><span>合计</span><strong>¥{total.toLocaleString("zh-CN")}</strong></div><button disabled={!canCheckout || checkingOut} onClick={openCheckout}>{checkingOut ? "正在打开支付…" : "去结算"}</button></footer></>}</section>{choosingPaymentMethod && <CheckoutMethodDialog onClose={() => setChoosingPaymentMethod(false)} onChooseProvider={(provider) => { void checkout(provider); }} />}</>;
}

function OrderList({ auth, focusOrderId }: { auth: AuthState; focusOrderId?: string }) {
  const [orders, setOrders] = useState<CustomerOrder[]>([]);
  const [checkoutOrders, setCheckoutOrders] = useState<CheckoutOrder[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [expandedOrder, setExpandedOrder] = useState<string | null>(null);
  const [resumingOrder, setResumingOrder] = useState<string | null>(null);
  const [refreshingPaymentOrder, setRefreshingPaymentOrder] = useState<string | null>(null);
  const [cancellingOrder, setCancellingOrder] = useState<string | null>(null);
  const [requestingRefundOrder, setRequestingRefundOrder] = useState<string | null>(null);
  const [confirmingRefundId, setConfirmingRefundId] = useState<string | null>(null);
  const [refreshingRefundId, setRefreshingRefundId] = useState<string | null>(null);
  const [focusedOrderId, setFocusedOrderId] = useState<string | null>(null);
  const [paymentReturnMessage, setPaymentReturnMessage] = useState<string | null>(null);
  const orderCardRefs = useRef(new Map<string, HTMLElement>());
  const refundRequestKeys = useRef(new Map<string, string>());
  const refundConfirmationKeys = useRef(new Map<string, string>());
  const load = async (paymentOrderNo?: string, isPaymentReturn = false): Promise<void> => {
    setLoading(true); setError("");
    if (isPaymentReturn) setPaymentReturnMessage("正在确认支付结果…");
    try {
      let [legacyOrders, currentOrders] = await Promise.all([listMyOrders(auth.token), listMyCheckoutOrders(auth.token)]);
      // The server front-return has already performed authoritative
      // queryTrans reconciliation.  Read local state first; refresh the
      // provider only when that convergence has not reached PAID yet.
      let returnedOrder = paymentOrderNo ? currentOrders.find((order) => order.order_no === paymentOrderNo) : undefined;
      if (isPaymentReturn && returnedOrder && returnedOrder.status !== "PAID" && returnedOrder.payment_status !== "SUCCEEDED") {
        try {
          await refreshCheckoutPayment(auth.token, returnedOrder.order_no);
          [legacyOrders, currentOrders] = await Promise.all([listMyOrders(auth.token), listMyCheckoutOrders(auth.token)]);
          returnedOrder = currentOrders.find((order) => order.order_no === paymentOrderNo);
        } catch {
          setPaymentReturnMessage("支付结果暂时无法确认，请稍后刷新。");
        }
      }
      setOrders(legacyOrders); setCheckoutOrders(currentOrders);
      if (isPaymentReturn) setPaymentReturnMessage(returnedOrder?.status === "PAID" || returnedOrder?.payment_status === "SUCCEEDED" ? "支付成功" : "支付结果暂时无法确认，请稍后刷新。");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "订单暂时无法读取");
    } finally { setLoading(false); }
  };
  useEffect(() => { const parameters = new URLSearchParams(window.location.search); const isPaymentReturn = parameters.get("payment_return") === "1"; const returnedOrderNo = isPaymentReturn ? parameters.get("checkout_order") ?? undefined : undefined; void load(returnedOrderNo, isPaymentReturn).finally(() => { if (isPaymentReturn) window.history.replaceState(null, "", `${window.location.pathname}?page=orders`); }); }, [auth.token]);
  useEffect(() => { const resetResume = (): void => setResumingOrder(null); window.addEventListener("pageshow", resetResume); window.addEventListener("focus", resetResume); return () => { window.removeEventListener("pageshow", resetResume); window.removeEventListener("focus", resetResume); }; }, []);
  useEffect(() => {
    if (!focusOrderId || loading) return;
    const found = checkoutOrders.some((order) => order.order_no === focusOrderId)
      || orders.some((order) => order.order_id === focusOrderId);
    if (!found) {
      setFocusedOrderId(null);
      return;
    }
    setExpandedOrder(focusOrderId);
    setFocusedOrderId(focusOrderId);
    const frame = window.requestAnimationFrame(() => {
      orderCardRefs.current.get(focusOrderId)?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    const timer = window.setTimeout(() => setFocusedOrderId(null), 2200);
    return () => { window.cancelAnimationFrame(frame); window.clearTimeout(timer); };
  }, [checkoutOrders, orders, loading, focusOrderId]);
  const resumePayment = async (orderNo: string): Promise<void> => { setResumingOrder(orderNo); setError(""); try { const session = await resumeCheckout(auth.token, orderNo, window.location.origin); openPaymentCheckout(session); } catch (reason) { setError(reason instanceof Error ? reason.message : "暂时无法继续付款"); await load(); } finally { setResumingOrder(null); } };
  const refreshPayment = async (orderNo: string): Promise<void> => { setRefreshingPaymentOrder(orderNo); setError(""); try { await refreshCheckoutPayment(auth.token, orderNo); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "支付结果暂时无法确认，请稍后重试"); await load(); } finally { setRefreshingPaymentOrder(null); } };
  const cancelPayment = async (orderNo: string): Promise<void> => { if (!window.confirm("确认取消这笔待支付订单吗？")) return; setCancellingOrder(orderNo); setError(""); try { await cancelCheckout(auth.token, orderNo); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "暂时无法取消订单"); await load(); } finally { setCancellingOrder(null); } };
  const requestRefund = async (order: CheckoutOrder): Promise<void> => {
    const reason = window.prompt("请简要说明退款原因（可留空）：", "");
    if (reason === null) return;
    setRequestingRefundOrder(order.order_no); setError("");
    try {
      const key = refundRequestKeys.current.get(order.order_no) ?? crypto.randomUUID();
      refundRequestKeys.current.set(order.order_no, key);
      await requestCheckoutRefund(auth.token, order.order_no, reason, key);
      refundRequestKeys.current.delete(order.order_no);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "暂时无法创建退款申请");
    } finally {
      setRequestingRefundOrder(null);
    }
  };
  const confirmRefund = async (order: CheckoutOrder): Promise<void> => {
    if (!order.refund_id || !window.confirm("确认原路全额退款吗？提交后将按当前支付渠道处理退款。")) return;
    setConfirmingRefundId(order.refund_id); setError("");
    try {
      const key = refundConfirmationKeys.current.get(order.refund_id) ?? crypto.randomUUID();
      refundConfirmationKeys.current.set(order.refund_id, key);
      await confirmCheckoutRefund(auth.token, order.refund_id, key);
      refundConfirmationKeys.current.delete(order.refund_id);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "退款暂时无法确认");
      await load();
    } finally {
      setConfirmingRefundId(null);
    }
  };
  const refreshRefund = async (order: CheckoutOrder): Promise<void> => {
    if (!order.refund_id) return;
    setRefreshingRefundId(order.refund_id); setError("");
    try {
      await refreshCheckoutRefund(auth.token, order.refund_id);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "退款状态暂时无法确认");
    } finally {
      setRefreshingRefundId(null);
    }
  };

  const checkoutLabel = (order: CheckoutOrder): string => {
    if (order.refund_status === "PENDING_CONFIRMATION") return "待确认退款";
    if (["PENDING_MERCHANT_REVIEW", "PENDING_FINANCE_APPROVAL"].includes(order.refund_status ?? "")) return "退款申请已提交";
    if (order.refund_status === "PROCESSING") return "退款处理中";
    if (["SUCCEEDED", "COMPLETED"].includes(order.refund_status ?? "") || order.status === "REFUNDED") return "已退款";
    if (order.refund_status === "FAILED") return "退款失败";
    if (order.fulfillment_status === "DELIVERED") return "已签收";
    if (order.fulfillment_status === "SHIPPED") return "已发货";
    if (order.status === "PAID") return "待发货";
    return order.status === "PENDING_PAYMENT" ? "等待付款" : "支付失败";
  };
  const checkoutDetail = (order: CheckoutOrder): string => {
    if (order.refund_status === "PENDING_CONFIRMATION") return "请确认后提交原路全额退款";
    if (["PENDING_MERCHANT_REVIEW", "PENDING_FINANCE_APPROVAL"].includes(order.refund_status ?? "")) return "商家正在核实退款申请，请留意后续结果";
    if (order.refund_status === "PROCESSING") return "已提交退款，正在确认退款结果";
    if (["SUCCEEDED", "COMPLETED"].includes(order.refund_status ?? "")) return "退款已完成";
    if (order.refund_status === "FAILED") return "支付渠道明确拒绝退款，请联系售后";
    if (order.tracking_company && order.tracking_number) return `${order.tracking_company} · ${order.tracking_number}`;
    return order.status === "PAID" ? "支付成功，等待发货" : `${paymentProviderLabel(order.payment_provider)} · ${order.payment_status}`;
  };

  return <><section className="orders panel">
    <div className="section-title"><div><p className="eyebrow">MY ORDERS</p><h2>我的订单</h2><p className="muted">显示你的新支付订单与已确认归属的历史订单。</p></div><button className="secondary" onClick={() => void load()}>刷新</button></div>
    {error && <p className="error">{error}</p>}
    {paymentReturnMessage && <p className="order-focus-notice">{paymentReturnMessage}</p>}
    {focusOrderId && !loading && checkoutOrders.every((order) => order.order_no !== focusOrderId) && orders.every((order) => order.order_id !== focusOrderId) && <p className="order-focus-notice">暂时无法在当前账户的订单中定位这笔订单，请确认订单信息。</p>}
    {loading ? <p className="empty">正在读取订单…</p> : <>
      {checkoutOrders.length > 0 && <><h3 className="order-group-title">沙箱结算订单</h3><div className="order-list">{checkoutOrders.map((order) => <article ref={(element) => { if (element) orderCardRefs.current.set(order.order_no, element); else orderCardRefs.current.delete(order.order_no); }} className={`order-card${focusedOrderId === order.order_no ? " order-card-focused" : ""}`} data-order-id={order.order_no} key={order.order_no}>
        <header><div><strong>{order.order_no}</strong><small>{formatDate(order.created_at)}</small></div><div className="order-status-group"><span className="status">{checkoutLabel(order)}</span><small>{paymentProviderLabel(order.payment_provider)}</small></div></header>
        <div className="order-products"><p>{order.product_name}<span>×{order.quantity}</span></p></div>
        <footer><div><strong>实付 ¥{(order.total_amount_cents / 100).toLocaleString("zh-CN")}</strong><small>{checkoutDetail(order)}</small></div>
          {order.status === "PENDING_PAYMENT" && <div className="order-actions">{order.payment_provider === "unionpay_test" ? <button className="secondary" disabled={refreshingPaymentOrder === order.order_no} onClick={() => void refreshPayment(order.order_no)}>{refreshingPaymentOrder === order.order_no ? "查询中…" : "刷新支付状态"}</button> : <button className="secondary" disabled={resumingOrder === order.order_no || cancellingOrder === order.order_no} onClick={() => void resumePayment(order.order_no)}>{resumingOrder === order.order_no ? "正在跳转…" : "继续付款"}</button>}{order.cancel_supported && <button className="secondary cancel-order-button" disabled={resumingOrder === order.order_no || cancellingOrder === order.order_no} onClick={() => void cancelPayment(order.order_no)}>{cancellingOrder === order.order_no ? "取消中…" : "取消订单"}</button>}</div>}
          {order.refund_eligible && !order.refund_status && <div className="order-actions"><button className="secondary" disabled={requestingRefundOrder === order.order_no} onClick={() => void requestRefund(order)}>{requestingRefundOrder === order.order_no ? "提交中…" : "申请退款"}</button></div>}
          {order.refund_supported && order.refund_status === "PENDING_CONFIRMATION" && order.refund_id && <div className="order-actions"><button className="secondary" disabled={confirmingRefundId === order.refund_id} onClick={() => void confirmRefund(order)}>{confirmingRefundId === order.refund_id ? "退款提交中…" : "确认退款"}</button></div>}
          {order.refund_supported && order.refund_status === "PROCESSING" && order.refund_id && <div className="order-actions"><button className="secondary" disabled={refreshingRefundId === order.refund_id} onClick={() => void refreshRefund(order)}>{refreshingRefundId === order.refund_id ? "查询中…" : "刷新退款状态"}</button></div>}
        </footer>
      </article>)}</div></>}
      {orders.length ? <><h3 className="order-group-title">历史订单</h3><div className="order-list">{orders.map((order) => <article ref={(element) => { if (element) orderCardRefs.current.set(order.order_id, element); else orderCardRefs.current.delete(order.order_id); }} className={`order-card${focusedOrderId === order.order_id ? " order-card-focused" : ""}`} data-order-id={order.order_id} key={order.order_id}>
        <header><div><strong>{order.order_id}</strong><small>{formatDate(order.order_date)}</small></div><span className="status">{order.status || "处理中"}</span></header>
        <div className="order-products">{order.items.slice(0, expandedOrder === order.order_id ? undefined : 2).map((item, index) => <p key={index}>{item.brand ? `${item.brand} · ` : ""}{item.product_name}<span>×{item.quantity ?? 1}</span></p>)}</div>
        <footer><div><strong>实付 ¥{order.paid_amount.toLocaleString("zh-CN")}</strong><small>{order.tracking.company && order.tracking.number ? `${order.tracking.company} · ${order.tracking.number}` : "暂无物流信息"}</small></div>{order.items.length > 2 && <button className="secondary" onClick={() => setExpandedOrder(expandedOrder === order.order_id ? null : order.order_id)}>{expandedOrder === order.order_id ? "收起" : `查看 ${order.items.length} 件商品`}</button>}</footer>
      </article>)}</div></> : checkoutOrders.length === 0 && <p className="empty">暂无订单。</p>}
    </>}
  </section></>;
}

/** 客户查看 Agent 售后处理进度，并在同一工单中继续补充问题。 */
function CustomerTicketCenter({ auth, focusTicketId }: { auth: AuthState; focusTicketId?: string }) {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selectedTicket, setSelectedTicket] = useState<Ticket | null>(null);
  const [messages, setMessages] = useState<TicketMessage[]>([]);
  const [reply, setReply] = useState("");
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const loadTickets = async (): Promise<void> => {
    setLoading(true); setError("");
    try { setTickets(await listTickets(auth.token)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "售后工单暂时无法读取"); }
    finally { setLoading(false); }
  };

  const selectTicket = async (ticketId: string): Promise<void> => {
    setError("");
    try {
      const [ticket, nextMessages] = await Promise.all([
        getTicket(auth.token, ticketId),
        listTicketMessages(auth.token, ticketId),
      ]);
      setSelectedTicket(ticket);
      setMessages(nextMessages);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取工单详情"); }
  };

  useEffect(() => { void loadTickets(); }, [auth.token]);

  useEffect(() => {
    if (selectedTicket || tickets.length === 0) return;
    const target = focusTicketId ? tickets.find((ticket) => ticket.ticket_id === focusTicketId) : tickets[0];
    if (target) void selectTicket(target.ticket_id);
  }, [tickets, selectedTicket?.ticket_id, focusTicketId]);

  useEffect(() => {
    if (!selectedTicket || !["AI待处理", "AI处理中"].includes(selectedTicket.status)) return;

    let active = true;
    const refreshPendingTicket = async (): Promise<void> => {
      try {
        const [ticket, nextMessages] = await Promise.all([
          getTicket(auth.token, selectedTicket.ticket_id),
          listTicketMessages(auth.token, selectedTicket.ticket_id),
        ]);
        if (!active) return;
        setSelectedTicket(ticket);
        setMessages(nextMessages);
        setTickets((items) => items.map((item) => item.ticket_id === ticket.ticket_id ? { ...item, ...ticket } : item));
      } catch {
        // 短轮询只是更新异步处理结果；临时网络失败不打断客户正在输入的内容。
      }
    };

    const timer = window.setInterval(() => { void refreshPendingTicket(); }, 2500);
    return () => { active = false; window.clearInterval(timer); };
  }, [auth.token, selectedTicket?.ticket_id, selectedTicket?.status]);

  const sendFollowUp = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    const content = reply.trim();
    if (!selectedTicket || !content || sending) return;
    setSending(true); setError("");
    try {
      const message = await sendCustomerTicketMessage(auth.token, selectedTicket.ticket_id, content);
      setMessages((items) => [...items, message]);
      setReply("");
      await Promise.all([loadTickets(), selectTicket(selectedTicket.ticket_id)]);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "发送补充信息失败"); }
    finally { setSending(false); }
  };

  const confirmResolved = async (): Promise<void> => {
    if (!selectedTicket || sending) return;
    setSending(true); setError("");
    try {
      await closeTicket(auth.token, selectedTicket.ticket_id);
      await Promise.all([loadTickets(), selectTicket(selectedTicket.ticket_id)]);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "暂时无法关闭工单"); }
    finally { setSending(false); }
  };

  const handleReplyKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== "Enter" || event.shiftKey || event.altKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  if (!loading && tickets.length === 0) return <section className="customer-ticket-empty panel">
    <p className="eyebrow">MY AFTER-SALES</p>
    <h2>暂时没有售后工单</h2>
    <p>当问题需要人工客服处理并创建工单后，处理进度会显示在这里。</p>
    <button className="secondary" onClick={() => void loadTickets()}>刷新状态</button>
    {error && <p className="error">{error}</p>}
  </section>;

  return <section className="customer-ticket-center">
    <section className="panel customer-ticket-list">
      <div className="section-title"><div><p className="eyebrow">MY AFTER-SALES</p><h2>我的售后</h2><p className="muted">Agent 会自动处理明确问题，复杂情况再转人工。</p></div><button className="secondary" onClick={() => void loadTickets()} disabled={loading}>刷新</button></div>
      {loading ? <p className="empty">正在读取售后进度…</p> : tickets.length ? <div className="ticket-list">{tickets.map((ticket) => <button className={`ticket-card ${selectedTicket?.ticket_id === ticket.ticket_id ? "active" : ""}`} key={ticket.ticket_id} onClick={() => void selectTicket(ticket.ticket_id)}><span className="status">{ticket.status}</span><strong>{ticket.ticket_id}</strong><small>{formatDate(ticket.created_at)}</small></button>)}</div> : <p className="empty">当问题需要人工客服处理并创建工单后，处理进度会显示在这里。</p>}
    </section>
    <section className="panel ticket-detail customer-ticket-detail">
      {selectedTicket ? <>
        <div className="section-title"><div><p className="eyebrow">AFTER-SALES CONVERSATION</p><h2>{selectedTicket.ticket_id}</h2><p className="muted">当前状态：{selectedTicket.status}</p></div></div>
        <div className="message-history customer-ticket-messages">{messages.length ? messages.map((message) => <Message key={message.message_id} message={message} />) : <p className="empty">暂时没有消息。</p>}</div>
        {selectedTicket.status === "待客户确认" && <div className="ticket-resolution-actions"><span className="hint">问题已解决？确认后会关闭这张工单。</span><button className="secondary" onClick={() => void confirmResolved()} disabled={sending}>确认已解决</button></div>}
        {selectedTicket.status !== "已关闭" && <><form className="composer customer-ticket-composer" onSubmit={sendFollowUp}><textarea value={reply} onChange={(event) => setReply(event.target.value)} onKeyDown={handleReplyKeyDown} maxLength={4000} placeholder="补充问题或回复 Agent…" /><button disabled={sending || !reply.trim()}>{sending ? "发送中…" : "发送"}</button></form><p className="customer-ticket-hint">Enter 发送 · Shift / Alt + Enter 换行</p></>}
        {selectedTicket.status === "已关闭" && <p className="customer-ticket-hint">这张工单已关闭。如仍需帮助，请在智能客服中发起新的售后请求。</p>}
      </> : <div className="ticket-detail-empty"><h2>查看售后处理进度</h2><p>从左侧选择一张工单，即可看到 Agent 的处理结果并继续追问。</p></div>}
    </section>
    {error && <p className="toast error">{error}</p>}
  </section>;
}

/** 运营只读库存台：用已存在商品表展示业务事实，再把分析工作交给现有 Agent。 */
function OperatorWorkspace({ auth, onSignOut }: { auth: AuthState; onSignOut: () => Promise<void> }) {
  const [products, setProducts] = useState<Product[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [running, setRunning] = useState(false);

  const load = async (): Promise<void> => {
    setLoading(true); setError("");
    try {
      const [laptops, phones] = await Promise.all([listProducts(auth.token, "laptops"), listProducts(auth.token, "phones")]);
      setProducts([...laptops.products, ...phones.products]);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "库存目录暂时不可用"); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [auth.token]);

  const askAgent = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    const text = question.trim();
    if (!text || running) return;
    setRunning(true); setAnswer(""); setError("");
    try {
      await streamChat(auth.token, text, undefined, (event) => {
        if (event.event === "token") setAnswer((value) => value + (event.content ?? ""));
        if (event.event === "done" && !answer) setAnswer(event.answer ?? event.data?.answer ?? "");
      });
    } catch (reason) { setError(reason instanceof Error ? reason.message : "运营 Agent 暂时不可用"); }
    finally { setRunning(false); }
  };

  const totalStock = products.reduce((total, product) => total + product.stock, 0);
  const lowStock = products.filter((product) => product.stock > 0 && product.stock <= 5);
  const outOfStock = products.filter((product) => product.stock <= 0);

  return <Shell title="运营 AI 工作台" subtitle="基于当前商品与库存事实，快速发现缺货风险并向 Agent 查询运营问题。" auth={auth} onSignOut={onSignOut}>
    <section className="metric-grid"><Metric label="目录 SKU" value={loading ? "—" : String(products.length)} /><Metric label="可用库存" value={loading ? "—" : String(totalStock)} /><Metric label="低库存 SKU" value={loading ? "—" : String(lowStock.length)} tone="warning" /><Metric label="缺货 SKU" value={loading ? "—" : String(outOfStock.length)} tone="danger" /></section>
    <div className="operator-grid"><section className="panel inventory-panel"><div className="section-title"><div><p className="eyebrow">INVENTORY OVERVIEW</p><h2>库存概览</h2></div><button className="secondary" onClick={() => void load()}>刷新</button></div>{error && <p className="error">{error}</p>}{loading ? <p className="empty">正在读取商品库存…</p> : <div className="inventory-list">{[...lowStock, ...outOfStock].length ? [...lowStock, ...outOfStock].map((product) => <article key={product.id} className="inventory-row"><div><strong>{product.product_name}</strong><small>{product.brand} · {product.product_type || "商品"}</small></div><span className={product.stock > 0 ? "stock-low" : "stock-empty"}>{product.stock > 0 ? `仅剩 ${product.stock}` : "已缺货"}</span></article>) : <p className="empty">当前目录没有低库存或缺货商品。</p>}</div>}</section>
      <section className="panel operator-ai"><p className="eyebrow">OPERATOR AGENT</p><h2>让 Agent 分析运营问题</h2><p className="muted">例如：哪些商品库存偏低？适合推荐什么替代型号？</p><form className="form-stack" onSubmit={askAgent}><textarea value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={2000} placeholder="输入运营问题" /><button disabled={running}>{running ? "Agent 分析中…" : "开始分析"}</button></form>{(answer || running) && <article className="agent-answer">{answer || "正在分析商品、知识库和可用工具…"}</article>}</section>
    </div>
    <FulfillmentPanel auth={auth} />
  </Shell>;
}

/** 运营登记发货事件；顺丰接入后只替换事件来源，不改订单读取链路。 */
function FulfillmentPanel({ auth }: { auth: AuthState }) {
  const [items, setItems] = useState<Fulfillment[]>([]);
  const [selected, setSelected] = useState<Fulfillment | null>(null);
  const [carrier, setCarrier] = useState("顺丰速运");
  const [trackingNumber, setTrackingNumber] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const load = async (): Promise<void> => {
    setLoading(true); setError("");
    try {
      const next = await listOperatorFulfillments(auth.token);
      setItems(next);
      setSelected((current) => next.find((item) => item.order_no === current?.order_no)
        ?? next.find((item) => item.status === "PENDING_FULFILLMENT") ?? null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "履约队列暂时不可用"); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [auth.token]);

  const ship = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!selected || selected.status !== "PENDING_FULFILLMENT" || !carrier.trim() || !trackingNumber.trim()) return;
    setSaving(true); setError("");
    try {
      await shipFulfillment(auth.token, selected.order_no, carrier.trim(), trackingNumber.trim());
      setTrackingNumber("");
      await load();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "登记发货失败"); }
    finally { setSaving(false); }
  };

  return <section className="panel fulfillment-panel"><div className="section-title"><div><p className="eyebrow">FULFILLMENT QUEUE</p><h2>发货队列</h2><p className="muted">演示环境由运营登记发货；接入顺丰后状态会由物流事件自动更新。</p></div><button className="secondary" onClick={() => void load()} disabled={loading}>刷新</button></div>{error && <p className="error">{error}</p>}{loading ? <p className="empty">正在读取履约队列…</p> : <div className="fulfillment-grid"><div className="fulfillment-list">{items.length ? items.map((item) => <button className={`fulfillment-row ${selected?.order_no === item.order_no ? "active" : ""}`} key={item.order_no} onClick={() => setSelected(item)}><strong>{item.order_no}</strong><span>{item.product_name} ×{item.quantity}</span><small>{item.status === "PENDING_FULFILLMENT" ? "待发货" : item.status === "SHIPPED" ? `${item.carrier} · ${item.tracking_number}` : item.status}</small></button>) : <p className="empty">暂无已付款待处理订单。</p>}</div><form className="form-stack fulfillment-form" onSubmit={ship}>{selected ? <><strong>{selected.order_no}</strong><small>{selected.product_name}</small>{selected.status === "PENDING_FULFILLMENT" ? <><label>承运商<input value={carrier} onChange={(event) => setCarrier(event.target.value)} maxLength={64} /></label><label>运单号<input value={trackingNumber} onChange={(event) => setTrackingNumber(event.target.value)} maxLength={128} placeholder="例如 SF1234567890" /></label><button disabled={saving || !trackingNumber.trim()}>{saving ? "登记中…" : "登记发货"}</button></> : <p className="muted">该订单已发货：{selected.carrier} · {selected.tracking_number}</p>}</> : <p className="empty">选择一笔订单后登记发货。</p>}</form></div>}</section>;
}

function Metric({ label, value, tone = "normal" }: { label: string; value: string; tone?: "normal" | "warning" | "danger" }) {
  return <article className={`metric ${tone}`}><span>{label}</span><strong>{value}</strong></article>;
}

function AgentWorkspace({ auth, onSignOut }: { auth: AuthState; onSignOut: () => Promise<void> }) {
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selectedTicket, setSelectedTicket] = useState<Ticket | null>(null);
  const [messages, setMessages] = useState<TicketMessage[]>([]);
  const [escalation, setEscalation] = useState<TicketEscalation | null>(null);
  const [draft, setDraft] = useState<SupportReplyDraft | null>(null);
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingTicketId, setLoadingTicketId] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "unclaimed" | "mine">("all");
  const [loading, setLoading] = useState(false);
  const refresh = async (): Promise<void> => {
    setLoading(true);
    setError("");
    try {
      const listed = await listTickets(auth.token);
      // 列表响应不能携带归属字段。用已有的范围详情接口确认当前客服自己的工单，
      // 未认领工单仍只会得到摘要，因此不会扩大数据可见范围。
      const loaded = await Promise.all(listed.map(async (ticket) => {
        try {
          const detail = await getTicket(auth.token, ticket.ticket_id);
          return detail.assigned_agent_id === auth.user.id
            ? { ...ticket, assigned_agent_id: auth.user.id }
            : ticket;
        } catch {
          return ticket;
        }
      }));
      // 列表接口只返回四个公开摘要字段；保留本次页面内已经确认的认领状态，
      // 避免刚认领后刷新又被显示为“待认领”。
      setTickets((current) => {
        const knownAssignments = new Map(
          current
            .filter((ticket) => ticket.assigned_agent_id !== undefined)
            .map((ticket) => [ticket.ticket_id, ticket.assigned_agent_id]),
        );
        return loaded.map((ticket) => knownAssignments.has(ticket.ticket_id)
          ? { ...ticket, assigned_agent_id: knownAssignments.get(ticket.ticket_id) }
          : ticket);
      });
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取工单队列");
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => { void refresh(); }, [auth.token]);
  useEffect(() => {
    const timer = window.setInterval(() => { void refresh(); }, 10000);
    return () => window.clearInterval(timer);
  }, [auth.token]);

  const queueTickets = useMemo(() => tickets.filter((ticket) => {
    if (filter === "unclaimed") return ticket.assigned_agent_id == null;
    if (filter === "mine") return ticket.assigned_agent_id === auth.user.id;
    return true;
  }), [auth.user.id, filter, tickets]);
  const queueStats = useMemo(() => ({
    total: tickets.length,
    unclaimed: tickets.filter((ticket) => ticket.assigned_agent_id == null).length,
    mine: tickets.filter((ticket) => ticket.assigned_agent_id === auth.user.id).length,
    aiProcessing: tickets.filter((ticket) => ticket.status === "AI待处理" || ticket.status === "AI处理中").length,
  }), [auth.user.id, tickets]);

  const select = async (ticketId: string): Promise<void> => {
    setError(""); setDraft(null);
    setEscalation(null);
    setLoadingTicketId(ticketId);
    // 先立即展示列表中的安全摘要，避免点击后等待网络请求才有视觉反馈。
    // 正文和消息仍然只在详情接口确认当前客服已认领后加载。
    const summary = tickets.find((ticket) => ticket.ticket_id === ticketId);
    if (summary) {
      setSelectedTicket(summary);
      setMessages([]);
    }
    try {
      const ticket = await getTicket(auth.token, ticketId);
      const ticketEscalation = await getTicketEscalation(auth.token, ticketId);
      // 未认领工单只能读取脱敏摘要，消息接口返回 404 是权限边界，不是加载失败。
      const items = ticket.assigned_agent_id === auth.user.id
        ? await listTicketMessages(auth.token, ticketId)
        : [];
      setSelectedTicket(ticket);
      setEscalation(ticketEscalation);
      setMessages(items);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取工单"); }
    finally { setLoadingTicketId(null); }
  };
  const claim = async (): Promise<void> => {
    if (!selectedTicket) return; setBusy(true); setError("");
    try {
      const result = await claimTicket(auth.token, selectedTicket.ticket_id);
      setTickets((items) => items.map((ticket) => ticket.ticket_id === result.ticket_id
        ? { ...ticket, assigned_agent_id: result.assigned_agent_id }
        : ticket));
      await select(selectedTicket.ticket_id);
      await refresh();
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "认领失败"); } finally { setBusy(false); }
  };
  const createDraft = async (): Promise<void> => {
    if (!selectedTicket) return; setBusy(true); setError("");
    try { const next = await requestReplyDraft(auth.token, selectedTicket.ticket_id); setDraft(next); setContent(next.draft); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法生成草稿"); } finally { setBusy(false); }
  };
  const send = async (event: FormEvent): Promise<void> => {
    event.preventDefault(); if (!selectedTicket || !content.trim()) return; setBusy(true); setError("");
    try { const created = await sendAgentTicketMessage(auth.token, selectedTicket.ticket_id, content.trim(), Boolean(draft)); setMessages((items) => [...items, created]); setContent(""); setDraft(null); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "发送失败"); } finally { setBusy(false); }
  };
  const close = async (): Promise<void> => {
    if (!selectedTicket || busy) return;
    setBusy(true); setError("");
    try {
      await closeTicket(auth.token, selectedTicket.ticket_id);
      await refresh();
      await select(selectedTicket.ticket_id);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法关闭工单"); }
    finally { setBusy(false); }
  };
  const owned = selectedTicket?.assigned_agent_id === auth.user.id;
  const claimable = selectedTicket?.status === "待处理" || selectedTicket?.status === "待人工处理";
  const escalationLabel = escalation?.status === "DELIVERED"
    ? "飞书已送达"
    : escalation?.status === "PENDING" || escalation?.status === "DELIVERING"
      ? "飞书待投递"
      : escalation?.status === "RETRY_WAIT"
        ? "飞书重试中"
        : escalation?.status === "DLQ"
          ? "飞书投递失败"
          : null;

  return <Shell title="客服 AI 工作台" subtitle="Agent 先处理明确问题；只有需要人工判断的工单才进入你的队列。" auth={auth} onSignOut={onSignOut}>
    <section className="agent-metrics">
      <article><span>队列总数</span><strong>{queueStats.total}</strong><small>当前可见工单</small></article>
      <article><span>待认领</span><strong>{queueStats.unclaimed}</strong><small>可以直接认领处理</small></article>
      <article><span>我的工单</span><strong>{queueStats.mine}</strong><small>已由你负责</small></article>
      <article className={queueStats.aiProcessing ? "agent-metric-warning" : ""}><span>Agent 处理中</span><strong>{queueStats.aiProcessing}</strong><small>完成后才会转人工</small></article>
    </section>
    <div className="agent-grid"><section className="panel ticket-queue"><div className="section-title"><div><p className="eyebrow">WORK QUEUE</p><h2>工单队列</h2></div><button className="secondary" onClick={() => void refresh()} disabled={loading}>{loading ? "刷新中…" : "刷新"}</button></div><div className="agent-queue-tabs" role="tablist" aria-label="工单筛选"><button className={filter === "all" ? "active" : "secondary"} onClick={() => setFilter("all")}>全部 {queueStats.total}</button><button className={filter === "unclaimed" ? "active" : "secondary"} onClick={() => setFilter("unclaimed")}>待认领 {queueStats.unclaimed}</button><button className={filter === "mine" ? "active" : "secondary"} onClick={() => setFilter("mine")}>我的 {queueStats.mine}</button></div>{queueTickets.length ? queueTickets.map((ticket) => <button className={`ticket-card ${selectedTicket?.ticket_id === ticket.ticket_id ? "active" : ""}`} key={ticket.ticket_id} onClick={() => void select(ticket.ticket_id)}><span className={`status status-${ticket.status}`}>{ticket.status}</span><strong>{ticket.ticket_id}</strong><small>{ticket.urgency} · {formatDate(ticket.created_at)}{ticket.assigned_agent_id === auth.user.id ? " · 我已认领" : ticket.assigned_agent_id == null ? " · 待认领" : ""}</small><small>{ticket.issue_summary || "暂无问题摘要"}</small></button>) : <div className="agent-empty"><div className="agent-empty-icon">✓</div><h3>{loading ? "正在读取队列" : "当前没有需要人工处理的工单"}</h3><p>{loading ? "正在连接工单服务…" : "客户工单会先由 Agent 自动处理。只有知识不足、客户明确要求人工或涉及订单/支付争议时，才会进入这里。"}</p><small>演示建议：使用 customer 账号在智能客服中描述一个售后问题，创建工单后再刷新本页面。</small></div>}</section>
      <section className="panel ticket-detail">{selectedTicket ? <>
        <div className="section-title"><div><p className="eyebrow">TICKET DETAIL</p><h2>{selectedTicket.ticket_id}</h2><p className="muted">{selectedTicket.issue || selectedTicket.issue_summary || "尚未认领，先认领后查看详情"}</p></div><div className="ticket-detail-actions">
          {escalationLabel && <span className={`status escalation-status escalation-${escalation?.status?.toLowerCase()}`}>{escalationLabel}</span>}
          {loadingTicketId === selectedTicket.ticket_id ? <span className="status">正在读取详情…</span> : owned ? <><span className="status">{selectedTicket.status === "已关闭" ? "已关闭" : "人工处理中"}</span>{selectedTicket.status !== "已关闭" && <button className="secondary" onClick={() => void close()} disabled={busy}>关闭工单</button>}</> : claimable ? <button onClick={() => void claim()} disabled={busy}>认领工单</button> : <span className="status">Agent 处理中</span>}
        </div></div>
        {loadingTicketId === selectedTicket.ticket_id ? <p className="empty">正在确认工单权限…</p> : owned ? <>
          <div className="message-history">{messages.map((message) => <Message key={message.message_id} message={message} />)}</div>
          {selectedTicket.status !== "已关闭" && <><div className="draft-actions"><button className="secondary" onClick={() => void createDraft()} disabled={busy}>✨ 生成 AI 回复草稿</button>{draft && <span className="hint">引用 {draft.knowledge_references.length} 条知识资料{draft.needs_human_follow_up ? " · 需补充依据" : ""}</span>}</div><form className="composer" onSubmit={send}><textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder="编辑后发送给客户" maxLength={4000} /><button disabled={busy}>{busy ? "处理中…" : "发送回复"}</button></form></>}
        </> : <p className="empty">{claimable ? "认领后可查看消息、生成 AI 草稿并回复客户。" : "Agent 正在处理这张工单，转人工后即可认领。"}</p>}
      </> : <div className="ticket-detail-empty"><div className="agent-detail-icon">AI</div><h2>选择一张工单</h2><p>左侧显示当前客服可处理的工单。认领后可以查看完整对话、让 AI 生成回复草稿，并由你确认后发送。</p></div>}</section>
    </div>{error && <p className="toast error">{error}</p>}
  </Shell>;
}

function Message({ message }: { message: TicketMessage }) {
  const label = message.author_role === "customer" ? "客户" : message.author_role === "ai" ? "AI" : "客服";
  return <article className={`message ${message.author_role}`}><header><strong>{label}</strong><small>{formatDate(message.created_at)}</small></header><p>{message.content}</p>{message.ai_assisted && <small className="ai-note">AI 辅助草稿，经客服确认发送</small>}</article>;
}

type FinanceQueueTab = "pending" | "processing" | "succeeded" | "all";

function financeStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    PENDING_FINANCE_APPROVAL: "待财务审批",
    PENDING_CONFIRMATION: "待客户确认",
    PENDING_MERCHANT_REVIEW: "商家审核中",
    PROCESSING: "退款处理中",
    SUCCEEDED: "退款成功",
    COMPLETED: "退款成功",
    FAILED: "退款失败",
    REJECTED: "已驳回",
    PENDING: "支付待确认",
    NOT_PAID: "未支付",
  };
  return labels[status] ?? "待核查";
}

function financeElapsedLabel(ageSeconds: number): string {
  const seconds = Math.max(0, Math.floor(ageSeconds));
  if (seconds < 60) return "刚刚";
  const minutes = Math.floor(seconds / 60);
  const days = Math.floor(minutes / (24 * 60));
  const hours = Math.floor((minutes % (24 * 60)) / 60);
  const remainingMinutes = minutes % 60;
  if (days > 0) return hours > 0 ? `${days}天${hours}小时` : `${days}天`;
  if (hours > 0) return remainingMinutes > 0 ? `${hours}小时${remainingMinutes}分钟` : `${hours}小时`;
  return `${minutes}分钟`;
}

function FinanceWorkspace({ auth, onSignOut }: { auth: AuthState; onSignOut: () => Promise<void> }) {
  const [refunds, setRefunds] = useState<FinanceRefund[]>([]);
  const [anomalies, setAnomalies] = useState<FinanceAnomaly[]>([]);
  const [summary, setSummary] = useState<FinanceAnomalySummary | null>(null);
  const [showSummaryPage, setShowSummaryPage] = useState(false);
  const [financeTab, setFinanceTab] = useState<FinanceQueueTab>("pending");
  const [selectedRefundId, setSelectedRefundId] = useState<string | null>(null);
  const [summaryBusy, setSummaryBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [decisionBusy, setDecisionBusy] = useState<string | null>(null);
  const [refreshingRefundId, setRefreshingRefundId] = useState<string | null>(null);
  const load = async (): Promise<void> => {
    setLoading(true); setError("");
    try {
      const [nextRefunds, nextAnomalies] = await Promise.all([
        listFinanceRefunds(auth.token),
        listFinanceAnomalies(auth.token),
      ]);
      setRefunds(nextRefunds);
      setAnomalies(nextAnomalies);
      setSummary(null);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "退款队列暂时不可用"); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [auth.token]);

  useEffect(() => {
    const syncSummaryRoute = (): void => {
      setShowSummaryPage(new URLSearchParams(window.location.search).get("page") === "finance-summary" && summary !== null);
    };
    window.addEventListener("popstate", syncSummaryRoute);
    return () => window.removeEventListener("popstate", syncSummaryRoute);
  }, [summary]);

  const decide = async (refund: FinanceRefund, action: "approve" | "reject"): Promise<void> => {
    const decisionNote = window.prompt(action === "approve" ? "审批备注（可选）" : "驳回原因（建议填写）", "") ?? "";
    if (action === "reject" && !decisionNote.trim()) return;
    setDecisionBusy(refund.refund_id); setError("");
    try {
      const result = action === "approve"
        ? await approveFinanceRefund(auth.token, refund.refund_id, decisionNote.trim(), crypto.randomUUID())
        : await rejectFinanceRefund(auth.token, refund.refund_id, decisionNote.trim(), crypto.randomUUID());
      setRefunds((items) => items.map((item) => item.refund_id === refund.refund_id ? {
        ...item,
        status: result.status,
        finance_decision_note: decisionNote.trim(),
        finance_decided_at: new Date().toISOString(),
      } : item));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "退款决策失败"); }
    finally { setDecisionBusy(null); await load(); }
  };

  const refreshRefund = async (refund: FinanceRefund): Promise<void> => {
    setRefreshingRefundId(refund.refund_id); setError("");
    try { await refreshFinanceRefund(auth.token, refund.refund_id); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "退款状态暂时无法确认"); }
    finally { setRefreshingRefundId(null); await load(); }
  };

  const pending = refunds.filter((refund) => refund.status === "PENDING_FINANCE_APPROVAL").length;
  const processing = refunds.filter((refund) => refund.status === "PROCESSING").length;
  const succeeded = refunds.filter((refund) => refund.status === "SUCCEEDED").length;
  const visibleRefunds = useMemo(() => refunds.filter((refund) => {
    if (financeTab === "pending") return refund.status === "PENDING_FINANCE_APPROVAL";
    if (financeTab === "processing") return refund.status === "PROCESSING";
    if (financeTab === "succeeded") return refund.status === "SUCCEEDED" || refund.status === "COMPLETED";
    return true;
  }), [financeTab, refunds]);
  const selectedRefund = refunds.find((refund) => refund.refund_id === selectedRefundId) ?? null;
  const anomalyLabel = (anomaly: FinanceAnomaly): string => {
    if (anomaly.anomaly_type === "REFUND_PENDING_APPROVAL") return "退款待审批";
    if (anomaly.anomaly_type === "REFUND_FAILED") return "退款失败";
    if (anomaly.anomaly_type === "REFUND_PROCESSING_TIMEOUT") return "退款处理超时";
    if (anomaly.anomaly_type === "PAYMENT_PENDING_TIMEOUT") return "支付长时间未完成";
    return "支付处理超时";
  };

  const generateSummary = async (): Promise<void> => {
    setSummaryBusy(true); setError(""); setSummary(null);
    try {
      const nextSummary = await summarizeFinanceAnomalies(auth.token);
      setSummary(nextSummary);
      window.history.pushState(null, "", `${window.location.pathname}?page=finance-summary`);
      setShowSummaryPage(true);
    }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Agent 摘要暂时不可用"); }
    finally { setSummaryBusy(false); }
  };

  if (showSummaryPage && summary) {
    return <FinanceSummaryPage
      auth={auth}
      summary={summary}
      anomalies={anomalies}
      onBack={() => {
        window.history.pushState(null, "", `${window.location.pathname}?page=finance`);
        setShowSummaryPage(false);
      }}
      onSignOut={onSignOut}
    />;
  }

  return <Shell title="财务工作台" subtitle="查看退款队列和支付结果；审批、支付和退款状态变化必须经过确定性业务服务。" auth={auth} onSignOut={onSignOut}>
    <section className="metric-grid"><Metric label="退款总数" value={loading ? "—" : String(refunds.length)} /><Metric label="待处理" value={loading ? "—" : String(pending)} tone={pending ? "warning" : "normal"} /><Metric label="处理中" value={loading ? "—" : String(processing)} /><Metric label="已成功" value={loading ? "—" : String(succeeded)} /><Metric label="资金异常" value={loading ? "—" : String(anomalies.length)} tone={anomalies.length ? "danger" : "normal"} /></section>
    <section className="panel finance-anomaly-panel"><div className="section-title"><div><p className="eyebrow">EXCEPTION QUEUE</p><h2>资金异常</h2><p className="muted">仅根据本地支付和退款事实筛选；这里不会自动改变资金状态。</p></div><button className="secondary" onClick={() => void generateSummary()} disabled={loading || summaryBusy || !anomalies.length}>{summaryBusy ? "生成中…" : "AI 分析"}</button></div>{loading ? <p className="empty">正在扫描资金异常…</p> : anomalies.length ? <div className="finance-anomaly-list">{anomalies.map((anomaly) => <article className="finance-anomaly-row" key={`${anomaly.anomaly_type}-${anomaly.reference_id}`}><div><strong>{anomalyLabel(anomaly)}</strong><small>{anomaly.order_no} · {formatDate(anomaly.occurred_at)}</small></div><div><span className="status finance-anomaly-status">{financeStatusLabel(anomaly.status)}</span><strong>¥{(anomaly.amount_cents / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</strong></div><p>{anomaly.reason || "需要财务核查本地事实和外部渠道状态"} · 已持续 {financeElapsedLabel(anomaly.age_seconds)}</p></article>)}</div> : <div className="role-empty"><div className="agent-empty-icon">✓</div><h3>当前没有资金异常</h3><p>支付和退款状态目前均在可接受范围内。</p></div>}</section>
    <section className="panel finance-refund-panel"><div className="section-title"><div><p className="eyebrow">REFUND QUEUE</p><h2>退款队列</h2><p className="muted">只处理进入财务审批范围的退款；金额和订单事实由服务端确定。</p></div><button className="secondary" onClick={() => void load()} disabled={loading || decisionBusy !== null}>{loading ? "读取中…" : "刷新"}</button></div>{error && <p className="error">{error}</p>}
      <div className="finance-queue-tabs" role="tablist" aria-label="退款状态筛选">{([ ["pending", `待审批 ${pending}`], ["processing", `处理中 ${processing}`], ["succeeded", `已完成 ${succeeded}`], ["all", `全部 ${refunds.length}`] ] as Array<[FinanceQueueTab, string]>).map(([tab, label]) => <button key={tab} className={financeTab === tab ? "active" : "secondary"} role="tab" aria-selected={financeTab === tab} onClick={() => setFinanceTab(tab)}>{label}</button>)}</div>
      {loading ? <p className="empty">正在读取退款记录…</p> : visibleRefunds.length ? <div className="finance-refund-layout"><div className="finance-refund-list">{visibleRefunds.map((refund) => <article className={`finance-refund-row${selectedRefundId === refund.refund_id ? " selected" : ""}`} key={refund.refund_id}><div><strong>{refund.order_no}</strong><small>申请于 {formatDate(refund.requested_at)}</small></div><div><span className={`status finance-status-${refund.status}`}>{financeStatusLabel(refund.status)}</span><strong>¥{(refund.amount_cents / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</strong></div><p>{refund.reason || "客户未填写原因"}</p><div className="finance-row-footer"><button className="text-button" onClick={() => setSelectedRefundId(refund.refund_id)}>{selectedRefundId === refund.refund_id ? "已查看详情" : "查看详情"}</button>{refund.status === "PENDING_FINANCE_APPROVAL" && <div className="finance-decision-actions"><button disabled={decisionBusy !== null} onClick={() => void decide(refund, "approve")}>批准并发起退款</button><button className="secondary" disabled={decisionBusy !== null} onClick={() => void decide(refund, "reject")}>驳回</button></div>}{refund.status === "PROCESSING" && <div className="finance-decision-actions"><button className="secondary" disabled={refreshingRefundId === refund.refund_id} onClick={() => void refreshRefund(refund)}>{refreshingRefundId === refund.refund_id ? "查询中…" : "刷新退款状态"}</button></div>}</div></article>)}</div>{selectedRefund && <aside className="finance-refund-detail"><div className="section-title"><div><p className="eyebrow">REFUND DETAIL</p><h3>退款详情</h3></div><button className="text-button" onClick={() => setSelectedRefundId(null)}>关闭</button></div><dl><div><dt>订单</dt><dd>{selectedRefund.order_no}</dd></div><div><dt>状态</dt><dd>{financeStatusLabel(selectedRefund.status)}</dd></div><div><dt>金额</dt><dd>¥{(selectedRefund.amount_cents / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</dd></div><div><dt>申请时间</dt><dd>{formatDate(selectedRefund.requested_at)}</dd></div><div><dt>备注</dt><dd>{selectedRefund.reason || "客户未填写原因"}</dd></div></dl><details><summary>技术信息</summary><code>{selectedRefund.refund_id}</code></details></aside>}</div> : <div className="role-empty"><div className="agent-empty-icon">✓</div><h3>当前没有符合条件的退款</h3><p>{financeTab === "all" ? "客户提交退款申请后，记录会出现在这里。" : "切换其他状态标签查看全部退款。"}</p></div>}
    </section>
  </Shell>;
}

function FinanceSummaryPage({
  auth,
  summary,
  anomalies,
  onBack,
  onSignOut,
}: {
  auth: AuthState;
  summary: FinanceAnomalySummary;
  anomalies: FinanceAnomaly[];
  onBack: () => void;
  onSignOut: () => Promise<void>;
}) {
  const anomalyLabel = (anomaly: FinanceAnomaly): string => {
    if (anomaly.anomaly_type === "REFUND_PENDING_APPROVAL") return "退款待审批";
    if (anomaly.anomaly_type === "REFUND_FAILED") return "退款失败";
    if (anomaly.anomaly_type === "REFUND_PROCESSING_TIMEOUT") return "退款处理超时";
    if (anomaly.anomaly_type === "PAYMENT_PENDING_TIMEOUT") return "支付长时间未完成";
    return "支付处理超时";
  };

  return <Shell title="资金异常 Agent 摘要" subtitle="基于本次扫描到的支付和退款事实生成；摘要不会直接改变资金状态。" auth={auth} onSignOut={onSignOut}>
    <button className="secondary finance-summary-back" onClick={onBack}>← 返回资金异常队列</button>
    <section className="finance-summary-page">
      <article className="panel finance-summary-hero">
        <p className="eyebrow">FINANCE AGENT REPORT</p>
        <h2>本次核查摘要</h2>
        <p className="agent-summary-text">{summary.summary}</p>
        <small>基于 {summary.anomaly_count} 条扫描事实 · 生成于 {formatDate(summary.generated_at)}</small>
      </article>
      <section className="panel finance-summary-facts">
        <div className="section-title"><div><p className="eyebrow">SOURCE FACTS</p><h2>对应异常事实</h2></div><span className="status">只读</span></div>
        {anomalies.length ? <div className="finance-anomaly-list">{anomalies.map((anomaly) => <article className="finance-anomaly-row" key={`${anomaly.anomaly_type}-${anomaly.reference_id}`}><div><strong>{anomalyLabel(anomaly)}</strong><small>{anomaly.order_no} · {formatDate(anomaly.occurred_at)}</small></div><div><span className="status finance-anomaly-status">{financeStatusLabel(anomaly.status)}</span><strong>¥{(anomaly.amount_cents / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2 })}</strong></div><p>{anomaly.reason || "需要财务核查本地事实和外部渠道状态"} · 已持续 {financeElapsedLabel(anomaly.age_seconds)}</p></article>)}</div> : <p className="empty">本次没有可展示的异常事实。</p>}
      </section>
    </section>
  </Shell>;
}

function UnavailableWorkspace({ auth, onSignOut }: { auth: AuthState; onSignOut: () => Promise<void> }) {
  const roleConfig = {
    finance: {
      eyebrow: "FINANCE CONTROL DESK",
      title: "财务工作台",
      subtitle: "处理退款审批、支付异常和对账差异；财务不修改订单事实。",
      available: ["退款申请审批", "支付失败与回调异常", "对账差异跟进"],
      next: "财务命令 API 接入后，这里会显示待审批申请和异常队列。",
    },
    admin: {
      eyebrow: "ADMIN CONSOLE",
      title: "系统管理台",
      subtitle: "管理用户、角色授权和系统配置；管理员不处理日常订单或退款。",
      available: ["用户与角色管理", "系统配置", "权限与审计元数据"],
      next: "管理 API 接入后，这里会开放配置和审计入口。",
    },
  } as const;
  const config = roleConfig[auth.user.role as "finance" | "admin"] ?? {
    eyebrow: "INTERNAL WORKSPACE",
    title: "内部工作台",
    subtitle: "当前角色的业务入口正在接入。",
    available: [],
    next: "请联系管理员配置角色权限。",
  };
  return <Shell title={config.title} subtitle={config.subtitle} auth={auth} onSignOut={onSignOut}>
    <section className="role-workspace-hero"><p className="eyebrow">{config.eyebrow}</p><h2>{auth.user.username}，这是你的工作入口</h2><p className="muted">系统会根据角色限制数据范围和可执行命令。当前页面不会显示没有后端能力支撑的按钮。</p></section>
    <section className="role-capability-grid">{config.available.map((item) => <article className="panel role-capability" key={item}><span className="role-capability-icon">{item.slice(0, 1)}</span><h3>{item}</h3><p>由受控业务服务处理，操作记录会关联当前用户和请求。</p><span className="role-capability-status">即将开放</span></article>)}</section>
    <section className="panel role-next-step"><p className="eyebrow">NEXT STEP</p><h2>当前阶段</h2><p className="muted">{config.next}</p><p className="hint">客服和运营角色已有可操作页面；财务和管理员功能正在按各自权限接入。</p></section>
  </Shell>;
}
