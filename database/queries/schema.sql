-- MCConnect database schema, version 1.
-- Applied by `python -m database.manage init`. Bump SCHEMA_VERSION in
-- databaseManagerV2.py and add a migration when changing this file.

CREATE TABLE public.schema_version(
  version integer NOT NULL
);

CREATE TABLE public.player(
  uuid uuid PRIMARY KEY,
  "name" text NOT NULL
);
CREATE INDEX player_name_idx ON public.player (lower("name"));

CREATE TABLE public.server_admins(
  id serial PRIMARY KEY,
  username text NOT NULL UNIQUE,
  email text NOT NULL UNIQUE,
  "password" text NOT NULL,
  email_verified boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.email_verification(
  admin_id integer PRIMARY KEY REFERENCES public.server_admins (id) ON DELETE CASCADE,
  token text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.servers(
  id serial PRIMARY KEY,
  owner_id integer NOT NULL REFERENCES public.server_admins (id),
  subdomain text NOT NULL UNIQUE,
  mc_server_domain text NOT NULL,
  discord_url text,
  server_description_short text NOT NULL,
  server_description_long text NOT NULL,
  server_name text NOT NULL,
  server_key character(64) NOT NULL UNIQUE,
  license_type integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX servers_owner_idx ON public.servers (owner_id);

-- One row per (player, server). player_id is the id used everywhere else.
CREATE TABLE public.player_server_info(
  player_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  mojang_uuid uuid NOT NULL REFERENCES public.player (uuid),
  server_id integer NOT NULL REFERENCES public.servers (id) ON DELETE CASCADE,
  "online" boolean NOT NULL DEFAULT false,
  first_seen timestamptz,
  last_seen timestamptz,
  prefix_id integer,
  web_access_permissions smallint NOT NULL DEFAULT 3,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT player_server_info_server_player_unique UNIQUE (server_id, mojang_uuid)
);
CREATE INDEX player_server_info_mojang_uuid_idx ON public.player_server_info (mojang_uuid);

CREATE TABLE public.prefixes(
  prefix_id serial PRIMARY KEY,
  prefix_owner_id uuid NOT NULL REFERENCES public.player_server_info (player_id) ON DELETE CASCADE,
  prefix_text text NOT NULL,
  "password" text
);

ALTER TABLE public.player_server_info
  ADD CONSTRAINT player_server_info_prefix_id_fkey
    FOREIGN KEY (prefix_id) REFERENCES public.prefixes (prefix_id) ON DELETE SET NULL;

-- Player statistics. "category" is one of the constants in database/stats.py,
-- "object" the minecraft id (minecraft:stone, ...), "value" the stat value.
CREATE TABLE public.actions(
  player_id uuid NOT NULL REFERENCES public.player_server_info (player_id) ON DELETE CASCADE,
  category smallint NOT NULL,
  "object" text NOT NULL,
  "value" bigint NOT NULL,
  CONSTRAINT actions_pkey PRIMARY KEY (player_id, category, "object")
);

CREATE TABLE public.ban_reasons(
  id serial PRIMARY KEY,
  reason text NOT NULL UNIQUE,
  ban_duration_in_days integer NOT NULL
);

CREATE TABLE public.banned_players(
  id serial PRIMARY KEY,
  banned_player_id uuid NOT NULL REFERENCES public.player_server_info (player_id) ON DELETE CASCADE,
  moderator_id uuid REFERENCES public.player_server_info (player_id) ON DELETE SET NULL,
  ban_reason_id integer NOT NULL REFERENCES public.ban_reasons (id),
  "comment" text,
  ban_start timestamptz NOT NULL DEFAULT now(),
  ban_end timestamptz NOT NULL
);
CREATE INDEX banned_players_banned_player_id_idx ON public.banned_players (banned_player_id);

-- Pending web logins: the pin is delivered in-game by the socket server
-- (via NOTIFY login_pin) and has to be entered on the website.
CREATE TABLE public.login(
  player_id uuid PRIMARY KEY REFERENCES public.player_server_info (player_id) ON DELETE CASCADE,
  pin integer NOT NULL,
  attempts smallint NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE public.block_lookup(
  "name" text PRIMARY KEY
);

CREATE TABLE public.item_lookup(
  "name" text PRIMARY KEY
);
