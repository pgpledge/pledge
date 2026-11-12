-- Typed ids: pledge.account_id, pledge.transfer_id, pledge.entry_id.
--
-- Each is a 16-byte fixed-length type laid out exactly like uuid (LIKE = uuid). Only the text
-- I/O functions are ours (Rust, src/id.rs); binary I/O, comparison, hashing and sort support
-- (incl. abbreviated keys) are Postgres's own uuid internals, so btree/hash indexes, joins,
-- FKs, GROUP BY and merge/hash joins behave exactly like uuid.
--
-- The operators live in pg_catalog (the functions and opclasses stay in pledge): pg_catalog is
-- always searched, so `a.id = b.id`, joins and IN work for every role without putting pledge on
-- the search_path (ORMs render plain `=`). They are extension members, dropped with it. Only
-- same-type operators exist, so account_id = transfer_id stays a type error.
--
-- Generated from one template per type; keep the three blocks identical apart from names.

-- ---------------------------------------------------------------- pledge.account_id
CREATE TYPE pledge.account_id;
CREATE FUNCTION pledge.account_id_in(cstring) RETURNS pledge.account_id
    AS 'MODULE_PATHNAME', 'account_id_in_wrapper' LANGUAGE c IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.account_id_out(pledge.account_id) RETURNS cstring
    AS 'MODULE_PATHNAME', 'account_id_out_wrapper' LANGUAGE c IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.account_id_recv(internal) RETURNS pledge.account_id
    AS 'uuid_recv' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.account_id_send(pledge.account_id) RETURNS bytea
    AS 'uuid_send' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE;
CREATE TYPE pledge.account_id (
    INPUT = pledge.account_id_in, OUTPUT = pledge.account_id_out,
    RECEIVE = pledge.account_id_recv, SEND = pledge.account_id_send,
    LIKE = uuid
);

CREATE FUNCTION pledge.account_id_eq(pledge.account_id, pledge.account_id) RETURNS bool
    AS 'uuid_eq' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_ne(pledge.account_id, pledge.account_id) RETURNS bool
    AS 'uuid_ne' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_lt(pledge.account_id, pledge.account_id) RETURNS bool
    AS 'uuid_lt' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_le(pledge.account_id, pledge.account_id) RETURNS bool
    AS 'uuid_le' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_gt(pledge.account_id, pledge.account_id) RETURNS bool
    AS 'uuid_gt' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_ge(pledge.account_id, pledge.account_id) RETURNS bool
    AS 'uuid_ge' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_cmp(pledge.account_id, pledge.account_id) RETURNS int4
    AS 'uuid_cmp' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_hash(pledge.account_id) RETURNS int4
    AS 'uuid_hash' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.account_id_hash_extended(pledge.account_id, int8) RETURNS int8
    AS 'uuid_hash_extended' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;

CREATE OPERATOR pg_catalog.= (
    LEFTARG = pledge.account_id, RIGHTARG = pledge.account_id, FUNCTION = pledge.account_id_eq,
    COMMUTATOR = OPERATOR(pg_catalog.=), NEGATOR = OPERATOR(pg_catalog.<>),
    RESTRICT = eqsel, JOIN = eqjoinsel, HASHES, MERGES
);
CREATE OPERATOR pg_catalog.<> (
    LEFTARG = pledge.account_id, RIGHTARG = pledge.account_id, FUNCTION = pledge.account_id_ne,
    COMMUTATOR = OPERATOR(pg_catalog.<>), NEGATOR = OPERATOR(pg_catalog.=),
    RESTRICT = neqsel, JOIN = neqjoinsel
);
CREATE OPERATOR pg_catalog.< (
    LEFTARG = pledge.account_id, RIGHTARG = pledge.account_id, FUNCTION = pledge.account_id_lt,
    COMMUTATOR = OPERATOR(pg_catalog.>), NEGATOR = OPERATOR(pg_catalog.>=),
    RESTRICT = scalarltsel, JOIN = scalarltjoinsel
);
CREATE OPERATOR pg_catalog.<= (
    LEFTARG = pledge.account_id, RIGHTARG = pledge.account_id, FUNCTION = pledge.account_id_le,
    COMMUTATOR = OPERATOR(pg_catalog.>=), NEGATOR = OPERATOR(pg_catalog.>),
    RESTRICT = scalarlesel, JOIN = scalarlejoinsel
);
CREATE OPERATOR pg_catalog.> (
    LEFTARG = pledge.account_id, RIGHTARG = pledge.account_id, FUNCTION = pledge.account_id_gt,
    COMMUTATOR = OPERATOR(pg_catalog.<), NEGATOR = OPERATOR(pg_catalog.<=),
    RESTRICT = scalargtsel, JOIN = scalargtjoinsel
);
CREATE OPERATOR pg_catalog.>= (
    LEFTARG = pledge.account_id, RIGHTARG = pledge.account_id, FUNCTION = pledge.account_id_ge,
    COMMUTATOR = OPERATOR(pg_catalog.<=), NEGATOR = OPERATOR(pg_catalog.<),
    RESTRICT = scalargesel, JOIN = scalargejoinsel
);

CREATE OPERATOR CLASS pledge.account_id_ops DEFAULT FOR TYPE pledge.account_id USING btree AS
    OPERATOR 1 pg_catalog.<,
    OPERATOR 2 pg_catalog.<=,
    OPERATOR 3 pg_catalog.=,
    OPERATOR 4 pg_catalog.>=,
    OPERATOR 5 pg_catalog.>,
    FUNCTION 1 pledge.account_id_cmp(pledge.account_id, pledge.account_id),
    FUNCTION 2 (pledge.account_id, pledge.account_id) pg_catalog.uuid_sortsupport(internal);
CREATE OPERATOR CLASS pledge.account_id_hash_ops DEFAULT FOR TYPE pledge.account_id USING hash AS
    OPERATOR 1 pg_catalog.=,
    FUNCTION 1 pledge.account_id_hash(pledge.account_id),
    FUNCTION 2 pledge.account_id_hash_extended(pledge.account_id, int8);

-- Same bits as uuid: free, explicit-only casts (so account_id = transfer_id stays a type error).
CREATE CAST (pledge.account_id AS uuid) WITHOUT FUNCTION;
CREATE CAST (uuid AS pledge.account_id) WITHOUT FUNCTION;

-- The first 48 bits of a UUIDv7 are unix milliseconds. Decoded here because PG17's
-- uuid_extract_timestamp() only understands version-1 UUIDs.
CREATE FUNCTION pledge.id_timestamp(pledge.account_id) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN pg_catalog.to_timestamp(
        ('x' || pg_catalog.left(pg_catalog.replace($1::uuid::text, '-', ''), 12))::bit(48)::bigint / 1000.0);

-- ---------------------------------------------------------------- pledge.transfer_id
CREATE TYPE pledge.transfer_id;
CREATE FUNCTION pledge.transfer_id_in(cstring) RETURNS pledge.transfer_id
    AS 'MODULE_PATHNAME', 'transfer_id_in_wrapper' LANGUAGE c IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.transfer_id_out(pledge.transfer_id) RETURNS cstring
    AS 'MODULE_PATHNAME', 'transfer_id_out_wrapper' LANGUAGE c IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.transfer_id_recv(internal) RETURNS pledge.transfer_id
    AS 'uuid_recv' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.transfer_id_send(pledge.transfer_id) RETURNS bytea
    AS 'uuid_send' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE;
CREATE TYPE pledge.transfer_id (
    INPUT = pledge.transfer_id_in, OUTPUT = pledge.transfer_id_out,
    RECEIVE = pledge.transfer_id_recv, SEND = pledge.transfer_id_send,
    LIKE = uuid
);

CREATE FUNCTION pledge.transfer_id_eq(pledge.transfer_id, pledge.transfer_id) RETURNS bool
    AS 'uuid_eq' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_ne(pledge.transfer_id, pledge.transfer_id) RETURNS bool
    AS 'uuid_ne' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_lt(pledge.transfer_id, pledge.transfer_id) RETURNS bool
    AS 'uuid_lt' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_le(pledge.transfer_id, pledge.transfer_id) RETURNS bool
    AS 'uuid_le' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_gt(pledge.transfer_id, pledge.transfer_id) RETURNS bool
    AS 'uuid_gt' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_ge(pledge.transfer_id, pledge.transfer_id) RETURNS bool
    AS 'uuid_ge' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_cmp(pledge.transfer_id, pledge.transfer_id) RETURNS int4
    AS 'uuid_cmp' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_hash(pledge.transfer_id) RETURNS int4
    AS 'uuid_hash' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.transfer_id_hash_extended(pledge.transfer_id, int8) RETURNS int8
    AS 'uuid_hash_extended' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;

CREATE OPERATOR pg_catalog.= (
    LEFTARG = pledge.transfer_id, RIGHTARG = pledge.transfer_id, FUNCTION = pledge.transfer_id_eq,
    COMMUTATOR = OPERATOR(pg_catalog.=), NEGATOR = OPERATOR(pg_catalog.<>),
    RESTRICT = eqsel, JOIN = eqjoinsel, HASHES, MERGES
);
CREATE OPERATOR pg_catalog.<> (
    LEFTARG = pledge.transfer_id, RIGHTARG = pledge.transfer_id, FUNCTION = pledge.transfer_id_ne,
    COMMUTATOR = OPERATOR(pg_catalog.<>), NEGATOR = OPERATOR(pg_catalog.=),
    RESTRICT = neqsel, JOIN = neqjoinsel
);
CREATE OPERATOR pg_catalog.< (
    LEFTARG = pledge.transfer_id, RIGHTARG = pledge.transfer_id, FUNCTION = pledge.transfer_id_lt,
    COMMUTATOR = OPERATOR(pg_catalog.>), NEGATOR = OPERATOR(pg_catalog.>=),
    RESTRICT = scalarltsel, JOIN = scalarltjoinsel
);
CREATE OPERATOR pg_catalog.<= (
    LEFTARG = pledge.transfer_id, RIGHTARG = pledge.transfer_id, FUNCTION = pledge.transfer_id_le,
    COMMUTATOR = OPERATOR(pg_catalog.>=), NEGATOR = OPERATOR(pg_catalog.>),
    RESTRICT = scalarlesel, JOIN = scalarlejoinsel
);
CREATE OPERATOR pg_catalog.> (
    LEFTARG = pledge.transfer_id, RIGHTARG = pledge.transfer_id, FUNCTION = pledge.transfer_id_gt,
    COMMUTATOR = OPERATOR(pg_catalog.<), NEGATOR = OPERATOR(pg_catalog.<=),
    RESTRICT = scalargtsel, JOIN = scalargtjoinsel
);
CREATE OPERATOR pg_catalog.>= (
    LEFTARG = pledge.transfer_id, RIGHTARG = pledge.transfer_id, FUNCTION = pledge.transfer_id_ge,
    COMMUTATOR = OPERATOR(pg_catalog.<=), NEGATOR = OPERATOR(pg_catalog.<),
    RESTRICT = scalargesel, JOIN = scalargejoinsel
);

CREATE OPERATOR CLASS pledge.transfer_id_ops DEFAULT FOR TYPE pledge.transfer_id USING btree AS
    OPERATOR 1 pg_catalog.<,
    OPERATOR 2 pg_catalog.<=,
    OPERATOR 3 pg_catalog.=,
    OPERATOR 4 pg_catalog.>=,
    OPERATOR 5 pg_catalog.>,
    FUNCTION 1 pledge.transfer_id_cmp(pledge.transfer_id, pledge.transfer_id),
    FUNCTION 2 (pledge.transfer_id, pledge.transfer_id) pg_catalog.uuid_sortsupport(internal);
CREATE OPERATOR CLASS pledge.transfer_id_hash_ops DEFAULT FOR TYPE pledge.transfer_id USING hash AS
    OPERATOR 1 pg_catalog.=,
    FUNCTION 1 pledge.transfer_id_hash(pledge.transfer_id),
    FUNCTION 2 pledge.transfer_id_hash_extended(pledge.transfer_id, int8);

-- Same bits as uuid: free, explicit-only casts (so account_id = transfer_id stays a type error).
CREATE CAST (pledge.transfer_id AS uuid) WITHOUT FUNCTION;
CREATE CAST (uuid AS pledge.transfer_id) WITHOUT FUNCTION;

-- The first 48 bits of a UUIDv7 are unix milliseconds. Decoded here because PG17's
-- uuid_extract_timestamp() only understands version-1 UUIDs.
CREATE FUNCTION pledge.id_timestamp(pledge.transfer_id) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN pg_catalog.to_timestamp(
        ('x' || pg_catalog.left(pg_catalog.replace($1::uuid::text, '-', ''), 12))::bit(48)::bigint / 1000.0);

-- ---------------------------------------------------------------- pledge.entry_id
CREATE TYPE pledge.entry_id;
CREATE FUNCTION pledge.entry_id_in(cstring) RETURNS pledge.entry_id
    AS 'MODULE_PATHNAME', 'entry_id_in_wrapper' LANGUAGE c IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.entry_id_out(pledge.entry_id) RETURNS cstring
    AS 'MODULE_PATHNAME', 'entry_id_out_wrapper' LANGUAGE c IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.entry_id_recv(internal) RETURNS pledge.entry_id
    AS 'uuid_recv' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION pledge.entry_id_send(pledge.entry_id) RETURNS bytea
    AS 'uuid_send' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE;
CREATE TYPE pledge.entry_id (
    INPUT = pledge.entry_id_in, OUTPUT = pledge.entry_id_out,
    RECEIVE = pledge.entry_id_recv, SEND = pledge.entry_id_send,
    LIKE = uuid
);

CREATE FUNCTION pledge.entry_id_eq(pledge.entry_id, pledge.entry_id) RETURNS bool
    AS 'uuid_eq' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_ne(pledge.entry_id, pledge.entry_id) RETURNS bool
    AS 'uuid_ne' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_lt(pledge.entry_id, pledge.entry_id) RETURNS bool
    AS 'uuid_lt' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_le(pledge.entry_id, pledge.entry_id) RETURNS bool
    AS 'uuid_le' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_gt(pledge.entry_id, pledge.entry_id) RETURNS bool
    AS 'uuid_gt' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_ge(pledge.entry_id, pledge.entry_id) RETURNS bool
    AS 'uuid_ge' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_cmp(pledge.entry_id, pledge.entry_id) RETURNS int4
    AS 'uuid_cmp' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_hash(pledge.entry_id) RETURNS int4
    AS 'uuid_hash' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;
CREATE FUNCTION pledge.entry_id_hash_extended(pledge.entry_id, int8) RETURNS int8
    AS 'uuid_hash_extended' LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF;

CREATE OPERATOR pg_catalog.= (
    LEFTARG = pledge.entry_id, RIGHTARG = pledge.entry_id, FUNCTION = pledge.entry_id_eq,
    COMMUTATOR = OPERATOR(pg_catalog.=), NEGATOR = OPERATOR(pg_catalog.<>),
    RESTRICT = eqsel, JOIN = eqjoinsel, HASHES, MERGES
);
CREATE OPERATOR pg_catalog.<> (
    LEFTARG = pledge.entry_id, RIGHTARG = pledge.entry_id, FUNCTION = pledge.entry_id_ne,
    COMMUTATOR = OPERATOR(pg_catalog.<>), NEGATOR = OPERATOR(pg_catalog.=),
    RESTRICT = neqsel, JOIN = neqjoinsel
);
CREATE OPERATOR pg_catalog.< (
    LEFTARG = pledge.entry_id, RIGHTARG = pledge.entry_id, FUNCTION = pledge.entry_id_lt,
    COMMUTATOR = OPERATOR(pg_catalog.>), NEGATOR = OPERATOR(pg_catalog.>=),
    RESTRICT = scalarltsel, JOIN = scalarltjoinsel
);
CREATE OPERATOR pg_catalog.<= (
    LEFTARG = pledge.entry_id, RIGHTARG = pledge.entry_id, FUNCTION = pledge.entry_id_le,
    COMMUTATOR = OPERATOR(pg_catalog.>=), NEGATOR = OPERATOR(pg_catalog.>),
    RESTRICT = scalarlesel, JOIN = scalarlejoinsel
);
CREATE OPERATOR pg_catalog.> (
    LEFTARG = pledge.entry_id, RIGHTARG = pledge.entry_id, FUNCTION = pledge.entry_id_gt,
    COMMUTATOR = OPERATOR(pg_catalog.<), NEGATOR = OPERATOR(pg_catalog.<=),
    RESTRICT = scalargtsel, JOIN = scalargtjoinsel
);
CREATE OPERATOR pg_catalog.>= (
    LEFTARG = pledge.entry_id, RIGHTARG = pledge.entry_id, FUNCTION = pledge.entry_id_ge,
    COMMUTATOR = OPERATOR(pg_catalog.<=), NEGATOR = OPERATOR(pg_catalog.<),
    RESTRICT = scalargesel, JOIN = scalargejoinsel
);

CREATE OPERATOR CLASS pledge.entry_id_ops DEFAULT FOR TYPE pledge.entry_id USING btree AS
    OPERATOR 1 pg_catalog.<,
    OPERATOR 2 pg_catalog.<=,
    OPERATOR 3 pg_catalog.=,
    OPERATOR 4 pg_catalog.>=,
    OPERATOR 5 pg_catalog.>,
    FUNCTION 1 pledge.entry_id_cmp(pledge.entry_id, pledge.entry_id),
    FUNCTION 2 (pledge.entry_id, pledge.entry_id) pg_catalog.uuid_sortsupport(internal);
CREATE OPERATOR CLASS pledge.entry_id_hash_ops DEFAULT FOR TYPE pledge.entry_id USING hash AS
    OPERATOR 1 pg_catalog.=,
    FUNCTION 1 pledge.entry_id_hash(pledge.entry_id),
    FUNCTION 2 pledge.entry_id_hash_extended(pledge.entry_id, int8);

-- Same bits as uuid: free, explicit-only casts (so account_id = transfer_id stays a type error).
CREATE CAST (pledge.entry_id AS uuid) WITHOUT FUNCTION;
CREATE CAST (uuid AS pledge.entry_id) WITHOUT FUNCTION;

-- The first 48 bits of a UUIDv7 are unix milliseconds. Decoded here because PG17's
-- uuid_extract_timestamp() only understands version-1 UUIDs.
CREATE FUNCTION pledge.id_timestamp(pledge.entry_id) RETURNS timestamptz
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN pg_catalog.to_timestamp(
        ('x' || pg_catalog.left(pg_catalog.replace($1::uuid::text, '-', ''), 12))::bit(48)::bigint / 1000.0);
