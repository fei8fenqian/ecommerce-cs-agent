"""Baseline for the current PostgreSQL schema.

This migration describes the schema exported from the existing PostgreSQL
database.  It is intended to bootstrap an empty database.  The existing
database must be marked with ``alembic stamp`` after this migration has been
reviewed; this migration must not be upgraded against that existing database.
"""

from typing import Sequence, Union

from alembic import op  # type: ignore[attr-defined]

# revision identifiers, used by Alembic.
revision: str = "046df10e2506"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the current schema in an empty PostgreSQL database."""
    # The product and knowledge tables use the pgvector type and HNSW indexes.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
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
        )
        """)

    op.execute("""
        CREATE TABLE public.knowledge_chunks (
            id character varying(128) NOT NULL,
            source character varying(128),
            title character varying(256),
            content text,
            embedding public.vector(1024)
        )
        """)

    op.execute("""
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
        )
        """)

    op.execute("""
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
        )
        """)

    op.execute("""
        CREATE TABLE public.order_items (
            id integer NOT NULL,
            order_id character varying(20),
            product_name text,
            category character varying(10),
            brand character varying(20),
            price numeric(12,2),
            quantity integer
        )
        """)

    op.execute("""
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
        )
        """)

    op.execute("""
        CREATE TABLE public.tickets (
            id integer NOT NULL,
            ticket_id character varying(20) NOT NULL,
            customer_name character varying(50) DEFAULT ''::character varying,
            phone character varying(20) DEFAULT ''::character varying,
            issue text NOT NULL,
            urgency character varying(10) DEFAULT 'medium'::character varying,
            status character varying(10) DEFAULT '待处理'::character varying,
            created_at timestamp without time zone DEFAULT now()
        )
        """)

    op.execute("""
        CREATE TABLE public.users (
            id integer NOT NULL,
            username character varying(64) NOT NULL,
            password_hash character varying(128),
            role character varying(32)
        )
        """)

    # Preserve the sequence names and defaults from the current database.
    for sequence_name, table_name in (
        ("order_items_id_seq", "order_items"),
        ("orders_id_seq", "orders"),
        ("tickets_id_seq", "tickets"),
        ("users_id_seq", "users"),
    ):
        op.execute(f"""
            CREATE SEQUENCE public.{sequence_name}
                AS integer
                START WITH 1
                INCREMENT BY 1
                NO MINVALUE
                NO MAXVALUE
                CACHE 1
            """)
        op.execute(f"ALTER SEQUENCE public.{sequence_name} OWNED BY public.{table_name}.id")
        op.execute(f"""
            ALTER TABLE ONLY public.{table_name}
            ALTER COLUMN id SET DEFAULT nextval('public.{sequence_name}'::regclass)
            """)

    op.execute("ALTER TABLE ONLY public.component_products ADD CONSTRAINT component_products_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.knowledge_chunks ADD CONSTRAINT knowledge_chunks_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.laptop_products ADD CONSTRAINT laptop_products_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.order_items ADD CONSTRAINT order_items_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.orders ADD CONSTRAINT orders_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.orders ADD CONSTRAINT orders_order_id_key UNIQUE (order_id)")
    op.execute("ALTER TABLE ONLY public.phone_products ADD CONSTRAINT phone_products_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.tickets ADD CONSTRAINT tickets_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.tickets ADD CONSTRAINT tickets_ticket_id_key UNIQUE (ticket_id)")
    op.execute("ALTER TABLE ONLY public.users ADD CONSTRAINT users_pkey PRIMARY KEY (id)")
    op.execute("ALTER TABLE ONLY public.users ADD CONSTRAINT users_username_key UNIQUE (username)")

    # Keep the indexes that exist in the current database, including the
    # duplicate product indexes.  Index cleanup belongs in a later migration.
    op.execute(
        "CREATE INDEX component_products_embedding_idx ON public.component_products "
        "USING hnsw (embedding public.vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_component_embedding ON public.component_products "
        "USING hnsw (embedding public.vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_knowledge_embedding ON public.knowledge_chunks "
        "USING hnsw (embedding public.vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_laptop_embedding ON public.laptop_products USING hnsw (embedding public.vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_phone_embedding ON public.phone_products USING hnsw (embedding public.vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX laptop_products_embedding_idx ON public.laptop_products "
        "USING hnsw (embedding public.vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX phone_products_embedding_idx ON public.phone_products "
        "USING hnsw (embedding public.vector_cosine_ops)"
    )

    op.execute(
        "ALTER TABLE ONLY public.order_items "
        "ADD CONSTRAINT order_items_order_id_fkey "
        "FOREIGN KEY (order_id) REFERENCES public.orders(order_id) ON DELETE CASCADE"
    )


def downgrade() -> None:
    """This baseline is intentionally irreversible."""
    raise RuntimeError("The baseline migration must not be downgraded; it represents the existing database schema.")
