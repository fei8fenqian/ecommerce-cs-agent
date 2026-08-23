import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  AuthState,
  Product,
  CustomerOrder,
  SessionItem,
  SupportReplyDraft,
  Ticket,
  TicketMessage,
  claimTicket,
  createCustomerTicket,
  getSession,
  getTicket,
  listSessions,
  listProducts,
  listMyOrders,
  listTicketMessages,
  listTickets,
  requestReplyDraft,
  register,
  sendAgentTicketMessage,
  sendCustomerTicketMessage,
  signIn,
  signOut,
  streamChat,
} from "./api";

const STORAGE_KEY = "ecommerce-agent.auth";

function loadAuth(): AuthState | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as AuthState) : null;
  } catch {
    return null;
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

/** MVP 入口：按登录身份加载客户服务台或内部客服工作台。 */
export function App() {
  const [auth, setAuth] = useState<AuthState | null>(loadAuth);

  const onSignedIn = (nextAuth: AuthState): void => {
    saveAuth(nextAuth);
    setAuth(nextAuth);
  };

  const onSignOut = async (): Promise<void> => {
    if (auth) await signOut(auth.token).catch(() => undefined);
    saveAuth(null);
    setAuth(null);
  };

  if (!auth) return <LoginPage onSignedIn={onSignedIn} />;
  if (auth.user.role === "customer") return <CustomerWorkspace auth={auth} onSignOut={onSignOut} />;
  if (auth.user.role === "agent") return <AgentWorkspace auth={auth} onSignOut={onSignOut} />;
  if (auth.user.role === "operator") return <OperatorWorkspace auth={auth} onSignOut={onSignOut} />;
  return <UnavailableWorkspace auth={auth} onSignOut={onSignOut} />;
}

function LoginPage({ onSignedIn }: { onSignedIn: (auth: AuthState) => void }) {
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
        <p className="eyebrow">GEEX DIGITAL · AI SERVICE DESK</p>
        <h1>{mode === "login" ? "欢迎回来" : "创建客户账号"}</h1>
        <p className="muted">{mode === "login" ? "客户咨询、智能处理与人工客服协作，都在同一个工作流内。" : "注册后即可使用商品咨询、订单与工单服务。"}</p>
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
  const [page, setPage] = useState<"service" | "catalog" | "orders">("service");
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionId, setSessionId] = useState<string | undefined>();
  const [chatMessages, setChatMessages] = useState<Array<{ id: string; role: "user" | "assistant"; content: string }>>([]);
  const [query, setQuery] = useState("");
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [selectedTicket, setSelectedTicket] = useState<Ticket | null>(null);
  const [ticketMessages, setTicketMessages] = useState<TicketMessage[]>([]);
  const [followUp, setFollowUp] = useState("");
  const [newTicketIssue, setNewTicketIssue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [streamStatus, setStreamStatus] = useState("");

  const refreshTickets = async (): Promise<void> => setTickets(await listTickets(auth.token));
  const openTicket = async (ticketId: string): Promise<void> => {
    setError("");
    try {
      const [ticket, messages] = await Promise.all([getTicket(auth.token, ticketId), listTicketMessages(auth.token, ticketId)]);
      setSelectedTicket(ticket);
      setTicketMessages(messages);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取工单"); }
  };

  useEffect(() => { void listSessions(auth.token).then(setSessions).catch(() => undefined); void refreshTickets().catch(() => undefined); }, [auth.token]);

  const startNewChat = (): void => {
    setPage("service");
    setSessionId(undefined);
    setChatMessages([]);
    setQuery("");
    setError("");
  };

  const openSession = async (nextSessionId: string): Promise<void> => {
    if (busy) return;
    setError("");
    try {
      const session = await getSession(auth.token, nextSessionId);
      const restoredMessages = session.messages
        .filter((message) => (message.role === "user" || message.role === "assistant") && Boolean(message.content))
        .map((message, index) => ({
          id: `history-${nextSessionId}-${index}`,
          role: message.role as "user" | "assistant",
          content: message.content ?? "",
        }));
      setSessionId(session.session_id);
      setChatMessages(restoredMessages);
      setPage("service");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法读取历史会话");
    }
  };

  const submitChat = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    const text = query.trim();
    if (!text || busy) return;
    setBusy(true); setError(""); setStreamStatus("正在连接智能客服…"); setQuery("");
    const assistantId = `assistant-${Date.now()}`;
    setChatMessages((items) => [...items, { id: `user-${Date.now()}`, role: "user", content: text }, { id: assistantId, role: "assistant", content: "" }]);
    try {
      await streamChat(auth.token, text, sessionId, (event) => {
        if (event.event === "start") {
          setSessionId(event.session_id); setStreamStatus("正在分析问题…"); return;
        }
        if (event.event === "tool_call") { setStreamStatus("正在查询业务数据…"); return; }
        if (event.event === "token") {
          setStreamStatus("正在生成回答…");
          setChatMessages((items) => items.map((message) => message.id === assistantId ? { ...message, content: message.content + (event.content ?? "") } : message));
          return;
        }
        if (event.event === "done") {
          const completedAnswer = event.answer ?? event.data?.answer;
          if (completedAnswer) setChatMessages((items) => items.map((message) => message.id === assistantId && !message.content ? { ...message, content: completedAnswer } : message));
          setStreamStatus("");
        }
      });
      setSessions(await listSessions(auth.token));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "智能客服暂时不可用");
      setChatMessages((items) => items.filter((message) => message.id !== assistantId || message.content));
    } finally { setBusy(false); setStreamStatus(""); }
  };

  const submitFollowUp = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!selectedTicket || !followUp.trim()) return;
    setBusy(true); setError("");
    try {
      const created = await sendCustomerTicketMessage(auth.token, selectedTicket.ticket_id, followUp.trim());
      setTicketMessages((items) => [...items, created]); setFollowUp(""); await refreshTickets();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "发送失败"); }
    finally { setBusy(false); }
  };

  const createTicket = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    const issue = newTicketIssue.trim();
    if (!issue || busy) return;
    setBusy(true); setError("");
    try {
      const ticket = await createCustomerTicket(auth.token, issue);
      setNewTicketIssue("");
      await refreshTickets();
      await openTicket(ticket.ticket_id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "工单创建失败"); }
    finally { setBusy(false); }
  };

  return <main className="customer-chat-app">
    <aside className="chat-sidebar">
      <div className="chat-brand"><span>G</span><strong>Geex AI</strong></div>
      <button className="new-chat-button" onClick={startNewChat}>＋ 新建对话</button>
      <nav className="chat-page-nav" aria-label="客户服务导航">
        <button className={page === "service" ? "active" : "secondary"} onClick={() => setPage("service")}>✦ AI 服务</button>
        <button className={page === "catalog" ? "active" : "secondary"} onClick={() => setPage("catalog")}>商品目录</button>
        <button className={page === "orders" ? "active" : "secondary"} onClick={() => setPage("orders")}>我的订单</button>
      </nav>
      <section className="sidebar-sessions"><p className="sidebar-label">最近对话</p>{sessions.length ? sessions.slice(0, 10).map((session) => <button className={`session-row ${sessionId === session.session_id ? "active" : ""}`} key={session.session_id} onClick={() => void openSession(session.session_id)}><strong>{session.title || "新对话"}</strong><small>{session.message_count} 条消息</small></button>) : <p className="sidebar-empty">暂无历史对话</p>}</section>
      <section className="sidebar-tickets"><p className="sidebar-label">我的工单</p>{tickets.slice(0, 3).map((ticket) => <button className="session-row" key={ticket.ticket_id} onClick={() => void openTicket(ticket.ticket_id)}><strong>{ticket.ticket_id}</strong><small>{ticket.status} · {ticket.urgency}</small></button>)}<form className="ticket-create" onSubmit={createTicket}><label>需要售后协助？</label><textarea value={newTicketIssue} onChange={(event) => setNewTicketIssue(event.target.value)} placeholder="描述问题，AI 会优先处理" maxLength={4000} /><button disabled={busy || !newTicketIssue.trim()}>提交工单</button></form></section>
      <div className="chat-account"><span>{auth.user.username}</span><button className="text-button" onClick={() => void onSignOut()}>退出</button></div>
    </aside>
    <section className="chat-main">
      <header className="chat-main-header"><div><strong>{page === "service" ? "智能客服" : page === "catalog" ? "商品目录" : "我的订单"}</strong><span>{page === "service" ? (sessionId ? "当前会话" : "新对话") : "Geex Digital"}</span></div><span className="role-badge">客户服务台</span></header>
      {page === "catalog" ? <ProductCatalog auth={auth} onAsk={(product) => { setPage("service"); setQuery(`我想了解 ${product.product_name}，请介绍它的配置、适用场景和库存情况。`); }} /> : page === "orders" ? <OrderList auth={auth} /> : <section className="chat-canvas">
        <div className="chat-history chatgpt-history">{chatMessages.length === 0 ? <div className="chat-welcome"><p className="eyebrow">GEEX DIGITAL · AI ASSISTANT</p><h1>今天想解决什么问题？</h1><p>我可以介绍商品、查询已归属订单，也能帮你发起售后工单。</p><div className="prompt-grid"><button className="prompt-card" onClick={() => setQuery("帮我推荐一台预算 5000 元左右的笔记本")}>推荐一台预算 5000 元的笔记本</button><button className="prompt-card" onClick={() => setQuery("帮我查询订单物流")}>查询我的订单物流</button><button className="prompt-card" onClick={() => setQuery("哪些手机目前有库存？")}>查询有库存的手机</button></div></div> : chatMessages.map((message) => <article className={`bubble ${message.role}${!message.content ? " thinking" : ""}`} key={message.id}><span className="message-avatar">{message.role === "user" ? auth.user.username.slice(0, 1).toUpperCase() : "G"}</span><div>{message.content || streamStatus || "正在思考…"}</div></article>)}</div>
        <form className="composer chatgpt-composer" onSubmit={submitChat}><textarea value={query} onChange={(event) => setQuery(event.target.value)} placeholder="给 Geex AI 发送消息" maxLength={2000} rows={1} /><button aria-label="发送消息" disabled={busy || !query.trim()}>{busy ? "…" : "↑"}</button></form><p className="chat-disclaimer">AI 可能出错；订单和售后结果请以系统记录为准。</p>
      </section>}
    </section>
    {selectedTicket && <TicketConversation title={`工单 ${selectedTicket.ticket_id}`} messages={ticketMessages} error={error} onClose={() => setSelectedTicket(null)} composer={{ value: followUp, placeholder: "继续补充问题，AI 会重新处理未认领工单", submitLabel: "补充问题", disabled: busy, onChange: setFollowUp, onSubmit: submitFollowUp }} />}
    {error && <p className="toast error">{error}</p>}
  </main>;
}

function ProductCatalog({ auth, onAsk }: { auth: AuthState; onAsk: (product: Product) => void }) {
  const [category, setCategory] = useState<"laptops" | "phones">("laptops");
  const [query, setQuery] = useState("");
  const [products, setProducts] = useState<Product[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async (nextCategory = category, nextQuery = query): Promise<void> => {
    setLoading(true); setError("");
    try { setProducts(await listProducts(auth.token, nextCategory, nextQuery)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "商品目录暂时不可用"); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [auth.token, category]);
  const search = (event: FormEvent): void => { event.preventDefault(); void load(); };

  return <section className="catalog"><header className="catalog-header"><div><p className="eyebrow">PRODUCT CATALOG</p><h2>发现适合你的数码产品</h2><p className="muted">实时展示已入库商品；库存与价格以当前系统数据为准。</p></div><form className="catalog-search" onSubmit={search}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索品牌或商品名称" maxLength={100} /><button>搜索</button></form></header><div className="catalog-tabs"><button className={category === "laptops" ? "active" : "secondary"} onClick={() => setCategory("laptops")}>笔记本</button><button className={category === "phones" ? "active" : "secondary"} onClick={() => setCategory("phones")}>手机</button></div>{error && <p className="error">{error}</p>}<div className="product-grid">{loading ? <p className="empty">正在读取商品目录…</p> : products.length ? products.map((product) => <article className="product-card" key={product.id}>{product.image_url ? <img src={product.image_url} alt="" /> : <div className="product-visual product-image-missing"><span aria-hidden="true">▧</span><small>暂无商品图片</small></div>}<div className="product-info"><span className="product-type">{product.product_type || (category === "laptops" ? "笔记本" : "手机")}</span><h3>{product.product_name}</h3><p>{product.description.slice(0, 84) || "查看 AI 客服了解详细配置。"}</p><div className="product-bottom"><strong>{product.price === null ? "价格待询" : `¥${product.price.toLocaleString("zh-CN")}`}</strong><span className={product.stock > 0 ? "in-stock" : "out-stock"}>{product.stock > 0 ? `现货 ${product.stock}` : "暂时缺货"}</span></div><button className="secondary" onClick={() => onAsk(product)}>问 AI 了解这款</button></div></article>) : <p className="empty">没有找到匹配商品，换个关键词试试。</p>}</div></section>;
}

function OrderList({ auth }: { auth: AuthState }) {
  const [orders, setOrders] = useState<CustomerOrder[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [expandedOrder, setExpandedOrder] = useState<string | null>(null);
  const load = async (): Promise<void> => { setLoading(true); setError(""); try { setOrders(await listMyOrders(auth.token)); } catch (reason) { setError(reason instanceof Error ? reason.message : "订单暂时无法读取"); } finally { setLoading(false); } };
  useEffect(() => { void load(); }, [auth.token]);

  return <section className="orders panel"><div className="section-title"><div><p className="eyebrow">MY ORDERS</p><h2>我的订单</h2><p className="muted">只显示已完成账户归属确认的订单。</p></div><button className="secondary" onClick={() => void load()}>刷新</button></div>{error && <p className="error">{error}</p>}{loading ? <p className="empty">正在读取订单…</p> : orders.length ? <div className="order-list">{orders.map((order) => <article className="order-card" key={order.order_id}><header><div><strong>{order.order_id}</strong><small>{formatDate(order.order_date)}</small></div><span className="status">{order.status || "处理中"}</span></header><div className="order-products">{order.items.slice(0, expandedOrder === order.order_id ? undefined : 2).map((item, index) => <p key={index}>{item.brand ? `${item.brand} · ` : ""}{item.product_name}<span>×{item.quantity ?? 1}</span></p>)}</div><footer><div><strong>实付 ¥{order.paid_amount.toLocaleString("zh-CN")}</strong><small>{order.tracking.company && order.tracking.number ? `${order.tracking.company} · ${order.tracking.number}` : "暂无物流信息"}</small></div>{order.items.length > 2 && <button className="secondary" onClick={() => setExpandedOrder(expandedOrder === order.order_id ? null : order.order_id)}>{expandedOrder === order.order_id ? "收起" : `查看 ${order.items.length} 件商品`}</button>}</footer></article>)}</div> : <p className="empty">暂无已归属订单。历史订单无法核验时不会在这里展示。</p>}</section>;
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
      setProducts([...laptops, ...phones]);
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
    <div className="operator-grid"><section className="panel inventory-panel"><div className="section-title"><div><p className="eyebrow">INVENTORY OVERVIEW</p><h2>库存概览</h2></div><button className="secondary" onClick={() => void load()}>刷新</button></div>{error && <p className="error">{error}</p>}{loading ? <p className="empty">正在读取商品库存…</p> : <div className="inventory-list">{[...lowStock, ...outOfStock].length ? [...lowStock, ...outOfStock].map((product) => <article key={product.id} className="inventory-row"><div><strong>{product.product_name}</strong><small>{product.brand} · {product.warehouse || "未标注仓库"}</small></div><span className={product.stock > 0 ? "stock-low" : "stock-empty"}>{product.stock > 0 ? `仅剩 ${product.stock}` : "已缺货"}</span></article>) : <p className="empty">当前目录没有低库存或缺货商品。</p>}</div>}</section>
      <section className="panel operator-ai"><p className="eyebrow">OPERATOR AGENT</p><h2>让 Agent 分析运营问题</h2><p className="muted">例如：哪些商品库存偏低？适合推荐什么替代型号？</p><form className="form-stack" onSubmit={askAgent}><textarea value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={2000} placeholder="输入运营问题" /><button disabled={running}>{running ? "Agent 分析中…" : "开始分析"}</button></form>{(answer || running) && <article className="agent-answer">{answer || "正在分析商品、知识库和可用工具…"}</article>}</section>
    </div>
  </Shell>;
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

function TicketConversation({ title, messages, error, onClose, composer }: { title: string; messages: TicketMessage[]; error: string; onClose: () => void; composer: { value: string; placeholder: string; submitLabel: string; disabled: boolean; onChange: (value: string) => void; onSubmit: (event: FormEvent) => Promise<void> } }) {
  return <section className="modal-backdrop"><section className="modal panel"><div className="section-title"><h2>{title}</h2><button className="secondary" onClick={onClose}>关闭</button></div><div className="message-history">{messages.map((message) => <Message key={message.message_id} message={message} />)}</div><form className="composer" onSubmit={(event) => void composer.onSubmit(event)}><textarea value={composer.value} onChange={(event) => composer.onChange(event.target.value)} placeholder={composer.placeholder} maxLength={4000} /><button disabled={composer.disabled}>{composer.submitLabel}</button></form>{error && <p className="error">{error}</p>}</section></section>;
}

function Message({ message }: { message: TicketMessage }) {
  const label = message.author_role === "customer" ? "客户" : message.author_role === "ai" ? "AI" : "客服";
  return <article className={`message ${message.author_role}`}><header><strong>{label}</strong><small>{formatDate(message.created_at)}</small></header><p>{message.content}</p>{message.ai_assisted && <small className="ai-note">AI 辅助草稿，经客服确认发送</small>}</article>;
}

function UnavailableWorkspace({ auth, onSignOut }: { auth: AuthState; onSignOut: () => Promise<void> }) {
  const roleLabel = useMemo(() => auth.user.role || "当前", [auth.user.role]);
  return <Shell title="内部工作台正在接入" subtitle={`${roleLabel} 角色的业务页面将在支付、退款和报表闭环实现时开放。`} auth={auth} onSignOut={onSignOut}><section className="panel"><h2>当前可演示能力</h2><p>客户服务台和客服 AI 工作台已经可用。登录 customer 或 agent 账号体验完整工单闭环。</p></section></Shell>;
}
