--
-- PostgreSQL database dump
--

\restrict 5IPDhugooIyCU2HlsstoGgcYS80VlOCUTQCsaSS4niXR09r67QPyrSSv2CVs7Vv

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: component_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.component_products (
    id character varying(128) NOT NULL,
    product_name character varying(512),
    category character varying(64),
    price numeric,
    url character varying(512),
    normalized jsonb,
    params jsonb,
    description text,
    embedding public.vector(1024),
    metadata jsonb,
    content_hash character varying(32),
    stock integer DEFAULT 0,
    warehouse character varying(50) DEFAULT ''::character varying
);


--
-- Name: knowledge_chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledge_chunks (
    id character varying(128) NOT NULL,
    source character varying(128),
    title character varying(256),
    content text,
    embedding public.vector(1024)
);


--
-- Name: laptop_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.laptop_products (
    id character varying(128) NOT NULL,
    product_name character varying(512),
    brand character varying(64),
    price numeric,
    product_type character varying(32),
    description text,
    embedding public.vector(1024),
    metadata jsonb,
    status character varying(16) DEFAULT '在售'::character varying,
    stock integer DEFAULT 0,
    warehouse character varying(50) DEFAULT ''::character varying,
    content_hash character varying(32)
);


--
-- Name: order_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_items (
    id integer NOT NULL,
    order_id character varying(20),
    product_name text,
    category character varying(10),
    brand character varying(20),
    price numeric(12,2),
    quantity integer
);


--
-- Name: order_items_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.order_items_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: order_items_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.order_items_id_seq OWNED BY public.order_items.id;


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders (
    id integer NOT NULL,
    order_id character varying(20) NOT NULL,
    customer_id character varying(10),
    customer_name character varying(20),
    order_date date,
    status character varying(10),
    total_amount numeric(12,2),
    paid_amount numeric(12,2),
    discount numeric(12,2),
    payment_method character varying(20),
    payment_time timestamp without time zone,
    tracking_company character varying(20),
    tracking_number character varying(30),
    shipping_address text,
    phone character varying(11),
    created_at timestamp without time zone DEFAULT now(),
    delivered_at date
);


--
-- Name: orders_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.orders_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: orders_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.orders_id_seq OWNED BY public.orders.id;


--
-- Name: phone_products; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.phone_products (
    id character varying(128) NOT NULL,
    product_name character varying(512),
    brand character varying(64),
    price numeric,
    description text,
    embedding public.vector(1024),
    metadata jsonb,
    status character varying(16) DEFAULT '在售'::character varying,
    stock integer DEFAULT 0,
    warehouse character varying(50) DEFAULT ''::character varying,
    content_hash character varying(32)
);


--
-- Name: tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tickets (
    id integer NOT NULL,
    ticket_id character varying(20) NOT NULL,
    customer_name character varying(50) DEFAULT ''::character varying,
    phone character varying(20) DEFAULT ''::character varying,
    issue text NOT NULL,
    urgency character varying(10) DEFAULT 'medium'::character varying,
    status character varying(10) DEFAULT '待处理'::character varying,
    created_at timestamp without time zone DEFAULT now()
);


--
-- Name: tickets_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tickets_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tickets_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tickets_id_seq OWNED BY public.tickets.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    username character varying(64) NOT NULL,
    password_hash character varying(128),
    role character varying(32)
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: order_items id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items ALTER COLUMN id SET DEFAULT nextval('public.order_items_id_seq'::regclass);


--
-- Name: orders id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders ALTER COLUMN id SET DEFAULT nextval('public.orders_id_seq'::regclass);


--
-- Name: tickets id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets ALTER COLUMN id SET DEFAULT nextval('public.tickets_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: component_products component_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.component_products
    ADD CONSTRAINT component_products_pkey PRIMARY KEY (id);


--
-- Name: knowledge_chunks knowledge_chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledge_chunks
    ADD CONSTRAINT knowledge_chunks_pkey PRIMARY KEY (id);


--
-- Name: laptop_products laptop_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.laptop_products
    ADD CONSTRAINT laptop_products_pkey PRIMARY KEY (id);


--
-- Name: order_items order_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_pkey PRIMARY KEY (id);


--
-- Name: orders orders_order_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_order_id_key UNIQUE (order_id);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (id);


--
-- Name: phone_products phone_products_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.phone_products
    ADD CONSTRAINT phone_products_pkey PRIMARY KEY (id);


--
-- Name: tickets tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_pkey PRIMARY KEY (id);


--
-- Name: tickets tickets_ticket_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tickets
    ADD CONSTRAINT tickets_ticket_id_key UNIQUE (ticket_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: component_products_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX component_products_embedding_idx ON public.component_products USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_component_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_component_embedding ON public.component_products USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_knowledge_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_knowledge_embedding ON public.knowledge_chunks USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_laptop_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_laptop_embedding ON public.laptop_products USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: idx_phone_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_phone_embedding ON public.phone_products USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: laptop_products_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX laptop_products_embedding_idx ON public.laptop_products USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: phone_products_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX phone_products_embedding_idx ON public.phone_products USING hnsw (embedding public.vector_cosine_ops);


--
-- Name: order_items order_items_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_items
    ADD CONSTRAINT order_items_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 5IPDhugooIyCU2HlsstoGgcYS80VlOCUTQCsaSS4niXR09r67QPyrSSv2CVs7Vv

