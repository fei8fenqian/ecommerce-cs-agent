import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  AuthState,
  Cart,
  CheckoutOrder,
  Fulfillment,
  Product,
  ProductCatalogPage,
  ProductDetail,
  CustomerOrder,
  SessionItem,
  SupportReplyDraft,
  Ticket,
  TicketMessage,
  addCartItem,
  askPublicAssistant,
  cancelCheckout,
  claimTicket,
  checkoutCart,
  createCheckout,
  deleteCartItem,
  deleteSession,
  getCart,
  getSession,
  getProductDetail,
  getTicket,
  listSessions,
  listProducts,
  listMyOrders,
  listMyCheckoutOrders,
  listOperatorFulfillments,
  listTicketMessages,
  listTickets,
  requestReplyDraft,
  refreshCheckoutPayment,
  resumeCheckout,
  register,
  sendCustomerTicketMessage,
  sendAgentTicketMessage,
  shipFulfillment,
  signIn,
  signOut,
  streamChat,
  updateCartItem,
} from "./api";

const STORAGE_KEY = "ecommerce-agent.auth";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  sequenceNo?: number;
};

function toChatMessages(
  sessionId: string,
  messages: Array<{ role: string; content?: string; sequence_no?: number }>,
): ChatMessage[] {
  // 将会话 API 的持久化消息转换为聊天页面需要的显示数据。
  return messages
    .filter((message) => (message.role === "user" || message.role === "assistant") && Boolean(message.content))
    .map((message, index) => ({
      id: `history-${sessionId}-${message.sequence_no ?? index}`,
      role: message.role as "user" | "assistant",
      content: message.content ?? "",
      sequenceNo: message.sequence_no,
    }));
}

function loadAuth(): AuthState | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    const auth = raw ? (JSON.parse(raw) as AuthState) : null;
    if (!auth?.token || !auth.user || isJwtExpired(auth.token)) {
      window.localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return auth;
  } catch {
    window.localStorage.removeItem(STORAGE_KEY);
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
  if (auth) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(auth));
  else window.localStorage.removeItem(STORAGE_KEY);
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

  const onSignedIn = (nextAuth: AuthState): void => {
    saveAuth(nextAuth);
    setAuth(nextAuth);
  };

  const onSignOut = async (): Promise<void> => {
    if (auth) await signOut(auth.token).catch(() => undefined);
    saveAuth(null);
    returnToPublicCatalog();
    setAuth(null);
  };

  if (!auth) return <PublicStorefront onSignedIn={onSignedIn} />;
  if (auth.user.role === "customer") return <CustomerWorkspace auth={auth} onSignOut={onSignOut} />;
  if (auth.user.role === "agent") return <AgentWorkspace auth={auth} onSignOut={onSignOut} />;
  if (auth.user.role === "operator") return <OperatorWorkspace auth={auth} onSignOut={onSignOut} />;
  return <UnavailableWorkspace auth={auth} onSignOut={onSignOut} />;
}

/** 未登录用户先看到公开商城；交易和个人数据入口再要求认证。 */
function PublicStorefront({ onSignedIn }: { onSignedIn: (auth: AuthState) => void }) {
  const parameters = new URLSearchParams(window.location.search);
  const initialCategory = parameters.get("category");
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
  const openAssistant = (question = ""): void => { setAssistantQuestion(question); setPage("assistant"); };
  if (page === "login") return <LoginPage onSignedIn={finishSignIn} onBack={() => { window.history.replaceState(null, "", window.location.pathname); setPage("catalog"); }} />;

  return <main className="storefront">
    <header className="store-topbar">
      <button className="store-brand" onClick={() => setPage("catalog")}><span>G</span><strong>Geex Digital</strong></button>
      <nav aria-label="商城导航">{page !== "catalog" && <button onClick={() => setPage("catalog")}>商品目录</button>}<button onClick={() => openAssistant()}>智能客服</button><button onClick={() => openLogin("cart")}>购物车</button><button onClick={() => openLogin("orders")}>我的订单</button><button onClick={() => openLogin("tickets")}>售后服务</button><i /> <span>你好，</span><button className="store-login" onClick={() => openLogin("catalog")}>请登录</button><button onClick={() => openLogin("catalog")}>免费注册</button></nav>
    </header>
    <section className="store-content">
      {page === "catalog" ? <ProductCatalog onView={openProduct} /> : page === "product" && detailTarget ? <ProductDetailPage category={detailTarget.category} productId={detailTarget.productId} onBack={() => setPage("catalog")} onAsk={(product, question) => openAssistant(question || `我想了解 ${product.product_name}`)} onRequireLogin={() => openLogin("product")} /> : <PublicAssistantPage initialQuestion={assistantQuestion} onBrowseProducts={() => setPage("catalog")} />}
    </section>
  </main>;
}

/** 已登录客户沿用公开商城外观，仅在交易与个人入口中使用认证身份。 */
function CustomerStorefront({
  auth,
  page,
  detailTarget,
  initialQuery,
  onNavigate,
  onViewProduct,
  onBackToCatalog,
  onAskProduct,
  onSignOut,
}: {
  auth: AuthState;
  page: "catalog" | "product" | "orders" | "cart" | "tickets";
  detailTarget: { category: "laptops" | "phones" | "components"; productId: string } | null;
  initialQuery: string;
  onNavigate: (page: "service" | "catalog" | "product" | "orders" | "cart" | "tickets") => void;
  onViewProduct: (category: "laptops" | "phones" | "components", productId: string) => void;
  onBackToCatalog: () => void;
  onAskProduct: (product: Product, question?: string) => void;
  onSignOut: () => Promise<void>;
}) {
  const navigate = (nextPage: "service" | "catalog" | "orders" | "cart" | "tickets"): void => {
    if (nextPage === "catalog") window.history.pushState(null, "", `${window.location.pathname}?page=catalog`);
    onNavigate(nextPage);
  };
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
      {page === "catalog" ? <ProductCatalog auth={auth} initialQuery={initialQuery} onView={onViewProduct} />
        : page === "product" && detailTarget ? <ProductDetailPage auth={auth} category={detailTarget.category} productId={detailTarget.productId} onBack={onBackToCatalog} onAsk={onAskProduct} />
          : page === "cart" ? <CartPage auth={auth} />
            : page === "orders" ? <OrderList auth={auth} />
              : <CustomerTicketCenter auth={auth} />}
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
  const detailCategoryFromUrl = new URLSearchParams(window.location.search).get("category");
  const detailProductIdFromUrl = new URLSearchParams(window.location.search).get("product");
  const [page, setPage] = useState<"service" | "catalog" | "product" | "orders" | "cart" | "tickets">(() =>
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
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [sessionsExpanded, setSessionsExpanded] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [streamStatus, setStreamStatus] = useState("");
  const [editingMessageId, setEditingMessageId] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const chatHistoryRef = useRef<HTMLDivElement>(null);
  const smoothScrollOnNextMessageRef = useRef(false);
  const scrollToOpenedSessionRef = useRef(false);
  const selectedSessionIdRef = useRef<string | undefined>(undefined);
  const sessionLoadRequestRef = useRef(0);
  const sessionCacheRef = useRef(new Map<string, ChatMessage[]>());
  const pendingUrlSessionRef = useRef(new URLSearchParams(window.location.search).get("session") ?? undefined);

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
    sessionLoadRequestRef.current += 1;
    selectedSessionIdRef.current = undefined;
    replaceSessionUrl(undefined);
    setPage("service");
    setSessionId(undefined);
    setChatMessages([]);
    setQuery("");
    setError("");
    setEditingMessageId(null);
  };

  const openSession = async (nextSessionId: string): Promise<void> => {
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

  const submitChatMessage = async (
    submittedText = query,
    replaceFromSequence?: number,
  ): Promise<void> => {
    const text = submittedText.trim();
    if (!text || busy) return;
    // 用户发送后将当前会话平滑带到新消息；后续 SSE token 继续贴住最新回复。
    smoothScrollOnNextMessageRef.current = true;
    setBusy(true); setError(""); setStreamStatus("正在思考…"); setQuery(""); setEditingMessageId(null);
    const assistantId = `assistant-${Date.now()}`;
    if (replaceFromSequence !== undefined) {
      setChatMessages((items) => items.filter((message) => (message.sequenceNo ?? -1) < replaceFromSequence));
    }
    setChatMessages((items) => [...items, { id: `user-${Date.now()}`, role: "user", content: text }, { id: assistantId, role: "assistant", content: "" }]);
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
            setChatMessages((items) => items.map((message) => message.id === assistantId ? { ...message, content: message.content + (event.content ?? "") } : message));
          }
          return;
        }
        if (event.event === "done") {
          const completedAnswer = event.answer ?? event.data?.answer;
          if (selectedSessionIdRef.current === activeSessionId) {
            if (completedAnswer) setChatMessages((items) => items.map((message) => message.id === assistantId && !message.content ? { ...message, content: completedAnswer } : message));
            setStreamStatus("");
          }
        }
      }, replaceFromSequence);
      setSessions(await listSessions(auth.token));
      if (activeSessionId && selectedSessionIdRef.current === activeSessionId) {
        const persisted = await getSession(auth.token, activeSessionId);
        const persistedMessages = toChatMessages(persisted.session_id, persisted.messages);
        sessionCacheRef.current.set(persisted.session_id, persistedMessages);
        setChatMessages(persistedMessages);
      }
    } catch (reason) {
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

  const saveEditedMessage = (message: ChatMessage): void => {
    if (message.sequenceNo === undefined) return;
    void submitChatMessage(editingValue, message.sequenceNo);
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
    setPage("service");
    setQuery(question.trim() || `我想了解 ${product.product_name}，请介绍它的配置、适用场景和库存情况。`);
  };

  // 商品浏览与交易入口使用与游客相同的商城壳；只有智能客服需要会话侧栏。
  const storefront = page !== "service" ? <CustomerStorefront
      auth={auth}
      page={page}
      detailTarget={detailTarget}
      initialQuery={catalogQueryFromUrl}
      onNavigate={setPage}
      onViewProduct={openProductDetail}
      onBackToCatalog={returnToCatalog}
      onAskProduct={askAboutProduct}
      onSignOut={onSignOut}
    /> : null;

  return storefront ?? <main className={`customer-chat-app${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
    <aside className="chat-sidebar">
      <div className="chat-brand"><span>G</span><strong>Geex AI</strong><button className="sidebar-toggle" aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"} onClick={() => setSidebarCollapsed((value) => !value)}>{sidebarCollapsed ? "›" : "‹"}</button></div>
      <button className="new-chat-button" onClick={startNewChat}>＋ 新建对话</button>
      <nav className="chat-page-nav" aria-label="客户服务导航">
        <button className={page === "catalog" ? "active" : "secondary"} onClick={() => setPage("catalog")}>商品目录</button>
        <button className={page === "cart" ? "active" : "secondary"} onClick={() => setPage("cart")}>购物车</button>
        <button className={page === "orders" ? "active" : "secondary"} onClick={() => setPage("orders")}>我的订单</button>
        <button className={page === "tickets" ? "active" : "secondary"} onClick={() => setPage("tickets")}>我的售后</button>
      </nav>
      <section className="sidebar-sessions"><button className="sidebar-section-toggle" onClick={() => setSessionsExpanded((value) => !value)}><span>最近对话</span><span>{sessionsExpanded ? "⌃" : "⌄"}</span></button>{sessionsExpanded && (sessions.length ? sessions.slice(0, 10).map((session) => <div className={`session-item ${sessionId === session.session_id ? "active" : ""}`} key={session.session_id}><a className="session-row" href={sessionUrl(session.session_id)} onClick={(event) => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); void openSession(session.session_id); }}><span>{session.title || "新对话"}</span><small>{session.message_count} 条消息</small></a><button className="session-delete" aria-label={`删除会话：${session.title || "新对话"}`} onClick={() => void removeSession(session.session_id)}>×</button></div>) : <p className="sidebar-empty">暂无历史对话</p>)}</section>
      <div className="chat-account"><span>{auth.user.username}</span><button className="text-button" onClick={() => void onSignOut()}>退出</button></div>
    </aside>
    <section className="chat-main">
      <header className="chat-main-header"><div><strong>{page === "service" ? "智能客服" : page === "catalog" ? "商品目录" : page === "product" ? "商品详情" : page === "orders" ? "我的订单" : page === "cart" ? "购物车" : "我的售后"}</strong><span>{page === "service" ? (sessionId ? "当前会话" : "新对话") : "Geex Digital"}</span></div><span className="role-badge">客户服务台</span></header>
      {page === "catalog" ? <div className="customer-page-scroll"><ProductCatalog initialQuery={catalogQueryFromUrl} auth={auth} onView={openProductDetail} /></div> : page === "product" && detailTarget ? <div className="customer-page-scroll"><ProductDetailPage auth={auth} category={detailTarget.category} productId={detailTarget.productId} onBack={returnToCatalog} onAsk={askAboutProduct} /></div> : page === "orders" ? <div className="customer-page-scroll"><OrderList auth={auth} /></div> : page === "cart" ? <div className="customer-page-scroll"><CartPage auth={auth} /></div> : page === "tickets" ? <div className="customer-page-scroll"><CustomerTicketCenter auth={auth} /></div> : <section className="chat-canvas">
        <div ref={chatHistoryRef} className="chat-history chatgpt-history">{chatMessages.length === 0 ? <div className="chat-welcome"><p className="eyebrow">GEEX DIGITAL · AI ASSISTANT</p><h1>今天想解决什么问题？</h1><p>我可以介绍商品、查询已归属订单，也能帮你发起售后工单。</p><div className="prompt-grid"><button className="prompt-card" onClick={() => setQuery("帮我推荐一台预算 5000 元左右的笔记本")}>推荐一台预算 5000 元的笔记本</button><button className="prompt-card" onClick={() => setQuery("帮我查询订单物流")}>查询我的订单物流</button><button className="prompt-card" onClick={() => setQuery("哪些手机目前有库存？")}>查询有库存的手机</button></div></div> : chatMessages.map((message) => <article className={`bubble ${message.role}${!message.content ? " thinking" : ""}${editingMessageId === message.id ? " editing" : ""}`} key={message.id}><div className="message-content">{message.role === "user" && editingMessageId === message.id ? <div className="message-edit"><textarea value={editingValue} onChange={(event) => setEditingValue(event.target.value)} maxLength={2000} autoFocus /></div> : message.content ? message.role === "assistant" ? <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown> : message.content : streamStatus || "正在思考…"}</div>{message.role === "user" && message.sequenceNo !== undefined && !busy && <div className="message-actions">{editingMessageId === message.id ? <><button className="secondary" onClick={() => { setEditingMessageId(null); setEditingValue(""); }}>取消</button><button onClick={() => saveEditedMessage(message)} disabled={!editingValue.trim()}>生成</button></> : <button className="message-edit-button" onClick={() => { setEditingMessageId(message.id); setEditingValue(message.content); }}>编辑</button>}</div>}</article>)}</div>
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

function ProductCatalog({ auth, onView, initialQuery = "" }: { auth?: AuthState; onView: (category: "laptops" | "phones" | "components", productId: string) => void; initialQuery?: string }) {
  const [category, setCategory] = useState<"laptops" | "phones" | "components">("laptops");
  const [query, setQuery] = useState(initialQuery);
  const [products, setProducts] = useState<Product[]>([]);
  const [catalogPage, setCatalogPage] = useState<ProductCatalogPage | null>(null);
  const [brand, setBrand] = useState("");
  const [componentCategory, setComponentCategory] = useState("");
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
  useEffect(() => { void load(category, query, 1, "", ""); }, [auth?.token]);
  const selectCategory = (nextCategory: "laptops" | "phones" | "components"): void => {
    setCategory(nextCategory); setBrand(""); setComponentCategory("");
    void load(nextCategory, query, 1, "", "");
  };
  const search = (event: FormEvent): void => { event.preventDefault(); void load(category, query, 1, brand, componentCategory); };
  const selectBrand = (nextBrand: string): void => { setBrand(nextBrand); void load(category, query, 1, nextBrand, componentCategory); };
  const selectComponentCategory = (nextComponentCategory: string): void => { setComponentCategory(nextComponentCategory); void load(category, query, 1, brand, nextComponentCategory); };
  const hasPreviousPage = (catalogPage?.page ?? 1) > 1;
  const hasNextPage = catalogPage !== null && catalogPage.page * catalogPage.page_size < catalogPage.total;
  return <section className="catalog"><header className="catalog-header"><div><p className="eyebrow">PRODUCT CATALOG</p><h2>发现适合你的数码产品</h2><p className="muted">共 {catalogPage?.total ?? 0} 件商品；价格以当前系统数据为准。</p></div><form className="catalog-search" onSubmit={search}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索品牌或商品名称" maxLength={100} /><button>搜索</button></form></header><div className="catalog-tabs">{PRODUCT_CATEGORIES.map(([value, label]) => <button key={value} className={category === value ? "active" : "secondary"} onClick={() => selectCategory(value)}>{label}</button>)}</div>{catalogPage && category !== "components" && catalogPage.brands.length > 0 && <div className="catalog-filters"><button className={!brand ? "active" : "secondary"} onClick={() => selectBrand("")}>全部品牌</button>{catalogPage.brands.map((item) => <button key={item} className={brand === item ? "active" : "secondary"} onClick={() => selectBrand(item)}>{item}</button>)}</div>}{catalogPage && category === "components" && <div className="catalog-filters"><button className={!componentCategory ? "active" : "secondary"} onClick={() => selectComponentCategory("")}>全部配件</button>{Object.entries(catalogPage.component_categories).map(([value, label]) => <button key={value} className={componentCategory === value ? "active" : "secondary"} onClick={() => selectComponentCategory(value)}>{label}</button>)}</div>}{error && <p className="error">{error}</p>}<div className="product-grid">{loading ? <p className="empty">正在读取商品目录…</p> : products.length ? products.map((product) => <a className="product-card product-card-button product-card-link" key={product.id} href={productDetailUrl(category, product.id)} onClick={(event) => { if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; event.preventDefault(); onView(category, product.id); }}><ProductImage product={product} /><div className="product-info"><span className="product-type">{product.product_type || (category === "laptops" ? "笔记本" : category === "phones" ? "手机" : "电脑配件")}</span><h3>{product.product_name}</h3><p>{product.description.slice(0, 84) || "查看详细配置与 AI 咨询。"}</p><div className="product-bottom"><strong>{product.price === null ? "价格待询" : `¥${product.price.toLocaleString("zh-CN")}`}</strong>{product.stock <= 0 && <span className="out-stock">暂时缺货</span>}</div></div></a>) : <p className="empty">没有找到匹配商品，换个关键词试试。</p>}</div>{catalogPage && <nav className="catalog-pagination" aria-label="商品分页"><button className="secondary" disabled={!hasPreviousPage || loading} onClick={() => void load(category, query, catalogPage.page - 1, brand, componentCategory)}>上一页</button><span>第 {catalogPage.page} / {Math.max(1, Math.ceil(catalogPage.total / catalogPage.page_size))} 页</span><button className="secondary" disabled={!hasNextPage || loading} onClick={() => void load(category, query, catalogPage.page + 1, brand, componentCategory)}>下一页</button></nav>}</section>;
}

/** 商品详情承载参数、购买和面向该商品的 AI 咨询，目录卡只负责快速浏览。 */
/** 不登录也能提问的公开导购页；浏览器刷新后不会保留聊天内容。 */
function PublicAssistantPage({ initialQuestion, onBrowseProducts }: { initialQuestion: string; onBrowseProducts: () => void }) {
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
      const result = await askPublicAssistant(text);
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
    if (!product || category === "components" || buying) return;
    if (!window.confirm(`确认购买「${product.product_name}」吗？将跳转至支付宝沙箱付款。`)) return;
    setBuying(true); setError("");
    try {
      const session = await createCheckout(auth.token, category, product.id, window.location.origin);
      window.location.assign(session.payment_url);
      // 跳转被浏览器、网络或收银台拦截时，不能让当前页面永久停在“正在跳转”。
      window.setTimeout(() => setBuying(false), 10_000);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "暂时无法创建支付订单");
      setBuying(false);
    }
  };
  const addToCart = async (): Promise<void> => {
    if (!auth) { onRequireLogin?.(); return; }
    if (!product || category === "components" || addingToCart) return;
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
  return <section className="product-detail">
    <button className="detail-back secondary" onClick={onBack}>← 返回商品目录</button>
    {error && <p className="error">{error}</p>}
    <div className="product-detail-layout">
      <section className="product-detail-main panel">
        <div className="product-hero"><ProductImage product={product} /><div><p className="detail-category">{product.product_type || "商品详情"}</p><h1 title={product.product_name}>{productDisplayName(product.product_name)}</h1><p className="muted product-detail-summary">{product.brand ? `${product.brand} · ` : ""}{product.description || "查看以下详细规格。"}</p><div className="detail-price-row"><strong>{product.price === null ? "价格待询" : `¥${product.price.toLocaleString("zh-CN")}`}</strong>{product.stock <= 0 && <span className="out-stock">暂时缺货</span>}</div>{category !== "components" && <><div className="product-actions"><button className="secondary" disabled={product.stock <= 0 || addingToCart} onClick={() => void addToCart()}>{addingToCart ? "加入中…" : "加入购物车"}</button><button className="detail-buy" disabled={product.stock <= 0 || buying} onClick={() => void buy()}>{buying ? "正在跳转…" : "立即购买"}</button></div>{!auth && <p className="muted cart-login-tip">加入购物车或购买时需要登录。</p>}{cartMessage && <p className="cart-success">{cartMessage}</p>}</>}</div></div>
        <h2>商品参数</h2>
        <div className="specification-table">{product.specifications.length ? product.specifications.map((specification) => <div className="specification-row" key={specification.name}><span>{specification.name}</span><strong>{specification.value}</strong></div>) : <p className="empty">暂未提供详细参数。</p>}</div>
      </section>
      <aside className="product-detail-ai panel"><p className="eyebrow">AI PRODUCT EXPERT</p><h2>问问 AI</h2><p className="muted">围绕这款商品的配置、使用场景或搭配方案提问。</p><form onSubmit={ask}><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder={`例如：${product.product_name} 适合玩 3A 游戏吗？`} maxLength={400} rows={5} /><button disabled={!question.trim()}>开始咨询</button></form><button className="text-button detail-default-question" onClick={() => onAsk(product)}>让 AI 介绍这款商品</button></aside>
    </div>
  </section>;
}

/** 客户在付款前统一核对商品、数量和总额的轻量购物车页面。 */
function CartPage({ auth }: { auth: AuthState }) {
  const [cart, setCart] = useState<Cart>({ items: [] });
  const [loading, setLoading] = useState(true);
  const [updatingItem, setUpdatingItem] = useState<number | null>(null);
  const [checkingOut, setCheckingOut] = useState(false);
  const [error, setError] = useState("");

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
  const checkout = async (): Promise<void> => {
    if (checkingOut || cart.items.length === 0 || cart.items.some((item) => !item.available)) return;
    setCheckingOut(true); setError("");
    try {
      const session = await checkoutCart(auth.token, window.location.origin);
      window.location.assign(session.payment_url);
      window.setTimeout(() => setCheckingOut(false), 10_000);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "暂时无法创建支付订单"); setCheckingOut(false); }
  };
  const total = cart.items.reduce((sum, item) => sum + (item.price ?? 0) * item.quantity, 0);
  const canCheckout = cart.items.length > 0 && cart.items.every((item) => item.available && item.price !== null);

  return <section className="cart-page panel"><div className="section-title"><div><p className="eyebrow">SHOPPING CART</p><h2>购物车</h2><p className="muted">结算前会再次核验商品价格与库存。</p></div><button className="secondary" onClick={() => void load()} disabled={loading}>刷新</button></div>{error && <p className="error">{error}</p>}{loading ? <p className="empty">正在读取购物车…</p> : cart.items.length === 0 ? <p className="empty">购物车还是空的。去商品详情页把想买的商品加入这里吧。</p> : <><div className="cart-items">{cart.items.map((item) => <article className={`cart-item${item.available ? "" : " unavailable"}`} key={item.item_id}><div><p className="product-type">{item.brand || "商品"}</p><h3>{item.product_name}</h3><p className="muted">{item.available ? `库存可用：${item.stock}` : "商品已下架或当前库存不足，请删除后重新选择。"}</p></div><div className="cart-item-price"><strong>{item.price === null ? "价格待询" : `¥${item.price.toLocaleString("zh-CN")}`}</strong><div className="quantity-control"><button className="secondary" disabled={updatingItem === item.item_id} onClick={() => void changeQuantity(item.item_id, item.quantity - 1)}>−</button><span>{item.quantity}</span><button className="secondary" disabled={updatingItem === item.item_id || item.quantity >= Math.min(5, item.stock)} onClick={() => void changeQuantity(item.item_id, item.quantity + 1)}>＋</button></div><button className="text-button cart-remove" disabled={updatingItem === item.item_id} onClick={() => void remove(item.item_id)}>删除</button></div></article>)}</div><footer className="cart-summary"><div><span>合计</span><strong>¥{total.toLocaleString("zh-CN")}</strong></div><button disabled={!canCheckout || checkingOut} onClick={() => void checkout()}>{checkingOut ? "正在跳转…" : "去支付宝沙箱付款"}</button></footer></>}</section>;
}

function OrderList({ auth }: { auth: AuthState }) {
  const [orders, setOrders] = useState<CustomerOrder[]>([]);
  const [checkoutOrders, setCheckoutOrders] = useState<CheckoutOrder[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [expandedOrder, setExpandedOrder] = useState<string | null>(null);
  const [resumingOrder, setResumingOrder] = useState<string | null>(null);
  const [cancellingOrder, setCancellingOrder] = useState<string | null>(null);
  const load = async (paymentOrderNo?: string): Promise<void> => { setLoading(true); setError(""); try { if (paymentOrderNo) await refreshCheckoutPayment(auth.token, paymentOrderNo); const [legacyOrders, currentOrders] = await Promise.all([listMyOrders(auth.token), listMyCheckoutOrders(auth.token)]); setOrders(legacyOrders); setCheckoutOrders(currentOrders); } catch (reason) { setError(reason instanceof Error ? reason.message : "订单暂时无法读取"); } finally { setLoading(false); } };
  useEffect(() => { const parameters = new URLSearchParams(window.location.search); const returnedOrderNo = parameters.get("payment_return") === "1" ? parameters.get("checkout_order") ?? undefined : undefined; void load(returnedOrderNo); if (parameters.get("payment_return") === "1") window.history.replaceState(null, "", `${window.location.pathname}?page=orders`); }, [auth.token]);
  useEffect(() => { const resetResume = (): void => setResumingOrder(null); window.addEventListener("pageshow", resetResume); window.addEventListener("focus", resetResume); return () => { window.removeEventListener("pageshow", resetResume); window.removeEventListener("focus", resetResume); }; }, []);
  const resumePayment = async (orderNo: string): Promise<void> => { setResumingOrder(orderNo); setError(""); try { const session = await resumeCheckout(auth.token, orderNo, window.location.origin); window.location.assign(session.payment_url); window.setTimeout(() => setResumingOrder(null), 10_000); } catch (reason) { setError(reason instanceof Error ? reason.message : "暂时无法继续付款"); setResumingOrder(null); } };
  const cancelPayment = async (orderNo: string): Promise<void> => { if (!window.confirm("确认取消这笔待支付订单吗？")) return; setCancellingOrder(orderNo); setError(""); try { await cancelCheckout(auth.token, orderNo); await load(); } catch (reason) { setError(reason instanceof Error ? reason.message : "暂时无法取消订单"); await load(); } finally { setCancellingOrder(null); } };

  const checkoutLabel = (order: CheckoutOrder): string => {
    if (order.fulfillment_status === "DELIVERED") return "已签收";
    if (order.fulfillment_status === "SHIPPED") return "已发货";
    if (order.status === "PAID") return "待发货";
    return order.status === "PENDING_PAYMENT" ? "等待付款" : "支付失败";
  };
  const checkoutDetail = (order: CheckoutOrder): string => {
    if (order.tracking_company && order.tracking_number) return `${order.tracking_company} · ${order.tracking_number}`;
    return order.status === "PAID" ? "支付成功，等待发货" : `支付宝沙箱 · ${order.payment_status}`;
  };

  return <section className="orders panel"><div className="section-title"><div><p className="eyebrow">MY ORDERS</p><h2>我的订单</h2><p className="muted">显示你的新支付订单与已确认归属的历史订单。</p></div><button className="secondary" onClick={() => void load()}>刷新</button></div>{error && <p className="error">{error}</p>}{loading ? <p className="empty">正在读取订单…</p> : <>{checkoutOrders.length > 0 && <><h3 className="order-group-title">沙箱结算订单</h3><div className="order-list">{checkoutOrders.map((order) => <article className="order-card" key={order.order_no}><header><div><strong>{order.order_no}</strong><small>{formatDate(order.created_at)}</small></div><span className="status">{checkoutLabel(order)}</span></header><div className="order-products"><p>{order.product_name}<span>×{order.quantity}</span></p></div><footer><div><strong>实付 ¥{(order.total_amount_cents / 100).toLocaleString("zh-CN")}</strong><small>{checkoutDetail(order)}</small></div>{order.status === "PENDING_PAYMENT" && <div className="order-actions"><button className="secondary" disabled={resumingOrder === order.order_no || cancellingOrder === order.order_no} onClick={() => void resumePayment(order.order_no)}>{resumingOrder === order.order_no ? "正在跳转…" : "继续付款"}</button><button className="secondary cancel-order-button" disabled={resumingOrder === order.order_no || cancellingOrder === order.order_no} onClick={() => void cancelPayment(order.order_no)}>{cancellingOrder === order.order_no ? "取消中…" : "取消订单"}</button></div>}</footer></article>)}</div></>}{orders.length ? <><h3 className="order-group-title">历史订单</h3><div className="order-list">{orders.map((order) => <article className="order-card" key={order.order_id}><header><div><strong>{order.order_id}</strong><small>{formatDate(order.order_date)}</small></div><span className="status">{order.status || "处理中"}</span></header><div className="order-products">{order.items.slice(0, expandedOrder === order.order_id ? undefined : 2).map((item, index) => <p key={index}>{item.brand ? `${item.brand} · ` : ""}{item.product_name}<span>×{item.quantity ?? 1}</span></p>)}</div><footer><div><strong>实付 ¥{order.paid_amount.toLocaleString("zh-CN")}</strong><small>{order.tracking.company && order.tracking.number ? `${order.tracking.company} · ${order.tracking.number}` : "暂无物流信息"}</small></div>{order.items.length > 2 && <button className="secondary" onClick={() => setExpandedOrder(expandedOrder === order.order_id ? null : order.order_id)}>{expandedOrder === order.order_id ? "收起" : `查看 ${order.items.length} 件商品`}</button>}</footer></article>)}</div></> : checkoutOrders.length === 0 && <p className="empty">暂无订单。</p>}</>}</section>;
}

/** 客户查看 Agent 售后处理进度，并在同一工单中继续补充问题。 */
function CustomerTicketCenter({ auth }: { auth: AuthState }) {
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

  const handleReplyKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key !== "Enter" || event.shiftKey || event.altKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  };

  if (!loading && tickets.length === 0) return <section className="customer-ticket-empty panel">
    <p className="eyebrow">MY AFTER-SALES</p>
    <h2>暂时没有售后工单</h2>
    <p>在智能客服中描述设备问题、保修或退款诉求后，Agent 会自动创建工单并优先处理；处理进度会显示在这里。</p>
    <button className="secondary" onClick={() => void loadTickets()}>刷新状态</button>
    {error && <p className="error">{error}</p>}
  </section>;

  return <section className="customer-ticket-center">
    <section className="panel customer-ticket-list">
      <div className="section-title"><div><p className="eyebrow">MY AFTER-SALES</p><h2>我的售后</h2><p className="muted">Agent 会自动处理明确问题，复杂情况再转人工。</p></div><button className="secondary" onClick={() => void loadTickets()} disabled={loading}>刷新</button></div>
      {loading ? <p className="empty">正在读取售后进度…</p> : tickets.length ? <div className="ticket-list">{tickets.map((ticket) => <button className={`ticket-card ${selectedTicket?.ticket_id === ticket.ticket_id ? "active" : ""}`} key={ticket.ticket_id} onClick={() => void selectTicket(ticket.ticket_id)}><span className="status">{ticket.status}</span><strong>{ticket.ticket_id}</strong><small>{formatDate(ticket.created_at)}</small></button>)}</div> : <p className="empty">暂时没有售后工单。你可以直接在智能客服中描述问题，Agent 会为你创建并处理。</p>}
    </section>
    <section className="panel ticket-detail customer-ticket-detail">
      {selectedTicket ? <><div className="section-title"><div><p className="eyebrow">AFTER-SALES CONVERSATION</p><h2>{selectedTicket.ticket_id}</h2><p className="muted">当前状态：{selectedTicket.status}</p></div></div><div className="message-history customer-ticket-messages">{messages.length ? messages.map((message) => <Message key={message.message_id} message={message} />) : <p className="empty">暂时没有消息。</p>}</div><form className="composer customer-ticket-composer" onSubmit={sendFollowUp}><textarea value={reply} onChange={(event) => setReply(event.target.value)} onKeyDown={handleReplyKeyDown} maxLength={4000} placeholder="补充问题或回复 Agent…" /><button disabled={sending || !reply.trim()}>{sending ? "发送中…" : "发送"}</button></form><p className="customer-ticket-hint">Enter 发送 · Shift / Alt + Enter 换行</p></> : <div className="ticket-detail-empty"><h2>查看售后处理进度</h2><p>从左侧选择一张工单，即可看到 Agent 的处理结果并继续追问。</p></div>}
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
  const [draft, setDraft] = useState<SupportReplyDraft | null>(null);
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = async (): Promise<void> => setTickets(await listTickets(auth.token));
  useEffect(() => { void refresh().catch(() => undefined); }, [auth.token]);

  const select = async (ticketId: string): Promise<void> => {
    setError(""); setDraft(null);
    try { const [ticket, items] = await Promise.all([getTicket(auth.token, ticketId), listTicketMessages(auth.token, ticketId)]); setSelectedTicket(ticket); setMessages(items); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取工单"); }
  };
  const claim = async (): Promise<void> => {
    if (!selectedTicket) return; setBusy(true); setError("");
    try { await claimTicket(auth.token, selectedTicket.ticket_id); await select(selectedTicket.ticket_id); await refresh(); }
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
  const owned = selectedTicket?.assigned_agent_id === auth.user.id;

  return <Shell title="客服 AI 工作台" subtitle="认领工单后，AI 基于知识库生成有依据的回复；客服确认后再发送。" auth={auth} onSignOut={onSignOut}>
    <div className="agent-grid"><section className="panel ticket-queue"><div className="section-title"><h2>待处理工单</h2><button className="secondary" onClick={() => void refresh()}>刷新</button></div>{tickets.length ? tickets.map((ticket) => <button className="ticket-card" key={ticket.ticket_id} onClick={() => void select(ticket.ticket_id)}><span className={`status status-${ticket.status}`}>{ticket.status}</span><strong>{ticket.ticket_id}</strong><small>{ticket.urgency} · {formatDate(ticket.created_at)}</small></button>) : <p className="empty">当前没有可见工单</p>}</section>
      <section className="panel ticket-detail">{selectedTicket ? <><div className="section-title"><div><h2>{selectedTicket.ticket_id}</h2><p className="muted">{selectedTicket.issue || "尚未认领，先认领后查看详情"}</p></div>{!owned && <button onClick={() => void claim()} disabled={busy}>认领工单</button>}</div>{owned ? <><div className="message-history">{messages.map((message) => <Message key={message.message_id} message={message} />)}</div><div className="draft-actions"><button className="secondary" onClick={() => void createDraft()} disabled={busy}>✨ 生成 AI 回复草稿</button>{draft && <span className="hint">引用 {draft.knowledge_references.length} 条知识资料{draft.needs_human_follow_up ? " · 需补充依据" : ""}</span>}</div><form className="composer" onSubmit={send}><textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder="编辑后发送给客户" maxLength={4000} /><button disabled={busy}>{busy ? "处理中…" : "发送回复"}</button></form></> : <p className="empty">认领后可查看消息、生成 AI 草稿并回复客户。</p>}</> : <p className="empty">从左侧选择一张工单开始处理。</p>}</section>
    </div>{error && <p className="toast error">{error}</p>}
  </Shell>;
}

function Message({ message }: { message: TicketMessage }) {
  const label = message.author_role === "customer" ? "客户" : message.author_role === "ai" ? "AI" : "客服";
  return <article className={`message ${message.author_role}`}><header><strong>{label}</strong><small>{formatDate(message.created_at)}</small></header><p>{message.content}</p>{message.ai_assisted && <small className="ai-note">AI 辅助草稿，经客服确认发送</small>}</article>;
}

function UnavailableWorkspace({ auth, onSignOut }: { auth: AuthState; onSignOut: () => Promise<void> }) {
  const roleLabel = useMemo(() => auth.user.role || "当前", [auth.user.role]);
  return <Shell title="内部工作台正在接入" subtitle={`${roleLabel} 角色的业务页面将在支付、退款和报表闭环实现时开放。`} auth={auth} onSignOut={onSignOut}><section className="panel"><h2>当前可演示能力</h2><p>客户服务台和客服 AI 工作台已经可用。登录 customer 或 agent 账号体验完整工单闭环。</p></section></Shell>;
}
